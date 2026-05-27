# rebuild trigger v3
"""
app.py
─────────────────────────────────────────────────────────
JRE일본부동산 — 마이소크 → 네이버 블로그 자동 작성 시스템

특징:
- 한 번에 최대 5개 도면(마이소크) 업로드 → 블로그 5개 일괄 생성 (병렬 처리)
- JPG/PNG/WEBP/GIF/PDF 지원
- 파일마다 글 스타일을 따로 선택 가능
- 사무실 공용 비밀번호 인증 (작성자 이름 입력 없음, 새로고침해도 유지)
- 카카오톡 요약 원클릭 복사
- 생성된 블로그 전체 ZIP 다운로드 (G드라이브에 수동 저장)

실행:
- 로컬: streamlit run app.py  (.env)
- 클라우드: Streamlit Secrets
"""

import hashlib
import io
import json
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

import streamlit as st
from dotenv import load_dotenv

from src.analyzer import analyze_property_sheet, format_error_korean
from src.generator import (
    VISA_LABELS,
    build_naver_smarteditor_html,
    generate_blog_post,
    generate_sns_content,
    format_sns_for_display,
    list_available_styles,
)
from src.naver_publisher import NaverCafeClient, format_publish_error_korean
from src.persistence import (
    load_history,
    add_to_history,
    delete_from_history,
    clear_history,
    toggle_favorite,
    delete_old_history,
    save_session,
    load_session,
    clear_session,
    cleanup_old_sessions,
    generate_session_id,
    HISTORY_RETENTION_DAYS,
)

# ─────────────────────────────────────────────────
# 설정 로드 (로컬 .env / 클라우드 Streamlit Secrets)
# ─────────────────────────────────────────────────
load_dotenv()
try:
    for _key in st.secrets:
        _val = st.secrets[_key]
        if isinstance(_val, str):
            os.environ.setdefault(_key, _val)
except Exception:
    pass

MAX_UPLOADS = 5

# ─────────────────────────────────────────────────
# 동시 병렬 처리 워커 수
# ─────────────────────────────────────────────────
# 2로 설정 — Streamlit Cloud 무료 플랜(RAM 1GB) 안정성 최우선.
# - 단일 사용자: 5파일을 2+2+1 배치로 처리 (약 60초)
# - 3대 동시 사용: 3 × 2 = 6개 동시 처리 (메모리 여유)
# 속도보다 안정성을 우선. 큰 PDF가 섞여도 안정 작동.
MAX_PARALLEL_WORKERS = 5

st.set_page_config(
    page_title="🏠 JRE일본부동산 블로그 자동작성",
    page_icon="🏠",
    layout="wide",
)

# ─────────────────────────────────────────────────
# 📁 생성된 블로그 ZIP을 저장할 권장 폴더 경로
#     변경하려면 아래 한 줄만 바꾸세요.
# ─────────────────────────────────────────────────
OUTPUT_FOLDER_PATH = (
    r"G:\내 드라이브\0.사내공유\1.부동산_공유\1.안건\5.매물취합\블로그작성"
)


# ─────────────────────────────────────────────────
# 통계 분석 헬퍼 — 이력 데이터에서 정보 추출
# ─────────────────────────────────────────────────
def _extract_ward(title: str) -> str:
    """제목에서 구 추출. 표준 제목: '[이타바시구][도부토조선][도부네리마]역 ...'"""
    if not title:
        return "기타"
    m = re.match(r"\[([^\]]+)\]", title)
    if not m:
        return "기타"
    return m.group(1)


def _extract_line(title: str) -> str:
    """제목에서 노선 추출. 표준 제목 2번째 [...]."""
    if not title:
        return "기타"
    matches = re.findall(r"\[([^\]]+)\]", title)
    if len(matches) >= 2:
        return matches[1]
    return "기타"


def _extract_station(title: str) -> str:
    """제목에서 역명 추출. 표준 제목 3번째 [...]."""
    if not title:
        return "기타"
    matches = re.findall(r"\[([^\]]+)\]", title)
    if len(matches) >= 3:
        return matches[2]
    return "기타"


def _extract_rent(title: str) -> int:
    """제목에서 월세 추출 (엔 단위). '월세¥80,000+관리비¥5,000' → 80000 + 5000 = 85000"""
    if not title:
        return 0
    total = 0
    # 월세¥XX,XXX
    m_rent = re.search(r"월세[¥￥]\s*([\d,]+)", title)
    if m_rent:
        try:
            total += int(m_rent.group(1).replace(",", ""))
        except ValueError:
            pass
    # 관리비¥X,XXX
    m_mgmt = re.search(r"관리비[¥￥]\s*([\d,]+)", title)
    if m_mgmt:
        try:
            total += int(m_mgmt.group(1).replace(",", ""))
        except ValueError:
            pass
    return total


def _extract_room_type(title: str) -> str:
    """제목에서 방구조 추출. '도보5분 1K 월세¥...' → '1K'"""
    if not title:
        return "기타"
    # 도보N분 [방구조] 월세 패턴
    m = re.search(r"도보\s*\d+\s*분\s+([\w\d]+)\s+월세", title)
    if m:
        return m.group(1)
    return "기타"


def _aggregate_by_month(history: list) -> dict:
    """월별 매물 건수 집계: {'2026-05': 12, '2026-06': 8, ...}"""
    counter = {}
    for h in history:
        ts = h.get("timestamp", "")
        if ts:
            month = ts[:7]  # 'YYYY-MM'
            counter[month] = counter.get(month, 0) + 1
    # 시간순 정렬
    return dict(sorted(counter.items()))


def _aggregate_by_day(history: list, days: int = 30) -> dict:
    """최근 N일간 일별 매물 건수: {'2026-05-27': 3, '2026-05-26': 2, ...}"""
    counter = {}
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    for h in history:
        ts = h.get("timestamp", "")
        if ts and ts >= cutoff_iso:
            day = ts[:10]  # 'YYYY-MM-DD'
            counter[day] = counter.get(day, 0) + 1
    # 시간순 정렬
    return dict(sorted(counter.items()))


def _aggregate_by_ward(history: list) -> dict:
    """구별 매물 건수: {'이타바시구': 8, '신주쿠구': 5, ...} - 내림차순"""
    counter = {}
    for h in history:
        ward = _extract_ward(h.get("title", ""))
        counter[ward] = counter.get(ward, 0) + 1
    return dict(sorted(counter.items(), key=lambda x: -x[1]))


def _aggregate_by_station(history: list, top_n: int = 10) -> dict:
    """역별 매물 건수 TOP N: {'이케부쿠로': 3, '신주쿠': 2, ...} - 내림차순"""
    counter = {}
    for h in history:
        station = _extract_station(h.get("title", ""))
        counter[station] = counter.get(station, 0) + 1
    sorted_items = sorted(counter.items(), key=lambda x: -x[1])
    return dict(sorted_items[:top_n])


def _aggregate_by_line(history: list, top_n: int = 10) -> dict:
    """노선별 매물 건수 TOP N - 내림차순"""
    counter = {}
    for h in history:
        line = _extract_line(h.get("title", ""))
        counter[line] = counter.get(line, 0) + 1
    sorted_items = sorted(counter.items(), key=lambda x: -x[1])
    return dict(sorted_items[:top_n])


