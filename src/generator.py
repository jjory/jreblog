"""
generator.py
─────────────────────────────────────────────────────────
구조화된 부동산 JSON → 한국 네이버 블로그 글 자동 생성.

핵심 특징:
1. 일본 부동산 용어를 한국인 관점에서 재해석
2. 추천 이유는 비자별로 나누지 않고 통합하여 알기 쉽게
3. 네이버 인기 검색어 기반 해시태그
4. 상담 신청에 JRE일본부동산 강점 + 입주 후 서포트 안내
5. 블로그 생성 실패 방지를 위한 재시도·검증 로직
"""

import os
import json
import re
import time
from pathlib import Path
from typing import Optional, Literal

import anthropic

DEFAULT_MODEL = "claude-opus-4-7"

# 스타일 프리셋 시스템
STYLES_DIR = Path(__file__).parent.parent / "styles"
DEFAULT_STYLE = "친근형"

VisaType = Literal[
    "all", "business_manager", "work", "student", "working_holiday", "permanent"
]

VISA_LABELS = {
    "all": "전체 (특정 비자 강조 안 함)",
    "business_manager": "경영관리 비자",
    "work": "취업 비자",
    "student": "유학 비자",
    "working_holiday": "워킹홀리데이",
    "permanent": "영주권자",
}

# ─────────────────────────────────────────────────
# 네이버 일본 부동산 인기 검색어 기반 필수 해시태그
# (검색 노출을 위해 모든 글에 기본 포함)
# ─────────────────────────────────────────────────
CORE_HASHTAGS = [
    "#일본부동산", "#도쿄부동산", "#도쿄월세", "#도쿄원룸",
    "#일본집구하기", "#도쿄집구하기", "#일본워홀", "#일본유학",
    "#일본취업", "#일본생활", "#일본워킹홀리데이", "#도쿄맨션",
    "#일본한인부동산", "#도쿄살이",
]


def list_available_styles() -> list[str]:
    """styles/ 폴더에서 사용 가능한 스타일 목록 반환."""
    if not STYLES_DIR.exists():
        return [DEFAULT_STYLE]
    styles = [
        f.stem for f in STYLES_DIR.glob("*.md")
        if f.name != "README.md" and not f.name.startswith("_")
    ]
    if not styles:
        return [DEFAULT_STYLE]
    styles.sort(key=lambda s: (s != DEFAULT_STYLE, s))
    return styles


def load_style(style_name: str) -> str:
    """스타일 파일 내용 로드."""
    style_path = STYLES_DIR / f"{style_name}.md"
    if not style_path.exists():
        return (
            "- 친근하고 따뜻한 톤으로 작성\n"
            "- 한국 손님이 부담 없이 읽을 수 있는 자연스러운 한국어"
        )
    return style_path.read_text(encoding="utf-8").strip()


