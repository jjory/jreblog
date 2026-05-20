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
2. **첫 200자가 SEO 핵심**: 지역·역명·방구조·월세가 첫 문단에 모두 들어가야 함
3. **월세 표기 규칙**:
   - 원화(₩) 환산은 표시하지 말 것. 엔화(¥)로만 표기
   - 월세와 관리비는 항상 함께 표기. 예: "월세 ¥88,000 + 관리비 ¥5,000"
4. **면적 표기 규칙**: 제곱미터(㎡)로만 표기. "약 ○평" 같은 평수 환산은 절대 쓰지 말 것
5. **"신축" 단어 사용 규칙**: 건축 후 5년 이내인 경우에만 "신축" 표현 사용 가능.
   5년을 초과하면 "신축", "신축급" 등의 표현 절대 금지 (대신 "○년 건축" 등 사실 표기)
6. **전철 노선명은 일본어 한글 음 표기**로 통일:
   - 올바른 예: JR 야마노테선, 후쿠토신선, 마루노우치선, 긴자선, 한조몬선,
     도자이선, 유라쿠초선, 난보쿠선, 오에도선, 케이오선, 오다큐선, 도큐선,
     세이부선, 도부선, 츠쿠바익스프레스
   - 잘못된 예: 부도심선(X→후쿠토신선), 동서선(X→도자이선)
   - "최기 역"·"최기역" 등 일본어 한자음 표기 금지 → 반드시 **"가장 가까운 역"** 사용
7. **스타일 가이드** — 다음 스타일 지시를 따라 톤·구조를 결정하세요:
{style_instructions}
8. **상담 신청 섹션은 스타일 가이드와 무관하게 항상 신뢰형 톤 고정** (자세한 규칙은 아래 참고)
9. 절대 사실을 지어내지 말 것. 데이터에 없는 정보는 정확히 **"(현지 확인 필요)"** 라고 표기
   - 이 표시는 시스템에서 자동으로 빨간색으로 강조됩니다. 다른 표현으로 바꾸지 마세요
10. **초기비용(敷金·礼金·중개수수료 등) 항목 본문 미게재.**
11. **본문 금지 항목** — 다음은 절대 본문에 포함하지 마세요:
   - 매물명·건물명·물건명
   - 주소·소재지·번지수
   - 계약기간·계약 형태
   - 입주조건·외국인 입주 가능 여부 (보증회사 가입 가능 여부 포함)
12. **용어 통일 — 절대 다음 단어를 사용하지 마세요**:
   - "평면도" 금지 → **"방구조"** 사용
   - "구조" (단독 사용) 금지 → **"건물구조"** 사용
   - "준공년도", "준공", "준공일" 금지 → **"건축년도"** 사용
   - "최기 역", "최기역" 금지 → **"가장 가까운 역"** 사용

# 이번 글 특별 지시사항
{custom_instructions}

# 매물 데이터
{property_json}

# 출력 형식 (반드시 아래 JSON 구조로만 출력, 코드 블록 없이, 모든 키 포함)
{{
  "title": "네이버 블로그 SEO 최적화 제목 (35자 이내, 지역·역·방구조·월세 포함, 단 매물명·주소는 제외)",
  "meta_description": "검색 노출용 첫 문단 (150자 이내)",
  "html_content": "SmartEditor 호환 HTML 본문 (h2/h3/p/ul/li/table/tr/td/strong 태그만 사용. div/class 금지. table에는 inline style 필수)",
  "hashtags": ["#태그1", "#태그2", ...],
  "summary_for_chat": "카카오톡용 요약. 항목별 이모지+줄바꿈 형식 (서술형 금지). 형식은 아래 [카카오톡 요약 규칙] 참고"
}}

# html_content 구성 가이드 (이 순서 그대로, 각 섹션 사이 빈 줄)

⭐ **섹션 헤더는 반드시 다음 이모지 그대로 사용** (스타일 가이드와 무관하게 고정):
   - <h2>📋 매물 기본정보</h2>
   - <h2>🏠 방 구조와 설비</h2>
   - <h2>📍 위치와 생활 인프라</h2>
   - <h2>✨ 추천 이유</h2>
   - <h2>💬 상담 신청</h2>

