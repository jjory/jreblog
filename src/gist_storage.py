"""
gist_storage.py
─────────────────────────────────────────────────────────
GitHub Gist를 영구 저장소로 사용.

⭐ 견고성 강화 (v2):
- 모든 함수가 예외 없이 None/False/[] 등 안전한 값 반환
- 토큰 검증 사전 수행
- 네트워크 오류 시에도 앱이 죽지 않음
- 로그용 print 메시지 추가 (Streamlit Cloud Manage 로그에서 확인 가능)

저장하는 데이터:
- 작업 이력 (영구, 수동 삭제만)
- 네이버 OAuth 토큰 (재인증 불필요)

필요한 것:
- GITHUB_TOKEN: Personal Access Token (Account scope: Gists)
- GIST_ID: 데이터 저장할 Gist ID (없으면 자동 생성)
"""

import os
import json
import sys
from datetime import datetime
from typing import Optional

import requests


GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 15

# Gist 파일명 규약
HISTORY_FILE = "jre_history.json"
TOKENS_FILE = "jre_tokens.json"
SESSION_FILE_PREFIX = "jre_session_"


def _log(msg: str):
    """Streamlit Cloud Manage 로그에서 확인 가능한 print."""
    print(f"[gist_storage] {msg}", file=sys.stderr, flush=True)