# ─────────────────────────────────────────────────
# 블로그 생성 프롬프트
# ─────────────────────────────────────────────────
BLOG_GENERATION_PROMPT = """\
당신은 도쿄 신주쿠에 위치한 외국인 전문 부동산 'JRE일본부동산'의 한국인 모객 전문 마케터입니다.
일본에 거주하려는 한국인을 대상으로, 아래 일본 부동산 매물 데이터를 가지고
**네이버 블로그**에 게시할 글을 작성하세요.

# 작성 원칙 (매우 중요)
1. **단순 번역 금지 — 재해석**: 일본 특유의 부동산 제도·용어는 한국인이 이해할 수 있게 풀어서 설명
2. **첫 200자가 SEO 핵심**: 지역·역명·평면도·월세가 첫 문단에 모두 들어가야 함
3. **월세 표기 규칙**:
   - 원화(₩) 환산은 표시하지 말 것. 엔화(¥)로만 표기
   - 월세와 관리비는 항상 함께 표기. 예: "월세 ¥88,000 + 관리비 ¥5,000"
4. **면적 표기 규칙**: 제곱미터(㎡)로만 표기. "약 ○평" 같은 평수 환산은 절대 쓰지 말 것
5. **"신축" 단어 사용 규칙**: 건축 후 5년 이내인 경우에만 "신축"이라는 단어 사용 가능.
   5년을 초과하면 "신축", "신축급" 등의 표현을 절대 쓰지 말 것
   (대신 "비교적 최근 준공", "○년 준공" 등 사실 위주로 표기)
6. **전철 노선명은 일본어 한글 음 표기**로 통일:
   - 일본어 발음을 한글로 적되, 한국식 의역은 금지
   - 올바른 예: JR 야마노테선, 후쿠토신선, 마루노우치선, 긴자선, 한조몬선,
     도자이선, 유라쿠초선, 난보쿠선, 오에도선, 케이오선, 오다큐선, 도큐선,
     세이부선, 도부선, 츠쿠바익스프레스
   - 잘못된 예: 부도심선(X→후쿠토신선), 동서선(X→도자이선), 신주쿠선(주의: 도에이신주쿠선)
   - 데이터의 노선명이 한국식 의역이면 일본어 음으로 교정해서 표기
7. **외국인 입주 가능 여부**를 명확히 (보증회사, 외국인 가능 여부)
8. **스타일 가이드** — 다음 스타일 지시를 따라 톤·구조를 결정하세요:
{style_instructions}
9. 절대 사실을 지어내지 말 것. 데이터에 없는 정보는 "(현지 확인 필요)" 표기
10. **초기비용(敷金·礼金·중개수수료 등) 항목은 본문에 넣지 마세요.** 금액 부담 관련 표나
   계산은 작성하지 않습니다. (상담 시 별도 안내하므로 블로그에는 미게재)

# 이번 글 특별 지시사항
{custom_instructions}

# 매물 데이터
{property_json}

# 출력 형식 (반드시 아래 JSON 구조로만 출력, 코드 블록 없이, 모든 키 포함)
{{
  "title": "네이버 블로그용 SEO 최적화 제목 (35자 이내, 지역·역·평면도·월세 포함)",
  "meta_description": "검색 노출용 첫 문단 (150자 이내)",
  "html_content": "SmartEditor 호환 HTML 본문 (h2/h3/p/ul/li/table/strong 태그만 사용. div/style/class 금지)",
  "hashtags": ["#태그1", "#태그2", ...],
  "summary_for_chat": "카카오톡으로 손님에게 바로 보낼 수 있는 3줄 요약"
}}

# html_content 구성 가이드 (이 순서대로, 각 섹션 사이 빈 줄)
1. 인사말 + 한 줄 매물 소개
2. <h2>매물 기본정보</h2> — 위치·역(일본어 음 표기 노선명)·평면도·면적(㎡만)·
   월세(관리비 함께)·구조·방향 등을 표로
3. <h2>방 구조와 설비</h2> — 방 구성, 에어컨·욕실·세탁기 등 설비
4. <h2>위치와 생활 인프라</h2> — 교통, 주변 편의시설, 가능하면 한인 마트·한국 음식점
5. <h2>추천 이유</h2> — 아래 [추천 이유 작성 규칙] 참고
6. <h2>상담 신청</h2> — 아래 [상담 신청 작성 규칙]을 그대로 반영

# [추천 이유 작성 규칙]
- 제목은 반드시 "추천 이유" (특정 비자 이름을 제목에 넣지 말 것)
- 비자 종류별로 문단을 나누지 말고, **이 매물의 장점을 하나로 통합**해서 작성
- 누구에게나 와닿는, 알기 쉬운 장점만 3~5개 골라 간결하게 (중복 표현 금지)
- 예: 역세권, 채광, 한인타운 접근성, 욕실분리, 외국인 입주 가능 등
  매물 데이터에서 실제로 확인되는 강점만 사용
- "신축"은 건축 5년 이내일 때만 사용 가능 (작성 원칙 5번 준수)

# [상담 신청 작성 규칙]
이 섹션에는 다음 내용을 자연스럽게 녹여서 작성하세요 (항목 나열이 아닌 문단으로):
- JRE일본부동산은 도쿄 신주쿠에 위치한 외국인 전문 부동산으로, 도쿄·수도권의
  임대와 매매 중개 및 부동산 관리를 전문으로 합니다.
- 한국어로 상담·서포트가 가능하며, 한국에서 일본 입국 전에 미리 상담·계약을 진행할 수 있습니다.
- 유학생·워홀러·직장인·법인 등 다양한 형태의 고객에게 희망 조건에 맞춘 매물을 제안합니다.
- 입주 후에도 **중고가전 렌탈 연계**와 **수도·전기·가스 등 라이프라인(생활 인프라) 연결**까지
  서포트하여, 일본에서의 새 출발을 끝까지 돕습니다.
- 마지막에 카카오톡 상담 안내: 카카오톡 ID `{kakao}`, 전화 `{phone}`

# 해시태그 규칙
- hashtags 배열에는 아래 필수 태그를 모두 포함하고, 추가로 이 매물 특성(지역명·역명·평면도 등)
  관련 태그를 5개 이내로 더하세요. 전체 20개 이내.
- 필수 태그: {core_hashtags}

스타일 가이드와 충돌하는 경우 스타일 가이드를 우선하되, 위 섹션 구성과 규칙은 반드시 지키세요.
각 섹션은 정보 위주로 간결하게. 광고 문구만 채우지 말 것.
"""


def _extract_blog_json(raw_text: str) -> dict:
    """Claude 응답에서 블로그 JSON을 안전하게 추출."""
    text = raw_text.strip()
    if text.startswith("```"):
        # 첫 줄(```json) 제거
        parts = text.split("\n", 1)
        text = parts[1] if len(parts) > 1 else text
        if text.rstrip().endswith("```"):
            text = text.rsplit("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSON 구조를 찾을 수 없습니다.")
    return json.loads(text[start : end + 1])