1. **인사말 + 한 줄 매물 소개** (매물명·주소 언급 금지)

2. **<h2>📋 매물 기본정보</h2>**
   ⚠️ **헤더 바로 다음에 표만 작성. 표 앞에 어떤 설명 문단도 절대 넣지 말 것**
   (스타일에 따라 "이 매물은 ...입니다" 같은 인트로 문장 추가 금지)

   반드시 다음 8개 항목을 표로 (이 순서 그대로, 추가·삭제 금지):

   | 항목 | 표기 예시 |
   |------|-----------|
   | 가장 가까운 역 | (예) JR 야마노테선 신오쿠보역 도보 5분 |
   | 방구조 | 1K |
   | 전용면적 | 23.5㎡ |
   | 월세/관리비 | ¥88,000 + 관리비 ¥5,000 |
   | 건물구조 | RC조 |
   | 방향 | 남향 |
   | 건축년도 | 2018년 |
   | 입주가능일 | 2026년 6월 1일 |

   ⭐ **표 HTML 형식 — 네이버 SmartEditor에서 표가 사라지지 않도록 다음 inline style 필수**:

   ```html
   <table style="border-collapse:collapse;width:100%;border:1px solid #ddd">
     <tr>
       <td style="border:1px solid #ddd;padding:8px 12px;background:#f5f5f5;width:30%;font-weight:bold">가장 가까운 역</td>
       <td style="border:1px solid #ddd;padding:8px 12px">JR 야마노테선 신오쿠보역 도보 5분</td>
     </tr>
     <tr>
       <td style="border:1px solid #ddd;padding:8px 12px;background:#f5f5f5;font-weight:bold">방구조</td>
       <td style="border:1px solid #ddd;padding:8px 12px">1K</td>
     </tr>
     ...8개 행 모두...
   </table>
   ```

   - 데이터에 없는 항목은 셀 값에 "(현지 확인 필요)"로 표기
   - 매물명·주소·계약기간은 이 표에 절대 넣지 마세요

3. **<h2>🏠 방 구조와 설비</h2>** — 방 구성, 에어컨·욕실·세탁기 등 설비

4. **<h2>📍 위치와 생활 인프라</h2>** — 교통, 주변 편의시설, 가능하면 한인 마트·한국 음식점

5. **<h2>✨ 추천 이유</h2>**
   - 비자 종류별로 나누지 말고 매물 장점을 통합해서 작성
   - 누구에게나 와닿는 알기 쉬운 장점 3~5개, 중복 없이
   - 예: 역세권, 채광, 한인타운 접근성, 욕실분리 등
   - 매물 데이터에서 실제 확인되는 강점만 사용
   - "신축"은 건축 5년 이내일 때만 (작성 원칙 5번 준수)

6. **<h2>💬 상담 신청</h2>** — 아래 [상담 신청 작성 규칙]을 그대로 반영

# [상담 신청 작성 규칙] — ⭐ 스타일 가이드 무관, 항상 다음 톤 고정
**톤**: 믿고 맡길 수 있는 신뢰감 있는 전문가형
- 합쇼체 (~합니다, ~입니다) 유지, 정중하고 자신감 있게
- 감탄사·과장·구어체 표현 자제 ("정말 좋아요!" X)
- 회사의 전문성과 한국어 서포트 역량을 차분하게 어필
- 이모지는 최소화 (있어도 1~2개)

**내용** (자연스러운 문단으로 녹여서, 항목 나열 X):
- JRE일본부동산은 도쿄 신주쿠에 위치한 외국인 전문 부동산입니다.
  도쿄·수도권의 임대·매매 중개와 부동산 관리를 전문으로 합니다.
- 모든 상담과 서포트가 한국어로 가능하며, 일본 입국 전 한국에서 미리
  매물 상담과 계약 절차를 진행할 수 있습니다.
- 유학생·워홀러·직장인·법인 등 다양한 고객층에 맞춰 희망 조건에 부합하는
  매물을 책임감 있게 제안해 드립니다.
- 입주 후에도 **중고가전 렌탈 연계**와 **수도·전기·가스 등 라이프라인 연결**까지
  세심하게 서포트하여, 일본 정착의 모든 단계를 함께합니다.
