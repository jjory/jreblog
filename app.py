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
    list_available_styles,
)
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
def _extract_property_number(filename: str) -> str:
    """파일명 앞 7자리 숫자를 매물번호로 추출. '1234567_도면.jpg' → '1234567'"""
    if not filename:
        return ""
    m = re.match(r"^(\d{7})", filename.strip())
    if m:
        return m.group(1)
    return ""


def _insert_property_number_to_table(html: str, prop_num: str) -> str:
    """본문 '매물 기본정보' 표 맨 위에 매물번호 행을 삽입.
    첫 번째 <table>의 첫 <tr> 앞에 매물번호 행 추가.
    이미 매물번호 행이 있으면 중복 삽입 안 함.
    """
    if not html or not prop_num:
        return html
    # 이미 매물번호가 들어있으면 스킵
    if "매물번호" in html:
        return html

    # 첫 번째 <table ...> 다음에 매물번호 행 삽입
    num_row = (
        '<tr>'
        '<td style="border:1px solid #ddd;padding:8px 12px;background:#f5f5f5;'
        'width:30%;font-weight:bold">매물번호</td>'
        f'<td style="border:1px solid #ddd;padding:8px 12px">{prop_num}</td>'
        '</tr>'
    )
    # <table ...> 태그를 찾아 그 직후에 삽입
    m = re.search(r"(<table[^>]*>)", html)
    if m:
        insert_pos = m.end()
        return html[:insert_pos] + num_row + html[insert_pos:]
    # 표가 없으면 원본 유지
    return html


def _extract_ward(title: str) -> str:
    """제목에서 구/시 추출. 실제 제목: '이타바시구 도부토조선 나리마스역 도보 5분 ...'
    또는 매물번호가 앞에 붙은 경우: '[1234567] 이타바시구 ...' """
    if not title:
        return "기타"
    # 매물번호 [숫자] 제거
    t = re.sub(r"^\[\d+\]\s*", "", title.strip())
    # 첫 단어가 '~구' 또는 '~시'로 끝나면 그것이 지역
    m = re.match(r"^([가-힣]+(?:구|시|정|초|쿠))", t)
    if m:
        return m.group(1)
    # fallback: 첫 단어
    first_word = t.split()[0] if t.split() else ""
    return first_word if first_word else "기타"


def _extract_line(title: str) -> str:
    """제목에서 노선 추출. 실제 제목: '이타바시구 도부토조선 나리마스역 ...'
    구 다음 ~선/~라인으로 끝나는 단어."""
    if not title:
        return "기타"
    t = re.sub(r"^\[\d+\]\s*", "", title.strip())
    # ~선 또는 ~라인으로 끝나는 단어 찾기 (역 앞)
    m = re.search(r"([\w가-힣]+(?:선|라인|Line))\s+[\w가-힣]+역", t)
    if m:
        return m.group(1)
    # JR 야마노테선 같은 복합 노선
    m2 = re.search(r"(JR\s*[\w가-힣]+선)", t)
    if m2:
        return m2.group(1).replace(" ", "")
    return "기타"


def _extract_station(title: str) -> str:
    """제목에서 역명 추출. 실제 제목: '... 나리마스역 도보 5분 ...'
    ~역으로 끝나는 단어 (역 글자 제외)."""
    if not title:
        return "기타"
    t = re.sub(r"^\[\d+\]\s*", "", title.strip())
    # ~역 패턴 (역명만 추출)
    m = re.search(r"([\w가-힣]+)역", t)
    if m:
        return m.group(1)
    return "기타"


def _extract_rent(title: str) -> int:
    """제목에서 월세 추출 (엔 단위). '월세 ¥80,000+관리비 ¥5,000' → 85000"""
    if not title:
        return 0
    total = 0
    # 월세 ¥XX,XXX (공백 허용)
    m_rent = re.search(r"월세\s*[¥￥]\s*([\d,]+)", title)
    if m_rent:
        try:
            total += int(m_rent.group(1).replace(",", ""))
        except ValueError:
            pass
    # 관리비 ¥X,XXX (공백 허용)
    m_mgmt = re.search(r"관리비\s*[¥￥]\s*([\d,]+)", title)
    if m_mgmt:
        try:
            total += int(m_mgmt.group(1).replace(",", ""))
        except ValueError:
            pass
    return total


