"""
gist_storage.py
─────────────────────────────────────────────────────────
GitHub Gist를 영구 저장소로 사용.

Streamlit Cloud의 /tmp는 슬립·재배포 시 휘발되므로,
중요한 데이터는 GitHub Gist에 저장하면 영구 보존됨.

저장하는 데이터:
- 작업 이력 (영구, 수동 삭제만)
- 네이버 OAuth 토큰 (재인증 불필요)
- 현재 작업 세션 (선택적)

필요한 것:
- GITHUB_TOKEN: Personal Access Token (scope: gist)
- GIST_ID: 데이터 저장할 Gist ID (없으면 자동 생성)

Streamlit Secrets에 추가:
  GITHUB_TOKEN = "ghp_..."
  GIST_ID = ""  # 처음엔 비워두면 자동 생성
"""

import os
import json
from datetime import datetime
from typing import Optional

import requests


GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 15

# Gist 파일명 규약
HISTORY_FILE = "jre_history.json"
TOKENS_FILE = "jre_tokens.json"
SESSION_FILE_PREFIX = "jre_session_"


class GistStorage:
    """
    GitHub Gist 기반 영구 저장소.

    사용 예:
        storage = GistStorage(token=os.getenv("GITHUB_TOKEN"))

        # 이력 저장
        storage.save_history([{...}, {...}])

        # 이력 로드
        history = storage.load_history()

        # 토큰 저장
        storage.save_tokens({"access_token": "...", "refresh_token": "..."})

        # 토큰 로드
        tokens = storage.load_tokens()
    """

    def __init__(
        self,
        token: Optional[str] = None,
        gist_id: Optional[str] = None,
    ):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.gist_id = gist_id or os.getenv("GIST_ID", "")

        if not self.token:
            raise ValueError(
                "GITHUB_TOKEN이 설정되지 않았습니다. "
                "Streamlit Secrets에 추가하세요."
            )

        self._headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # gist_id가 없으면 자동 생성 (최초 1회만)
        if not self.gist_id:
            self.gist_id = self._create_initial_gist()

    # ─────────────────────────────────────────────────
    # Gist 생성/조회 (저수준 API)
    # ─────────────────────────────────────────────────
    def _create_initial_gist(self) -> str:
        """최초 1회 Gist 생성."""
        url = f"{GITHUB_API_BASE}/gists"
        data = {
            "description": "JRE Blog System - Persistent Storage",
            "public": False,  # 비공개 Gist
            "files": {
                HISTORY_FILE: {"content": "[]"},
                TOKENS_FILE: {"content": "{}"},
            },
        }
        resp = requests.post(
            url, headers=self._headers, json=data, timeout=DEFAULT_TIMEOUT
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"Gist 생성 실패: HTTP {resp.status_code} - {resp.text[:200]}"
            )
        return resp.json()["id"]

    def _get_gist(self) -> dict:
        """Gist 전체 데이터 조회."""
        url = f"{GITHUB_API_BASE}/gists/{self.gist_id}"
        resp = requests.get(url, headers=self._headers, timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Gist 조회 실패: HTTP {resp.status_code} - {resp.text[:200]}"
            )
        return resp.json()

    def _update_files(self, files: dict) -> None:
        """Gist 파일들 업데이트.

        files = {
            "filename1.json": {"content": "..."},
            "filename2.json": {"content": "..."},
        }
        """
        url = f"{GITHUB_API_BASE}/gists/{self.gist_id}"
        data = {"files": files}
        resp = requests.patch(
            url, headers=self._headers, json=data, timeout=DEFAULT_TIMEOUT
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Gist 업데이트 실패: HTTP {resp.status_code} - {resp.text[:200]}"
            )

    def _read_file(self, filename: str, default=None):
        """Gist에서 특정 파일 내용 읽기."""
        try:
            gist = self._get_gist()
            files = gist.get("files", {})
            if filename not in files:
                return default
            content = files[filename].get("content", "")
            if not content:
                return default
            return json.loads(content)
        except (json.JSONDecodeError, RuntimeError):
            return default

    def _write_file(self, filename: str, data) -> bool:
        """Gist에 특정 파일 쓰기."""
        try:
            self._update_files({
                filename: {"content": json.dumps(data, ensure_ascii=False, indent=2)},
            })
            return True
        except RuntimeError:
            return False

    # ─────────────────────────────────────────────────
    # 이력 (영구 저장, 수동 삭제만)
    # ─────────────────────────────────────────────────
    def load_history(self) -> list[dict]:
        """이력 로드."""
        data = self._read_file(HISTORY_FILE, default=[])
        if not isinstance(data, list):
            return []
        return data

    def save_history(self, history: list[dict]) -> bool:
        """이력 전체 저장."""
        return self._write_file(HISTORY_FILE, history)

    # ─────────────────────────────────────────────────
    # OAuth 토큰 (영구 저장)
    # ─────────────────────────────────────────────────
    def load_tokens(self) -> dict:
        """저장된 OAuth 토큰들 로드."""
        data = self._read_file(TOKENS_FILE, default={})
        if not isinstance(data, dict):
            return {}
        return data

    def save_tokens(self, tokens: dict) -> bool:
        """OAuth 토큰들 저장."""
        # 기존 토큰과 병합 (다른 서비스 토큰 보존)
        existing = self.load_tokens()
        existing.update(tokens)
        return self._write_file(TOKENS_FILE, existing)

    def delete_token(self, key: str) -> bool:
        """특정 토큰 삭제."""
        existing = self.load_tokens()
        if key in existing:
            del existing[key]
            return self._write_file(TOKENS_FILE, existing)
        return True

    # ─────────────────────────────────────────────────
    # 세션 (현재 작업 — 옵션)
    # ─────────────────────────────────────────────────
    def save_session(self, session_id: str, data: dict) -> bool:
        """현재 진행 중인 작업 저장."""
        filename = f"{SESSION_FILE_PREFIX}{session_id}.json"
        wrapped = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "data": data,
        }
        return self._write_file(filename, wrapped)

    def load_session(self, session_id: str) -> Optional[dict]:
        """저장된 세션 로드."""
        filename = f"{SESSION_FILE_PREFIX}{session_id}.json"
        wrapped = self._read_file(filename, default=None)
        if not wrapped:
            return None
        return wrapped.get("data")

    def get_gist_url(self) -> str:
        """Gist 웹 URL (디버깅용)."""
        return f"https://gist.github.com/{self.gist_id}"


# ─────────────────────────────────────────────────
# 모듈 레벨 헬퍼 — 옵션 (편의용)
# ─────────────────────────────────────────────────
_storage_instance: Optional[GistStorage] = None


def get_storage() -> Optional[GistStorage]:
    """싱글톤 인스턴스 반환. 환경변수가 없으면 None."""
    global _storage_instance

    if _storage_instance is not None:
        return _storage_instance

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return None

    try:
        _storage_instance = GistStorage(token=token)
        return _storage_instance
    except Exception:
        return None


def is_available() -> bool:
    """Gist 저장소 사용 가능 여부."""
    return get_storage() is not None
