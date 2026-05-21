"""
naver_publisher.py
─────────────────────────────────────────────────────────
네이버 카페 「글쓰기 API」 연동 모듈.

⚠️ 사전 준비 (한 번만):
1. https://developers.naver.com 접속 → 애플리케이션 등록
2. 사용 API: "네이버 아이디로 로그인" + "카페" 둘 다 추가
3. 환경: WEB
4. 서비스 URL/Callback URL: https://jreblog.streamlit.app
5. 매니저 권한 보유 카페에서만 검수 없이 즉시 사용 가능

OAuth Flow:
   사용자가 1회 인증 → refresh_token Streamlit session_state 저장
   → 이후 토큰 자동 갱신 (영구 자동 발행)

API 사양:
   POST https://openapi.naver.com/v1/cafe/{clubid}/menu/{menuid}/articles
   Headers: Authorization: Bearer {access_token}
   Body (form-urlencoded):
     - subject:     제목 (UTF-8 url encoded)
     - contenttext: 본문 HTML (UTF-8 url encoded)
     - isopen:      true (전체 공개), false (멤버 공개)
"""

import os
import json
import time
import secrets
from typing import Optional
from urllib.parse import urlencode, quote

import requests


# ─────────────────────────────────────────────────
# 네이버 OAuth + 카페 API 엔드포인트
# ─────────────────────────────────────────────────
NAVER_AUTH_URL = "https://nid.naver.com/oauth2.0/authorize"
NAVER_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
NAVER_PROFILE_URL = "https://openapi.naver.com/v1/nid/me"
NAVER_CAFE_WRITE_URL = "https://openapi.naver.com/v1/cafe/{clubid}/menu/{menuid}/articles"

# 기본 타임아웃·재시도
DEFAULT_TIMEOUT = 15
MAX_RETRIES = 2


