"""
drive_sync.py
─────────────────────────────────────────────────────────
Google Drive 동기화 모듈 (Phase 2: 기반 함수)

회사 Google Workspace의 서비스 계정으로 인증하여
DRIVE_ROOT_FOLDER_ID 아래 날짜 폴더(YYYYMMDD)를 자동 생성·관리한다.

핵심 흐름:
1. 서비스 계정 자격증명 로드 (Render 환경변수)
2. 날짜 폴더 자동 생성 (도쿄 시간 기준)
3. 폴더 내 미처리 파일 목록 (createdTime 오름차순)
4. 파일 다운로드 (메모리)
5. 처리 후 "처리완료" 하위 폴더로 이동

⚠️ Phase 2는 토대만 제공. 폴링·자동 파이프라인·UI 알림은 Phase 3·4에서.

필수 환경변수:
  GOOGLE_SERVICE_ACCOUNT_JSON — 서비스 계정 JSON 키 전체 내용
  DRIVE_ROOT_FOLDER_ID        — Drive 루트 폴더 ID (JRE_매물도면)

필수 패키지 (requirements.txt):
  google-api-python-client
  google-auth
"""

import io
import json
import os
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError


# ─────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────

# Drive API 권한 범위 — 읽기·쓰기 (폴더 생성·파일 이동 필요)
SCOPES = ["https://www.googleapis.com/auth/drive"]

# 처리완료 하위 폴더 이름 (고정)
PROCESSED_SUBFOLDER_NAME = "처리완료"

# 파일 최대 크기 (Render 메모리 보호) — analyzer.py와 동일 정책
MAX_FILE_SIZE_MB = 30
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# 지원 확장자 (analyzer.py와 동일)
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}

# 일본 시간 기준 (사장님 사무실: 도쿄)
TZ_TOKYO = ZoneInfo("Asia/Tokyo")

# 날짜 폴더 정규식: YYYYMMDD (8자리 숫자)
_DATE_FOLDER_RE = re.compile(r"^\d{8}$")


# ─────────────────────────────────────────────────
# 예외
# ─────────────────────────────────────────────────

class DriveConfigError(Exception):
    """Drive 설정 오류 (환경변수 누락·인증 실패 등)."""
    pass


# ─────────────────────────────────────────────────
# 인증 (모듈 캐시)
# ─────────────────────────────────────────────────

_drive_service = None  # 모듈 캐시: 매번 재인증 안 함


