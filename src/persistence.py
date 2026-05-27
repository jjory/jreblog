"""
persistence.py
─────────────────────────────────────────────────────────
영구 데이터 저장소.

저장 계층 (우선순위):
1. GitHub Gist (영구, 슬립·재배포 와도 살아남음) ⭐ 주 저장소
2. /tmp/jre_data (백업, 빠른 접근)

이력 정책: 수동 삭제 전까지 영구 보존 (자동 삭제 없음)
세션 정책: 24시간 retention (현재 진행 중인 작업)

GITHUB_TOKEN 미설정 시 /tmp만 사용 (옛 동작).
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

# ⭐ 이력 자동 삭제 안 함 — 영구 보존 (수동 삭제만)
SESSION_RETENTION_HOURS = 24       # 임시 세션 보관 기간 (이건 자동 삭제 OK)
MAX_HISTORY_ITEMS = 1000           # 이력 최대 개수 (메모리 보호)

# 표시용 (기존 코드와의 호환성)
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


def _get_gist_storage():
    """GitHub Gist 저장소 (있으면)."""
    try:
        from src.gist_storage import get_storage
        return get_storage()
    except ImportError:
        return None


# ─────────────────────────────────────────────────
# 작업 이력 (영구 저장)
# ─────────────────────────────────────────────────
def load_history() -> list:
    """이력 로드. Gist 우선, /tmp 폴백. 자동 삭제 없음."""
    gist = _get_gist_storage()
    if gist:
        try:
            history = gist.load_history()
            _safe_write_json(HISTORY_FILE, history)  # /tmp 캐시 동기화
            return history
        except Exception:
            pass

    data = _safe_read_json(HISTORY_FILE)
    if data is None or not isinstance(data, list):
        return []
    return data


def add_to_history(items: list) -> int:
    """이력 추가. Gist + /tmp 동시 저장."""
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

    # Gist 우선 저장
    gist = _get_gist_storage()
    if gist:
        try:
            gist.save_history(history)
        except Exception:
            pass

    _safe_write_json(HISTORY_FILE, history)
    return len(history)


def delete_from_history(ids: list) -> int:
    """선택 항목 삭제. Gist + /tmp 동기화."""
    if not ids:
        return len(load_history())

    history = load_history()
    id_set = set(ids)
    filtered = [h for h in history if h.get("id") not in id_set]

    gist = _get_gist_storage()
    if gist:
        try:
            gist.save_history(filtered)
        except Exception:
            pass

    _safe_write_json(HISTORY_FILE, filtered)
    return len(filtered)


def clear_history() -> bool:
    """전체 이력 삭제."""
    gist = _get_gist_storage()
    if gist:
        try:
            gist.save_history([])
        except Exception:
            pass
    return _safe_write_json(HISTORY_FILE, [])


# ─────────────────────────────────────────────────
# OAuth 토큰 (네이버 — 영구 저장)
# ─────────────────────────────────────────────────
def save_naver_tokens(access_token: str, refresh_token: str) -> bool:
    """네이버 OAuth 토큰을 Gist에 영구 저장."""
    gist = _get_gist_storage()
    if not gist:
        return False
    try:
        return gist.save_tokens({
            "naver_access_token": access_token,
            "naver_refresh_token": refresh_token,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception:
        return False


def load_naver_tokens():
    """저장된 네이버 토큰 로드. (access_token, refresh_token) 또는 (None, None)."""
    gist = _get_gist_storage()
    if not gist:
        return (None, None)
    try:
        tokens = gist.load_tokens()
        return (
            tokens.get("naver_access_token"),
            tokens.get("naver_refresh_token"),
        )
    except Exception:
        return (None, None)


def clear_naver_tokens() -> bool:
    """저장된 네이버 토큰 삭제 (재인증 필요)."""
    gist = _get_gist_storage()
    if not gist:
        return False
    try:
        gist.delete_token("naver_access_token")
        gist.delete_token("naver_refresh_token")
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────
# 임시 세션 (현재 작업, 24시간 retention)
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


def is_gist_enabled() -> bool:
    """Gist 저장소 사용 중인지 확인."""
    return _get_gist_storage() is not None