# ─────────────────────────────────────────────────
# 카페 API 클라이언트
# ─────────────────────────────────────────────────
class NaverCafeClient:
    """
    네이버 카페 글쓰기 API 클라이언트.

    사용 예:
        client = NaverCafeClient(
            client_id=...,
            client_secret=...,
            redirect_uri="https://jreblog.streamlit.app",
            club_id="31042538",
            default_menu_id="6",
        )

        # 1단계 — 인증 URL 생성 (사용자에게 보여줌)
        auth_url = client.get_auth_url()

        # 2단계 — 사용자가 인증 후 ?code= 로 돌아옴
        client.exchange_code_for_token(code)

        # 3단계 — 글쓰기
        article_url = client.write_article(
            subject="제목",
            content_html="<p>본문</p>",
            menu_id="6",
            is_open=True,
        )
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        club_id: Optional[str] = None,
        default_menu_id: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ):
        self.client_id = client_id or os.getenv("NAVER_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("NAVER_CLIENT_SECRET", "")
        self.redirect_uri = redirect_uri or os.getenv(
            "NAVER_REDIRECT_URI", "https://jreblog.streamlit.app"
        )
        self.club_id = club_id or os.getenv("NAVER_CAFE_CLUB_ID", "")
        self.default_menu_id = default_menu_id or os.getenv(
            "NAVER_CAFE_DEFAULT_MENU_ID", ""
        )

        self.access_token = access_token
        self.refresh_token = refresh_token

        if not self.client_id or not self.client_secret:
            raise ValueError(
                "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 설정되지 않았습니다. "
                "Streamlit Secrets에 추가하세요."
            )
        if not self.club_id:
            raise ValueError(
                "NAVER_CAFE_CLUB_ID 가 설정되지 않았습니다. "
                "Streamlit Secrets에 추가하세요."
            )

    # ─────────────────────────────────────────────────
    # 1단계: OAuth 인증 URL 생성
    # ─────────────────────────────────────────────────
    def get_auth_url(self, state: Optional[str] = None) -> str:
        """
        사용자가 클릭할 네이버 인증 URL.
        클릭 → 네이버 로그인 → 동의 → redirect_uri로 ?code=xxx&state=yyy 로 돌아옴.
        """
        if state is None:
            state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{NAVER_AUTH_URL}?{urlencode(params)}"

    # ─────────────────────────────────────────────────
    # 2단계: code → access_token + refresh_token 교환
    # ─────────────────────────────────────────────────
    def exchange_code_for_token(self, code: str, state: Optional[str] = None) -> dict:
        """
        ?code= 로 받은 인증 코드를 access_token + refresh_token으로 교환.
        반환된 토큰을 인스턴스에 자동 저장.
        """
        params = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "state": state or "",
        }
        resp = requests.get(NAVER_TOKEN_URL, params=params, timeout=DEFAULT_TIMEOUT)
        data = resp.json()

        if "error" in data:
            raise RuntimeError(
                f"네이버 토큰 발급 실패: {data.get('error')} - "
                f"{data.get('error_description', '')}"
            )

        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        return data

    # ─────────────────────────────────────────────────
    # refresh_token으로 access_token 갱신 (자동)
    # ─────────────────────────────────────────────────
    def refresh_access_token(self) -> str:
        """
        만료된 access_token을 refresh_token으로 갱신.
        새 access_token을 인스턴스에 저장하고 반환.
        """
        if not self.refresh_token:
            raise RuntimeError(
                "refresh_token이 없습니다. 다시 인증해야 합니다."
            )

        params = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }
        resp = requests.get(NAVER_TOKEN_URL, params=params, timeout=DEFAULT_TIMEOUT)
        data = resp.json()

        if "error" in data:
            raise RuntimeError(
                f"토큰 갱신 실패: {data.get('error')} - "
                f"{data.get('error_description', '')}"
            )

        self.access_token = data.get("access_token")
        return self.access_token

    # ─────────────────────────────────────────────────
    # 인증 사용자 프로필 조회 (디버깅용)
    # ─────────────────────────────────────────────────
    def get_profile(self) -> dict:
        """현재 인증된 사용자의 이름·이메일 조회. 인증 확인용."""
        if not self.access_token:
            raise RuntimeError("access_token이 없습니다. 먼저 인증하세요.")

        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = requests.get(NAVER_PROFILE_URL, headers=headers, timeout=DEFAULT_TIMEOUT)
        data = resp.json()

        if data.get("resultcode") != "00":
            raise RuntimeError(f"프로필 조회 실패: {data.get('message')}")

        return data.get("response", {})

    # ─────────────────────────────────────────────────
    # 카페 글쓰기 (핵심 기능)
    # ─────────────────────────────────────────────────
    def write_article(
        self,
        subject: str,
        content_html: str,
        menu_id: Optional[str] = None,
        is_open: bool = True,
        auto_refresh: bool = True,
    ) -> dict:
        """
        카페 게시판에 글 작성.

        네이버 카페 API 공식 swagger 사양:
        - URL: POST /v1/cafe/{clubid}/menu/{menuid}/articles
        - Content-Type: application/x-www-form-urlencoded
        - Required: subject (UTF-8), content (UTF-8)
        - Optional: openyn (true=전체공개, 기본 멤버공개)

        Args:
            subject: 제목 (UTF-8)
            content_html: 본문 HTML (UTF-8)
            menu_id: 게시판 ID (없으면 default_menu_id 사용)
            is_open: True = 전체 공개, False = 멤버 공개
            auto_refresh: access_token 만료 시 자동 갱신 (권장)

        Returns:
            {
                "article_id": "276",          # 카페 글 번호
                "article_url": "https://...", # 카페 글 직접 링크
                "success": True,
                "raw": {...}                  # 네이버 API 원본 응답
            }
        """
        if not self.access_token:
            raise RuntimeError("access_token이 없습니다. 먼저 인증하세요.")

        menu = menu_id or self.default_menu_id
        if not menu:
            raise ValueError("menu_id가 필요합니다.")

        url = NAVER_CAFE_WRITE_URL.format(clubid=self.club_id, menuid=menu)
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            # Content-Type은 requests가 자동 설정 (form-urlencoded)
        }

        # 네이버 카페 API 파라미터 (공식 swagger 명세)
        # - subject: 제목 (UTF-8)
        # - content: 본문 (UTF-8) ⭐ "contenttext"가 아님!
        # - openyn: true (전체 공개) / false (멤버 공개)
        data = {
            "subject": subject,
            "content": content_html,
        }
        if is_open:
            data["openyn"] = "true"

        # requests가 자동으로 form-urlencoded + UTF-8 처리 (가장 안전)
        resp = requests.post(url, headers=headers, data=data, timeout=DEFAULT_TIMEOUT)

        # 401 = 토큰 만료 → 자동 갱신 후 재시도
        if resp.status_code == 401 and auto_refresh and self.refresh_token:
            try:
                self.refresh_access_token()
                headers["Authorization"] = f"Bearer {self.access_token}"
                resp = requests.post(url, headers=headers, data=data, timeout=DEFAULT_TIMEOUT)
            except Exception as e:
                raise RuntimeError(f"토큰 갱신 실패 → 재인증 필요: {e}")

        # 응답 파싱
        try:
            result = resp.json()
        except ValueError:
            raise RuntimeError(
                f"네이버 응답 파싱 실패 (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        # 네이버 카페 API 응답 형식 (성공/실패 모두 message.status 확인)
        # 성공: {"message": {"status": "200", "result": {"articleId": "276", ...}}}
        # 실패: {"message": {"status": "500", "error": {"code": "AP001", "msg": "..."}}}
        msg = result.get("message", {})
        status = str(msg.get("status", ""))

        if status != "200":
            error = msg.get("error", {})
            error_code = error.get("code", "")
            error_msg = error.get("msg") or error.get("error_message") or str(result)
            raise RuntimeError(
                f"카페 글쓰기 실패 [{error_code}]: {error_msg}"
            )

        # 성공 응답에서 article_id 추출
        result_obj = msg.get("result", {})
        article_id = (
            result_obj.get("articleId")
            or result_obj.get("articleid")
            or ""
        )

        # 카페 글 직접 링크 조립
        if article_id:
            article_url = (
                f"https://cafe.naver.com/ca-fe/cafes/{self.club_id}/articles/{article_id}"
            )
        else:
            article_url = f"https://cafe.naver.com/ca-fe/cafes/{self.club_id}"

        return {
            "article_id": str(article_id),
            "article_url": article_url,
            "success": True,
            "raw": result,
        }


# ─────────────────────────────────────────────────
# 편의 함수: Streamlit에서 사용하기 좋은 헬퍼
# ─────────────────────────────────────────────────
def format_publish_error_korean(e: Exception) -> str:
    """
    카페 글쓰기 에러를 사용자 친화적인 한국어로 변환.
    """
    err = str(e).lower()

    # 카페 API 고유 에러 코드
    if "ap001" in err or "파라미터가 유효하지 않" in str(e):
        return (
            "📝 **요청 파라미터 오류 [AP001]**\n"
            "→ 제목·본문이 비어 있거나, 본문에 카페가 허용하지 않는 태그가 있을 수 있습니다.\n"
            "→ 본문 HTML에서 `<script>`, `<iframe>`, 외부 링크 등을 확인해 보세요."
        )

    if "토큰 갱신 실패" in str(e) or "refresh" in err:
        return (
            "🔁 **인증 만료** — 다시 네이버 인증이 필요합니다.\n"
            "→ 4번 탭의 '인증 해제' 후 '네이버 카페 인증' 버튼을 다시 클릭하세요."
        )
    if "access_token" in err and ("없습니다" in str(e) or "missing" in err):
        return (
            "🔐 **인증되지 않음**\n"
            "→ 먼저 '네이버 카페 인증' 버튼을 클릭해 인증을 완료하세요."
        )
    if "401" in err or "unauthorized" in err:
        return (
            "🔐 **인증 정보 오류**\n"
            "→ Client ID/Secret가 정확한지 Streamlit Secrets에서 확인하세요."
        )
    if "403" in err or "forbidden" in err or "permission" in err:
        return (
            "🚫 **글쓰기 권한 부족**\n"
            "→ 해당 게시판에 글쓰기 권한이 있는지 카페에서 확인하세요.\n"
            "→ 매니저 권한이 있는 카페·게시판만 사용 가능합니다."
        )
    if "429" in err or "rate" in err or "quota" in err:
        return (
            "⏱️ **API 호출 한도 초과**\n"
            "→ 잠시 후 다시 시도하세요. (일 25,000건 한도)"
        )
    if "club" in err and "not found" in err:
        return (
            "❓ **카페를 찾을 수 없음**\n"
            "→ NAVER_CAFE_CLUB_ID 가 정확한지 확인하세요."
        )
    if "menu" in err and "not found" in err:
        return (
            "❓ **게시판을 찾을 수 없음**\n"
            "→ menu_id 가 정확한지 카페에서 확인하세요."
        )
    if "timeout" in err:
        return (
            "⏰ **응답 시간 초과**\n"
            "→ 네이버 서버 일시 지연. 1~2분 후 다시 시도하세요."
        )

    return f"⚠️ **카페 글쓰기 실패**: {str(e)[:250]}"