class GistStorage:
    """GitHub Gist 기반 영구 저장소 (견고 버전)."""

    def __init__(
        self,
        token: Optional[str] = None,
        gist_id: Optional[str] = None,
    ):
        self.token = (token or os.getenv("GITHUB_TOKEN", "")).strip()
        self.gist_id = (gist_id or os.getenv("GIST_ID", "")).strip()
        self._initialized = False

        if not self.token:
            raise ValueError("GITHUB_TOKEN이 비어 있습니다.")

        # 토큰 형식 사전 검증
        if not (
            self.token.startswith("github_pat_")
            or self.token.startswith("ghp_")
            or self.token.startswith("gho_")
        ):
            raise ValueError(
                f"GITHUB_TOKEN 형식이 잘못됐습니다. "
                f"'github_pat_' 또는 'ghp_'로 시작해야 합니다. "
                f"현재 시작: '{self.token[:15]}...'"
            )

        self._headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # 토큰 유효성 사전 검증 (실패 시 에러)
        if not self._validate_token():
            raise ValueError(
                "GITHUB_TOKEN이 유효하지 않습니다. GitHub에서 다시 발급하세요."
            )

        # gist_id가 없으면 자동 생성
        if not self.gist_id:
            self.gist_id = self._create_initial_gist()

        self._initialized = True
        _log(f"GistStorage 초기화 완료. Gist ID: {self.gist_id[:8]}...")

    def _validate_token(self) -> bool:
        """토큰이 유효한지 GitHub API로 확인."""
        try:
            resp = requests.get(
                f"{GITHUB_API_BASE}/user",
                headers=self._headers,
                timeout=DEFAULT_TIMEOUT,
            )
            if resp.status_code == 200:
                user = resp.json().get("login", "?")
                _log(f"토큰 인증 성공: {user}")
                return True
            else:
                _log(f"토큰 인증 실패: HTTP {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            _log(f"토큰 인증 중 예외: {e}")
            return False

    def _create_initial_gist(self) -> str:
        """최초 1회 Gist 생성."""
        url = f"{GITHUB_API_BASE}/gists"
        data = {
            "description": "JRE Blog System - Persistent Storage",
            "public": False,
            "files": {
                HISTORY_FILE: {"content": "[]"},
                TOKENS_FILE: {"content": "{}"},
            },
        }
        try:
            resp = requests.post(
                url, headers=self._headers, json=data, timeout=DEFAULT_TIMEOUT
            )
            if resp.status_code != 201:
                raise RuntimeError(
                    f"Gist 생성 실패: HTTP {resp.status_code} - {resp.text[:200]}"
                )
            new_id = resp.json()["id"]
            _log(f"새 Gist 생성됨: {new_id}")
            return new_id
        except Exception as e:
            _log(f"Gist 생성 예외: {e}")
            raise

    def _get_gist(self) -> Optional[dict]:
        """Gist 전체 데이터 조회 (실패 시 None)."""
        try:
            url = f"{GITHUB_API_BASE}/gists/{self.gist_id}"
            resp = requests.get(
                url, headers=self._headers, timeout=DEFAULT_TIMEOUT
            )
            if resp.status_code != 200:
                _log(f"Gist 조회 실패: HTTP {resp.status_code}")
                return None
            return resp.json()
        except Exception as e:
            _log(f"Gist 조회 예외: {e}")
            return None

    def _update_files(self, files: dict) -> bool:
        """Gist 파일들 업데이트 (성공 시 True)."""
        try:
            url = f"{GITHUB_API_BASE}/gists/{self.gist_id}"
            data = {"files": files}
            resp = requests.patch(
                url, headers=self._headers, json=data, timeout=DEFAULT_TIMEOUT
            )
            if resp.status_code != 200:
                _log(f"Gist 업데이트 실패: HTTP {resp.status_code}")
                return False
            return True
        except Exception as e:
            _log(f"Gist 업데이트 예외: {e}")
            return False

    def _read_file(self, filename: str, default=None):
        """Gist에서 특정 파일 내용 읽기 (실패 시 default)."""
        gist = self._get_gist()
        if not gist:
            return default
        files = gist.get("files", {})
        if filename not in files:
            return default
        content = files[filename].get("content", "")
        if not content:
            return default
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return default

    def _write_file(self, filename: str, data) -> bool:
        """Gist에 특정 파일 쓰기 (성공 시 True)."""
        return self._update_files({
            filename: {"content": json.dumps(data, ensure_ascii=False, indent=2)},
        })

    # ─────────────────────────────────────────────────
    # 이력
    # ─────────────────────────────────────────────────
    def load_history(self) -> list:
        data = self._read_file(HISTORY_FILE, default=[])
        if not isinstance(data, list):
            return []
        return data

    def save_history(self, history: list) -> bool:
        return self._write_file(HISTORY_FILE, history)

    # ─────────────────────────────────────────────────
    # OAuth 토큰
    # ─────────────────────────────────────────────────
    def load_tokens(self) -> dict:
        data = self._read_file(TOKENS_FILE, default={})
        if not isinstance(data, dict):
            return {}
        return data

    def save_tokens(self, tokens: dict) -> bool:
        existing = self.load_tokens()
        existing.update(tokens)
        return self._write_file(TOKENS_FILE, existing)

    def delete_token(self, key: str) -> bool:
        existing = self.load_tokens()
        if key in existing:
            del existing[key]
            return self._write_file(TOKENS_FILE, existing)
        return True

    def get_gist_url(self) -> str:
        return f"https://gist.github.com/{self.gist_id}"


# ─────────────────────────────────────────────────
# 모듈 레벨 헬퍼
# ─────────────────────────────────────────────────
_storage_instance: Optional[GistStorage] = None
_init_attempted: bool = False  # 한 번이라도 시도했는지
_last_error: Optional[str] = None


def get_storage() -> Optional[GistStorage]:
    """싱글톤 인스턴스 반환. 실패 시 None (앱 죽지 않음)."""
    global _storage_instance, _init_attempted, _last_error

    if _storage_instance is not None:
        return _storage_instance

    if _init_attempted:
        # 이미 한 번 실패했으면 재시도 안 함 (성능)
        return None

    _init_attempted = True

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        _log("GITHUB_TOKEN 환경변수가 비어있음. Gist 비활성화.")
        _last_error = "GITHUB_TOKEN 미설정"
        return None

    try:
        _storage_instance = GistStorage(token=token)
        return _storage_instance
    except Exception as e:
        _log(f"GistStorage 초기화 실패: {e}")
        _last_error = str(e)
        _storage_instance = None
        return None


def is_available() -> bool:
    return get_storage() is not None


def get_last_error() -> Optional[str]:
    """초기화 실패 이유 반환 (디버깅용)."""
    return _last_error


def reset():
    """캐시 초기화 (재시도 강제)."""
    global _storage_instance, _init_attempted, _last_error
    _storage_instance = None
    _init_attempted = False
    _last_error = None
