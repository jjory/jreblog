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
USERS_FILE = STORAGE_DIR / "users.json"
ADMIN_EMAIL = "info@win-bro.com"
ALLOWED_DOMAIN = "@win-bro.com"
ADMIN_SETTINGS_FILE = STORAGE_DIR / "admin_settings.json"

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


def add_to_history(items: list, user_email: str = "") -> int:
    if not items:
        return 0

    history = load_history()

    now_iso = datetime.now().isoformat(timespec="seconds")
    for item in items:
        if "id" not in item:
            item["id"] = secrets.token_urlsafe(8)
        if "timestamp" not in item:
            item["timestamp"] = now_iso
        # 작성자 이메일 저장 (없으면 빈 문자열)
        if "user_email" not in item:
            item["user_email"] = user_email

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


def toggle_favorite(item_id: str) -> bool:
    """즐겨찾기 토글. 반환: 토글 후 favorite 상태"""
    if not item_id:
        return False
    history = load_history()
    new_state = False
    for h in history:
        if h.get("id") == item_id:
            current = h.get("favorite", False)
            h["favorite"] = not current
            new_state = h["favorite"]
            break
    _safe_write_json(HISTORY_FILE, history)
    return new_state


def delete_old_history(months: int) -> int:
    """N개월 이상 된 이력 일괄 삭제. 즐겨찾기는 보호. 반환: 삭제된 건수"""
    if months < 1:
        return 0
    history = load_history()
    cutoff = datetime.now() - timedelta(days=months * 30)
    cutoff_iso = cutoff.isoformat(timespec="seconds")

    kept = []
    deleted_count = 0
    for h in history:
        # 즐겨찾기는 보호
        if h.get("favorite"):
            kept.append(h)
            continue
        # timestamp 비교
        ts = h.get("timestamp", "")
        if ts and ts < cutoff_iso:
            deleted_count += 1
        else:
            kept.append(h)

    _safe_write_json(HISTORY_FILE, kept)
    return deleted_count


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


# ─────────────────────────────────────────────────
# 사용자 관리 (이메일 + 비밀번호 기반)
# ─────────────────────────────────────────────────
def _hash_password(password: str) -> str:
    """bcrypt 해시 생성 (단방향 암호화)"""
    import bcrypt
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    """비밀번호 검증"""
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def is_valid_email(email: str) -> bool:
    """@win-bro.com 도메인만 허용"""
    if not email:
        return False
    email = email.strip().lower()
    return email.endswith(ALLOWED_DOMAIN) and len(email) > len(ALLOWED_DOMAIN)


def load_users() -> dict:
    """users.json 로드 (없으면 빈 dict)"""
    _ensure_dir()
    data = _safe_read_json(USERS_FILE)
    if data is None or not isinstance(data, dict):
        return {}
    return data


def save_users(users: dict) -> bool:
    """users.json 저장"""
    _ensure_dir()
    return _safe_write_json(USERS_FILE, users)


def init_admin_if_needed(initial_password: str) -> bool:
    """관리자 계정 초기화 (없으면 생성). 이미 있으면 변경 안 함."""
    if not initial_password:
        return False
    users = load_users()
    if ADMIN_EMAIL in users:
        return False  # 이미 있음
    users[ADMIN_EMAIL] = {
        "role": "admin",
        "password_hash": _hash_password(initial_password),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_by": "system",
        "last_login": "",
    }
    save_users(users)
    return True


def authenticate_user(email: str, password: str) -> Optional[dict]:
    """로그인 인증. 성공 시 사용자 정보 반환, 실패 시 None."""
    if not email or not password:
        return None
    email = email.strip().lower()
    users = load_users()
    user = users.get(email)
    if not user:
        return None
    if not _verify_password(password, user.get("password_hash", "")):
        return None
    # 마지막 로그인 시간 업데이트
    user["last_login"] = datetime.now().isoformat(timespec="seconds")
    users[email] = user
    save_users(users)
    return {"email": email, **user, "password_hash": ""}  # 해시는 반환 안 함


def add_user(email: str, password: str, created_by: str) -> tuple:
    """새 사용자 추가. 반환: (성공 여부, 메시지)"""
    if not is_valid_email(email):
        return False, f"이메일은 {ALLOWED_DOMAIN} 도메인만 사용 가능합니다."
    email = email.strip().lower()
    if not password or len(password) < 4:
        return False, "비밀번호는 최소 4자 이상이어야 합니다."

    users = load_users()
    if email in users:
        return False, "이미 등록된 이메일입니다."

    users[email] = {
        "role": "user",  # 일반 사용자 (admin은 init 시에만 생성)
        "password_hash": _hash_password(password),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_by": created_by,
        "last_login": "",
    }
    save_users(users)
    return True, f"사용자 {email} 추가 완료"


def delete_user(email: str) -> tuple:
    """사용자 삭제. 관리자(info@win-bro.com)는 삭제 불가."""
    email = email.strip().lower()
    if email == ADMIN_EMAIL:
        return False, "관리자 계정은 삭제할 수 없습니다."
    users = load_users()
    if email not in users:
        return False, "존재하지 않는 사용자입니다."
    del users[email]
    save_users(users)
    return True, f"사용자 {email} 삭제 완료"


def change_password(email: str, new_password: str) -> tuple:
    """비밀번호 변경"""
    if not new_password or len(new_password) < 4:
        return False, "비밀번호는 최소 4자 이상이어야 합니다."
    email = email.strip().lower()
    users = load_users()
    if email not in users:
        return False, "존재하지 않는 사용자입니다."
    users[email]["password_hash"] = _hash_password(new_password)
    save_users(users)
    return True, f"비밀번호 변경 완료"


def list_users() -> list:
    """모든 사용자 목록 (비밀번호 해시는 제외)"""
    users = load_users()
    result = []
    for email, info in users.items():
        result.append({
            "email": email,
            "role": info.get("role", "user"),
            "created_at": info.get("created_at", ""),
            "created_by": info.get("created_by", ""),
            "last_login": info.get("last_login", ""),
        })
    # 관리자 먼저, 그 다음 이름순
    result.sort(key=lambda x: (x["role"] != "admin", x["email"]))
    return result


# ─────────────────────────────────────────────────
# 관리자 전역 설정 (모든 사용자에게 반영)
# ─────────────────────────────────────────────────
def load_admin_settings() -> dict:
    """관리자가 설정한 분석 엔진·AI 모델 기본값"""
    _ensure_dir()
    data = _safe_read_json(ADMIN_SETTINGS_FILE)
    if data is None or not isinstance(data, dict):
        return {
            "engine": "hybrid",
            "model": "claude-opus-4-7",
        }
    return data


def save_admin_settings(engine: str, model: str) -> bool:
    """관리자만 호출 가능. 모든 사용자가 이 설정을 사용함."""
    _ensure_dir()
    return _safe_write_json(ADMIN_SETTINGS_FILE, {
        "engine": engine,
        "model": model,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
