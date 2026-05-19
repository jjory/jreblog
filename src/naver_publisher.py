"""
naver_publisher.py
─────────────────────────────────────────────────────────
네이버 OpenAPI 「블로그 글쓰기 API」 연동 모듈.

⚠️ 사전 준비 (개발자가 한 번만 하면 됨):
1. https://developers.naver.com 접속 → 애플리케이션 등록
2. 사용 API: "네이버 아이디로 로그인" + "블로그" 선택
3. **네아로 심사 통과 (약 3일 소요)** — 심사 통과 전에는 본인 계정만 사용 가능
4. Callback URL 등록 → access_token 발급

OAuth Flow:
   사용자가 1회 인증 → refresh_token 저장 → 이후 자동 갱신
"""

import os
import json
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests


NAVER_AUTH_URL = "https://nid.naver.com/oauth2.0/authorize"
NAVER_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
NAVER_BLOG_WRITE_URL = "https://openapi.naver.com/blog/writePost.json"
NAVER_BLOG_CATEGORIES_URL = "https://openapi.naver.com/blog/listCategory.json"
NAVER_PROFILE_URL = "https://openapi.naver.com/v1/nid/me"

TOKEN_FILE = Path.home() / ".naver_blog_token.json"


class NaverBlogClient:
    """네이버 블로그 OAuth 2.0 + 글쓰기 API 클라이언트."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ):
        self.client_id = client_id or os.getenv("NAVER_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("NAVER_CLIENT_SECRET")
        self.redirect_uri = redirect_uri or os.getenv("NAVER_REDIRECT_URI")

        if not (self.client_id and self.client_secret):
            raise ValueError(
                "NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 환경변수를 설정하세요. "
                "https://developers.naver.com 에서 발급 가능."
            )

        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._load_saved_token()

    # ────────────────────────────────────────
    # OAuth 2.0 인증
    # ────────────────────────────────────────
    def get_auth_url(self, state: str = "RANDOM_STATE") -> str:
        """사용자가 브라우저로 방문할 인증 URL 생성."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{NAVER_AUTH_URL}?{urlencode(params)}"

    def exchange_code_for_token(self, code: str, state: str = "RANDOM_STATE") -> dict:
        """인증 코드(callback에서 받은 ?code=...) → access_token 교환."""
        params = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "state": state,
        }
        resp = requests.get(NAVER_TOKEN_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        if "access_token" not in data:
            raise RuntimeError(f"토큰 발급 실패: {data}")
        
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        self._save_token(data)
        return data

    def refresh_access_token(self) -> dict:
        """refresh_token으로 access_token 갱신."""
        if not self._refresh_token:
            raise RuntimeError("refresh_token이 없습니다. 다시 인증하세요.")
        
        params = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self._refresh_token,
        }
        resp = requests.get(NAVER_TOKEN_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        self._access_token = data["access_token"]
        self._save_token(data)
        return data

    def _save_token(self, token_data: dict) -> None:
        """토큰을 로컬 파일에 저장 (운영 시에는 안전한 저장소 사용 권장)."""
        with open(TOKEN_FILE, "w") as f:
            json.dump(
                {
                    "access_token": self._access_token,
                    "refresh_token": self._refresh_token,
                    "expires_in": token_data.get("expires_in"),
                },
                f,
            )
        TOKEN_FILE.chmod(0o600)

    def _load_saved_token(self) -> None:
        if TOKEN_FILE.exists():
            try:
                data = json.loads(TOKEN_FILE.read_text())
                self._access_token = data.get("access_token")
                self._refresh_token = data.get("refresh_token")
            except Exception:
                pass

    @property
    def is_authenticated(self) -> bool:
        return self._access_token is not None

    # ────────────────────────────────────────
    # 블로그 API
    # ────────────────────────────────────────
    def get_profile(self) -> dict:
        """인증된 사용자 프로필 (테스트용)."""
        resp = self._authed_request("GET", NAVER_PROFILE_URL)
        return resp.json()

    def list_categories(self) -> dict:
        """블로그 카테고리 목록 조회."""
        resp = self._authed_request("GET", NAVER_BLOG_CATEGORIES_URL)
        return resp.json()

    def write_post(
        self,
        title: str,
        contents: str,
        category_no: Optional[int] = None,
        tags: Optional[list[str]] = None,
        is_open: int = 2,  # 0:비공개, 1:이웃공개, 2:전체공개
    ) -> dict:
        """
        네이버 블로그에 글 발행.
        
        Args:
            title: 글 제목
            contents: HTML 본문
            category_no: 카테고리 번호 (None이면 기본 카테고리)
            tags: 태그 리스트 (해시태그 # 없이)
            is_open: 공개 설정 (2=전체공개)
        
        Returns:
            네이버 API 응답
        """
        data = {
            "title": title,
            "contents": contents,
            "isOpen": is_open,
        }
        if category_no is not None:
            data["categoryNo"] = category_no
        if tags:
            # 네이버는 콤마로 구분된 태그 문자열을 받음
            data["tag"] = ",".join(t.lstrip("#") for t in tags)

        resp = self._authed_request(
            "POST", NAVER_BLOG_WRITE_URL, data=data
        )
        return resp.json()

    def _authed_request(
        self,
        method: str,
        url: str,
        data: Optional[dict] = None,
        retried: bool = False,
    ) -> requests.Response:
        """access_token 자동 갱신 포함한 인증 요청."""
        if not self._access_token:
            raise RuntimeError("인증되지 않았습니다. get_auth_url()부터 시작하세요.")
        
        headers = {"Authorization": f"Bearer {self._access_token}"}
        resp = requests.request(
            method, url, headers=headers, data=data, timeout=30
        )
        
        # 토큰 만료 시 1회 자동 갱신
        if resp.status_code == 401 and not retried and self._refresh_token:
            self.refresh_access_token()
            return self._authed_request(method, url, data, retried=True)
        
        resp.raise_for_status()
        return resp


def publish_to_naver(blog_post: dict, category_no: Optional[int] = None) -> dict:
    """
    generator.generate_blog_post() 결과를 네이버에 바로 발행.
    
    사용 전 NaverBlogClient로 한 번 인증해두어야 함.
    """
    client = NaverBlogClient()
    if not client.is_authenticated:
        raise RuntimeError(
            "네이버 인증이 필요합니다. 다음 URL에서 인증 후 callback을 처리하세요:\n"
            + client.get_auth_url()
        )

    return client.write_post(
        title=blog_post["title"],
        contents=blog_post["html_content"],
        category_no=category_no,
        tags=blog_post.get("hashtags", []),
        is_open=2,
    )