def _extract_room_type(title: str) -> str:
    """제목에서 방구조 추출. '도보 5분 1K 월세...' → '1K'"""
    if not title:
        return "기타"
    # 도보 N분 [방구조] 월세 패턴 (공백 허용)
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
                        # ⭐ 로그인 시 새 세션 ID 발급 + 이전 작업 상태 초기화
                        # (계정 전환 시 이전 계정의 분석 결과가 따라오지 않게)
                        st.query_params["sid"] = generate_session_id()
                        st.session_state.pop("properties", None)
                        st.session_state.pop("blog_posts", None)
                        st.session_state.pop("untranslated_alert", None)
                        st.session_state.pop("_analysis_done_this_session", None)
                        st.session_state.pop("_session_restored", None)
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
    """로그아웃 — 세션 + URL 토큰 + 작업 상태 모두 제거"""
    st.session_state.pop("user", None)
    # remembered_email은 유지 (다음 로그인 시 자동 입력)
    if "user_email" in st.query_params:
        del st.query_params["user_email"]
    if "auth" in st.query_params:
        del st.query_params["auth"]
    # ⭐ 작업 상태 + 세션 ID 초기화 (다음 로그인 계정에 안 따라가게)
    st.session_state.pop("properties", None)
    st.session_state.pop("blog_posts", None)
    st.session_state.pop("untranslated_alert", None)
    st.session_state.pop("_analysis_done_this_session", None)
    st.session_state.pop("_session_restored", None)
    if "sid" in st.query_params:
        del st.query_params["sid"]





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

    # 분석/생성을 한 번이라도 한 세션이면 복원하지 않음
    # (새 분석 결과를 디스크의 옛 데이터가 덮어쓰는 버그 방지)
    if st.session_state.get("_analysis_done_this_session"):
        st.session_state["_session_restored"] = True
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


def _persist_session(overwrite: bool = False):
    """현재 작업 상태를 디스크에 자동 저장.
    overwrite=True면 properties가 없어도 빈 값으로 덮어써서
    옛 데이터가 남지 않게 함.
    """
    sid = st.query_params.get("sid", "")
    if not sid:
        return

    data = {}
    if st.session_state.get("properties"):
        data["properties"] = st.session_state["properties"]
    if st.session_state.get("blog_posts"):
        data["blog_posts"] = st.session_state["blog_posts"]

    if data or overwrite:
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

# 사용자 환영 카드 — 회사 전체 통계 표시 (이력은 모두 공유)
try:
    _all_history = load_history()
    # 기존 이력 호환성 (user_email 없는 경우)
    for h in _all_history:
        if not h.get("user_email"):
            h["user_email"] = ADMIN_EMAIL

    # 회사 전체 매물 (관리자/사용자 모두 동일하게 표시)
    _shown_history = _all_history

    _my_count = len(_shown_history)
    _my_cost = _my_count * 110  # 매물당 약 110원 (Opus + Extended Thinking)
    _my_this_month = sum(
        1 for h in _shown_history
        if h.get("timestamp", "")[:7] == datetime.now().strftime("%Y-%m")
    )
except Exception:
    _my_count = _my_cost = _my_this_month = 0

