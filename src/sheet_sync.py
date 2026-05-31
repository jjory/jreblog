"""
sheet_sync.py
─────────────────────────────────────────────────────────
Google Sheets 翻訳DB 동기화 모듈 (Phase: 하이브리드 동기화)

사장님이 관리하시는 스프레드시트의 路線 시트에서
노선/역명/지명 번역 매핑을 자동으로 읽어와서
translation_db.py의 하드코딩 매핑에 "추가"하는 시스템.

핵심 정책:
- 시트는 "추가만" — 하드코딩 매핑이 우선 (충돌 시 시트 무시)
- 시트 다운/실패 시 하드코딩만 사용 → 앱은 항상 정상 동작
- 1시간 캐시 (TTL) + 앱 시작 시 1회 로드

필수 환경변수:
  TRANSLATION_SHEET_ID  — 스프레드시트 ID (없으면 시트 미사용, 하드코딩만)
  GOOGLE_SERVICE_ACCOUNT_JSON — Drive 서비스 계정과 공통 사용

시트 구조 (路線 시트):
  컬럼 1: 번호
  컬럼 2: 路線名 일본어
  컬럼 3: 영어노선명
  컬럼 4: 한국어노선명          ← LINE_MAP[일본어] = 한글
  컬럼 5: (빈)
  컬럼 6: 駅名 일본어
  컬럼 7: 영어역명
  컬럼 8: 한글역명              ← STATION_MAP[일본어] = 한글
  컬럼 9: 소요시간
  컬럼 10: (빈)
  컬럼 11: 地名 일본어
  컬럼 12: 영어지명
  컬럼 13: 한글지명             ← WARD_MAP[일본어] = 한글
"""

import json
import os
import time
import threading
from typing import Optional

# 시트 시트 이름 (路線 시트 = 노선/역/지명 통합 시트)
_SHEET_NAME = "路線"

# Drive와 동일한 SCOPES (서비스 계정은 한 번에 모든 권한 부여됨)
_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# 캐시 TTL (초) — 1시간
_CACHE_TTL_SECONDS = 3600

# 스레드 안전 캐시
_cache_lock = threading.Lock()
_cache_data = None
_cache_timestamp = 0.0


def _is_sheet_configured() -> bool:
    """시트 동기화가 설정됐는지 확인 — 환경변수 둘 다 있어야 함."""
    return bool(
        os.getenv("TRANSLATION_SHEET_ID", "").strip()
        and os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    )


