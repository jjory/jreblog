"""
persistence.py
─────────────────────────────────────────────────────────
서버 디스크 기반 데이터 영속화 모듈 (단순 안정 버전).

저장 위치:
- /tmp/jre_data/history.json     # 이력 (수동 삭제만, 영구 보존)
- /tmp/jre_data/session_<id>.json # 임시 세션 (24시간 retention)

주의:
- Streamlit Cloud reboot 시 /tmp 데이터 사라질 수 있음
- 일반 새로고침에는 유지됨
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

# 영구 보존 (수동 삭제만)
SESSION_RETENTION_HOURS = 24
MAX_HISTORY_ITEMS = 1000
HISTORY_RETENTION_DAYS = -1  # -1 = 영구 보존


def _ensure_dir():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _safe_read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _safe_write_json(path: Path, data) -> bool:
    try:
        _ensure_dir()
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
# 작업 이력 (영구 저장)
# ─────────────────────────────────────────────────
def load_history() -> list:
    """이력 로드. 자동 삭제 없음."""
    data = _safe_read_json(HISTORY_FILE)
    if data is None or not isinstance(data, list):
        return []
    return data


def add_to_history(items: list) -> int:
    if not items:
        return 0

    history = load_history()

    now_iso = datetime.now().isoformat(timespec="seconds")
    for item in items:
        if "id" not in item:
            item["id"] = secrets.token_urlsafe(8)
        if "timestamp" not in item:
            item["timestamp"] = now_iso

    history = items + history
    history = history[:MAX_HISTORY_ITEMS]

    _safe_write_json(HISTORY_FILE, history)
    return len(history)


def delete_from_history(ids: list) -> int:
    if not ids:
        return len(load_history())

    history = load_history()
    id_set = set(ids)
    filtered = [h for h in history if h.get("id") not in id_set]

    _safe_write_json(HISTORY_FILE, filtered)
    return len(filtered)


def clear_history() -> bool:
    return _safe_write_json(HISTORY_FILE, [])


# ─────────────────────────────────────────────────
# 임시 세션 (24시간 retention)
# ─────────────────────────────────────────────────
def save_session(session_id: str, data: dict) -> bool:
    if not session_id:
        return False
    _ensure_dir()
    path = STORAGE_DIR / f"session_{session_id}.json"
    wrapped = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "data": data,
    }
    return _safe_write_json(path, wrapped)


def load_session(session_id: str):
    if not session_id:
        return None
    path = STORAGE_DIR / f"session_{session_id}.json"
    wrapped = _safe_read_json(path)
    if wrapped is None or not isinstance(wrapped, dict):
        return None
    try:
        saved_at = datetime.fromisoformat(wrapped.get("saved_at", ""))
        if datetime.now() - saved_at > timedelta(hours=SESSION_RETENTION_HOURS):
            return None
    except (ValueError, TypeError):
        return None
    return wrapped.get("data")


def clear_session(session_id: str) -> bool:
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
    _ensure_dir()
    cutoff = time.time() - SESSION_RETENTION_HOURS * 3600
    for path in STORAGE_DIR.glob("session_*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def generate_session_id() -> str:
    return secrets.token_urlsafe(12)