def _load_credentials() -> service_account.Credentials:
    """환경변수에서 서비스 계정 자격증명 로드."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise DriveConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON 환경변수가 설정되지 않았습니다.\n"
            "→ Render 대시보드 → Environment에서 추가하세요."
        )
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DriveConfigError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON 형식 오류: {e}\n"
            "→ JSON 파일 내용을 통째로 (중괄호 포함) 복사해서 환경변수에 넣어주세요."
        )
    try:
        return service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
    except Exception as e:
        raise DriveConfigError(
            f"서비스 계정 자격증명 생성 실패: {e}\n"
            "→ JSON 키 파일이 손상되었거나 잘못된 형식입니다. "
            "Google Cloud Console에서 새 키를 발급받으세요."
        )


def _get_root_folder_id() -> str:
    """환경변수에서 루트 폴더 ID 반환."""
    fid = os.getenv("DRIVE_ROOT_FOLDER_ID", "").strip()
    if not fid:
        raise DriveConfigError(
            "DRIVE_ROOT_FOLDER_ID 환경변수가 설정되지 않았습니다.\n"
            "→ Drive 폴더 URL에서 폴더 ID를 복사해 Render 환경변수에 추가하세요."
        )
    return fid


def get_drive_service():
    """Drive API 서비스 객체 (캐시됨, 매번 재인증 안 함)."""
    global _drive_service
    if _drive_service is None:
        creds = _load_credentials()
        _drive_service = build(
            "drive", "v3", credentials=creds, cache_discovery=False
        )
    return _drive_service


# ─────────────────────────────────────────────────
# Phase 1 검증용: 연결 + 권한 종합 확인
# ─────────────────────────────────────────────────

def verify_connection() -> dict:
    """
    Phase 1 설정 검증 — 사장님 사이드바의 "🔗 Drive 연결 테스트" 버튼이 호출.

    Returns:
        {
            "ok": bool,                      # 전체 OK
            "service_account_email": str,    # 인증된 서비스 계정 이메일
            "root_folder_id": str,
            "root_folder_name": str,
            "writable": bool,                # 폴더 쓰기 권한 (편집자)
            "error": str | None,             # 실패 시 한국어 안내
        }
    """
    result = {
        "ok": False,
        "service_account_email": None,
        "root_folder_id": None,
        "root_folder_name": None,
        "writable": False,
        "error": None,
    }

    try:
        # 1) 자격증명 로드 → 서비스 계정 이메일 확인
        creds = _load_credentials()
        result["service_account_email"] = creds.service_account_email

        # 2) 루트 폴더 ID 확인
        root_id = _get_root_folder_id()
        result["root_folder_id"] = root_id

        # 3) Drive API 호출로 폴더 메타 조회
        service = get_drive_service()
        folder_meta = service.files().get(
            fileId=root_id,
            fields="id, name, mimeType, capabilities",
            supportsAllDrives=True,
        ).execute()

        # 폴더 타입 검증
        if folder_meta.get("mimeType") != "application/vnd.google-apps.folder":
            result["error"] = (
                f"ID `{root_id}`는 폴더가 아닙니다 (파일이거나 잘못된 ID).\n"
                "→ Drive에서 폴더를 열고 URL의 `/folders/` 뒤 값을 다시 복사하세요."
            )
            return result

        result["root_folder_name"] = folder_meta.get("name", "(이름 없음)")

        # 4) 쓰기 권한(편집자) 확인
        caps = folder_meta.get("capabilities", {})
        result["writable"] = bool(caps.get("canAddChildren", False))
        if not result["writable"]:
            result["error"] = (
                f"폴더에 **쓰기 권한이 없습니다**. Drive에서 폴더 공유 시 "
                f"`{result['service_account_email']}`을 **'편집자'**로 추가해주세요."
            )
            return result

        result["ok"] = True
        return result

    except DriveConfigError as e:
        result["error"] = str(e)
        return result
    except HttpError as e:
        status = e.resp.status if hasattr(e, "resp") else "?"
        if status == 404:
            result["error"] = (
                f"폴더 ID `{result.get('root_folder_id')}`를 찾을 수 없습니다.\n"
                "→ DRIVE_ROOT_FOLDER_ID 환경변수를 다시 확인하거나, "
                "폴더가 삭제·이동되지 않았는지 Drive에서 확인하세요."
            )
        elif status == 403:
            result["error"] = (
                f"폴더 접근 권한이 없습니다.\n"
                f"→ Drive에서 `{result.get('service_account_email')}`을 "
                "폴더 공유 대상에 '편집자' 권한으로 추가했는지 확인하세요."
            )
        else:
            result["error"] = f"Drive API 오류 (HTTP {status}): {e}"
        return result
    except Exception as e:
        result["error"] = f"예상치 못한 오류: {type(e).__name__}: {e}"
        return result


# ─────────────────────────────────────────────────
# 폴더 관리
# ─────────────────────────────────────────────────

def _today_folder_name() -> str:
    """오늘 날짜를 YYYYMMDD 형식으로 (도쿄 시간 기준)."""
    return datetime.now(TZ_TOKYO).strftime("%Y%m%d")


def _find_subfolder_by_name(parent_id: str, name: str) -> Optional[str]:
    """parent_id 아래에 name 일치 하위 폴더 ID 반환 (없으면 None)."""
    service = get_drive_service()
    # 작은따옴표 escape (안전)
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"'{parent_id}' in parents and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"name = '{safe_name}' and trashed = false"
    )
    resp = service.files().list(
        q=query,
        fields="files(id, name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_subfolder(parent_id: str, name: str) -> str:
    """parent_id 아래에 새 폴더 생성 → ID 반환."""
    service = get_drive_service()
    folder_meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    new_folder = service.files().create(
        body=folder_meta,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return new_folder["id"]


def get_or_create_today_folder() -> tuple[str, str]:
    """
    오늘 날짜 폴더(YYYYMMDD)를 보장 — 없으면 생성.

    Returns:
        (folder_id, folder_name)
    """
    root_id = _get_root_folder_id()
    today_name = _today_folder_name()
    existing = _find_subfolder_by_name(root_id, today_name)
    if existing:
        return existing, today_name
    new_id = _create_subfolder(root_id, today_name)
    return new_id, today_name


def get_or_create_processed_subfolder(date_folder_id: str) -> str:
    """date_folder_id 아래에 '처리완료' 하위 폴더 ID 반환 — 없으면 생성."""
    existing = _find_subfolder_by_name(date_folder_id, PROCESSED_SUBFOLDER_NAME)
    if existing:
        return existing
    return _create_subfolder(date_folder_id, PROCESSED_SUBFOLDER_NAME)


def list_date_folders() -> list[dict]:
    """
    루트 폴더 아래의 모든 날짜 폴더(YYYYMMDD) 목록.
    최신 날짜 먼저 (역순 정렬).

    Returns:
        [{"id": str, "name": str, "createdTime": str}, ...]
    """
    service = get_drive_service()
    root_id = _get_root_folder_id()
    query = (
        f"'{root_id}' in parents and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"trashed = false"
    )
    resp = service.files().list(
        q=query,
        fields="files(id, name, createdTime)",
        pageSize=100,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    folders = resp.get("files", [])
    # YYYYMMDD 형식만 필터, 이름 역순 (가장 최신 먼저)
    date_folders = [f for f in folders if _DATE_FOLDER_RE.fullmatch(f.get("name", ""))]
    date_folders.sort(key=lambda f: f["name"], reverse=True)
    return date_folders


def find_most_recent_date_folder() -> Optional[dict]:
    """가장 최신 날짜 폴더 — 없으면 None."""
    folders = list_date_folders()
    return folders[0] if folders else None


# ─────────────────────────────────────────────────
# 파일 목록 / 다운로드 / 이동
# ─────────────────────────────────────────────────

def list_pending_files(date_folder_id: str) -> list[dict]:
    """
    date_folder_id 안의 미처리 파일 (처리완료 하위 폴더 제외).

    - createdTime 오름차순 (Drive 업로드 시간순 — 사장님 요청 #5)
    - 지원 확장자만 (JPG/PNG/WEBP/GIF/PDF)

    Returns:
        [{"id": str, "name": str, "createdTime": str, "size": int, "mimeType": str}, ...]
    """
    service = get_drive_service()
    # 파일만 (폴더 제외), 휴지통 제외
    query = (
        f"'{date_folder_id}' in parents and "
        f"mimeType != 'application/vnd.google-apps.folder' and "
        f"trashed = false"
    )
    resp = service.files().list(
        q=query,
        orderBy="createdTime asc",
        fields="files(id, name, createdTime, size, mimeType)",
        pageSize=100,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])

    # 지원 확장자만 + size를 int로 변환
    result = []
    for f in files:
        name = f.get("name", "")
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in SUPPORTED_EXTS:
            continue
        try:
            f["size"] = int(f.get("size", 0))
        except (ValueError, TypeError):
            f["size"] = 0
        result.append(f)
    return result


def download_file_bytes(file_id: str) -> bytes:
    """
    파일 ID로부터 바이트 다운로드 (메모리 적재).

    Raises:
        ValueError: 파일이 MAX_FILE_SIZE_BYTES 초과
    """
    service = get_drive_service()
    # 크기 사전 확인
    meta = service.files().get(
        fileId=file_id,
        fields="size, name",
        supportsAllDrives=True,
    ).execute()
    try:
        size_bytes = int(meta.get("size", 0))
    except (ValueError, TypeError):
        size_bytes = 0
    if size_bytes > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"파일이 너무 큽니다: {meta.get('name')} "
            f"({size_bytes / 1024 / 1024:.1f}MB > {MAX_FILE_SIZE_MB}MB 제한)"
        )

    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def move_file_to_folder(file_id: str, target_folder_id: str) -> None:
    """파일을 target_folder_id로 이동 (기존 부모 제거)."""
    service = get_drive_service()
    # 현재 부모 조회 (이동 시 제거할 부모)
    meta = service.files().get(
        fileId=file_id,
        fields="parents",
        supportsAllDrives=True,
    ).execute()
    prev_parents = ",".join(meta.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=target_folder_id,
        removeParents=prev_parents,
        fields="id, parents",
        supportsAllDrives=True,
    ).execute()


# ─────────────────────────────────────────────────
# 자동 처리 로그 (Phase 3)
# 처리 성공·실패·스킵 기록을 디스크에 영구 저장.
# 30일치 유지, 4번 탭과 1번 탭 expander에서 조회.
# ─────────────────────────────────────────────────

def _get_auto_log_file():
    """자동 처리 로그 파일 경로 (persistence와 동일한 영구 디스크)."""
    try:
        from src.persistence import HISTORY_FILE
    except ImportError:
        from persistence import HISTORY_FILE
    return HISTORY_FILE.parent / "auto_processing_log.json"


def load_auto_log() -> list[dict]:
    """자동 처리 로그 전체 로드."""
    log_file = _get_auto_log_file()
    if not log_file.exists():
        return []
    try:
        with open(log_file, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except (json.JSONDecodeError, IOError):
        return []


def append_auto_log(entries: list[dict], retention_days: int = 30) -> None:
    """
    자동 처리 결과를 로그에 추가 + 오래된 항목(retention_days 초과) 자동 제거.

    Args:
        entries: [
            {
                "timestamp": ISO 8601 문자열,
                "filename": str,
                "drive_file_id": str,
                "user_email": str,
                "status": "success" | "success_no_move" | "failed" | "skipped",
                "is_reprocess": bool (성공 시),
                "error": str (실패/스킵/no_move 시)
            }, ...
        ]
        retention_days: 보관 일수 (기본 30일)
    """
    from datetime import datetime, timedelta

    log_file = _get_auto_log_file()
    existing = load_auto_log()

    # 새 항목 추가
    existing.extend(entries)

    # retention_days보다 오래된 항목 제거
    cutoff = datetime.now(TZ_TOKYO) - timedelta(days=retention_days)
    fresh = []
    for e in existing:
        try:
            ts_str = e.get("timestamp", "")
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=TZ_TOKYO)
            if ts >= cutoff:
                fresh.append(e)
        except (KeyError, ValueError, TypeError):
            # 파싱 실패한 항목은 유지 (안전)
            fresh.append(e)

    # 저장
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(fresh, f, ensure_ascii=False, indent=2)