def _build_sheets_service():
    """
    Google Sheets API 서비스 객체 생성.
    drive_sync의 서비스 계정 자격증명을 재사용 (Sheets readonly 스코프로).
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    import httplib2

    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=_SHEETS_SCOPES
        )
        # ⭐ 3초 timeout — 시트 응답 느릴 때 앱 시작이 무한 대기되지 않게
        http = httplib2.Http(timeout=3)
        # 인증 wrapper 적용
        from google_auth_httplib2 import AuthorizedHttp
        try:
            authed_http = AuthorizedHttp(creds, http=http)
            return build("sheets", "v4", http=authed_http, cache_discovery=False)
        except ImportError:
            # google_auth_httplib2 없으면 timeout 없는 기본 동작
            return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def _parse_sheet_values(values: list) -> dict:
    """
    路線 시트의 raw values 배열을 파싱해서 매핑 딕셔너리로 변환.

    Returns:
        {
            "lines":    {"日本語": "한글", ...},
            "stations": {"日本語": "한글", ...},
            "wards":    {"日本語": "한글", ...},
            "station_times": {"駅名(일/한 둘 다 키)": "신주쿠까지N분,환승N회", ...},
        }
    """
    result = {"lines": {}, "stations": {}, "wards": {}, "station_times": {}}
    if not values:
        return result

    # 헤더 행 + 1행 (영어/한글 헤더) 스킵
    for row in values[2:]:  # 인덱스 2부터 데이터 시작 (시트 첫 2행은 헤더)
        # 컬럼 인덱스: 1(노선일), 3(노선한), 5(역일), 7(역한), 8(소요시간), 10(지명일), 12(지명한)
        # 안전 추출 (행이 짧을 수 있음)
        def cell(idx):
            return (row[idx].strip() if idx < len(row) and row[idx] else "")

        # 노선: 컬럼 1(일) → 컬럼 3(한)  (0-indexed)
        line_jp = cell(1)
        line_ko = cell(3)
        if line_jp and line_ko and not _has_only_ascii_or_korean(line_jp):
            result["lines"][line_jp] = line_ko

        # 역명: 컬럼 5(일) → 컬럼 7(한)
        station_jp = cell(5)
        station_ko = cell(7)
        if station_jp and station_ko and not _has_only_ascii_or_korean(station_jp):
            result["stations"][station_jp] = station_ko

        # ⭐ 신주쿠 소요시간·환승: 컬럼 8(I열) — 역명(일/한)을 키로 매핑
        #    예: "신주쿠까지31분,환승0회"  (값이 있을 때만)
        station_time = cell(8)
        if station_time:
            if station_jp:
                result["station_times"][station_jp] = station_time
            if station_ko:
                result["station_times"][station_ko] = station_time

        # 지명: 컬럼 10(일) → 컬럼 12(한)
        ward_jp = cell(10)
        ward_ko = cell(12)
        if ward_jp and ward_ko and not _has_only_ascii_or_korean(ward_jp):
            result["wards"][ward_jp] = ward_ko

    return result


def _has_only_ascii_or_korean(text: str) -> bool:
    """일본어 한자/가나가 없는 텍스트인지 (있으면 False) — 잘못된 행 필터링용."""
    for ch in text:
        # CJK 한자 (中-龯), 히라가나, 가타카나 범위
        code = ord(ch)
        if (0x4E00 <= code <= 0x9FFF) or (0x3040 <= code <= 0x309F) or (0x30A0 <= code <= 0x30FF):
            return False
    return True


def load_translation_from_sheet() -> Optional[dict]:
    """
    Google Sheets에서 번역 매핑 로드 (캐시 1시간).

    Returns:
        성공: {"lines": {...}, "stations": {...}, "wards": {...}}
        실패: None  (호출자는 None이면 하드코딩만 사용)
    """
    global _cache_data, _cache_timestamp

    if not _is_sheet_configured():
        return None

    # 캐시 체크
    with _cache_lock:
        now = time.time()
        if _cache_data is not None and (now - _cache_timestamp) < _CACHE_TTL_SECONDS:
            return _cache_data

    # 시트 새로 로드
    try:
        service = _build_sheets_service()
        if service is None:
            return None

        sheet_id = os.getenv("TRANSLATION_SHEET_ID", "").strip()
        # 시트 범위: A:M (충분히 넓게)
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{_SHEET_NAME}!A:M",
        ).execute()

        values = result.get("values", [])
        parsed = _parse_sheet_values(values)

        # 캐시 갱신
        with _cache_lock:
            _cache_data = parsed
            _cache_timestamp = time.time()

        return parsed
    except Exception as e:
        # 시트 실패는 silent fail — 하드코딩으로 fallback
        # (디버깅을 위해 로그만 남기되 예외 전파 X)
        import sys
        print(f"[sheet_sync] 시트 로드 실패 (하드코딩으로 fallback): {e}", file=sys.stderr)
        return None


def get_station_time(station: str) -> str:
    """
    역명(일본어 또는 한글)으로 '신주쿠까지 소요시간·환승' 문자열을 조회.
    예: get_station_time("下井草") 또는 get_station_time("시모이구사")
        → "신주쿠까지21분,환승0회"
    매칭 없으면 "" (제안표에선 공란 처리).
    시트 미설정/실패 시에도 "" 반환 (예외 안 던짐).
    """
    if not station:
        return ""
    data = load_translation_from_sheet()
    if not data:
        return ""
    times = data.get("station_times", {})
    s = station.strip()
    # 정확 일치 우선
    if s in times:
        return times[s]
    # 역명에 '역'이 붙어 오는 경우 제거 후 재시도 (예: '시모이구사역')
    if s.endswith("역") and s[:-1] in times:
        return times[s[:-1]]
    return ""


def invalidate_cache() -> None:
    """캐시 강제 무효화 (관리자가 즉시 시트 반영 원할 때)."""
    global _cache_data, _cache_timestamp
    with _cache_lock:
        _cache_data = None
        _cache_timestamp = 0.0


def get_cache_status() -> dict:
    """캐시 상태 조회 (관리자 UI용)."""
    with _cache_lock:
        cached = _cache_data is not None
        age = time.time() - _cache_timestamp if cached else 0
        counts = {
            "lines": len(_cache_data.get("lines", {})) if cached else 0,
            "stations": len(_cache_data.get("stations", {})) if cached else 0,
            "wards": len(_cache_data.get("wards", {})) if cached else 0,
            "station_times": len(_cache_data.get("station_times", {})) if cached else 0,
        }
    return {
        "configured": _is_sheet_configured(),
        "cached": cached,
        "age_seconds": age,
        "counts": counts,
    }