- 카카오톡 상담 안내: 카카오톡 ID `{kakao}`, 전화 `{phone}`

# 해시태그 규칙
- 다음 필수 태그를 모두 포함하고, 추가로 이 매물 특성(지역명·역명·방구조 등)
  관련 태그를 5개 이내로 더하세요. 전체 20개 이내.
- 필수 태그: {core_hashtags}

# [카카오톡 요약 (summary_for_chat) 규칙] — ⭐ 줄글 금지, 항목별 이모지 형식 고정

서술형(줄글)로 쓰지 말고, **다음과 같이 항목별 이모지 + 한 줄씩 작성**해서 카카오톡에서
한눈에 읽기 쉽게 만드세요. 줄바꿈은 \\n 사용.

**필수 형식** (이 구조 그대로):

```
🚉 [노선명] [역명]역 도보 [N]분
🏠 [방구조] / [면적]㎡
💴 월세 ¥[금액] + 관리비 ¥[금액]
🗓 [건축년도] / [방향]

✨ 추천 포인트
• [장점 1]
• [장점 2]
• [장점 3]

```

**작성 시 주의**:
- 데이터에 없는 항목 줄은 통째로 생략 (예: 방향 미상이면 🗓 줄에서 방향 부분 생략)
- 추천 포인트는 2~4개, 매물의 실제 강점만 (광고 문구 X)
- 매물명·주소·계약기간은 절대 포함하지 말 것
- 첫 줄에 인사말이나 광고성 문구 넣지 말 것 (바로 정보부터 시작)
- 마지막 줄은 카카오톡 ID 안내로 고정

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


def _highlight_check_needed(html: str) -> str:
    """
    '(현지 확인 필요)' 표시를 빨간색 강조로 변환.
    사장님이 수동으로 채워야 할 부분이 한눈에 보이도록 시각적으로 처리.
    """
    # 다양한 띄어쓰기·표기 패턴 모두 처리
    pattern = re.compile(r"\(\s*현지\s*확인\s*필요\s*\)")
    replacement = (
        '<strong style="color:#d32f2f;background:#fff3cd;padding:2px 6px;'
        'border-radius:3px;border:1px solid #ffc107">'
        '⚠️ 현지 확인 필요</strong>'
    )
    return pattern.sub(replacement, html)


def _ensure_table_styles(html: str) -> str:
    """
    표(table/tr/td)에 inline style이 빠져 있으면 자동으로 채워 넣음.
    네이버 SmartEditor가 style 없는 단순 table을 무시·축소하는 문제 방지.
    AI가 가끔 빈 <table>을 만들 때 대비한 안전망.
    """
    # <table>에 style이 없으면 기본 style 주입
    def _inject_table(m):
        attrs = m.group(1) or ""
        if "style=" in attrs:
            return m.group(0)
        return (
            '<table style="border-collapse:collapse;width:100%;'
            'border:1px solid #ddd;margin:10px 0"' + attrs + '>'
        )
    html = re.sub(r"<table([^>]*)>", _inject_table, html)

    # <td>에 style이 없으면 기본 style 주입 (좌측 셀과 우측 셀 구분은 어렵지만
    # 일단 border와 padding이라도 보장)
    def _inject_td(m):
        attrs = m.group(1) or ""
        if "style=" in attrs:
            return m.group(0)
        return '<td style="border:1px solid #ddd;padding:8px 12px"' + attrs + '>'
    html = re.sub(r"<td([^>]*)>", _inject_td, html)

    # <th>도 동일 처리
    def _inject_th(m):
        attrs = m.group(1) or ""
        if "style=" in attrs:
            return m.group(0)
        return (
            '<th style="border:1px solid #ddd;padding:8px 12px;'
            'background:#f5f5f5;font-weight:bold;text-align:left"' + attrs + '>'
        )
    html = re.sub(r"<th([^>]*)>", _inject_th, html)

    return html


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
        kakao=os.getenv("KAKAO_TALK_ID", "japanreal"),
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

            # "(현지 확인 필요)" 표시를 빨간색으로 강조 (수동 편집 필요한 부분 시각화)
            if result.get("html_content"):
                result["html_content"] = _ensure_table_styles(result["html_content"])
                result["html_content"] = _highlight_check_needed(result["html_content"])

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
