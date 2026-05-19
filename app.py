"""
app.py
─────────────────────────────────────────────────────────
JRE일본부동산 — 마이소크 → 네이버 블로그 자동 작성 시스템

특징:
- 한 번에 최대 5개 도면(마이소크) 업로드 → 블로그 5개 일괄 생성
- JPG/PNG/WEBP/GIF/PDF 지원
- 사무실 공용 비밀번호 인증 (작성자 이름 입력 없음)

실행:
- 로컬: streamlit run app.py  (.env 파일에서 설정 읽음)
- 클라우드(Streamlit Community Cloud): Secrets에서 설정 읽음
"""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import streamlit as st
from dotenv import load_dotenv

from src.analyzer import analyze_property_sheet
from src.generator import (
    VISA_LABELS,
    build_naver_smarteditor_html,
    generate_blog_post,
    list_available_styles,
)
from src.naver_publisher import NaverBlogClient

# ─────────────────────────────────────────────────
# 설정 로드 — 로컬과 클라우드 모두 지원
#  · 로컬     : .env 파일 (load_dotenv)
#  · 클라우드 : Streamlit Secrets → 환경변수로 연결
# ─────────────────────────────────────────────────
load_dotenv()  # 로컬 .env (클라우드에는 .env가 없으므로 무시됨)

try:
    # Streamlit Cloud의 Secrets를 환경변수로 복사
    # (setdefault 이므로 이미 설정된 .env 값이 우선)
    for _key in st.secrets:
        _val = st.secrets[_key]
        if isinstance(_val, str):
            os.environ.setdefault(_key, _val)
except Exception:
    # secrets.toml이 없으면(로컬 .env만 사용) 그냥 통과
    pass

MAX_UPLOADS = 5  # 한 번에 처리할 수 있는 도면 최대 개수

st.set_page_config(
    page_title="🏠 JRE일본부동산 블로그 자동작성",
    page_icon="🏠",
    layout="wide",
)


# ────────────────────────────────────────────────
# 사무실 공용 비밀번호 인증 (작성자 이름 입력 없음)
# ────────────────────────────────────────────────
def _check_office_password() -> bool:
    office_pw = os.getenv("OFFICE_PASSWORD", "").strip()
    if not office_pw:
        return True  # 비밀번호 미설정 → 인증 생략
    if st.session_state.get("authenticated"):
        return True

    st.title("🔐 JRE일본부동산 블로그 시스템")
    st.caption("사무실 비밀번호를 입력하세요.")
    with st.form("login_form"):
        password = st.text_input("사무실 비밀번호", type="password")
        submitted = st.form_submit_button("입장")
    if submitted:
        if password == office_pw:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    return False


if not _check_office_password():
    st.stop()


st.title("🏠 JRE일본부동산 — 네이버 블로그 자동작성 시스템")
st.caption(
    f"마이소크(物件図面) 최대 {MAX_UPLOADS}개 업로드 → "
    "Claude Opus 4.7 분석 → 한국어 블로그 일괄 생성 → 네이버 발행"
)

# ────────────────────────────────────────────────
# 사이드바 설정
# ────────────────────────────────────────────────
with st.sidebar:
    if os.getenv("OFFICE_PASSWORD", "").strip():
        if st.button("🚪 로그아웃", use_container_width=True):
            st.session_state.clear()
            st.rerun()
        st.divider()

    st.header("⚙️ 설정")

    target_visa = st.selectbox(
        "타깃 비자 (참고용)",
        options=list(VISA_LABELS.keys()),
        format_func=lambda k: VISA_LABELS[k],
        index=0,
        help="블로그의 톤 참고용입니다. 추천 이유는 비자별로 나누지 않고 통합 작성됩니다.",
    )

    available_styles = list_available_styles()
    style_name = st.selectbox(
        "📝 글 스타일",
        options=available_styles,
        index=0,
        help="styles/ 폴더의 .md 파일로 톤·구조를 수정할 수 있습니다.",
    )

    st.divider()
    st.subheader("AI 모델")
    model = st.selectbox(
        "모델 선택",
        ["claude-opus-4-7", "claude-sonnet-4-6"],
        help="Opus 4.7은 정확도 최고, Sonnet 4.6은 더 빠르고 저렴",
    )

# ────────────────────────────────────────────────
# 4단계 탭
# ────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs(
    ["1️⃣ 도면 업로드", "2️⃣ 추출 결과", "3️⃣ 블로그 생성·미리보기", "4️⃣ 네이버 발행"]
)

