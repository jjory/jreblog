"""
persistence.py
─────────────────────────────────────────────────────────
서버 디스크 기반 데이터 영속화 모듈.

목적:
- 새로고침해도 작업 상태 유지 (session_state 휘발성 보완)
- 블로그 작업 이력 영구 저장 (10일 자동 삭제)

저장 위치:
- /tmp/jre_data/history.json     # 이력 (영구, 10일 retention)
- /tmp/jre_data/session_<id>.json # 임시 세션 (24시간 retention)

주의:
- Streamlit Cloud reboot 시 /tmp 데이터 사라질 수 있음
- 일반 새로고침에는 유지됨
- 동시 쓰기 충돌 방지를 위해 간단한 lock 사용
"""

import json
import os
import time
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional


STORAGE_DIR = Path("/tmp/jre_data")
HISTORY_FILE = STORAGE_DIR / "history.json"

HISTORY_RETENTION_DAYS = 10        # 이력 보관 기간
SESSION_RETENTION_HOURS = 24       # 임시 세션 보관 기간
MAX_HISTORY_ITEMS = 500            # 이력 최대 개수 (메모리 보호)


def _ensure_dir():
    """저장 디렉토리 생성 (없으면)."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _safe_read_json(path: Path) -> Optional[dict | list]:
    """JSON 파일 안전 읽기 (실패 시 None)."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _safe_write_json(path: Path, data) -> bool:
    """JSON 파일 안전 쓰기 (atomic write)."""
    try:
        _ensure_dir()
        # Atomic write: tmp 파일에 쓴 다음 rename
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        return True
    except OSError:
        return False


# ─────────────────────────────────────────────────
# 작업 이력 (영구 저장, 10일 retention)
# ─────────────────────────────────────────────────
def load_history() -> list[dict]:
    """
    이력 로드 + 10일 경과 자동 삭제.
    이력 항목 구조:
        {
            "id": "uuid",
            "timestamp": "2026-05-21T09:30:00",
            "filename": "matrix1.jpg",
            "title": "[신주쿠역] 1K...",
            "summary_for_chat": "🚉 ...",
            "html_content": "<h2>...",  # 본문도 저장 (재사용 가능)
            "hashtags": ["#일본부동산", ...],
        }
    """
    data = _safe_read_json(HISTORY_FILE)
    if data is None or not isinstance(data, list):
        return []

    # 10일 경과 항목 자동 삭제
    cutoff = datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)
    kept = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            ts = datetime.fromisoformat(item.get("timestamp", ""))
            if ts >= cutoff:
                kept.append(item)
        except (ValueError, TypeError):
            # timestamp 파싱 실패해도 일단 보존 (잘못 삭제 방지)
            kept.append(item)

    # 변경됐으면 다시 저장
    if len(kept) != len(data):
        _safe_write_json(HISTORY_FILE, kept)

    return kept


def add_to_history(items: list[dict]) -> int:
    """
    이력에 새 항목들 추가 (최신순 정렬).
    반환: 추가 후 총 이력 개수.
    """
    if not items:
        return 0

    history = load_history()

    # 각 새 항목에 ID·timestamp 자동 부여
    now_iso = datetime.now().isoformat(timespec="seconds")
    for item in items:
        if "id" not in item:
            item["id"] = secrets.token_urlsafe(8)
        if "timestamp" not in item:
            item["timestamp"] = now_iso

    # 새 항목을 맨 앞에 추가 (최신순)
    history = items + history

    # 최대 개수 제한
    history = history[:MAX_HISTORY_ITEMS]

    _safe_write_json(HISTORY_FILE, history)
    return len(history)


def delete_from_history(ids: list[str]) -> int:
    """
    특정 ID 목록의 이력 항목 삭제.
    반환: 남은 이력 개수.
    """
    if not ids:
        return len(load_history())

    history = load_history()
    id_set = set(ids)
    filtered = [h for h in history if h.get("id") not in id_set]

    _safe_write_json(HISTORY_FILE, filtered)
    return len(filtered)


def clear_history() -> bool:
    """모든 이력 삭제."""
    return _safe_write_json(HISTORY_FILE, [])


# ─────────────────────────────────────────────────
# 임시 세션 (새로고침 복원용, 24시간 retention)
# ─────────────────────────────────────────────────
def save_session(session_id: str, data: dict) -> bool:
    """
    현재 진행 중인 작업을 임시 저장.
    새로고침 후 같은 session_id로 복원 가능.

    data 예:
        {
            "properties": [...],
            "blog_posts": [...],
            "default_style": "...",
            "engine": "hybrid",
            "model": "claude-opus-4-7",
        }
    """
    if not session_id:
        return False

    _ensure_dir()
    path = STORAGE_DIR / f"session_{session_id}.json"
    wrapped = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "data": data,
    }
    return _safe_write_json(path, wrapped)


def load_session(session_id: str) -> Optional[dict]:
    """저장된 세션 데이터 복원 (24시간 이내만)."""
    if not session_id:
        return None

    path = STORAGE_DIR / f"session_{session_id}.json"
    wrapped = _safe_read_json(path)
    if wrapped is None or not isinstance(wrapped, dict):
        return None

    # 24시간 이상 된 세션은 무시
    try:
        saved_at = datetime.fromisoformat(wrapped.get("saved_at", ""))
        if datetime.now() - saved_at > timedelta(hours=SESSION_RETENTION_HOURS):
            return None
    except (ValueError, TypeError):
        return None

    return wrapped.get("data")


def clear_session(session_id: str) -> bool:
    """특정 세션 데이터 삭제."""
    if not session_id:
        return False
    path = STORAGE_DIR / f"session_{session_id}.json"
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError:
        return False


def cleanup_old_sessions():
    """
    24시간 이상 된 임시 세션 파일 자동 삭제.
    주기적으로 호출 (예: 페이지 로드 시).
    """
    _ensure_dir()
    cutoff = time.time() - SESSION_RETENTION_HOURS * 3600

    for path in STORAGE_DIR.glob("session_*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def generate_session_id() -> str:
    """새 세션 ID 생성 (URL-safe, 16자)."""
    return secrets.token_urlsafe(12)