def _calc_rent_stats(history: list) -> dict:
    """월세 통계: 평균·최저·최고·중앙값"""
    rents = []
    for h in history:
        rent = _extract_rent(h.get("title", ""))
        if rent > 0:
            rents.append(rent)
    if not rents:
        return {"count": 0, "avg": 0, "min": 0, "max": 0, "median": 0}
    rents_sorted = sorted(rents)
    n = len(rents_sorted)
    median = rents_sorted[n // 2] if n % 2 == 1 else (rents_sorted[n // 2 - 1] + rents_sorted[n // 2]) // 2
    return {
        "count": n,
        "avg": sum(rents) // n,
        "min": min(rents),
        "max": max(rents),
        "median": median,
    }


def _format_yen(amount: int) -> str:
    """엔 금액을 보기 좋게 포맷: 85000 → '¥85,000'"""
    if amount <= 0:
        return "¥0"
    return f"¥{amount:,}"


def _build_excel_report(history: list) -> bytes:
    """이력 전체를 Excel(xlsx)로 변환. openpyxl 사용."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill
    except ImportError:
        return b""

    wb = Workbook()
    ws = wb.active
    ws.title = "매물 이력"

    # 헤더
    headers = ["번호", "제목", "구", "노선", "역", "방구조", "월세+관리비(엔)", "파일명", "생성일시", "즐겨찾기", "해시태그"]
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")

    # 데이터
    for idx, h in enumerate(history, start=1):
        title = h.get("title", "")
        ws.cell(row=idx + 1, column=1, value=idx)
        ws.cell(row=idx + 1, column=2, value=title)
        ws.cell(row=idx + 1, column=3, value=_extract_ward(title))
        ws.cell(row=idx + 1, column=4, value=_extract_line(title))
        ws.cell(row=idx + 1, column=5, value=_extract_station(title))
        ws.cell(row=idx + 1, column=6, value=_extract_room_type(title))
        ws.cell(row=idx + 1, column=7, value=_extract_rent(title))
        ws.cell(row=idx + 1, column=8, value=h.get("filename", ""))
        ws.cell(row=idx + 1, column=9, value=h.get("timestamp", ""))
        ws.cell(row=idx + 1, column=10, value="⭐" if h.get("favorite") else "")
        tags = h.get("hashtags", [])
        ws.cell(row=idx + 1, column=11, value=" ".join(tags) if tags else "")

    # 컬럼 너비 조정
    widths = [6, 50, 12, 18, 14, 10, 16, 22, 18, 8, 30]
    for col_idx, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = w

    # 통계 시트
    ws2 = wb.create_sheet("통계 요약")
    ws2.cell(row=1, column=1, value="구별 매물 분포").font = Font(bold=True, size=12)
    by_ward = _aggregate_by_ward(history)
    for i, (k, v) in enumerate(by_ward.items(), start=2):
        ws2.cell(row=i, column=1, value=k)
        ws2.cell(row=i, column=2, value=v)

    row_offset = len(by_ward) + 4
    ws2.cell(row=row_offset, column=1, value="역별 TOP 10").font = Font(bold=True, size=12)
    by_station = _aggregate_by_station(history, 10)
    for i, (k, v) in enumerate(by_station.items(), start=row_offset + 1):
        ws2.cell(row=i, column=1, value=k)
        ws2.cell(row=i, column=2, value=v)

    ws2.column_dimensions["A"].width = 20
    ws2.column_dimensions["B"].width = 10

    # 바이트로 변환
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─────────────────────────────────────────────────
# 인증 시스템 — 이메일 + 비밀번호 (역할 기반)
# ─────────────────────────────────────────────────
from src.persistence import (
    init_admin_if_needed,
    authenticate_user,
    add_user as db_add_user,
    delete_user as db_delete_user,
    change_password as db_change_password,
    list_users as db_list_users,
    load_admin_settings,
    save_admin_settings,
    is_valid_email,
    ADMIN_EMAIL,
    ALLOWED_DOMAIN,
)

# 첫 실행 시 관리자 계정 자동 생성 (Render 환경변수 ADMIN_INITIAL_PASSWORD 사용)
_admin_initial_pw = os.getenv("ADMIN_INITIAL_PASSWORD", "").strip()
if _admin_initial_pw:
    init_admin_if_needed(_admin_initial_pw)


def _email_token(email: str) -> str:
    """이메일로부터 URL 토큰 생성 (계정 기억용)"""
    seed = (email + os.getenv("ADMIN_INITIAL_PASSWORD", "secret")).encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:24]


def _check_login() -> bool:
    """로그인 확인. 성공 시 True, 로그인 화면 표시 시 False."""
    # ⭐ 네이버 OAuth callback 자동 우회 (이미 로그인된 상태)
    has_oauth_params = (
        st.query_params.get("code")
        or st.query_params.get("state")
        or st.query_params.get("error")
    )
    if has_oauth_params and st.session_state.get("user"):
        return True

    # URL 토큰으로 자동 로그인 (세션 무제한)
    url_email = st.query_params.get("user_email", "")
    url_token = st.query_params.get("auth", "")
    if url_email and url_token:
        expected = _email_token(url_email)
        if url_token == expected:
            # 토큰 검증되면 사용자 정보 다시 로드 (역할 변경 반영)
            from src.persistence import load_users
            users = load_users()
            info = users.get(url_email)
            if info:
                st.session_state["user"] = {
                    "email": url_email,
                    "role": info.get("role", "user"),
                }
                return True

    # session_state 직접 확인
    if st.session_state.get("user"):
        return True

    # 로그인 화면
    _render_login_screen()
    return False


def _render_login_screen():
    """로그인 화면 렌더링 — 가독성 좋은 디자인"""
    # 페이지 중앙 정렬
    _, col_center, _ = st.columns([1, 2, 1])
    with col_center:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown(
            "<div style='text-align:center;'>"
            "<h1 style='margin-bottom:0;'>🏠 JRE일본부동산</h1>"
            "<p style='color:#666;margin-top:0;'>네이버 블로그 자동작성 시스템</p>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.markdown("<br>", unsafe_allow_html=True)

        # 관리자 계정 미설정 시 안내
        from src.persistence import load_users
        users = load_users()
        if not users:
            st.error(
                "⚠️ **관리자 계정이 아직 설정되지 않았습니다.**\n\n"
                "Render Dashboard → Environment에서 "
                "`ADMIN_INITIAL_PASSWORD` 환경변수를 추가한 후 재배포해주세요."
            )
            st.stop()

        with st.container(border=True):
            st.markdown("### 🔐 로그인")

            with st.form("login_form_v2"):
                # 이메일 기억 기능 — 최근 로그인 이메일 자동 입력
                remembered_email = st.session_state.get("remembered_email", "")
                email = st.text_input(
                    "📧 이메일",
                    value=remembered_email,
                    placeholder="info@win-bro.com",
                    autocomplete="email",
                )
                password = st.text_input(
                    "🔒 비밀번호",
                    type="password",
                    autocomplete="current-password",
                )
                submitted = st.form_submit_button("로그인", type="primary", use_container_width=True)

                if submitted:
                    user = authenticate_user(email, password)
                    if user:
                        # 세션에 저장
                        st.session_state["user"] = {
                            "email": user["email"],
                            "role": user["role"],
                        }
                        st.session_state["remembered_email"] = user["email"]
                        # URL 토큰에 이메일 정보 저장 (계정 기억)
                        st.query_params["user_email"] = user["email"]
                        st.query_params["auth"] = _email_token(user["email"])
                        st.rerun()
                    else:
                        st.error("❌ 이메일 또는 비밀번호가 올바르지 않습니다.")

        st.markdown(
            "<br><div style='text-align:center;color:#999;font-size:12px;'>"
            "© WinBro LLC · JRE일본부동산"
            "</div>",
            unsafe_allow_html=True,
        )


# 로그인 확인 — 통과 못 하면 여기서 멈춤
if not _check_login():
    st.stop()


# 로그인된 사용자 정보 (전역에서 사용)
current_user = st.session_state.get("user", {})
current_email = current_user.get("email", "")
current_role = current_user.get("role", "user")
is_admin = (current_role == "admin")


def _logout():
    """로그아웃 — 세션 + URL 토큰 모두 제거"""
    st.session_state.pop("user", None)
    # remembered_email은 유지 (다음 로그인 시 자동 입력)
    if "user_email" in st.query_params:
        del st.query_params["user_email"]
    if "auth" in st.query_params:
        del st.query_params["auth"]





# ─────────────────────────────────────────────────
# 세션 ID 관리 + 자동 복원 (새로고침 시 작업 유지)
# ─────────────────────────────────────────────────
def _ensure_session_id() -> str:
    """
    URL 쿼리에 session ID 보장.
    - 이미 있으면 그 ID 사용 (새로고침해도 같음)
    - 없으면 새로 생성하고 URL에 추가
    """
    sid = st.query_params.get("sid", "")
    if not sid:
        sid = generate_session_id()
        st.query_params["sid"] = sid
    return sid


def _restore_session_if_needed():
    """
    페이지 첫 로드 시 디스크에서 세션 데이터 복원.
    session_state에 이미 데이터가 있으면 (= 같은 탭 진행 중) 복원 안 함.
    """
    if st.session_state.get("_session_restored"):
        return  # 이미 복원 시도함

    sid = st.query_params.get("sid", "")
    if not sid:
        return

    # 새로고침 직후엔 session_state가 비어 있음
    # 디스크에서 복원 시도
    saved = load_session(sid)
    if saved:
        if saved.get("properties") and not st.session_state.get("properties"):
            st.session_state["properties"] = saved["properties"]
        if saved.get("blog_posts") and not st.session_state.get("blog_posts"):
            st.session_state["blog_posts"] = saved["blog_posts"]

    st.session_state["_session_restored"] = True


def _persist_session():
    """현재 작업 상태를 디스크에 자동 저장."""
    sid = st.query_params.get("sid", "")
    if not sid:
        return

    data = {}
    if st.session_state.get("properties"):
        data["properties"] = st.session_state["properties"]
    if st.session_state.get("blog_posts"):
        data["blog_posts"] = st.session_state["blog_posts"]

    if data:
        save_session(sid, data)


# 페이지 첫 로드 시: 세션 ID 보장 + 자동 복원 + 오래된 세션 정리
_ensure_session_id()
_restore_session_if_needed()

# 임시 세션 파일 자동 정리 (24시간 이상 된 것)
if not st.session_state.get("_cleanup_done"):
    cleanup_old_sessions()
    st.session_state["_cleanup_done"] = True


st.title("🏠 JRE일본부동산")
st.caption(
    f"네이버 블로그 자동작성 시스템 · 마이소크 최대 {MAX_UPLOADS}개 업로드 → "
    "AI 병렬 분석 → 한국어 블로그 일괄 생성"
)

# 사용자 환영 카드 — 관리자는 전체 매물, 일반 사용자는 본인 매물 표시
try:
    _all_history = load_history()
    # 기존 이력 호환성 (user_email 없는 경우)
    for h in _all_history:
        if not h.get("user_email"):
            h["user_email"] = ADMIN_EMAIL

    if is_admin:
        # 관리자: 전사 전체 매물 표시
        _shown_history = _all_history
    else:
        # 일반 사용자: 본인 매물만
        _shown_history = [
            h for h in _all_history
            if h.get("user_email") == current_email
        ]

    _my_count = len(_shown_history)
    _my_cost = _my_count * 110  # 매물당 약 110원 (Opus + Extended Thinking)
    _my_this_month = sum(
        1 for h in _shown_history
        if h.get("timestamp", "")[:7] == datetime.now().strftime("%Y-%m")
    )
except Exception:
    _my_count = _my_cost = _my_this_month = 0

# 라벨도 권한별로 다르게
_role_label = "👑 관리자" if is_admin else "👤 사용자"
_count_label = "전체 매물" if is_admin else "내 매물"
st.markdown(
    f"""
<div style='background:linear-gradient(135deg,#E3F2FD 0%,#F3E5F5 100%);
            padding:14px 18px;border-radius:10px;margin-bottom:12px;
            border:1px solid #BBDEFB;'>
  <div style='display:flex;justify-content:space-between;align-items:center;
              flex-wrap:wrap;gap:12px;'>
    <div>
      <span style='font-size:11px;color:#1976D2;font-weight:600;'>{_role_label}</span><br>
      <span style='font-size:15px;color:#333;font-weight:600;'>{current_email}</span>
    </div>
    <div style='display:flex;gap:20px;flex-wrap:wrap;'>
      <div style='text-align:center;'>
        <div style='font-size:11px;color:#666;'>{_count_label}</div>
        <div style='font-size:18px;font-weight:700;color:#1976D2;'>{_my_count}건</div>
      </div>
      <div style='text-align:center;'>
        <div style='font-size:11px;color:#666;'>이번 달</div>
        <div style='font-size:18px;font-weight:700;color:#2E7D32;'>{_my_this_month}건</div>
      </div>
      <div style='text-align:center;'>
        <div style='font-size:11px;color:#666;'>AI 비용</div>
        <div style='font-size:18px;font-weight:700;color:#F57C00;'>{_my_cost:,}원</div>
      </div>
    </div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)