# ───── Tab 1: 업로드 (최대 5개) ─────
with tab1:
    st.subheader(f"마이소크 (物件図面) 업로드 — 한 번에 최대 {MAX_UPLOADS}개")
    st.caption("JPG · PNG · WEBP · GIF · PDF 형식 지원")

    uploaded_files = st.file_uploader(
        f"도면 파일을 최대 {MAX_UPLOADS}개까지 선택하세요 (한 번에 블로그 {MAX_UPLOADS}개 생성)",
        type=["jpg", "jpeg", "png", "webp", "gif", "pdf"],
        accept_multiple_files=True,
    )

    if uploaded_files:
        if len(uploaded_files) > MAX_UPLOADS:
            st.warning(
                f"⚠️ 최대 {MAX_UPLOADS}개까지만 처리됩니다. "
                f"앞의 {MAX_UPLOADS}개만 사용합니다."
            )
            uploaded_files = uploaded_files[:MAX_UPLOADS]

        st.write(f"**업로드된 파일: {len(uploaded_files)}개**")
        cols = st.columns(min(len(uploaded_files), MAX_UPLOADS))
        for i, uf in enumerate(uploaded_files):
            with cols[i]:
                if uf.name.lower().endswith(".pdf"):
                    st.info(f"📄 {uf.name}\n(PDF)")
                else:
                    st.image(uf, caption=uf.name, use_container_width=True)

        if st.button("🔍 전체 도면 분석 시작", type="primary"):
            properties = []
            progress = st.progress(0.0, text="분석 준비 중…")
            errors = []

            for i, uf in enumerate(uploaded_files):
                progress.progress(
                    i / len(uploaded_files),
                    text=f"[{i+1}/{len(uploaded_files)}] {uf.name} 분석 중…",
                )
                suffix = Path(uf.name).suffix
                with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(uf.getvalue())
                    tmp_path = tmp.name
                try:
                    data = analyze_property_sheet(tmp_path, model=model)
                    properties.append({"filename": uf.name, "data": data})
                except Exception as e:
                    errors.append(f"{uf.name}: {e}")
                finally:
                    os.unlink(tmp_path)

            progress.progress(1.0, text="분석 완료")

            if properties:
                st.session_state["properties"] = properties
                st.session_state.pop("blog_posts", None)  # 이전 결과 초기화
                st.success(
                    f"✅ {len(properties)}개 도면 분석 완료! 2번 탭에서 확인하세요."
                )
            if errors:
                st.error("일부 파일 분석 실패:\n" + "\n".join(errors))

# ───── Tab 2: 추출 결과 검토 ─────
with tab2:
    properties = st.session_state.get("properties")
    if not properties:
        st.info("👈 먼저 1번 탭에서 도면을 업로드하고 분석을 실행하세요.")
    else:
        st.subheader(f"📋 추출 결과 — 총 {len(properties)}개")
        st.caption("AI 추출 정보를 확인하고, 틀린 부분은 직접 수정하세요.")

        for idx, prop in enumerate(properties):
            data = prop["data"]
            station = data.get("nearest_station") or {}
            with st.expander(
                f"📄 {idx+1}. {prop['filename']}  —  "
                f"{data.get('layout', '?')} / 월세 ¥{data.get('rent_yen', 0):,}",
                expanded=(idx == 0),
            ):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**물건명**: {data.get('property_name', '?')}")
                    st.markdown(f"**주소**: {data.get('address', '?')}")
                    st.markdown(
                        f"**최기 역**: {station.get('line', '?')} "
                        f"{station.get('station', '?')}역 "
                        f"도보 {station.get('walk_minutes', '?')}분"
                    )
                with col2:
                    mgmt = data.get("management_fee_yen") or 0
                    st.metric(
                        "월세",
                        f"¥{data.get('rent_yen', 0):,}",
                        f"관리비 ¥{mgmt:,}",
                        delta_color="off",
                    )
                    st.markdown(
                        f"**평면도/면적**: {data.get('layout', '?')} "
                        f"/ {data.get('area_sqm', '?')}㎡"
                    )
                    st.markdown(
                        f"**추출 자신도**: {data.get('extraction_confidence', '?')}"
                    )

                edited = st.text_area(
                    "추출 데이터 (필요시 수정)",
                    value=json.dumps(data, ensure_ascii=False, indent=2),
                    height=260,
                    key=f"json_edit_{idx}",
                )
                try:
                    prop["data"] = json.loads(edited)
                except json.JSONDecodeError as e:
                    st.warning(f"⚠️ JSON 형식 오류: {e}")

        st.divider()
        st.markdown("### ✍️ 블로그 글 일괄 생성")
        st.caption(
            f"선택 설정 → 스타일: **{style_name}** · 모델: **{model}** · "
            f"비자(참고): **{VISA_LABELS.get(target_visa, '?')}**"
        )

        custom_instructions = st.text_area(
            "전체 글 공통 특별 지시 (선택)",
            placeholder=(
                "이번에 생성할 모든 글에 공통 적용할 지시. 예:\n"
                "• 여성 손님 대상, 안전성(오토락 등) 강조\n"
                "• 한인 마트·한국 음식점 정보 비중 늘리기"
            ),
            height=90,
        )

        if st.button(f"✍️ 블로그 {len(properties)}개 일괄 생성", type="primary"):
            blog_posts = []
            progress = st.progress(0.0, text="생성 준비 중…")
            errors = []

            for i, prop in enumerate(properties):
                progress.progress(
                    i / len(properties),
                    text=f"[{i+1}/{len(properties)}] {prop['filename']} 블로그 작성 중…",
                )
                try:
                    post = generate_blog_post(
                        property_data=prop["data"],
                        target_visa=target_visa,
                        style_name=style_name,
                        custom_instructions=custom_instructions,
                        model=model,
                    )
                    blog_posts.append({"filename": prop["filename"], "post": post})
                except Exception as e:
                    errors.append(f"{prop['filename']}: {e}")

            progress.progress(1.0, text="생성 완료")

            if blog_posts:
                st.session_state["blog_posts"] = blog_posts
                st.success(
                    f"✅ 블로그 {len(blog_posts)}개 생성 완료! 3번 탭에서 확인하세요."
                )
            if errors:
                st.error("일부 글 생성 실패:\n" + "\n".join(errors))