# 라벨 — 역할만 다르고 통계는 모두 전체
_role_label = "👑 관리자" if is_admin else "👤 사용자"
_count_label = "회사 매물"
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

    # ─── 📥 Excel 리포트 다운로드 (모든 사용자) ───
    with st.expander("📥 Excel 리포트 다운로드", expanded=False):
        st.caption("💡 전체 매물 이력 + 통계 요약 Excel")
        try:
            _sb_history = load_history()
            for _h in _sb_history:
                if not _h.get("user_email"):
                    _h["user_email"] = ADMIN_EMAIL
            excel_bytes = _build_excel_report(_sb_history)
            if excel_bytes:
                st.download_button(
                    f"📊 다운로드 ({len(_sb_history)}건)",
                    excel_bytes,
                    file_name=f"JRE_매물이력_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            else:
                st.warning("⚠️ openpyxl 미설치")
        except Exception as e:
            st.error(f"Excel 생성 실패: {e}")

    # ─── 🗑 오래된 이력 정리 (관리자 전용 — 삭제는 신중하게) ───
    if is_admin:
        with st.expander("🗑 오래된 이력 일괄 정리 (관리자)", expanded=False):
            st.caption("💡 즐겨찾기(⭐)는 삭제되지 않습니다.")
            months_to_clean = st.slider(
                "몇 개월 이상 삭제?",
                min_value=1, max_value=24, value=12, step=1,
                key="cleanup_months_slider_sidebar",
            )
            confirm_key = f"_confirm_cleanup_sb_{months_to_clean}"
            if st.button(
                f"🗑 {months_to_clean}개월+ 삭제",
                type="secondary",
                use_container_width=True,
                key=f"cleanup_btn_sb_{months_to_clean}",
            ):
                if st.session_state.get(confirm_key):
                    deleted = delete_old_history(months_to_clean)
                    st.session_state.pop(confirm_key, None)
                    st.success(f"✅ {deleted}건 삭제 완료 (즐겨찾기 보호)")
                    st.rerun()
                else:
                    st.session_state[confirm_key] = True
                    st.warning("⚠️ 한 번 더 클릭하면 삭제됩니다.")

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
# 블로그 일괄 생성 공통 헬퍼 (Tab 1 자동 + Tab 2 수동 공통 사용)
# ─────────────────────────────────────────────────
def _run_blog_generation(
    properties: list,
    custom_instructions: str,
    target_visa: str,
    model: str,
    current_email: str,
) -> None:
    """
    블로그 일괄 생성 (병렬 처리) + 이력 영구 저장.
    - Tab 1 자동 모드: 분석 직후 즉시 호출
    - Tab 2 수동 모드: 사장님이 버튼 클릭 시 호출
    UI(progress·success·error)를 직접 렌더링하고, 완료 시 st.rerun() 호출.
    """
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
            prop_num = properties[i].get("property_number", "")
            try:
                post = future.result()
                if prop_num:
                    post["html_content"] = _insert_property_number_to_table(
                        post.get("html_content", ""), prop_num
                    )
                blog_posts[i] = {
                    "filename": name,
                    "property_number": prop_num,
                    "post": post,
                }
            except Exception as e:
                errors.append(format_error_korean(e, name))
            done += 1
            progress.progress(done / total, text=f"[{done}/{total}] 생성 완료")

    progress.progress(1.0, text="✅ 생성 완료")
    blog_posts = [bp for bp in blog_posts if bp]

    if blog_posts:
        st.session_state["blog_posts"] = blog_posts
        # 디스크에 영구 이력 저장 (작업 이력 보관함에서 조회)
        new_history_items = []
        for bp in blog_posts:
            post = bp["post"]
            new_history_items.append({
                "filename": bp["filename"],
                "property_number": bp.get("property_number", ""),
                "title": post.get("title", ""),
                "summary_for_chat": post.get("summary_for_chat", ""),
                "html_content": post.get("html_content", ""),
                "hashtags": post.get("hashtags", []),
            })
        add_to_history(new_history_items, user_email=current_email)
        _persist_session()

        # 번역 DB 미등록 항목 수집 → 경고 표시
        all_untranslated = []
        for bp in blog_posts:
            for item in bp["post"].get("untranslated", []):
                all_untranslated.append(item)
        seen_keys = set()
        unique_untranslated = []
        for item in all_untranslated:
            k = (item.get("category"), item.get("original"))
            if k not in seen_keys:
                seen_keys.add(k)
                unique_untranslated.append(item)
        st.session_state["untranslated_alert"] = unique_untranslated

        st.success(
            f"✅ 블로그 {len(blog_posts)}개 생성 완료! "
            f"3번 탭에서 확인하시거나, **4️⃣ 작업 이력 보관함 탭**에서 "
            f"나중에라도 다시 조회할 수 있습니다."
        )
        st.balloons()
        # 2초 대기 후 새로고침 → 이력 보관함 자동 갱신
        time.sleep(2)
        st.rerun()
    if errors:
        st.error("⚠️ 일부 블로그 생성 실패")
        for err_msg in errors:
            st.markdown(err_msg)


# ─────────────────────────────────────────────────
# 4단계 탭
# ─────────────────────────────────────────────────

tab1, tab2, tab3, tab4 = st.tabs(
    [
        "1️⃣ 도면 업로드",
        "2️⃣ 추출 결과·스타일 선택",
        "3️⃣ 블로그 미리보기",
        "4️⃣ 📚 작업 이력 보관함",
    ]
)

# ───── Tab 1: 업로드 (병렬 분석) ─────
with tab1:
    st.subheader(f"마이소크 (物件図面) 업로드 — 한 번에 최대 {MAX_UPLOADS}개")
    st.caption("지원 형식: JPG · PNG · WEBP · GIF · PDF")
    st.info(
        "📌 **매물번호 안내**: 파일명 맨 앞에 **7자리 숫자**를 붙여주세요.\n\n"
        "예: `1234567_매물도면.jpg` → 매물번호 **1234567**\n\n"
        "매물번호는 블로그 제목·이력 보관함·검색에 사용됩니다."
    )

    uploaded_files = st.file_uploader(
        f"도면 파일을 최대 {MAX_UPLOADS}개까지 선택하세요",
        type=["jpg", "jpeg", "png", "webp", "gif", "pdf"],
        accept_multiple_files=True,
    )

    if uploaded_files:
        if len(uploaded_files) > MAX_UPLOADS:
            st.warning(f"⚠️ 최대 {MAX_UPLOADS}개까지만 처리됩니다.")
            uploaded_files = uploaded_files[:MAX_UPLOADS]

        # 매물번호 미인식 파일 경고
        no_number_files = [
            uf.name for uf in uploaded_files
            if not _extract_property_number(uf.name)
        ]
        if no_number_files:
            st.warning(
                "⚠️ **다음 파일은 7자리 매물번호가 인식되지 않았습니다:**\n\n"
                + "\n".join(f"- {n}" for n in no_number_files)
                + "\n\n매물번호 없이도 진행되지만, 파일명 앞에 7자리 숫자를 "
                "붙이는 것을 권장합니다. (예: `1234567_원래파일명.jpg`)"
            )

        st.write(f"**업로드된 파일: {len(uploaded_files)}개**")
        cols = st.columns(min(len(uploaded_files), MAX_UPLOADS))
        for i, uf in enumerate(uploaded_files):
            with cols[i]:
                if uf.name.lower().endswith(".pdf"):
                    st.info(f"📄 {uf.name}\n(PDF)")
                else:
                    st.image(uf, caption=uf.name, use_container_width=True)

        # ⭐ 자동 모드: 분석 완료 후 블로그 생성을 자동으로 이어서 진행 (한 번 클릭으로 끝)
        auto_generate_enabled = st.checkbox(
            "🚀 자동 모드 — 분석 완료 시 블로그도 바로 생성",
            value=True,
            key="auto_generate_enabled",
            help="끄면 분석만 하고 2번 탭에서 직접 생성 버튼을 누릅니다. "
                 "특별 지시사항을 추가하시려면 끄고 수동으로 진행하세요.",
        )

        if st.button("🔍 전체 도면 병렬 분석 시작", type="primary"):
            # ⭐ 이전 분석 결과 완전 초기화 (다른 도면인데 같은 결과 나오는 버그 방지)
            st.session_state.pop("properties", None)
            st.session_state.pop("blog_posts", None)
            st.session_state.pop("untranslated_alert", None)

            properties = []
            errors = []
            progress = st.progress(0.0, text="병렬 분석 시작…")

            # 파일 미리 읽기 (Streamlit UploadedFile은 thread-safe 안 함)
            # ⭐ 파일명 중복 방지: 같은 이름이면 인덱스 부여
            file_jobs = []
            seen_names = {}
            for uf in uploaded_files:
                base_name = uf.name
                if base_name in seen_names:
                    seen_names[base_name] += 1
                    # 같은 파일명 구분 (확장자 앞에 _2, _3)
                    stem = Path(base_name).stem
                    suf = Path(base_name).suffix
                    unique_name = f"{stem}_{seen_names[base_name]}{suf}"
                else:
                    seen_names[base_name] = 1
                    unique_name = base_name
                file_jobs.append((unique_name, uf.getvalue(), Path(uf.name).suffix))

            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as executor:
                # ⭐ 인덱스 기반 매핑 (파일명 중복돼도 결과 안 섞임)
                future_to_idx = {
                    executor.submit(_analyze_worker, file_bytes, suffix, engine, model): idx
                    for idx, (name, file_bytes, suffix) in enumerate(file_jobs)
                }

                results_by_idx = {}
                done = 0
                total = len(future_to_idx)
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    name = file_jobs[idx][0]
                    try:
                        data = future.result()
                        results_by_idx[idx] = {
                            "filename": name,
                            "property_number": _extract_property_number(name),
                            "data": data,
                            "style": default_style,  # 기본 스타일
                        }
                    except Exception as e:
                        errors.append(format_error_korean(e, name))
                    done += 1
                    progress.progress(
                        done / total,
                        text=f"[{done}/{total}] 분석 완료",
                    )

            progress.progress(1.0, text="✅ 분석 완료")
            # 업로드 순서대로 정렬 (인덱스 순)
            properties = [results_by_idx[i] for i in sorted(results_by_idx.keys())]

            if properties:
                st.session_state["properties"] = properties
                st.session_state.pop("blog_posts", None)
                # 이 세션에서 분석을 했음을 표시 (복원이 덮어쓰지 못하게)
                st.session_state["_analysis_done_this_session"] = True
                # 디스크에 자동 저장 (새 결과로 덮어쓰기)
                _persist_session(overwrite=True)

                # ⭐ 자동 모드: 분석 직후 블로그 생성을 같은 클릭에서 이어서 진행
                if auto_generate_enabled:
                    st.success(
                        f"✅ {len(properties)}개 도면 분석 완료. 블로그 자동 생성을 시작합니다…"
                    )
                    st.markdown("---")
                    st.markdown("### ✍️ 블로그 자동 생성 중")
                    # 자동 모드는 custom_instructions 없이 진행 (필요 시 자동 모드 끄고 수동)
                    _run_blog_generation(
                        properties=properties,
                        custom_instructions="",
                        target_visa=target_visa,
                        model=model,
                        current_email=current_email,
                    )
                else:
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
            # 제목: 파일명(일본어 PDF명) 대신 핵심 정보로 표시 + 관리비 포함
            _rent = data.get("rent_yen", 0) or 0
            _mgmt = data.get("management_fee_yen", 0) or 0
            _layout = data.get("layout", "?")
            if _mgmt > 0:
                _price_label = f"월세 ¥{_rent:,} + 관리비 ¥{_mgmt:,}"
            else:
                _price_label = f"월세 ¥{_rent:,}"
            _prop_num = prop.get("property_number", "")
            _num_label = f"[{_prop_num}] " if _prop_num else ""
            with st.expander(
                f"📄 {idx+1}. {_num_label}{_layout} / {_price_label}",
                expanded=(idx == 0),
            ):
                st.caption(f"📁 원본 파일: {prop['filename']}")
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
            # ⭐ 공통 헬퍼 호출 (Tab 1 자동 모드와 동일 로직)
            _run_blog_generation(
                properties=properties,
                custom_instructions=custom_instructions,
                target_visa=target_visa,
                model=model,
                current_email=current_email,
            )

# ───── Tab 3: 블로그 미리보기 ─────
with tab3:
    blog_posts = st.session_state.get("blog_posts")

    if not blog_posts:
        st.info(
            "👈 2번 탭에서 블로그 글을 생성하세요.\n\n"
            "💡 과거에 생성한 블로그 글은 **4️⃣ 작업 이력 보관함 탭**에서 다시 조회·다운로드 가능합니다."
        )
    else:
        st.subheader(f"📝 생성된 블로그 — 총 {len(blog_posts)}개")

        # ⭐ DB(路線 시트) 미등록 항목 경고 — 회사 DB 추가 등록 안내
        _untrans = st.session_state.get("untranslated_alert", [])
        if _untrans:
            warn_lines = []
            for item in _untrans:
                warn_lines.append(
                    f"- **{item.get('category')}**: `{item.get('original')}` "
                    f"({item.get('note', '')})"
                )
            st.warning(
                "⚠️ **회사 번역 DB에 등록되지 않은 항목이 있습니다.**\n\n"
                + "\n".join(warn_lines)
                + "\n\n위 항목은 일본어가 한국어로 번역되지 않았을 수 있습니다. "
                "**路線 시트(번역 DB)에 추가 등록**하면 다음부터 자동 번역됩니다.\n\n"
                "👉 DB: https://docs.google.com/spreadsheets/d/1D6u75qwjPodXS82SWaZhJ0MzNIn3Hf_GkPMiYutZthA"
            )

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

                # ⭐ 네이버 발행 보조 — 본문 HTML 클립보드 복사
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
                        📋 서식 포함 복사 (네이버 붙여넣기용)
                    </button>
                    <p id="status-{idx}" style="
                        margin-top:8px;
                        font-size:13px;
                        color:#555;
                        font-family:sans-serif;
                        min-height:18px;
                    "></p>
                    <script>
                        document.getElementById('naver-btn-{idx}').addEventListener('click', async function() {{
                            const html = {html_json};
                            const status = document.getElementById('status-{idx}');
                            try {{
                                // ⭐ 서식 포함(rich) 복사 — 네이버 에디터에 바로 Ctrl+V 하면
                                //    표·체크리스트·이모지·인사말이 모양 그대로 들어감
                                const blobHtml = new Blob([html], {{ type: 'text/html' }});
                                const blobText = new Blob([html], {{ type: 'text/plain' }});
                                const item = new ClipboardItem({{
                                    'text/html': blobHtml,
                                    'text/plain': blobText
                                }});
                                await navigator.clipboard.write([item]);
                                status.innerHTML = '✅ 서식 포함 복사 완료! 네이버 글쓰기 화면에 바로 Ctrl+V';
                                status.style.color = '#03c75a';
                            }} catch (err) {{
                                // 폴백: 일부 구형 브라우저는 ClipboardItem 미지원 → 텍스트로 복사
                                try {{
                                    await navigator.clipboard.writeText(html);
                                    status.innerHTML = '⚠️ 텍스트로만 복사됨 (이 브라우저는 서식 복사 미지원). Chrome 권장.';
                                    status.style.color = '#e67e22';
                                }} catch (e2) {{
                                    status.innerHTML = '❌ 복사 실패: ' + e2.message + ' (아래 HTML 다운로드 사용)';
                                    status.style.color = '#d32f2f';
                                }}
                            }}
                        }});
                    </script>
                    """,
                    height=110,
                )

                st.caption(
                    "💡 **사용법**: 위 초록색 버튼 클릭(서식 포함 복사) → 네이버 블로그 글쓰기 "
                    "화면에 **Ctrl+V** → 발행.  \n"
                    "⚠️ 네이버 에디터는 붙여넣을 때 **정렬·글자 크기를 리셋**합니다 (네이버 정책). "
                    "필요 시 **Ctrl+A 전체선택 → 가운데 정렬 → 글자 15** 한 번이면 글 전체에 적용됩니다."
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

# ───── Tab 4: 작업 이력 보관함 (구 카페발행 탭 위치) ─────
with tab4:
    try:
        _hist_preview = load_history()
        # 작업 이력은 모든 사용자가 공유 (관리자/사용자 구분 없음)
        _hist_count = len(_hist_preview)
    except Exception:
        _hist_preview = []
        _hist_count = 0

    # 파일 크기 계산 (모든 사용자에게 표시)
    try:
        from src.persistence import HISTORY_FILE
        if HISTORY_FILE.exists():
            _hist_bytes = HISTORY_FILE.stat().st_size
            if _hist_bytes < 1024:
                _hist_size = f"{_hist_bytes}B"
            elif _hist_bytes < 1024 * 1024:
                _hist_size = f"{_hist_bytes/1024:.1f}KB"
            else:
                _hist_size = f"{_hist_bytes/(1024*1024):.2f}MB"
        else:
            _hist_size = "0B"
    except Exception:
        _hist_size = ""

    # 최근 작업 / 가장 오래된 계산
    _hist_latest = ""
    _hist_oldest = ""
    try:
        if _hist_preview:
            _latest_ts = datetime.fromisoformat(_hist_preview[0]["timestamp"])
            _hist_latest = _latest_ts.strftime("%m/%d %H:%M")
            _oldest_ts = datetime.fromisoformat(_hist_preview[-1]["timestamp"])
            _days_old = (datetime.now() - _oldest_ts).days
            _hist_oldest = f"{_days_old}일전" if _days_old > 0 else "오늘"
    except Exception:
        pass

    # 한 줄 통계+안내 (상단 공간 최소화)
    if _hist_count == 0:
        st.caption("🗂️ 비어 있음 · 1·2번 탭에서 블로그 생성 시 자동 저장됨")
    else:
        _parts = [f"{_hist_count}건"]
        if _hist_size:
            _parts.append(_hist_size)
        if _hist_latest:
            _parts.append(f"🆕{_hist_latest}")
        if _hist_oldest:
            _parts.append(f"🗓{_hist_oldest}")
        st.caption(
            f"🗂️ {' · '.join(_parts)}  ·  영구 보존, 회사 전체 공유"
        )

    # 이력 로드 (실패해도 빈 리스트 반환) — 모든 사용자가 전체 이력 조회
    try:
        history_all = load_history()
        # 기존 이력 (user_email 없음) → admin@win-bro.com 작성으로 처리
        for h in history_all:
            if not h.get("user_email"):
                h["user_email"] = ADMIN_EMAIL
        # 작업 이력은 회사 공유: 모든 사용자가 전체 이력 조회
        history = history_all
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
        # 검색 + 즐겨찾기 필터 — 한 줄에 나란히
        _col_search, _col_fav = st.columns([4, 1])
        with _col_search:
            search_q = st.text_input(
                "🔍 검색 (매물번호·제목·파일명)",
                key="hist_search_v2",
                placeholder="예: 1234567, 신주쿠, 1K",
                label_visibility="collapsed",
            )
        with _col_fav:
            only_favorites = st.checkbox(
                "⭐ 즐겨찾기만",
                key="hist_only_favorites_v2",
                value=False,
            )

        # 필터링 (매물번호·제목·파일명)
        filtered = history
        if search_q:
            q = search_q.lower()
            filtered = [
                h for h in filtered
                if q in h.get("title", "").lower()
                or q in h.get("filename", "").lower()
                or q in h.get("property_number", "").lower()
            ]
        if only_favorites:
            filtered = [h for h in filtered if h.get("favorite")]

        if not filtered:
            st.warning(f"'{search_q}' 검색 결과 없음")
        else:
            # 결과수 캡션 + 모두선택/해제 작은 버튼 — 한 줄에
            _col_cnt, _col_sel, _col_unsel = st.columns([4, 1, 1])
            with _col_cnt:
                st.caption(f"검색 결과 **{len(filtered)}건**")
            with _col_sel:
                if st.button("☑️ 전체", key="hist_select_all", use_container_width=True):
                    for h in filtered:
                        st.session_state[f"hist_sel_{h['id']}"] = True
                    st.rerun()
            with _col_unsel:
                if st.button("⬜ 해제", key="hist_unselect_all", use_container_width=True):
                    for h in filtered:
                        st.session_state[f"hist_sel_{h['id']}"] = False
                    st.rerun()

            # 이력 목록 표시
            selected_ids = []
            for idx, h in enumerate(filtered):
                hid = h.get("id", "")
                title = h.get("title", "(제목 없음)")
                filename = h.get("filename", "")
                prop_num = h.get("property_number", "")
                timestamp = h.get("timestamp", "")
                is_fav = h.get("favorite", False)

                # 제목에 이미 [매물번호]가 포함됐는지 확인 (중복 방지)
                title_has_num = prop_num and title.startswith(f"[{prop_num}]")

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
                    # 순번 다음에 매물번호 표시 (제목에 이미 있으면 제목만)
                    if prop_num and not title_has_num:
                        display_title = f"**{idx+1}. [{prop_num}] {title}**{fav_badge}"
                    else:
                        display_title = f"**{idx+1}. {title}**{fav_badge}"
                    st.markdown(
                        f"{display_title}  \n"
                        f"<span style='color:#888;font-size:13px'>"
                        f"📁 {filename} · 🕒 {ts_display}{retention_warn}</span>",
                        unsafe_allow_html=True,
                    )

                # 📂 상세 보기 — 카톡 요약/본문 HTML/다운로드를 한 expander 안에 라디오로 통합
                summary = h.get("summary_for_chat", "")
                html = h.get("html_content", "")
                tags = h.get("hashtags", [])

                _mode_options = []
                if summary:
                    _mode_options.append("📱 카톡 요약")
                if html:
                    _mode_options.append("📄 본문 HTML")
                _mode_options.append("💾 다운로드")

                with st.expander("📂 상세 보기", expanded=False):
                    _mode = st.radio(
                        "보기 모드",
                        _mode_options,
                        horizontal=True,
                        key=f"hist_mode_{hid}",
                        label_visibility="collapsed",
                    )

                    if _mode == "📱 카톡 요약":
                        st.code(summary, language=None, wrap_lines=True)
                    elif _mode == "📄 본문 HTML":
                        if html:
                            st.markdown(html, unsafe_allow_html=True)
                        if tags:
                            st.markdown("**해시태그**")
                            st.code(" ".join(tags), language=None)
                    elif _mode == "💾 다운로드":
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

                # 얇은 회색선 (st.divider 대신 — 위아래 큰 패딩 제거)
                st.markdown(
                    '<hr style="border:none;border-top:1px solid #eee;margin:6px 0">',
                    unsafe_allow_html=True,
                )

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