# ───── 통계 표시 함수 (사이드바에서 호출) ─────
def _render_company_stats():
    """회사 전체 통계 - 모든 사용자 공개"""
    try:
        history_all = load_history()
        for h in history_all:
            if not h.get("user_email"):
                h["user_email"] = ADMIN_EMAIL
    except Exception:
        history_all = []

    if not history_all:
        st.info("아직 매물 데이터가 없습니다.")
        return

    # 4개 요약 카드
    cc1, cc2 = st.columns(2)
    with cc1:
        st.metric("📊 전체", f"{len(history_all)}건")
    with cc2:
        active_users = set(h.get("user_email", ADMIN_EMAIL) for h in history_all)
        st.metric("👥 사용자", f"{len(active_users)}명")

    cc3, cc4 = st.columns(2)
    with cc3:
        this_month = datetime.now().strftime("%Y-%m")
        this_month_count = sum(
            1 for h in history_all
            if h.get("timestamp", "")[:7] == this_month
        )
        st.metric("📅 이번달", f"{this_month_count}건")
    with cc4:
        fav_count = sum(1 for h in history_all if h.get("favorite"))
        st.metric("⭐ 즐겨찾기", f"{fav_count}건")

    st.markdown("**📅 월별 추이**")
    by_month = _aggregate_by_month(history_all)
    if by_month:
        st.bar_chart(by_month, height=150)

    # 월세 통계
    rent_stats = _calc_rent_stats(history_all)
    if rent_stats["count"] > 0:
        st.markdown("**💰 월세 통계**")
        st.caption(
            f"평균: {_format_yen(rent_stats['avg'])} · "
            f"중앙값: {_format_yen(rent_stats['median'])}"
        )
        st.caption(
            f"최저: {_format_yen(rent_stats['min'])} · "
            f"최고: {_format_yen(rent_stats['max'])}"
        )

    # 분포 (사이드바 좁아서 간소화)
    st.markdown("**🏷 구별 TOP 5**")
    by_ward = _aggregate_by_ward(history_all)
    if by_ward:
        for ward, cnt in list(by_ward.items())[:5]:
            st.caption(f"`{ward}` — **{cnt}건**")

    st.markdown("**🚉 역 TOP 5**")
    by_station = _aggregate_by_station(history_all, 5)
    if by_station:
        for station, cnt in by_station.items():
            st.caption(f"`{station}` — **{cnt}건**")


def _render_staff_stats():
    """직원별 작업 통계 - 모든 사용자 공개"""
    try:
        history_all = load_history()
        for h in history_all:
            if not h.get("user_email"):
                h["user_email"] = ADMIN_EMAIL
    except Exception:
        history_all = []

    if not history_all:
        st.info("아직 매물 데이터가 없습니다.")
        return

    st.caption("💡 각 직원의 작업량과 누적 AI 비용")

    # 사용자별 집계
    users_list = db_list_users()
    user_emails_set = {u["email"] for u in users_list}

    COST_PER_PROPERTY = 110

    user_stats = {}
    for h in history_all:
        uemail = h.get("user_email", ADMIN_EMAIL)
        if uemail not in user_stats:
            user_stats[uemail] = {
                "count": 0,
                "rent_total": 0,
                "rent_count": 0,
                "last": h.get("timestamp", ""),
            }
        user_stats[uemail]["count"] += 1
        rent = _extract_rent(h.get("title", ""))
        if rent > 0:
            user_stats[uemail]["rent_total"] += rent
            user_stats[uemail]["rent_count"] += 1
        ts = h.get("timestamp", "")
        if ts and ts > user_stats[uemail]["last"]:
            user_stats[uemail]["last"] = ts

    # 등록 사용자 + 매물 작성 사용자 모두 표시
    all_emails = user_emails_set | set(user_stats.keys())

    # 정렬: admin 먼저, 다음에 작업량 많은 순
    sorted_emails = sorted(all_emails, key=lambda e: (
        e != ADMIN_EMAIL,
        -user_stats.get(e, {}).get("count", 0),
        e,
    ))

    for uemail in sorted_emails:
        stats = user_stats.get(uemail, {"count": 0, "rent_total": 0, "rent_count": 0, "last": ""})
        u_info = next((u for u in users_list if u["email"] == uemail), None)
        u_role = u_info["role"] if u_info else "(삭제)"
        role_icon = "👑" if u_role == "admin" else "👤" if u_role == "user" else "❌"

        count = stats["count"]
        cost = count * COST_PER_PROPERTY

        # 이메일 짧게 표시 (사이드바 좁음)
        email_short = uemail.split("@")[0]

        with st.container(border=True):
            st.markdown(
                f"{role_icon} **{email_short}**  \n"
                f"<span style='color:#888;font-size:11px;'>{uemail}</span>",
                unsafe_allow_html=True,
            )
            sc1, sc2 = st.columns(2)
            sc1.metric("📝 매물", f"{count}건", label_visibility="visible")
            sc2.metric("💰 비용", f"{cost:,}원", label_visibility="visible")

    # 합계
    st.divider()
    total_props = sum(s["count"] for s in user_stats.values())
    total_cost = total_props * COST_PER_PROPERTY
    st.markdown(
        f"**📊 전사 합계**: {total_props}건 · "
        f"**💰 {total_cost:,}원**"
    )