def _validate_blog(result: dict) -> None:
    """블로그 결과에 필수 키가 모두 있는지 검증. 없으면 ValueError."""
    required = ["title", "html_content", "hashtags"]
    for key in required:
        if key not in result or not result[key]:
            raise ValueError(f"필수 항목 누락: {key}")
    if not isinstance(result["hashtags"], list):
        raise ValueError("hashtags 형식 오류")


def generate_blog_post(
    property_data: dict,
    target_visa: VisaType = "all",
    style_name: str = DEFAULT_STYLE,
    custom_instructions: str = "",
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    max_retries: int = 3,
) -> dict:
    """
    부동산 데이터 → 네이버 블로그 글 생성. (실패 방지 재시도 로직 포함)

    Args:
        property_data: analyzer.analyze_property_sheet() 결과
        target_visa: 타깃 비자 (참고용 컨텍스트, 섹션은 통합됨)
        style_name: 사용할 스타일
        custom_instructions: 이번 글에만 적용할 특별 지시
        model: Claude 모델
        api_key: API 키
        max_retries: 실패 시 재시도 횟수

    Returns:
        {title, meta_description, html_content, hashtags, summary_for_chat}
    """
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    style_instructions = load_style(style_name)

    # "신축" 표현 허용 여부 계산 (건축 5년 이내만 허용)
    prop = dict(property_data)  # 원본 보존
    age = prop.get("building_age_years")
    year = prop.get("construction_year")
    is_new = None
    if isinstance(age, (int, float)):
        is_new = age <= 5
    elif isinstance(year, (int, float)):
        from datetime import datetime
        is_new = (datetime.now().year - int(year)) <= 5
    if is_new is True:
        prop["_new_building_allowed"] = "이 매물은 건축 5년 이내이므로 '신축' 표현 사용 가능"
    elif is_new is False:
        prop["_new_building_allowed"] = "이 매물은 건축 5년 초과이므로 '신축' 표현 절대 금지"
    else:
        prop["_new_building_allowed"] = "건축 연수 불명 — '신축' 표현 사용하지 말 것"

    custom_text = (custom_instructions or "").strip()
    visa_hint = ""
    if target_visa and target_visa != "all":
        visa_hint = (
            f"\n(참고: 이 매물은 {VISA_LABELS.get(target_visa, '')} 손님이 주요 타깃입니다. "
            "단, 추천 이유 섹션은 비자별로 나누지 말고 통합해서 작성하세요.)"
        )
    if custom_text or visa_hint:
        custom_block = (custom_text + visa_hint).strip()
    else:
        custom_block = "(별도 지시사항 없음)"

    prompt = BLOG_GENERATION_PROMPT.format(
        style_instructions=style_instructions,
        custom_instructions=custom_block,
        property_json=json.dumps(prop, ensure_ascii=False, indent=2),
        kakao=os.getenv("KAKAO_TALK_ID", "japanreal2"),
        phone=os.getenv("COMPANY_PHONE", "070-8201-5740"),
        core_hashtags=" ".join(CORE_HASHTAGS),
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text
            result = _extract_blog_json(raw_text)
            _validate_blog(result)

            # 필수 해시태그가 빠졌으면 보강 (중복 제거)
            tags = result.get("hashtags", [])
            tag_set = {t.lower() for t in tags}
            for core in CORE_HASHTAGS:
                if core.lower() not in tag_set:
                    tags.append(core)
                    tag_set.add(core.lower())
            result["hashtags"] = tags[:20]

            # 누락 가능 키 기본값 채우기
            result.setdefault("meta_description", "")
            result.setdefault("summary_for_chat", "")

            result["_meta"] = {
                "target_visa": target_visa,
                "style_name": style_name,
                "custom_instructions": custom_text,
            }
            return result

        except (json.JSONDecodeError, ValueError, KeyError, IndexError) as e:
            # 파싱·검증 실패 → 재시도
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1.5)
                continue
        except anthropic.APIStatusError as e:
            last_error = e
            if e.status_code in (429, 500, 502, 503, 529) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except anthropic.APIError as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise

    raise RuntimeError(
        f"블로그 글 생성 실패 (재시도 {max_retries}회 초과): {last_error}"
    )


def build_naver_smarteditor_html(blog_post: dict) -> str:
    """네이버 블로그 SmartEditor에 그대로 붙여넣을 수 있는 HTML 생성."""
    title = blog_post.get("title", "")
    body = blog_post.get("html_content", "")
    hashtags = " ".join(blog_post.get("hashtags", []))

    return f"""<!-- 네이버 블로그 글쓰기 → SmartEditor에 그대로 붙여넣기 -->
<!-- 제목 (별도 입력): {title} -->

{body}

<p><br/></p>
<p>{hashtags}</p>
"""