# ───── Tab 3: 블로그 미리보기 ─────
with tab3:
    blog_posts = st.session_state.get("blog_posts")
    if not blog_posts:
        st.info("👈 2번 탭에서 블로그 글을 생성하세요.")
    else:
        st.subheader(f"📝 생성된 블로그 — 총 {len(blog_posts)}개")

        for idx, bp in enumerate(blog_posts):
            post = bp["post"]
            with st.expander(
                f"📝 {idx+1}. {post.get('title', bp['filename'])}",
                expanded=(idx == 0),
            ):
                st.text_input(
                    "제목", value=post.get("title", ""), key=f"title_{idx}"
                )
                st.text_area(
                    "카카오톡용 요약",
                    value=post.get("summary_for_chat", ""),
                    height=70,
                    key=f"summary_{idx}",
                )

                st.markdown("**미리보기**")
                st.markdown(post.get("html_content", ""), unsafe_allow_html=True)

                st.markdown("**해시태그**")
                st.code(" ".join(post.get("hashtags", [])))

                c1, c2 = st.columns(2)
                with c1:
                    st.download_button(
                        "💾 HTML 다운로드 (SmartEditor용)",
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

# ───── Tab 4: 네이버 발행 ─────
with tab4:
    st.subheader("📤 네이버 블로그 발행")
    blog_posts = st.session_state.get("blog_posts")
    if not blog_posts:
        st.info("먼저 블로그 글을 생성하세요.")
        st.stop()

    st.warning(
        "⚠️ 사전 준비: 네이버 개발자센터 앱 등록 + '네이버 아이디로 로그인' 심사 통과 "
        "+ .env에 NAVER_CLIENT_ID/SECRET 설정"
    )

    if not (os.getenv("NAVER_CLIENT_ID") and os.getenv("NAVER_CLIENT_SECRET")):
        st.error("네이버 환경변수가 설정되지 않았습니다.")
        st.info("💡 3번 탭에서 HTML을 다운로드해 네이버 블로그 글쓰기에 붙여넣으면 즉시 발행 가능합니다.")
        st.stop()

    try:
        client = NaverBlogClient()
    except ValueError as e:
        st.error(str(e))
        st.stop()

    if not client.is_authenticated:
        st.markdown(
            f"### 🔐 네이버 인증 필요\n"
            f"1. [이 URL]({client.get_auth_url()})에서 네이버 로그인\n"
            f"2. 리다이렉트 URL의 `?code=` 뒤 코드 복사\n"
            f"3. 아래에 붙여넣기"
        )
        code = st.text_input("Authorization Code")
        if code and st.button("토큰 발급"):
            try:
                client.exchange_code_for_token(code)
                st.success("✅ 인증 완료!")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 인증 실패: {e}")
        st.stop()

    st.markdown("발행할 글을 선택하세요.")
    for idx, bp in enumerate(blog_posts):
        post = bp["post"]
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"**{idx+1}. {post.get('title', '')}**")
        with c2:
            if st.button("🚀 발행", key=f"publish_{idx}"):
                with st.spinner("네이버에 발행 중…"):
                    try:
                        result = client.write_post(
                            title=post.get("title", ""),
                            contents=post.get("html_content", ""),
                            tags=post.get("hashtags", []),
                            is_open=2,
                        )
                        st.success(f"✅ {idx+1}번 글 발행 완료!")
                    except Exception as e:
                        st.error(f"❌ 발행 실패: {e}")