# ─────────────────────────────────────────────────
# 사이드바
# ─────────────────────────────────────────────────
with st.sidebar:
    # ─── 사용자 정보 카드 (가독성 개선) ───
    role_badge = "👑 관리자" if is_admin else "👤 사용자"
    st.markdown(
        f"<div style='padding:12px;background:#F0F7FF;border-radius:8px;border:1px solid #BBDEFB;'>"
        f"<div style='font-size:11px;color:#1976D2;font-weight:600;margin-bottom:4px;'>{role_badge}</div>"
        f"<div style='font-size:13px;color:#333;font-weight:500;word-break:break-all;'>{current_email}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    if st.button("🚪 로그아웃", use_container_width=True, key="logout_btn"):
        _logout()
        st.rerun()

    st.divider()

    # ─── 작업 설정 (모든 사용자) ───
    st.markdown("### ⚙️ 작업 설정")

    target_visa = st.selectbox(
        "타깃 비자 (참고용)",
        options=list(VISA_LABELS.keys()),
        format_func=lambda k: VISA_LABELS[k],
        index=0,
        help="추천 이유는 비자별로 나누지 않고 통합 작성됩니다.",
    )

    available_styles = list_available_styles()
    default_style = st.selectbox(
        "📝 기본 글 스타일",
        options=available_styles,
        index=0,
        help="2번 탭에서 매물마다 다른 스타일로 변경할 수 있습니다.",
    )

    st.divider()

    # ─── 관리자 전용: 분석 엔진 + AI 모델 ───
    # 일반 사용자는 admin이 저장한 기본값 자동 사용
    _admin_settings = load_admin_settings()

    if is_admin:
        st.markdown("### 🔬 분석 엔진 (관리자 전용)")
        has_gemini = bool(os.getenv("GEMINI_API_KEY", "").strip())
        engine_options = {
            "hybrid": "🔀 하이브리드 (무료+Claude) ⭐ 추천",
            "gemini": "🆓 Gemini 무료만",
            "claude": "💎 Claude 유료만 (최고 정확도)",
        }
        engine_keys = list(engine_options.keys())
        try:
            engine_idx = engine_keys.index(_admin_settings.get("engine", "hybrid"))
        except ValueError:
            engine_idx = 0
        engine = st.selectbox(
            "분석 엔진 선택",
            options=engine_keys,
            format_func=lambda k: engine_options[k],
            index=engine_idx,
            help="이 설정은 모든 사용자에게 적용됩니다.",
        )
        if engine in ("hybrid", "gemini") and not has_gemini:
            st.warning(
                "⚠️ GEMINI_API_KEY가 설정되지 않았습니다. "
                "https://aistudio.google.com/apikey 에서 무료 발급 후 "
                "환경변수에 추가하세요. 현재는 Claude만 사용됩니다."
            )

        st.markdown("### 🤖 AI 모델 (관리자 전용)")
        model_options = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
        try:
            model_idx = model_options.index(_admin_settings.get("model", "claude-opus-4-7"))
        except ValueError:
            model_idx = 0
        model = st.selectbox(
            "Claude 모델",
            model_options,
            index=model_idx,
            help=(
                "Opus 4.7: ⭐ 권장 — 최고 정확도 (도면 분석·블로그 생성에 가장 정확)\n"
                "Sonnet 4.6: 균형형 — 정확도 양호 + 빠름\n"
                "Haiku 4.5: 가장 빠름 — 간단한 매물·테스트용\n\n"
                "이 설정은 모든 사용자에게 적용됩니다."
            ),
        )

        # 변경되면 즉시 저장 (모든 사용자에게 반영)
        if engine != _admin_settings.get("engine") or model != _admin_settings.get("model"):
            save_admin_settings(engine, model)
            st.caption(f"✅ 모든 사용자에게 적용됨")

        st.divider()

        # ─── 관리자 전용: 사용자 관리 ───
        with st.expander("👥 사용자 관리 (관리자 전용)", expanded=False):
            users = db_list_users()
            st.caption(f"등록된 사용자: **{len(users)}명**")

            # 사용자 목록 표시
            for u in users:
                u_role = u["role"]
                u_email = u["email"]
                role_icon = "👑" if u_role == "admin" else "👤"
                last_login = u.get("last_login", "")
                last_login_str = last_login[:10] if last_login else "없음"

                with st.container(border=True):
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.markdown(
                            f"{role_icon} **{u_email}**  \n"
                            f"<span style='color:#888;font-size:11px;'>"
                            f"마지막 로그인: {last_login_str}</span>",
                            unsafe_allow_html=True,
                        )
                    with col2:
                        if u_role != "admin":
                            if st.button("🗑", key=f"del_user_{u_email}", help="이 사용자 삭제"):
                                ok, msg = db_delete_user(u_email)
                                if ok:
                                    st.success(msg)
                                    st.rerun()
                                else:
                                    st.error(msg)

            st.divider()
            st.markdown("**➕ 새 사용자 추가**")
            with st.form("add_user_form"):
                new_email = st.text_input(
                    "이메일",
                    placeholder=f"staff1{ALLOWED_DOMAIN}",
                    help=f"{ALLOWED_DOMAIN} 도메인만 허용",
                )
                new_password = st.text_input(
                    "비밀번호 (최소 4자)",
                    type="password",
                    help="이 비밀번호를 직원에게 직접 전달하세요.",
                )
                add_submitted = st.form_submit_button(
                    "➕ 사용자 추가",
                    type="primary",
                    use_container_width=True,
                )
                if add_submitted:
                    ok, msg = db_add_user(new_email, new_password, current_email)
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

            st.divider()
            st.markdown("**🔒 비밀번호 변경**")
            with st.form("change_pw_form"):
                pw_email = st.selectbox(
                    "사용자",
                    options=[u["email"] for u in users],
                )
                new_pw = st.text_input(
                    "새 비밀번호",
                    type="password",
                )
                pw_submitted = st.form_submit_button(
                    "🔒 비밀번호 변경",
                    use_container_width=True,
                )
                if pw_submitted:
                    ok, msg = db_change_password(pw_email, new_pw)
                    if ok:
                        st.success(msg)
                    else:
                        st.error(msg)

    else:
        # 일반 사용자: admin이 설정한 값 자동 사용 (UI에 표시 안 함)
        engine = _admin_settings.get("engine", "hybrid")
        model = _admin_settings.get("model", "claude-opus-4-7")

    st.divider()

    # ─── 📊 회사 전체 통계 (모든 사용자 공개) ───
    with st.expander("📊 회사 전체 통계", expanded=False):
        _render_company_stats()

    # ─── 📈 직원별 작업 통계 (모든 사용자 공개) ───
    with st.expander("📈 직원별 작업 통계", expanded=False):
        _render_staff_stats()

    st.divider()
    st.caption(
        "💡 **사용 안내**\n\n"
        "여러 직원이 동시에 접속해도 각자의 작업이 분리됩니다.\n\n"
        "⚠️ 같은 시각에 분석을 동시 실행하면 메모리 부족 에러가 날 수 있으니, "
        "가능하면 5~10분 간격을 두고 사용하세요."
    )



    st.divider()
    st.caption(
        f"💾 작업 완료 후 ZIP을 받으시면 다음 폴더에 저장하시는 것을 권장합니다:\n\n"
        f"`{OUTPUT_FOLDER_PATH}`"
    )


# ─────────────────────────────────────────────────
# 병렬 분석 워커
# ─────────────────────────────────────────────────
def _analyze_worker(file_bytes: bytes, suffix: str, engine: str, model: str) -> dict:
    """ThreadPoolExecutor 워커: 임시 파일에 저장 후 분석, 자동 삭제."""
    with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        return analyze_property_sheet(tmp_path, engine=engine, model=model)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _generate_worker(property_data, target_visa, style_name, custom_instructions, model):
    """ThreadPoolExecutor 워커: 블로그 생성."""
    return generate_blog_post(
        property_data=property_data,
        target_visa=target_visa,
        style_name=style_name,
        custom_instructions=custom_instructions,
        model=model,
    )


# ─────────────────────────────────────────────────
# 4단계 탭
# ─────────────────────────────────────────────────
# ───── 작업 이력 보관함 (메인 페이지 상단 expander) ─────
# ───── 작업 이력 보관함 (메인 페이지 상단 expander) ─────
# Expander 제목에 이력 건수·용량을 동적으로 표시 (펼치지 않고도 확인 가능)
try:
    _hist_preview = load_history()
    # 권한별 필터링 (제목 미리보기용)
    if is_admin:
        _hist_count = len(_hist_preview)
    else:
        _hist_count = sum(1 for h in _hist_preview 
                          if (h.get("user_email") or ADMIN_EMAIL) == current_email)
except Exception:
    _hist_count = 0

# 파일 크기 계산 (사람이 읽기 좋은 단위로 포맷) — 관리자만 정확한 디스크 사용량 표시
try:
    from src.persistence import HISTORY_FILE
    if HISTORY_FILE.exists() and is_admin:
        _hist_bytes = HISTORY_FILE.stat().st_size
        if _hist_bytes < 1024:
            _hist_size = f"{_hist_bytes}B"
        elif _hist_bytes < 1024 * 1024:
            _hist_size = f"{_hist_bytes/1024:.1f}KB"
        else:
            _hist_size = f"{_hist_bytes/(1024*1024):.2f}MB"
    else:
        _hist_size = ""
except Exception:
    _hist_size = ""

# Expander 제목: 건수와 용량 같이 표시 (일반 사용자는 본인 매물 기준)
_label_prefix = "📚 작업 이력 보관함" if is_admin else "📚 내 작업 이력"
if _hist_count == 0:
    _expander_label = f"{_label_prefix} (비어 있음 · 클릭하여 펼치기)"
elif _hist_size:
    _expander_label = f"{_label_prefix} ({_hist_count}건 · {_hist_size} · 클릭하여 펼치기)"
else:
    _expander_label = f"{_label_prefix} ({_hist_count}건 · 클릭하여 펼치기)"

with st.expander(_expander_label, expanded=False):
    st.caption(
        "💡 생성된 모든 블로그가 자동 저장됩니다. "
        "**영구 보존** — 수동 삭제 전까지 안 사라집니다."
    )

    # 이력 로드 (실패해도 빈 리스트 반환)
    try:
        history_all = load_history()
        # 기존 이력 (user_email 없음) → admin@win-bro.com 작성으로 처리
        for h in history_all:
            if not h.get("user_email"):
                h["user_email"] = ADMIN_EMAIL
        # 권한별 필터링: 관리자는 전체, 일반 사용자는 본인 매물만
        if is_admin:
            history = history_all
        else:
            history = [h for h in history_all if h.get("user_email") == current_email]
    except Exception as e:
        st.error(f"⚠️ 이력 로드 중 에러: {e}")
        history = []
        history_all = []

    if not history:
        st.info(
            "아직 저장된 이력이 없습니다.\n\n"
            "1·2번 탭에서 블로그를 생성하면 자동으로 여기에 저장됩니다."
        )
    else:
        # 통계 표시 (4개 칸: 건수·용량·최근·오래된)
        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        with col_s1:
            st.metric("📊 총 이력", f"{len(history)}건")
        with col_s2:
            st.metric("💾 저장 용량", _hist_size)
        with col_s3:
            try:
                latest_ts = datetime.fromisoformat(history[0]["timestamp"])
                latest_str = latest_ts.strftime("%m-%d %H:%M")
            except Exception:
                latest_str = "?"
            st.metric("🆕 최근 작업", latest_str)
        with col_s4:
            try:
                oldest_ts = datetime.fromisoformat(history[-1]["timestamp"])
                days_old = (datetime.now() - oldest_ts).days
                oldest_str = f"{days_old}일 전"
            except Exception:
                oldest_str = "?"
            st.metric("🗓 가장 오래된", oldest_str)

        st.divider()

        # ===== 📊 통계 대시보드 (중첩 expander, 펼침 기본) =====
        with st.expander("📊 통계 대시보드 (펼치기)", expanded=True):
            # 1) 월별 차트
            st.markdown("**📅 월별 매물 생성 추이**")
            by_month = _aggregate_by_month(history)
            if by_month:
                st.bar_chart(by_month, height=200)
            else:
                st.caption("데이터 없음")

            # 2) 최근 30일 일별 차트
            st.markdown("**📆 최근 30일 일별 추이**")
            by_day = _aggregate_by_day(history, days=30)
            if by_day:
                st.bar_chart(by_day, height=200)
            else:
                st.caption("최근 30일 데이터 없음")

            # 3) 월세 통계
            st.markdown("**💰 월세+관리비 통계**")
            rent_stats = _calc_rent_stats(history)
            if rent_stats["count"] > 0:
                rcol1, rcol2, rcol3, rcol4 = st.columns(4)
                rcol1.metric("평균", _format_yen(rent_stats["avg"]))
                rcol2.metric("중앙값", _format_yen(rent_stats["median"]))
                rcol3.metric("최저", _format_yen(rent_stats["min"]))
                rcol4.metric("최고", _format_yen(rent_stats["max"]))
                st.caption(f"※ 월세 정보가 표준 제목 형식인 {rent_stats['count']}건 기준")
            else:
                st.caption("월세 정보 추출 가능한 매물 없음")

            # 4) 구별·노선별·역별 분포 (3열)
            d_col1, d_col2, d_col3 = st.columns(3)
            with d_col1:
                st.markdown("**🏷 구별 분포**")
                by_ward = _aggregate_by_ward(history)
                if by_ward:
                    for ward, cnt in list(by_ward.items())[:10]:
                        bar = "▓" * min(cnt, 20)
                        st.markdown(f"`{ward[:8]:<10}` {bar} **{cnt}건**")
                else:
                    st.caption("데이터 없음")

            with d_col2:
                st.markdown("**🚆 노선 TOP 10**")
                by_line = _aggregate_by_line(history, 10)
                if by_line:
                    for line, cnt in by_line.items():
                        bar = "▓" * min(cnt, 20)
                        st.markdown(f"`{line[:10]:<12}` {bar} **{cnt}건**")
                else:
                    st.caption("데이터 없음")

            with d_col3:
                st.markdown("**🚉 역 TOP 10**")
                by_station = _aggregate_by_station(history, 10)
                if by_station:
                    for station, cnt in by_station.items():
                        bar = "▓" * min(cnt, 20)
                        st.markdown(f"`{station[:8]:<10}` {bar} **{cnt}건**")
                else:
                    st.caption("데이터 없음")

        # ===== 🗑 이력 정리 (오래된 이력 일괄 삭제) =====
        with st.expander("🗑 N개월 이상 된 이력 일괄 정리 (즐겨찾기 보호)", expanded=False):
            st.caption("💡 즐겨찾기(⭐)로 표시된 매물은 삭제되지 않습니다.")
            cleanup_col1, cleanup_col2 = st.columns([3, 1])
            with cleanup_col1:
                months_to_clean = st.slider(
                    "몇 개월 이상 된 이력을 삭제할까요?",
                    min_value=1,
                    max_value=24,
                    value=12,
                    step=1,
                    key="cleanup_months_slider",
                )
            with cleanup_col2:
                st.markdown("&nbsp;", unsafe_allow_html=True)  # 위아래 정렬용
                confirm_key = f"_confirm_cleanup_{months_to_clean}"
                if st.button(
                    f"🗑 {months_to_clean}개월+ 삭제",
                    type="secondary",
                    use_container_width=True,
                    key=f"cleanup_btn_{months_to_clean}",
                ):
                    if st.session_state.get(confirm_key):
                        deleted = delete_old_history(months_to_clean)
                        st.session_state.pop(confirm_key, None)
                        st.success(f"✅ {deleted}건의 오래된 이력 삭제 완료 (즐겨찾기 보호됨)")
                        st.rerun()
                    else:
                        st.session_state[confirm_key] = True
                        st.warning("⚠️ 한 번 더 클릭하면 삭제됩니다.")

        # ===== 📥 Excel 리포트 다운로드 =====
        with st.expander("📥 Excel 리포트 다운로드 (전체 이력 + 통계)", expanded=False):
            st.caption("💡 매물 이력 전체 + 통계 요약을 Excel 파일로 다운로드합니다.")
            try:
                excel_bytes = _build_excel_report(history)
                if excel_bytes:
                    st.download_button(
                        f"📊 Excel 리포트 다운로드 ({len(history)}건)",
                        excel_bytes,
                        file_name=f"JRE_매물이력_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )
                else:
                    st.warning("⚠️ Excel 생성 라이브러리(openpyxl)가 설치되지 않았습니다.")
            except Exception as e:
                st.error(f"Excel 생성 실패: {e}")

        st.divider()

        # 검색
        search_q = st.text_input(
            "🔍 검색 (제목·파일명)",
            key="hist_search_v2",
            placeholder="예: 신주쿠, 1K, japanreal2_1.jpg",
        )

        # ⭐ 즐겨찾기 필터
        only_favorites = st.checkbox(
            "⭐ 즐겨찾기만 보기",
            key="hist_only_favorites_v2",
            value=False,
        )

        # 필터링
        filtered = history
        if search_q:
            q = search_q.lower()
            filtered = [
                h for h in filtered
                if q in h.get("title", "").lower()
                or q in h.get("filename", "").lower()
            ]
        if only_favorites:
            filtered = [h for h in filtered if h.get("favorite")]

        if not filtered:
            st.warning(f"'{search_q}' 검색 결과 없음")
        else:
            st.markdown(f"**검색 결과: {len(filtered)}건**")

            # 선택 삭제 모드
            col_btn1, col_btn2 = st.columns([1, 5])
            with col_btn1:
                if st.button("☑️ 모두 선택", use_container_width=True):
                    for h in filtered:
                        st.session_state[f"hist_sel_{h['id']}"] = True
                    st.rerun()
            with col_btn2:
                if st.button("⬜ 모두 해제", use_container_width=True):
                    for h in filtered:
                        st.session_state[f"hist_sel_{h['id']}"] = False
                    st.rerun()

            st.divider()

            # 이력 목록 표시
            selected_ids = []
            for idx, h in enumerate(filtered):
                hid = h.get("id", "")
                title = h.get("title", "(제목 없음)")
                filename = h.get("filename", "")
                timestamp = h.get("timestamp", "")
                is_fav = h.get("favorite", False)

                # 표시용 시간 포맷
                try:
                    ts_obj = datetime.fromisoformat(timestamp)
                    ts_display = ts_obj.strftime("%Y-%m-%d %H:%M")
                    # ⭐ 영구 보존이라 자동 삭제 경고 없음
                    retention_warn = ""
                except Exception:
                    ts_display = timestamp
                    retention_warn = ""

                # 체크박스 + 즐겨찾기 + 제목
                col_chk, col_fav, col_info = st.columns([0.5, 0.5, 9])
                with col_chk:
                    checked = st.checkbox(
                        " ",
                        key=f"hist_sel_{hid}",
                        label_visibility="collapsed",
                    )
                    if checked:
                        selected_ids.append(hid)

                with col_fav:
                    fav_icon = "⭐" if is_fav else "☆"
                    if st.button(
                        fav_icon,
                        key=f"hist_fav_{hid}",
                        help="즐겨찾기 토글",
                        use_container_width=True,
                    ):
                        toggle_favorite(hid)
                        st.rerun()

                with col_info:
                    fav_badge = " ⭐" if is_fav else ""
                    st.markdown(
                        f"**{idx+1}. {title}**{fav_badge}  \n"
                        f"<span style='color:#888;font-size:13px'>"
                        f"📁 {filename} · 🕒 {ts_display}{retention_warn}</span>",
                        unsafe_allow_html=True,
                    )

                # 카톡 요약 표시 (접힘 상태가 기본)
                summary = h.get("summary_for_chat", "")
                if summary:
                    with st.expander("📱 카카오톡용 요약 보기", expanded=False):
                        st.code(summary, language=None, wrap_lines=True)

                # 추가 작업: 본문 + 다운로드 (펼침)
                with st.expander("📄 본문 HTML + 해시태그 + 다운로드", expanded=False):
                    html = h.get("html_content", "")
                    if html:
                        st.markdown(html, unsafe_allow_html=True)
                    tags = h.get("hashtags", [])
                    if tags:
                        st.markdown("**해시태그**")
                        st.code(" ".join(tags), language=None)

                    # 다운로드 버튼들
                    dl_col1, dl_col2 = st.columns(2)
                    with dl_col1:
                        st.download_button(
                            "💾 HTML 다운로드",
                            build_naver_smarteditor_html({
                                "title": title,
                                "html_content": html,
                                "hashtags": tags,
                            }),
                            file_name=f"history_{hid}_{Path(filename).stem}.html",
                            mime="text/html",
                            key=f"hist_dl_html_{hid}",
                            use_container_width=True,
                        )
                    with dl_col2:
                        st.download_button(
                            "📋 JSON 다운로드",
                            json.dumps(h, ensure_ascii=False, indent=2),
                            file_name=f"history_{hid}.json",
                            mime="application/json",
                            key=f"hist_dl_json_{hid}",
                            use_container_width=True,
                        )

                st.divider()

            # 선택 삭제 + 전체 삭제 버튼
            st.markdown("---")
            col_d1, col_d2, col_d3 = st.columns([2, 2, 6])
            with col_d1:
                if st.button(
                    f"🗑️ 선택 항목 삭제 ({len(selected_ids)}개)",
                    type="primary",
                    disabled=(len(selected_ids) == 0),
                    use_container_width=True,
                ):
                    delete_from_history(selected_ids)
                    # 선택 상태 초기화
                    for sid in selected_ids:
                        st.session_state.pop(f"hist_sel_{sid}", None)
                    st.success(f"✅ {len(selected_ids)}개 항목 삭제 완료")
                    st.rerun()

            with col_d2:
                if st.button(
                    "🗑️ 전체 삭제",
                    type="secondary",
                    use_container_width=True,
                ):
                    if st.session_state.get("_confirm_clear_all"):
                        clear_history()
                        st.session_state.pop("_confirm_clear_all", None)
                        st.success("✅ 모든 이력 삭제 완료")
                        st.rerun()
                    else:
                        st.session_state["_confirm_clear_all"] = True
                        st.warning("⚠️ 한 번 더 클릭하면 모든 이력이 삭제됩니다.")

            with col_d3:
                # 전체 백업 다운로드
                if filtered:
                    st.download_button(
                        f"📥 전체 백업 다운로드 ({len(history)}건 JSON)",
                        json.dumps(history, ensure_ascii=False, indent=2),
                        file_name=f"jre_history_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
                        mime="application/json",
                        use_container_width=True,
                    )



# ───── 📱 SNS·쇼츠 콘텐츠 자동 생성 (메인 페이지 상단 expander) ─────
# 5번 탭이 아니라 expander로 만들어 Streamlit st.tabs() 마지막 탭 버그 회피.
# 결과 표시는 라디오 버튼으로 (중첩 st.tabs 회피).
# 결과는 session_state에만 저장 (페이지 새로고침 시 사라짐).
_sns_blog_posts = st.session_state.get("blog_posts") or []
_sns_label_count = f"({len(_sns_blog_posts)}개 매물 준비)" if _sns_blog_posts else "(블로그 미생성)"
_sns_expander_label = f"📱 SNS·쇼츠 콘텐츠 자동 생성 {_sns_label_count} (클릭하여 펼치기)"

with st.expander(_sns_expander_label, expanded=False):
    st.caption(
        "💡 블로그 생성된 매물을 바탕으로 **인스타 캡션·카톡 메시지·유튜브 쇼츠 스크립트**를 "
        "자동 생성합니다."
    )

    # ⚠️ 비용 안내 강화 — 예상 비용 표시
    if _sns_blog_posts:
        expected_cost = len(_sns_blog_posts) * 240
        st.warning(
            f"💰 **예상 비용**: 매물당 약 240원 (Opus 기준) · "
            f"현재 {len(_sns_blog_posts)}건 모두 생성 시 약 **{expected_cost:,}원**"
        )

    if not _sns_blog_posts:
        st.info(
            "👈 먼저 **1·2번 탭**에서 매물을 분석하고 블로그를 생성하세요.\n\n"
            "블로그 생성된 매물만 SNS 콘텐츠 생성 가능합니다."
        )
    else:
        # SNS 콘텐츠는 별도 session_state에 저장 (블로그와 분리)
        if "sns_contents" not in st.session_state:
            st.session_state["sns_contents"] = {}

        sns_contents = st.session_state["sns_contents"]

        # 일괄 생성 버튼
        not_yet = [
            (idx, bp) for idx, bp in enumerate(_sns_blog_posts)
            if f"{bp['filename']}_{idx}" not in sns_contents
        ]

        st.markdown("### ⚡ 한꺼번에 생성")
        if not not_yet:
            st.success("✅ 모든 매물의 SNS 콘텐츠가 이미 생성되었습니다.")
        else:
            batch_cost = len(not_yet) * 240
            if st.button(
                f"🚀 미생성 {len(not_yet)}개 매물 SNS 생성 (예상 비용 {batch_cost:,}원)",
                type="primary",
                use_container_width=True,
                key="sns_batch_generate_btn",
            ):
                progress = st.progress(0.0, text="시작...")
                total = len(not_yet)
                gen_errors = []

                for i, (idx, bp) in enumerate(not_yet):
                    filename = bp["filename"]
                    title = bp["post"].get("title", filename)
                    progress.progress(
                        i / total,
                        text=f"[{i+1}/{total}] '{title[:30]}...' SNS 생성 중",
                    )
                    try:
                        sns_result = generate_sns_content(
                            property_data=bp["data"],
                            blog_post=bp["post"],
                            model=model,
                        )
                        sns_key = f"{filename}_{idx}"
                        sns_contents[sns_key] = sns_result
                    except Exception as e:
                        gen_errors.append({
                            "title": title,
                            "error": format_error_korean(e, filename),
                        })

                progress.progress(1.0, text="✅ 완료")
                st.session_state["sns_contents"] = sns_contents

                if gen_errors:
                    st.warning(f"⚠️ {len(gen_errors)}개 매물 SNS 생성 실패")
                    for ge in gen_errors:
                        st.markdown(f"- **{ge['title']}**: {ge['error']}")

                st.success(f"✅ {total - len(gen_errors)}개 매물 SNS 콘텐츠 생성 완료!")
                st.rerun()

        st.divider()
        st.markdown("### 📋 매물별 SNS 콘텐츠")
        st.caption("각 매물마다 개별 생성도 가능합니다. 생성된 결과는 라디오로 채널 전환.")

        # 매물 목록 표시
        for idx, bp in enumerate(_sns_blog_posts):
            post = bp["post"]
            filename = bp["filename"]
            title = post.get("title", filename)
            sns_key = f"{filename}_{idx}"
            has_sns = sns_key in sns_contents

            # 각 매물은 expander (중첩 expander - 작업 이력 보관함과 같은 패턴)
            label_icon = "✅" if has_sns else "⏳"
            label_status = "생성됨" if has_sns else "미생성"
            with st.expander(
                f"{label_icon} **{idx+1}. {title}**  ({label_status})",
                expanded=False,
            ):
                col_a, col_b = st.columns([3, 1])
                with col_a:
                    st.caption(f"📁 {filename}")
                with col_b:
                    if not has_sns:
                        if st.button(
                            "🎬 SNS 생성 (240원)",
                            key=f"sns_gen_one_{sns_key}",
                            type="primary",
                            use_container_width=True,
                        ):
                            try:
                                with st.spinner("SNS 콘텐츠 생성 중..."):
                                    sns_result = generate_sns_content(
                                        property_data=bp["data"],
                                        blog_post=post,
                                        model=model,
                                    )
                                    sns_contents[sns_key] = sns_result
                                    st.session_state["sns_contents"] = sns_contents
                                st.success("✅ 생성 완료!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ 생성 실패: {format_error_korean(e, filename)}")

                # 생성된 결과 표시 — 라디오 버튼으로 채널 전환 (st.tabs 회피)
                if has_sns:
                    sns_data = sns_contents[sns_key]
                    formatted = format_sns_for_display(sns_data)

                    # ⭐ 라디오 버튼으로 채널 선택 (중첩 st.tabs 회피 = 빈 화면 버그 없음)
                    channel = st.radio(
                        "콘텐츠 채널 선택:",
                        options=["📸 인스타그램", "💬 카카오톡", "🎬 유튜브 쇼츠"],
                        key=f"sns_channel_{sns_key}",
                        horizontal=True,
                        label_visibility="collapsed",
                    )

                    # ─── 인스타그램 ───
                    if channel == "📸 인스타그램":
                        st.markdown("**📸 인스타그램 피드 캡션**")
                        st.code(
                            sns_data.get("instagram_caption", ""),
                            language=None,
                            wrap_lines=True,
                        )

                        st.markdown("**#️⃣ 해시태그**")
                        tags = sns_data.get("instagram_hashtags", [])
                        st.code(" ".join(tags), language=None, wrap_lines=True)

                        st.markdown("**📋 전체 (캡션 + 해시태그) — 한 번에 복사**")
                        st.code(
                            formatted["instagram_full"],
                            language=None,
                            wrap_lines=True,
                        )

                        st.download_button(
                            "💾 인스타용 텍스트 다운로드",
                            formatted["instagram_full"],
                            file_name=f"instagram_{Path(filename).stem}.txt",
                            mime="text/plain",
                            key=f"sns_dl_insta_{sns_key}",
                        )

                    # ─── 카카오톡 ───
                    elif channel == "💬 카카오톡":
                        st.markdown("**💬 카카오톡 오픈채팅용 메시지**")
                        st.caption(
                            "💡 운영 중인 오픈채팅방에 그대로 복사·전송 가능. "
                            "짧고 정보 위주의 톤으로 작성됨."
                        )
                        st.code(
                            formatted["kakao"],
                            language=None,
                            wrap_lines=True,
                        )

                        st.download_button(
                            "💾 카톡용 텍스트 다운로드",
                            formatted["kakao"],
                            file_name=f"kakao_{Path(filename).stem}.txt",
                            mime="text/plain",
                            key=f"sns_dl_kakao_{sns_key}",
                        )

                    # ─── 유튜브 쇼츠 ───
                    elif channel == "🎬 유튜브 쇼츠":
                        st.markdown("**🎬 유튜브 쇼츠 60초 스크립트**")
                        st.caption(
                            "💡 영상 편집 시 자막·나레이션·시간 배분 그대로 활용 가능."
                        )

                        st.code(
                            formatted["youtube_full"],
                            language=None,
                            wrap_lines=True,
                        )

                        st.markdown("**📝 자막만 (영상 편집용)**")
                        st.caption(
                            "CapCut 등 영상 편집 앱에서 자막으로 그대로 사용 가능."
                        )
                        st.code(
                            formatted["youtube_subtitles_only"],
                            language=None,
                            wrap_lines=True,
                        )

                        col_dl1, col_dl2 = st.columns(2)
                        with col_dl1:
                            st.download_button(
                                "💾 전체 스크립트",
                                formatted["youtube_full"],
                                file_name=f"shorts_full_{Path(filename).stem}.txt",
                                mime="text/plain",
                                key=f"sns_dl_yt_full_{sns_key}",
                                use_container_width=True,
                            )
                        with col_dl2:
                            st.download_button(
                                "💾 자막만",
                                formatted["youtube_subtitles_only"],
                                file_name=f"shorts_subtitles_{Path(filename).stem}.txt",
                                mime="text/plain",
                                key=f"sns_dl_yt_sub_{sns_key}",
                                use_container_width=True,
                            )

        st.caption(
            "⚠️ **주의**: SNS 콘텐츠는 페이지 새로고침 시 사라집니다. "
            "필요한 내용은 즉시 다운로드하거나 복사하세요."
        )


tab1, tab2, tab3, tab4 = st.tabs(
    [
        "1️⃣ 도면 업로드",
        "2️⃣ 추출 결과·스타일 선택",
        "3️⃣ 블로그 미리보기",
        "4️⃣ 네이버 카페 발행",
    ]
)

# ───── Tab 1: 업로드 (병렬 분석) ─────
with tab1:
    st.subheader(f"마이소크 (物件図面) 업로드 — 한 번에 최대 {MAX_UPLOADS}개")
    st.caption("지원 형식: JPG · PNG · WEBP · GIF · PDF")

    uploaded_files = st.file_uploader(
        f"도면 파일을 최대 {MAX_UPLOADS}개까지 선택하세요",
        type=["jpg", "jpeg", "png", "webp", "gif", "pdf"],
        accept_multiple_files=True,
    )

    if uploaded_files:
        if len(uploaded_files) > MAX_UPLOADS:
            st.warning(f"⚠️ 최대 {MAX_UPLOADS}개까지만 처리됩니다.")
            uploaded_files = uploaded_files[:MAX_UPLOADS]

        st.write(f"**업로드된 파일: {len(uploaded_files)}개**")
        cols = st.columns(min(len(uploaded_files), MAX_UPLOADS))
        for i, uf in enumerate(uploaded_files):
            with cols[i]:
                if uf.name.lower().endswith(".pdf"):
                    st.info(f"📄 {uf.name}\n(PDF)")
                else:
                    st.image(uf, caption=uf.name, use_container_width=True)

        if st.button("🔍 전체 도면 병렬 분석 시작", type="primary"):
            properties = []
            errors = []
            progress = st.progress(0.0, text="병렬 분석 시작…")

            # 파일 미리 읽기 (Streamlit UploadedFile은 thread-safe 안 함)
            file_jobs = [
                (uf.name, uf.getvalue(), Path(uf.name).suffix)
                for uf in uploaded_files
            ]

            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as executor:
                future_to_name = {
                    executor.submit(_analyze_worker, file_bytes, suffix, engine, model): name
                    for name, file_bytes, suffix in file_jobs
                }

                done = 0
                total = len(future_to_name)
                for future in as_completed(future_to_name):
                    name = future_to_name[future]
                    try:
                        data = future.result()
                        properties.append({
                            "filename": name,
                            "data": data,
                            "style": default_style,  # 기본 스타일
                        })
                    except Exception as e:
                        errors.append(format_error_korean(e, name))
                    done += 1
                    progress.progress(
                        done / total,
                        text=f"[{done}/{total}] 분석 완료",
                    )

            progress.progress(1.0, text="✅ 분석 완료")
            # 업로드 순서대로 정렬 (병렬 완료 순서가 뒤죽박죽일 수 있음)
            order = {name: i for i, (name, _, _) in enumerate(file_jobs)}
            properties.sort(key=lambda p: order.get(p["filename"], 999))

            if properties:
                st.session_state["properties"] = properties
                st.session_state.pop("blog_posts", None)
                # 디스크에 자동 저장 (새로고침 대비)
                _persist_session()
                st.success(
                    f"✅ {len(properties)}개 도면 분석 완료! 2번 탭에서 확인하세요."
                )
            if errors:
                st.error("⚠️ 일부 파일 분석 실패")
                for err_msg in errors:
                    st.markdown(err_msg)

# ───── Tab 2: 추출 결과 + 파일별 스타일 선택 ─────
with tab2:
    properties = st.session_state.get("properties")
    if not properties:
        st.info("👈 먼저 1번 탭에서 도면을 업로드하고 분석을 실행하세요.")
    else:
        st.subheader(f"📋 추출 결과 — 총 {len(properties)}개")
        st.caption(
            "AI 추출 정보를 확인하고, 매물별로 글 스타일을 선택하세요. "
            "틀린 정보는 직접 수정 가능합니다."
        )

        for idx, prop in enumerate(properties):
            data = prop["data"]
            station = data.get("nearest_station") or {}
            with st.expander(
                f"📄 {idx+1}. {prop['filename']}  —  "
                f"{data.get('layout', '?')} / ¥{data.get('rent_yen', 0):,}",
                expanded=(idx == 0),
            ):
                col1, col2, col3 = st.columns([2, 2, 1])
                with col1:
                    st.markdown(
                        f"**가장 가까운 역**: {station.get('line', '?')} "
                        f"{station.get('station', '?')}역 "
                        f"도보 {station.get('walk_minutes', '?')}분"
                    )
                    st.markdown(
                        f"**방구조 / 전용면적**: {data.get('layout', '?')} "
                        f"/ {data.get('area_sqm', '?')}㎡"
                    )
                with col2:
                    mgmt = data.get("management_fee_yen") or 0
                    st.metric(
                        "월세",
                        f"¥{data.get('rent_yen', 0):,}",
                        f"관리비 ¥{mgmt:,}",
                        delta_color="off",
                    )
                with col3:
                    conf = data.get("extraction_confidence", "?")
                    if conf == "low":
                        st.error(f"⚠️ 자신도: {conf}")
                    elif conf == "medium":
                        st.warning(f"자신도: {conf}")
                    else:
                        st.success(f"자신도: {conf}")
                    engine_used = data.get("_engine_used", "?")
                    if "gemini" in engine_used and "claude" not in engine_used:
                        st.caption(f"🆓 {engine_used}")
                    elif "폴백" in engine_used:
                        st.caption(f"🔀 {engine_used}")
                    else:
                        st.caption(f"💎 {engine_used}")

                # 이 매물의 스타일 선택 (파일별 다른 스타일 가능)
                prop["style"] = st.selectbox(
                    f"이 매물의 글 스타일",
                    options=available_styles,
                    index=available_styles.index(prop.get("style", default_style))
                    if prop.get("style", default_style) in available_styles
                    else 0,
                    key=f"style_select_{idx}",
                    help="매물마다 다른 스타일을 선택할 수 있습니다.",
                )

                edited = st.text_area(
                    "추출 데이터 (필요시 직접 수정)",
                    value=json.dumps(data, ensure_ascii=False, indent=2),
                    height=240,
                    key=f"json_edit_{idx}",
                )
                try:
                    prop["data"] = json.loads(edited)
                except json.JSONDecodeError as e:
                    st.warning(f"⚠️ JSON 형식 오류: {e}")

        st.divider()
        st.markdown("### ✍️ 블로그 글 일괄 생성 (병렬 처리)")

        custom_instructions = st.text_area(
            "전체 글 공통 특별 지시 (선택)",
            placeholder=(
                "전체 글에 공통 적용할 지시. 예:\n"
                "• 여성 손님 대상, 안전성 강조\n"
                "• 한인 마트·한국 음식점 정보 비중 늘리기"
            ),
            height=80,
        )

        # 스타일 요약 표시
        style_summary = ", ".join(
            f"{i+1}번: {p['style']}" for i, p in enumerate(properties)
        )
        st.caption(f"📝 선택된 스타일: {style_summary}")

        if st.button(f"✍️ 블로그 {len(properties)}개 일괄 생성", type="primary"):
            blog_posts = [None] * len(properties)
            errors = []
            progress = st.progress(0.0, text="병렬 생성 시작…")

            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as executor:
                future_to_idx = {
                    executor.submit(
                        _generate_worker,
                        prop["data"],
                        target_visa,
                        prop["style"],
                        custom_instructions,
                        model,
                    ): i
                    for i, prop in enumerate(properties)
                }

                done = 0
                total = len(future_to_idx)
                for future in as_completed(future_to_idx):
                    i = future_to_idx[future]
                    name = properties[i]["filename"]
                    try:
                        post = future.result()
                        blog_posts[i] = {"filename": name, "post": post}
                    except Exception as e:
                        errors.append(format_error_korean(e, name))
                    done += 1
                    progress.progress(
                        done / total, text=f"[{done}/{total}] 생성 완료"
                    )

            progress.progress(1.0, text="✅ 생성 완료")
            blog_posts = [bp for bp in blog_posts if bp]

            if blog_posts:
                st.session_state["blog_posts"] = blog_posts

                # ⭐ 디스크에 영구 이력 저장 (이력 보관함 expander에서 조회)
                new_history_items = []
                for bp in blog_posts:
                    post = bp["post"]
                    new_history_items.append({
                        "filename": bp["filename"],
                        "title": post.get("title", ""),
                        "summary_for_chat": post.get("summary_for_chat", ""),
                        "html_content": post.get("html_content", ""),
                        "hashtags": post.get("hashtags", []),
                    })
                add_to_history(new_history_items, user_email=current_email)

                # 현재 세션도 디스크 저장 (새로고침 대비)
                _persist_session()

                st.success(
                    f"✅ 블로그 {len(blog_posts)}개 생성 완료! "
                    f"3번 탭에서 확인하시거나, 화면 상단 **'📚 작업 이력 보관함'** expander에서 "
                    f"나중에라도 다시 조회할 수 있습니다."
                )
                st.balloons()

                # 2초 대기 후 페이지 새로고침 → 이력 보관함 expander 자동 갱신
                time.sleep(2)
                st.rerun()
            if errors:
                st.error("⚠️ 일부 블로그 생성 실패")
                for err_msg in errors:
                    st.markdown(err_msg)

# ───── Tab 3: 블로그 미리보기 ─────
with tab3:
    blog_posts = st.session_state.get("blog_posts")

    if not blog_posts:
        st.info(
            "👈 2번 탭에서 블로그 글을 생성하세요.\n\n"
            "💡 과거에 생성한 블로그 글은 화면 상단의 **'📚 작업 이력 보관함'** expander에서 다시 조회·다운로드 가능합니다."
        )
    else:
        st.subheader(f"📝 생성된 블로그 — 총 {len(blog_posts)}개")

        # 전체 ZIP 다운로드 (G드라이브 저장용)
        target_folder = OUTPUT_FOLDER_PATH
        timestamp_label = datetime.now().strftime("%Y년%m월%d일 %H시%M분")
        timestamp_file = datetime.now().strftime("%Y%m%d_%H%M")

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, bp in enumerate(blog_posts):
                post = bp["post"]
                stem = Path(bp["filename"]).stem
                html = build_naver_smarteditor_html(post)
                zf.writestr(f"{timestamp_file}_{idx+1:02d}_{stem}.html", html)
                zf.writestr(
                    f"{timestamp_file}_{idx+1:02d}_{stem}.json",
                    json.dumps(post, ensure_ascii=False, indent=2),
                )
        zip_buf.seek(0)

        # 큰 안내 박스
        st.markdown(
            f"""
            <div style="background:#e3f2fd;border-left:5px solid #1976d2;
                        padding:14px 18px;border-radius:6px;margin:12px 0">
                <div style="font-size:15px;font-weight:600;color:#0d47a1;
                            margin-bottom:6px">
                    💾 작업 결과 저장 안내 ({timestamp_label} 작업분 — {len(blog_posts)}개)
                </div>
                <div style="font-size:13px;color:#1a1a2e;line-height:1.7">
                    아래 <b>ZIP 다운로드 버튼</b>을 누르면 압축 파일이 생성됩니다.<br>
                    다운로드 창에서 <b>저장 위치를 다음 폴더로 지정</b>하세요:
                </div>
                <div style="background:#fff;border:1px solid #bbdefb;
                            padding:8px 12px;border-radius:4px;margin-top:8px;
                            font-family:'Courier New',monospace;font-size:12px;
                            color:#0d47a1;word-break:break-all">
                    {target_folder}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        col_a, col_b = st.columns([1, 1])
        with col_a:
            # 경로 한 줄 → 우측 📋 아이콘으로 클립보드 복사
            st.markdown("**📋 저장 경로 (우측 아이콘 클릭하면 복사됨)**")
            st.code(target_folder, language=None)
        with col_b:
            st.markdown("**📦 ZIP 다운로드**")
            st.download_button(
                f"📦 ZIP 다운로드 ({len(blog_posts)}개 블로그)",
                zip_buf.getvalue(),
                file_name=f"블로그_{timestamp_file}.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )

        with st.expander("💡 매번 자동으로 G드라이브 폴더에 저장되게 하는 방법"):
            st.markdown(
                f"""
                **Chrome 다운로드 위치를 G드라이브 폴더로 설정하면 매번 자동 저장됩니다.**

                1. Chrome 우측 위 **⋮ → 설정**
                2. 왼쪽 메뉴 **다운로드**
                3. **"위치"** 옆 **"변경"** 클릭
                4. 다음 폴더 선택:
                   ```
                   {target_folder}
                   ```
                5. **"다운로드 전에 각 파일의 저장 위치 확인"** → 꺼두기

                > ⚠️ 이 설정 후에는 다른 곳에서 받는 파일도 이 폴더로 갑니다.
                > 매번 위치를 묻게 하려면 5번을 켜두세요. (그래도 시작 위치가
                > G드라이브 폴더라 매번 클릭 두세 번이면 끝납니다.)
                """
            )

        st.divider()

        for idx, bp in enumerate(blog_posts):
            post = bp["post"]
            with st.expander(
                f"📝 {idx+1}. {post.get('title', bp['filename'])}",
                expanded=(idx == 0),
            ):
                st.text_input("제목", value=post.get("title", ""), key=f"title_{idx}")

                # ⭐ 카카오톡용 요약 + 복사 버튼 (st.code의 내장 복사 아이콘 사용)
                st.markdown("**📱 카카오톡용 요약** (우측 상단 📋 아이콘 클릭하면 복사)")
                st.code(post.get("summary_for_chat", ""), language=None, wrap_lines=True)

                st.markdown("**미리보기**")
                st.caption("🚨 빨간색 배경의 '⚠️ 현지 확인 필요' 부분은 직접 채워 넣으세요")
                st.markdown(post.get("html_content", ""), unsafe_allow_html=True)

                st.markdown("**해시태그**")
                st.code(" ".join(post.get("hashtags", [])), language=None)

                # ⭐ 네이버 발행 보조 — 본문 HTML 자동 복사 + 글쓰기 페이지 열기
                st.markdown("---")
                st.markdown("**✍️ 네이버 블로그 발행 보조**")

                html_content = post.get("html_content", "")
                html_json = json.dumps(html_content)  # JS에 안전하게 전달

                st.components.v1.html(
                    f"""
                    <button id="naver-btn-{idx}"
                        style="
                            background:#03c75a;
                            color:white;
                            border:none;
                            padding:14px 24px;
                            font-size:15px;
                            font-weight:600;
                            border-radius:8px;
                            cursor:pointer;
                            width:100%;
                            box-shadow:0 2px 4px rgba(0,0,0,0.1);
                        ">
                        📋 본문 HTML 복사 + 네이버 블로그 글쓰기 열기
                    </button>
                    <p id="status-{idx}" style="
                        margin-top:8px;
                        font-size:13px;
                        color:#555;
                        font-family:sans-serif;
                        min-height:18px;
                    "></p>
                    <script>
                        document.getElementById('naver-btn-{idx}').addEventListener('click', function() {{
                            const html = {html_json};
                            const status = document.getElementById('status-{idx}');
                            navigator.clipboard.writeText(html).then(() => {{
                                status.innerHTML = '✅ HTML 클립보드에 복사 완료! 네이버 글쓰기 페이지가 새 탭에 열립니다.';
                                status.style.color = '#03c75a';
                                window.open('https://blog.naver.com/PostWriteForm.naver', '_blank');
                            }}).catch(err => {{
                                status.innerHTML = '❌ 복사 실패: ' + err.message + ' (아래 HTML 다운로드 사용)';
                                status.style.color = '#d32f2f';
                            }});
                        }});
                    </script>
                    """,
                    height=110,
                )

                st.caption(
                    "💡 사용법: 위 초록색 버튼 클릭 → 네이버 글쓰기 페이지가 새 탭에 열림 → "
                    "글쓰기 화면 우측 위 **'기본 도구'** 옆 ⋮ → **'HTML 편집'** 클릭 → "
                    "편집창에 **Ctrl+V** 로 붙여넣기 → 발행"
                )

                c1, c2 = st.columns(2)
                with c1:
                    st.download_button(
                        "💾 HTML 다운로드 (백업)",
                        build_naver_smarteditor_html(post),
                        file_name=f"blog_{idx+1}_{Path(bp['filename']).stem}.html",
                        mime="text/html",
                        key=f"dl_html_{idx}",
                    )
                with c2:
                    st.download_button(
                        "📋 JSON 다운로드",
                        json.dumps(post, ensure_ascii=False, indent=2),
                        file_name=f"blog_{idx+1}.json",
                        mime="application/json",
                        key=f"dl_json_{idx}",
                    )

# ───── Tab 4: 네이버 카페 자동 발행 ─────
with tab4:
    st.subheader("📤 네이버 카페 자동 발행")
    blog_posts = st.session_state.get("blog_posts")
    if not blog_posts:
        st.info("먼저 1·2·3번 탭에서 블로그 글을 생성하세요.")
        st.stop()

    # ─── 환경 변수 확인 ───
    naver_client_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_client_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    naver_club_id = os.getenv("NAVER_CAFE_CLUB_ID", "").strip()
    naver_menu_id = os.getenv("NAVER_CAFE_DEFAULT_MENU_ID", "").strip()
    naver_redirect = os.getenv(
        "NAVER_REDIRECT_URI", "https://jreblog.streamlit.app"
    ).strip()

    if not (naver_client_id and naver_client_secret and naver_club_id):
        st.error("⚠️ 네이버 카페 API 환경변수가 설정되지 않았습니다.")
        st.info(
            "💡 **설정 방법**: Streamlit Cloud → Settings → Secrets 에 다음을 추가:\n\n"
            "```\n"
            'NAVER_CLIENT_ID = "발급받은_Client_ID"\n'
            'NAVER_CLIENT_SECRET = "발급받은_Client_Secret"\n'
            'NAVER_CAFE_CLUB_ID = "카페_club_id"\n'
            'NAVER_CAFE_DEFAULT_MENU_ID = "기본_게시판_menu_id"\n'
            'NAVER_REDIRECT_URI = "https://jreblog.streamlit.app"\n'
            "```\n\n"
            "💡 우선은 3번 탭의 '📋 본문 HTML 복사' 버튼으로 수동 발행 가능합니다."
        )
        st.stop()

    # ─── 카페 클라이언트 준비 ───
    try:
        client = NaverCafeClient(
            client_id=naver_client_id,
            client_secret=naver_client_secret,
            redirect_uri=naver_redirect,
            club_id=naver_club_id,
            default_menu_id=naver_menu_id,
            access_token=st.session_state.get("naver_access_token"),
            refresh_token=st.session_state.get("naver_refresh_token"),
        )
    except Exception as e:
        st.error(format_publish_error_korean(e))
        st.stop()

    # ─── OAuth 인증 흐름: URL의 ?code= 자동 처리 ───
    auth_code_in_url = st.query_params.get("code", "")
    if auth_code_in_url and not st.session_state.get("naver_access_token"):
        with st.spinner("🔐 네이버 인증 처리 중..."):
            try:
                client.exchange_code_for_token(auth_code_in_url)
                st.session_state["naver_access_token"] = client.access_token
                st.session_state["naver_refresh_token"] = client.refresh_token
                # ?code= 제거 (auth 토큰은 유지)
                params = dict(st.query_params)
                params.pop("code", None)
                params.pop("state", None)
                st.query_params.clear()
                for k, v in params.items():
                    st.query_params[k] = v
                st.success("✅ 네이버 인증 완료! 이제 카페에 발행할 수 있습니다.")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 인증 실패: {format_publish_error_korean(e)}")

    # ─── 인증 안 됐으면: 인증 안내 ───
    if not st.session_state.get("naver_access_token"):
        st.markdown(
            "### 🔐 최초 1회 네이버 카페 인증 필요\n\n"
            "**카페 매니저 또는 부매니저 계정**으로 인증하면 이후 자동 갱신되어 "
            "영구 자동 발행이 가능합니다."
        )

        auth_url = client.get_auth_url()

        st.link_button(
            "🔐 네이버 카페 인증하기 (새 탭에서 열기)",
            auth_url,
            use_container_width=True,
        )

        st.caption(
            "1. 위 버튼 클릭 → 네이버 로그인 페이지가 새 탭에서 열림\n"
            "2. **글쓰기 권한이 있는 계정**으로 로그인:\n"
            "   - ✅ 카페 매니저 계정\n"
            "   - ✅ 카페 부매니저 계정\n"
            "   - ✅ 해당 게시판에 글쓰기 권한이 있는 일반 회원 계정\n"
            "3. '동의하기' 클릭\n"
            "4. 이 페이지로 자동 복귀 → 인증 완료\n\n"
            "💡 이 인증은 한 번만 하면 됩니다."
        )
        st.stop()

    # ─── 인증 완료 후 발행 UI ───
    st.success("✅ 네이버 인증 완료. 카페 발행 가능 상태입니다.")

    with st.expander("ℹ️ 발행 설정 확인", expanded=False):
        st.write(f"**카페 ID**: `{naver_club_id}`")
        st.write(f"**기본 게시판 ID**: `{naver_menu_id}`")
        try:
            profile = client.get_profile()
            st.write(f"**인증된 계정**: {profile.get('name', '?')} ({profile.get('email', '?')})")
        except Exception:
            pass

        if st.button("🔓 인증 해제 (재인증)"):
            st.session_state.pop("naver_access_token", None)
            st.session_state.pop("naver_refresh_token", None)
            st.rerun()

    st.markdown("---")
    st.markdown("### 📝 발행할 글 선택")

    # 발행 옵션
    col_opt1, col_opt2 = st.columns(2)
    with col_opt1:
        target_menu_id = st.text_input(
            "게시판 ID",
            value=naver_menu_id,
            help="다른 게시판에 올리려면 변경. 기본값 = Streamlit Secrets의 NAVER_CAFE_DEFAULT_MENU_ID",
        )
    with col_opt2:
        is_open_all = st.checkbox(
            "전체 공개로 발행",
            value=True,
            help="해제하면 카페 멤버만 볼 수 있음",
        )

    # 발행 대상 선택 (체크박스)
    st.markdown("**아래에서 발행할 글을 선택하세요:**")
    selected_indices = []
    for idx, bp in enumerate(blog_posts):
        post = bp["post"]
        title = post.get("title", bp["filename"])
        if st.checkbox(f"{idx+1}. {title}", key=f"publish_select_{idx}"):
            selected_indices.append(idx)

    # 일괄 발행 버튼
    st.markdown("---")
    if st.button(
        f"🚀 선택한 {len(selected_indices)}개 글 카페에 일괄 발행",
        type="primary",
        disabled=(len(selected_indices) == 0),
        use_container_width=True,
    ):
        if not target_menu_id:
            st.error("게시판 ID를 입력하세요.")
        else:
            progress = st.progress(0.0, text="발행 시작...")
            results = []
            errors = []

            # 최신 토큰 동기화
            client.access_token = st.session_state.get("naver_access_token")
            client.refresh_token = st.session_state.get("naver_refresh_token")

            total = len(selected_indices)
            for i, idx in enumerate(selected_indices):
                bp = blog_posts[idx]
                post = bp["post"]
                title = post.get("title", bp["filename"])
                progress.progress(
                    i / total, text=f"[{i+1}/{total}] '{title[:30]}...' 발행 중"
                )
                try:
                    result = client.write_article(
                        subject=title,
                        content_html=post.get("html_content", ""),
                        menu_id=target_menu_id,
                        is_open=is_open_all,
                    )
                    # 토큰이 갱신됐을 수 있으니 session_state에 다시 저장
                    st.session_state["naver_access_token"] = client.access_token
                    results.append({
                        "idx": idx,
                        "title": title,
                        "article_id": result["article_id"],
                        "article_url": result["article_url"],
                    })
                except Exception as e:
                    errors.append({
                        "idx": idx,
                        "title": title,
                        "error": format_publish_error_korean(e),
                    })

            progress.progress(1.0, text="✅ 발행 완료")

            # 결과 표시
            if results:
                st.success(f"✅ {len(results)}개 글 발행 성공!")
                for r in results:
                    st.markdown(
                        f"- **{r['idx']+1}. {r['title']}**  →  "
                        f"[카페에서 보기 (글 #{r['article_id']})]({r['article_url']})"
                    )

            if errors:
                st.error(f"⚠️ {len(errors)}개 글 발행 실패")
                for e in errors:
                    st.markdown(f"**{e['idx']+1}. {e['title']}**")
                    st.markdown(e["error"])
