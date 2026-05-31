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
from typing import Optional, Literal, Callable
from urllib.parse import quote

import anthropic

# 일본어→한국어 번역 사전 (사내 Google Sheets 路線 시트 기준)
try:
    from src.translation_db import (
        translate_line,
        translate_station,
        translate_ward,
        detect_untranslated,
    )
except ImportError:
    # 단독 실행/테스트 시 폴백
    from translation_db import (
        translate_line,
        translate_station,
        translate_ward,
        detect_untranslated,
    )

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
3. **월세 표기 규칙** ⭐ 매우 중요:
   - 원화(₩) 환산은 표시하지 말 것. 엔화(¥)로만 표기
   - **월세는 반드시 관리비(공익비)와 함께 표기.** 월세만 단독 표기 절대 금지
   - 예: "월세 ¥88,000 + 관리비 ¥5,000"
   - 단, 관리비가 0이거나 없는 경우에만 관리비 생략 가능 (이때만 월세 단독 표기 허용)
   - 본문 어디서든 월세를 언급할 때 이 규칙 적용 (제목 제외)
4. **면적 표기 규칙**: 제곱미터(㎡)로만 표기. "약 ○평" 같은 평수 환산은 절대 쓰지 말 것
5. **"신축" 단어 사용 규칙**: 건축 후 5년 이내인 경우에만 "신축" 표현 사용 가능.
   5년을 초과하면 "신축", "신축급" 등의 표현 절대 금지 (대신 "○년 건축" 등 사실 표기)
6. **전철 노선명은 일본어 한글 음 표기**로 통일:
   - 올바른 예: JR 야마노테선, 후쿠토신선, 마루노우치선, 긴자선, 한조몬선,
     도자이선, 유라쿠초선, 난보쿠선, 오에도선, 케이오선, 오다큐선, 도큐선,
     세이부선, 도부선, 츠쿠바익스프레스
   - 잘못된 예: 부도심선(X→후쿠토신선), 동서선(X→도자이선)
   - "최기 역"·"최기역" 등 일본어 한자음 표기 금지 → 반드시 **"가장 가까운 역"** 사용

# ⭐⭐ 절대 지켜야 할 핵심 규칙 (사실 기반 + 네거티브 금지) ⭐⭐

【A. 확인되지 않은 사실·추측 절대 금지】
- 매물 데이터에 명시되지 않은 내용을 추측하거나 지어내지 말 것
- 금지 예시:
  - "냄새·습기 걱정이 적은 게 장점" → 확인 불가한 추측. 절대 금지
  - "분양형 맨션이라 건물 퀄리티가 일반 임대용보다 좋은 편" → 근거 없는 추측. 절대 금지
  - "한식 요리도 충분히 가능" → 특정 요리 가능 여부 단정 금지
- 데이터로 확인되는 객관적 사실만 기재. 확인 안 되면 아예 언급하지 말 것

【B. 네거티브·부정적 표현 절대 금지】
- 단점·우려·불편을 암시하는 표현 금지. 사실만 중립적으로 기재
- 금지 예시:
  - "냄새·습기 걱정이 적다" → '걱정'이라는 부정 프레임 자체 금지
  - "짐 옮기실 때는 참고해 주세요" → 불편 암시. 금지
  - "5층까지 계단 이용이 필요한 점은 미리 알아두시면 좋겠습니다" → 부정 뉘앙스. 금지
- 팩트는 담담하게: "엘리베이터 없음 (5층)" 처럼 사실만, 부정적 부연 설명 없이

【C. 엘리베이터 표기 규칙】 ⭐ 매우 중요
- 도면(매물 데이터)에 "엘리베이터 없음"이 **명시된 경우에만** 없다고 기재
  → 이때도 "엘리베이터 없음 (○층)" 처럼 팩트만. 부정적 부연 금지
- 데이터에 엘리베이터 정보가 없거나 불명확하면 → **"엘리베이터 (현지 확인 필요)"** 로 표기
  (도면 미기재여도 실제로는 있는 경우가 많으므로 함부로 '없음' 단정 금지)

【D. 위치·생활 인프라 — 요충지 중심】 ⭐ 매우 중요
- 한인타운·한국 슈퍼·한식재료점 등 '한국 관련 가게'는 중요하지 않음. 강조하지 말 것
- 대신 **신주쿠·시부야·이케부쿠로·닛포리 등 주요 요충지까지의 소요 시간**을 중심으로 기재
  → 예: "신주쿠까지 전철 12분, 이케부쿠로까지 8분" 처럼 핵심 거점 접근성 강조
- 데이터에 소요 시간이 없으면 추측하지 말고 "(현지 확인 필요)" 또는 생략

【E. 용어 통일 — 다음 표현만 사용】 ⭐ 매우 중요
- 비데 관련: **"비데"** 로만 표기.
  → "워시렛", "온수세정 변기", "온수세정변기", "일본식 비데" 등 다른 표현 절대 금지
- 욕실·화장실 분리: **"욕실·화장실 분리형"** 으로만 표기.
  → "세퍼레이트 타입", "바스토일레 별도", "バス・トイレ別", "セパレート" 등
     일본어·외래어·일본어 발음 한글표기 절대 금지
- 주방/요리: 특정 요리(한식 등)가 가능하다고 단정하지 말 것.
  → "요리하기 편한 구조", "조리 공간이 넉넉한 편" 처럼 구조적 특징으로 표현
- 내진: **"내진 설계가 되어 있음"** 으로 표기.
  → "내진성이 좋다", "지진에 강하다" 같은 성능 단정 표현 금지
- 지명·고유명사: 일본어 한자음과 한국 한자음을 섞지 말 것.
  → 잘못된 예: "로카항춘원(芦花恒春園)" (한국 한자음 섞임)
  → 올바른 예: "로카코엔(芦花恒春園)" 처럼 일본어 발음으로 통일
- 일본어 원문 병기 금지: 번역이 있으면 한국어만 사용.
  → 잘못된 예: "'バス・トイレ別' 구조", "'바스토일레 별도' 타입"
  → 올바른 예: "욕실·화장실 분리형"

【F. 일본어→한국어 번역 누락 표시】 ⭐ DB 관리용
- 노선명·역명·지명 중 한국어로 번역하기 어려운(자신 없는) 일본어가 있으면
  해당 부분을 **"○○(번역확인필요)"** 형식으로 표기
  → 예: 川口元郷를 어떻게 읽는지 불확실하면 "카와구치모토고(번역확인필요)"
  → 이렇게 표시된 항목은 회사 DB에 추가 등록이 필요하다는 신호
- 확실히 아는 일본어는 정상 번역, 불확실한 것만 이 표시 사용

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
   - "워시렛", "온수세정변기" 금지 → **"비데"** 사용
   - "세퍼레이트", "바스토일레" 금지 → **"욕실·화장실 분리형"** 사용

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

# ⭐⭐ HTML 단락 분리 규칙 (가독성의 핵심) ⭐⭐
**한 <p> 태그 안에 한 문장만 작성하세요.** 한 단락에 여러 문장 절대 금지.
각 문장 끝에 </p>로 닫고, 다음 문장은 새로운 <p>로 시작하세요.
의미 그룹(예: 매물 소개 → 위치 설명) 사이에는 <p><br/></p>로 빈 줄 한 번 더 삽입.

✅ 좋은 예시 (반드시 이렇게):
<p>오늘 소개해 드릴 매물은 시나가와구에 있는 1K 원룸이에요.</p>
<p>도에이 아사쿠사선 타카나와다이역에서 도보 4분 거리입니다.</p>
<p>월세 ¥115,000 + 관리비 ¥5,000으로 시나가와·고탄다가 가까워요 😊</p>
<p><br/></p>
<p>시나가와는 신칸센과 공항 리무진까지 닿는 교통 허브이고, 고탄다는 야마노테선이 지나갑니다.</p>
<p>타카나와다이역에서 아사쿠사선을 타면 신바시·니혼바시도 환승 없이 연결돼요.</p>

❌ 절대 금지 (한 <p>에 여러 문장 묶기):
<p>오늘 소개해 드릴 매물은 시나가와구에 있는 1K 원룸이에요. 도에이 아사쿠사선 타카나와다이역에서 도보 4분, 월세 ¥115,000 + 관리비 ¥5,000으로 시나가와·고탄다가 가까운 위치랍니다 😊</p>

⭐ **섹션 헤더는 반드시 다음 이모지 그대로 사용** (스타일 가이드와 무관하게 고정):
   - <h2>📋 매물 기본정보</h2>
   - <h2>🏠 방 구조와 설비</h2>
   - <h2>📍 위치와 생활 인프라</h2>
   - <h2>✨ 추천 이유</h2>
   - <h2>💬 상담 신청</h2>

1. **한 줄 매물 소개만** (⚠️ 인사말 절대 금지)
   - "안녕하세요", "JRE일본부동산입니다" 같은 **인사말은 절대 쓰지 마세요.**
     인사말은 시스템이 본문 맨 위에 자동으로 넣습니다. AI가 또 쓰면 중복됩니다.
   - 곧바로 오늘 소개할 매물을 2~3줄로 자연스럽게 소개 (지역·역·방구조·월세+관리비 포함)
   - 매물명·주소는 언급 금지

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

3. **<h2>🏠 방 구조와 설비</h2>**
   - 먼저 방 구성을 2~3줄로 설명 (전용면적·방 개수·주방 형태 등 **데이터로 확인되는 것만**)
   - 그 다음 "설비도 꼼꼼하게 갖춰져 있어요 ✨" 같은 한 줄을 넣고,
     **확인된 설비를 ✅ 체크리스트로** 나열하세요.
     → 각 설비를 **`<p>✅ 설비명</p>` 형식으로 한 줄씩** 작성 (불릿/ul 쓰지 말고 p 태그로)
     → 예시:
       ```html
       <p>✅ 욕실·화장실 분리형</p>
       <p>✅ 비데</p>
       <p>✅ 발코니</p>
       <p>✅ 오토락 + 모니터 인터폰</p>
       <p>✅ 인터넷 무료</p>
       ```
   - ⚠️ 체크리스트에는 **데이터로 확인되는 설비만** 넣을 것 (추측·과장 금지, 작성 원칙 A·B·E 준수)
   - 비데가 있으면 "비데"로만 표기 (워시렛/온수세정변기 금지)
   - 욕실·화장실이 분리돼 있으면 "욕실·화장실 분리형"으로만 표기

4. **<h2>📍 위치와 생활 인프라</h2>**
   - ⭐ **신주쿠·시부야·이케부쿠로·닛포리 등 주요 요충지까지의 소요 시간** 중심으로 작성
   - 한인타운·한국 슈퍼·한식재료점 등은 강조하지 말 것 (중요하지 않음)
   - 소요 시간 데이터가 없으면 추측 금지, "(현지 확인 필요)" 또는 생략

5. **<h2>✨ 추천 이유</h2>**
   - 비자 종류별로 나누지 말고 매물 장점을 통합해서 작성
   - 누구에게나 와닿는 알기 쉬운 장점 3~5개, 중복 없이
   - 예: 역세권(요충지 접근성), 채광, 욕실·화장실 분리형, 요리하기 편한 구조 등
   - ⭐ 매물 데이터에서 **실제 확인되는 강점만** 사용 (추측·과장 절대 금지, 작성 원칙 A·B 준수)
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
- ⭐ **카카오톡 ID·전화번호·상담 안내는 절대 포함하지 말 것** (이미 다른 곳에 있음)

각 섹션은 정보 위주로 간결하게. 광고 문구만 채우지 말 것.
"""


# ─────────────────────────────────────────────────
# Prompt Caching 최적화 (Anthropic ephemeral cache)
# ─────────────────────────────────────────────────
# BLOG_GENERATION_PROMPT의 변수 부분(스타일·특별지시·매물데이터)은 매 요청마다 변동 →
# 이를 user 메시지로 분리하고, 안정 부분만 system 메시지에 두면 캐시 hit률이 극대화됨.
# 효과: TTFT(첫 토큰까지 시간) 약 60-80% 단축, 캐시 hit 부분 비용 90% 절감.
# 5분 cache TTL — 매물 5개 병렬 + 일배치 안에서 4건 이상 cache hit 기대.

def _build_stable_system_prompt() -> str:
    """모듈 로드/최초 호출 시 1회만 빌드 (kakao·phone·hashtags는 환경변수/상수)."""
    return (
        BLOG_GENERATION_PROMPT
        # 변수 부분은 user 메시지로 분리 → 안정 부분만 system에 둠
        .replace(
            "{style_instructions}",
            "(스타일 가이드는 user 메시지의 '## 스타일' 섹션 참조)",
        )
        .replace(
            "{custom_instructions}",
            "(특별 지시사항은 user 메시지의 '## 특별 지시사항' 섹션 참조)",
        )
        .replace(
            "{property_json}",
            "(매물 데이터는 user 메시지의 '## 매물 데이터' JSON 참조)",
        )
        .format(
            kakao=os.getenv("KAKAO_TALK_ID", "japanreal2"),
            phone=os.getenv("COMPANY_PHONE", "070-8201-5740"),
            core_hashtags=" ".join(CORE_HASHTAGS),
        )
    )


# 모듈 로드 시 1회 빌드 (앱 라이프타임 동안 안정)
_STABLE_SYSTEM_PROMPT = _build_stable_system_prompt()


def _build_user_data_block(
    style_instructions: str, custom_block: str, property_json: str
) -> str:
    """매 요청마다 변경되는 가변 부분 (캐싱되지 않음)."""
    return f"""# 이번 요청 데이터

## 스타일
{style_instructions}

## 특별 지시사항
{custom_block}

## 매물 데이터
```json
{property_json}
```

위 데이터로 system 프롬프트의 모든 지침에 따라 블로그 JSON을 출력하세요."""


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


# 상단 고정 인사말 (요청: 항상 동일하게 본문 맨 위에 삽입)
FIXED_GREETING_HTML = (
    '<p style="text-align:center;font-size:15px;line-height:1.8;margin:8px 0">'
    "안녕하세요🌸</p>\n"
    '<p style="text-align:center;font-size:15px;line-height:1.8;margin:8px 0">'
    "한국분들께 일본 부동산을 전문적으로 안내해드리고 있는 JRE일본부동산입니다 😊</p>\n"
    '<p style="text-align:center;font-size:15px;line-height:1.8;margin:8px 0"><br/></p>\n'
)


def _split_long_paragraphs(html: str) -> str:
    """
    AI가 한 <p> 안에 여러 문장을 묶어서 작성한 경우, 자동으로 분리.
    각 문장이 독립된 <p> 태그를 갖도록 → 네이버 블로그에서 줄바꿈 명확.

    분리 기준:
    - 마침표/물음표/느낌표 (. ! ?) + 공백 + 한글 시작 문자
    - 또는 자주 쓰는 문장 끝 이모지(😊🌸✨) + 공백 + 한글 시작

    보호 대상 (분리 안 함):
    - 표 셀(<td>, <th>) 안의 내용 — 표 구조 보존
    - 빈 <p><br/></p> — 빈 줄 그대로
    - 짧은 한 문장만 있는 <p> — 그대로

    예시:
    <p>매물은 1K입니다. 도보 4분이에요. 월세 ¥115,000입니다.</p>
    →
    <p>매물은 1K입니다.</p>
    <p>도보 4분이에요.</p>
    <p>월세 ¥115,000입니다.</p>
    """
    # 분리 정규식: 마침표/?/!/문장끝이모지 + 공백 + 한글 시작 (영문 대문자는 ¥나 영문 매물명 보호 위해 제외)
    # (?<=...) lookbehind는 다음 문장이 한글로 시작할 때만 분리
    SENTENCE_BOUNDARY = re.compile(
        r'(?<=[.!?😊🌸✨])\s+(?=[가-힣])',
        re.UNICODE
    )

    def _split_one_p(m):
        attrs = m.group(1) or ""
        content = m.group(2)

        # 빈 <p> 또는 <br/>만 있는 <p>는 그대로 (빈 줄 보존)
        stripped = re.sub(r'<br\s*/?>', '', content).strip()
        if not stripped:
            return m.group(0)

        # 분리 시도
        parts = SENTENCE_BOUNDARY.split(content)
        if len(parts) <= 1:
            return m.group(0)  # 단일 문장 → 그대로

        # 각 조각을 별도 <p>로 (원래 attrs 그대로 유지)
        rebuilt = []
        for piece in parts:
            piece = piece.strip()
            if piece:
                rebuilt.append(f'<p{attrs}>{piece}</p>')
        return '\n'.join(rebuilt)

    # <p>...</p> 패턴 매칭 (표 셀 안에는 보통 <p>를 안 쓰므로 안전)
    # 비탐욕(.*?) + DOTALL로 줄바꿈 있는 <p>도 처리
    return re.sub(r'<p([^>]*)>(.*?)</p>', _split_one_p, html, flags=re.DOTALL)


def _add_section_spacing(html: str) -> str:
    """
    섹션 헤더(<h2>) 앞에 빈 줄 한 번 더 보장 → 네이버에서 시각적 분리.
    이미 빈 <p><br/></p>가 직전에 있으면 추가 안 함.
    """
    # <h2> 직전에 빈 줄이 없으면 삽입
    # (이미 있으면 중복 방지)
    def _insert_blank(m):
        before = m.group(1)
        h2_open = m.group(2)
        if '<br' in before[-40:] and '<p' in before[-40:]:
            return m.group(0)  # 직전에 빈 <p><br/></p> 있음 → 그대로
        return before + '\n<p><br/></p>\n' + h2_open

    return re.sub(r'(.{0,60})(<h2[^>]*>)', _insert_blank, html, flags=re.DOTALL)


def _apply_naver_formatting(html: str) -> str:
    """
    네이버 블로그용 최종 포맷팅 (복사 붙여넣기만으로 게재 가능하게).
    - 본문 문단·헤더·리스트에 가운데 정렬 + 폰트 크기를 inline style로 주입
    - 기본 폰트 15px, 대카테고리 헤더(h2) 24px
    - 상단 고정 인사말을 맨 앞에 삽입
    ⚠️ 표(table/td/th)는 건드리지 않음 — 좌측 라벨 + 전체폭 레이아웃 유지.
    이미 style이 있는 태그는 덮어쓰지 않음 (표·강조 등 보존).
    """
    # 대카테고리 헤더 (📋 매물 기본정보 등) → 24px, 가운데, 굵게
    def _inject_h2(m):
        attrs = m.group(1) or ""
        if "style=" in attrs:
            return m.group(0)
        return (
            '<h2 style="text-align:center;font-size:24px;'
            'font-weight:bold;margin:26px 0 12px"' + attrs + ">"
        )
    html = re.sub(r"<h2([^>]*)>", _inject_h2, html)

    # 소제목(h3, 있을 경우) → 17px, 가운데
    def _inject_h3(m):
        attrs = m.group(1) or ""
        if "style=" in attrs:
            return m.group(0)
        return (
            '<h3 style="text-align:center;font-size:17px;'
            'font-weight:bold;margin:18px 0 8px"' + attrs + ">"
        )
    html = re.sub(r"<h3([^>]*)>", _inject_h3, html)

    # 본문 문단 → 15px, 가운데
    def _inject_p(m):
        attrs = m.group(1) or ""
        if "style=" in attrs:
            return m.group(0)
        return (
            '<p style="text-align:center;font-size:15px;'
            'line-height:1.8;margin:8px 0"' + attrs + ">"
        )
    html = re.sub(r"<p([^>]*)>", _inject_p, html)

    # 리스트(ul/ol) → 가운데, 불릿 제거 (체크리스트는 보통 <p>✅…로 오지만 안전망)
    def _inject_ul(m):
        attrs = m.group(1) or ""
        if "style=" in attrs:
            return m.group(0)
        return (
            '<ul style="text-align:center;font-size:15px;'
            'list-style:none;padding:0;margin:8px 0"' + attrs + ">"
        )
    html = re.sub(r"<ul([^>]*)>", _inject_ul, html)
    html = re.sub(r"<ol([^>]*)>", _inject_ul, html)

    def _inject_li(m):
        attrs = m.group(1) or ""
        if "style=" in attrs:
            return m.group(0)
        return '<li style="font-size:15px;line-height:1.8"' + attrs + ">"
    html = re.sub(r"<li([^>]*)>", _inject_li, html)

    # 상단 고정 인사말 삽입 (맨 앞)
    return FIXED_GREETING_HTML + html


def _extract_ward_from_address(address: str) -> str:
    """
    일본 주소에서 구(区) 또는 시(市) 부분을 추출.

    예시:
    - "東京都新宿区西新宿2-1-1" → "신주쿠구"
    - "東京都板橋区成増2-3-4" → "이타바시구"
    - "東京都渋谷区道玄坂1-2-3" → "시부야구"
    - "千葉県千葉市美浜区..." → "지바시 미하마구"
    - 추출 실패 → ""
    """
    if not address:
        return ""

    # 일본어 → 한국어 매핑 (주요 도쿄 23구 + 인근)
    ward_map = {
        # 도쿄 23구
        "千代田区": "지요다구",
        "中央区": "주오구",
        "港区": "미나토구",
        "新宿区": "신주쿠구",
        "文京区": "분쿄구",
        "台東区": "다이토구",
        "墨田区": "스미다구",
        "江東区": "고토구",
        "品川区": "시나가와구",
        "目黒区": "메구로구",
        "大田区": "오타구",
        "世田谷区": "세타가야구",
        "渋谷区": "시부야구",
        "中野区": "나카노구",
        "杉並区": "스기나미구",
        "豊島区": "도시마구",
        "北区": "기타구",
        "荒川区": "아라카와구",
        "板橋区": "이타바시구",
        "練馬区": "네리마구",
        "足立区": "아다치구",
        "葛飾区": "가쓰시카구",
        "江戸川区": "에도가와구",
        # 인근 주요 도시
        "横浜市": "요코하마시",
        "川崎市": "가와사키시",
        "千葉市": "지바시",
        "さいたま市": "사이타마시",
        "船橋市": "후나바시시",
        "市川市": "이치카와시",
        "松戸市": "마쓰도시",
        "柏市": "가시와시",
    }

    # 주소에서 매핑 키워드 찾기 (긴 키워드 우선 매칭)
    for jp, kr in sorted(ward_map.items(), key=lambda x: -len(x[0])):
        if jp in address:
            return kr

    # 매핑 실패 시 일반 패턴 추출 시도
    # "OO区" 또는 "OO市" 패턴 찾기
    ward_match = re.search(r"([一-龥ぁ-んァ-ヶ]+[区市町村])", address)
    if ward_match:
        return ward_match.group(1)  # 일본어 그대로

    return ""


def _extract_prefecture_korean(address: str) -> str:
    """
    주소에서 도도후켄(都道府県)을 추출해 한글로 변환.

    예:
      "東京都板橋区..."   → "도쿄도"
      "神奈川県川崎市..." → "가나가와현"
      "埼玉県さいたま市..."→ "사이타마현"
      "千葉県船橋市..."   → "치바현"
      "板橋区..." (도도후켄 없음) → ""  (사장님 결정 B: 누락 시 생략)
    """
    if not address:
        return ""

    # 도도후켄 한자 → 한글 매핑 (도쿄 외곽 + 수도권 중심)
    PREFECTURE_MAP = {
        "東京都": "도쿄도",
        "神奈川県": "가나가와현",
        "埼玉県": "사이타마현",
        "千葉県": "치바현",
        "茨城県": "이바라키현",
        "栃木県": "토치기현",
        "群馬県": "군마현",
        "山梨県": "야마나시현",
        "静岡県": "시즈오카현",
        # 대도시권 외 — 만일을 대비
        "大阪府": "오사카부",
        "京都府": "교토부",
        "北海道": "홋카이도",
    }
    # 주소 시작 부분에서 매칭 (긴 것 먼저 — 神奈川県이 県 하나보다 먼저)
    for jp_pref, ko_pref in sorted(PREFECTURE_MAP.items(), key=lambda x: -len(x[0])):
        if address.startswith(jp_pref) or jp_pref in address[:20]:
            return ko_pref
    return ""  # 도도후켄 누락 → 생략 (옵션 B)


def _build_standard_title(property_data: dict) -> str:
    """
    매물 정보로부터 표준 형식의 블로그 제목 생성.

    형식 (사장님 확정):
    [토도후켄] [행정구역] [노선] [역]역 도보 [분]분 [방구조] 월세 ¥[금액]+관리비 ¥[금액]

    예시:
    - 도쿄도 이타바시구 도부토조선 토키와다이역 도보 10분 1K 월세 ¥77,000+관리비 ¥5,000
    - 가나가와현 나카하라구 JR 남부선 무코우가하라역 도보 8분 1K 월세 ¥79,000+관리비 ¥9,000

    토도후켄 추출 실패 시 (옵션 B): 생략하고 종전 형식 유지
    - 이타바시구 도부토조선 토키와다이역 도보 10분 1K 월세 ¥77,000+관리비 ¥5,000

    데이터가 부족하면 가능한 부분만 채워 자연스럽게 생성.
    """
    parts = []

    address = property_data.get("address") or ""

    # 0. 토도후켄 (주소에서 추출, 없으면 생략)
    prefecture = _extract_prefecture_korean(address)
    if prefecture:
        parts.append(prefecture)

    # 1. 지역 (구/시) — 일본어 주소를 한국어로 번역
    # 먼저 번역 사전으로 시도 (埼玉県川口市 → 사이타마현 카와구치시)
    ward = translate_ward(address)
    # 번역 사전에 없으면 기존 추출 로직 폴백
    if not ward or ward == address:
        ward = _extract_ward_from_address(address)
    if ward:
        # 토도후켄 한글이 이미 ward 안에 포함된 경우 중복 제거
        # (예: prefecture="도쿄도", ward="도쿄도 신주쿠구" → ward를 "신주쿠구"로)
        if prefecture and ward.startswith(prefecture):
            ward = ward[len(prefecture):].strip()
        if ward:  # 빈 문자열 방지
            parts.append(ward)

    # 2. 노선 + 역 + 도보 — 일본어를 한국어로 번역
    station = property_data.get("nearest_station") or {}
    line_raw = (station.get("line") or "").strip()
    station_raw = (station.get("station") or "").strip()
    walk_min = station.get("walk_minutes")

    # 번역 적용 (사내 DB 기준, 매핑 없으면 원본 유지)
    line = translate_line(line_raw)
    station_name = translate_station(station_raw)

    # 노선 + 역명 결합
    if line and station_name:
        parts.append(f"{line} {station_name}역")
    elif station_name:
        parts.append(f"{station_name}역")

    # 도보 시간
    if walk_min is not None:
        try:
            walk_n = int(walk_min)
            parts.append(f"도보 {walk_n}분")
        except (ValueError, TypeError):
            pass

    # 3. 방구조
    layout = (property_data.get("layout") or "").strip()
    if layout:
        parts.append(layout)

    # 4. 월세 + 관리비
    rent = property_data.get("rent_yen")
    mgmt = property_data.get("management_fee_yen")

    if rent:
        try:
            rent_n = int(rent)
            money_part = f"월세 ¥{rent_n:,}"
            if mgmt:
                try:
                    mgmt_n = int(mgmt)
                    money_part += f"+관리비 ¥{mgmt_n:,}"
                except (ValueError, TypeError):
                    pass
            parts.append(money_part)
        except (ValueError, TypeError):
            pass

    # 부분들을 공백으로 연결
    title = " ".join(parts)

    # 너무 짧으면 fallback (최소한 방구조라도 있어야)
    if len(title) < 5:
        return f"{layout or '신규 매물'} 월세 정보"

    return title


def _append_map_link(summary: str, property_data: dict) -> str:
    """
    카카오톡 요약 끝에 구글맵 위치 링크를 자동으로 추가.

    검색 키워드 우선순위:
    1. address (주소) — 가장 정확
    2. property_name (매물명) — 건물명으로 검색
    3. nearest_station 역명 — 위치 대략 표시

    구글맵 URL: https://www.google.com/maps/search/?api=1&query=<encoded>
    카카오톡에서 클릭하면 자동으로 구글맵 앱 또는 웹이 열림.
    """
    if not summary:
        return summary

    address = (property_data.get("address") or "").strip()
    property_name = (property_data.get("property_name") or "").strip()
    nearest = property_data.get("nearest_station") or {}
    station = (nearest.get("station") or "").strip()

    # 검색 쿼리 결정 (주소 > 매물명 > 역명)
    query = address or property_name
    if not query and station:
        query = f"{station}駅"  # 역명 + 駅

    if not query:
        return summary  # 위치 정보 없으면 그대로 반환

    # URL 인코딩 (일본어 한자도 안전하게 처리)
    encoded = quote(query, safe="")
    map_url = f"https://www.google.com/maps/search/?api=1&query={encoded}"

    # 카톡 요약 끝에 위치 줄 추가 (이미 있으면 중복 추가 안 함)
    if "🗺️" in summary or "google.com/maps" in summary:
        return summary

    return f"{summary}\n\n🗺️ 위치 보기: {map_url}"


def generate_blog_post(
    property_data: dict,
    target_visa: VisaType = "all",
    style_name: str = DEFAULT_STYLE,
    custom_instructions: str = "",
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    max_retries: int = 3,
    stream_callback: Optional[Callable[[str], None]] = None,
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

    # ⭐ Prompt Caching 구조: 안정 system + 가변 user 메시지로 분리
    # _STABLE_SYSTEM_PROMPT는 모듈 로드 시 1회 빌드된 안정 부분 (캐시됨)
    property_json = json.dumps(prop, ensure_ascii=False, indent=2)
    user_data_block = _build_user_data_block(
        style_instructions=style_instructions,
        custom_block=custom_block,
        property_json=property_json,
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            # ─── Extended/Adaptive Thinking 모델별 자동 분기 ───
            # Opus 4.7:  thinking 제거됨 → adaptive 사용 (effort 파라미터)
            # Sonnet 4.6: adaptive thinking 지원
            # Haiku 4.5:  extended thinking (enabled + budget_tokens) 지원
            # 이외 모델:  thinking 미사용 (안전)
            request_params = {
                "model": model,
                "max_tokens": 16384,
                # ⭐ 안정 system 프롬프트에 cache_control 적용 → 캐시 hit 시 비용 90%↓
                "system": [
                    {
                        "type": "text",
                        "text": _STABLE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                # 가변 데이터만 user 메시지로 (캐싱되지 않음)
                "messages": [{"role": "user", "content": user_data_block}],
            }
            model_lower = model.lower()
            if "opus-4-7" in model_lower or "opus-4.7" in model_lower:
                # Opus 4.7: adaptive thinking
                request_params["thinking"] = {"type": "adaptive"}
            elif "sonnet-4-6" in model_lower or "sonnet-4.6" in model_lower:
                # Sonnet 4.6: adaptive thinking 지원
                request_params["thinking"] = {"type": "adaptive"}
            elif "haiku-4-5" in model_lower or "haiku-4.5" in model_lower:
                # Haiku 4.5: extended thinking
                request_params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": 5000,
                }
            # 그 외 모델은 thinking 미사용

            # ⭐ 스트리밍 vs 일반 호출 분기
            #   - stream_callback 제공 시: messages.stream + 토큰 단위 콜백
            #   - 없으면: 기존 messages.create (병렬 배치에서 사용)
            try:
                if stream_callback is not None:
                    # 스트리밍 모드: 실시간 토큰 출력
                    with client.messages.stream(**request_params) as stream:
                        for text_delta in stream.text_stream:
                            try:
                                stream_callback(text_delta)
                            except Exception:
                                # 콜백 실패가 본 처리에 영향 주지 않도록
                                pass
                        response = stream.get_final_message()
                else:
                    # 일반 호출 (병렬 배치용)
                    response = client.messages.create(**request_params)
            except Exception as thinking_err:
                # thinking 파라미터 호환성 문제 시 thinking 없이 재시도 (안전 폴백)
                err_str = str(thinking_err).lower()
                if "thinking" in err_str and "thinking" in request_params:
                    request_params.pop("thinking", None)
                    if stream_callback is not None:
                        with client.messages.stream(**request_params) as stream:
                            for text_delta in stream.text_stream:
                                try:
                                    stream_callback(text_delta)
                                except Exception:
                                    pass
                            response = stream.get_final_message()
                    else:
                        response = client.messages.create(**request_params)
                else:
                    raise

            # thinking 블록 + text 블록이 모두 있을 수 있음 - text만 추출
            raw_text = ""
            for block in response.content:
                if hasattr(block, "type") and block.type == "text":
                    raw_text = block.text
                    break
                # 일부 SDK는 text 속성을 직접 가짐
                if hasattr(block, "text") and not raw_text:
                    raw_text = block.text

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

            # ⭐ 제목을 표준 형식으로 강제 덮어쓰기
            # 형식: [지역] [노선] [역]역 도보 [분]분 [방구조] 월세 ¥[금액]+관리비 ¥[금액]
            # AI가 생성한 제목 대신 사장님이 지정한 정확한 표준 형식 사용
            standard_title = _build_standard_title(property_data)
            if standard_title:
                result["title"] = standard_title

            # ⭐ DB(路線 시트) 미등록 노선/역/지명 감지 → 화면 경고용
            try:
                result["untranslated"] = detect_untranslated(property_data)
            except Exception:
                result["untranslated"] = []

            # "(현지 확인 필요)" 표시를 빨간색으로 강조 (수동 편집 필요한 부분 시각화)
            if result.get("html_content"):
                result["html_content"] = _ensure_table_styles(result["html_content"])
                result["html_content"] = _highlight_check_needed(result["html_content"])
                # ⭐ 단락 자동 분리: 한 <p>에 여러 문장 묶인 케이스 자동 분리 (가독성)
                result["html_content"] = _split_long_paragraphs(result["html_content"])
                # ⭐ 섹션 헤더 앞에 빈 줄 보장 (네이버에서 시각적 분리)
                result["html_content"] = _add_section_spacing(result["html_content"])
                # ⭐ 네이버용 최종 포맷팅: 가운데 정렬 + 폰트 + 상단 고정 인사말
                result["html_content"] = _apply_naver_formatting(result["html_content"])

            # 카톡 요약에 구글맵 위치 링크 자동 추가
            if result.get("summary_for_chat"):
                result["summary_for_chat"] = _append_map_link(
                    result["summary_for_chat"], property_data
                )

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


# ─────────────────────────────────────────────────
# SNS 콘텐츠 자동 생성 (인스타·카톡·유튜브 쇼츠)
# ─────────────────────────────────────────────────
SNS_PROMPT = """\
당신은 한국인 일본 거주 손님을 대상으로 하는 일본 부동산 SNS 마케팅 전문가입니다.
아래 매물 정보와 블로그 글을 바탕으로, 세 가지 채널용 콘텐츠를 작성하세요.

# 매물 정보 (JSON)
{property_json}

# 이미 생성된 블로그 정보
- 제목: {blog_title}
- 카톡 요약:
{summary_for_chat}

# 출력 (JSON only, 코드블럭 없이):

{{
  "instagram_caption": "<인스타 피드 캡션, 200~300자, 한국어, 손님 친화 톤>",
  "instagram_hashtags": ["#태그1", "#태그2", ...],
  "kakao_openchat": "<카카오톡 오픈채팅방 메시지, 100~150자, 이모지 활용, 핵심 정보만>",
  "youtube_shorts_script": {{
    "hook": "<0~3초 강력한 도입 문구, 한 줄>",
    "scenes": [
      {{
        "time": "0~10s",
        "subtitle": "<자막 텍스트, 한 줄>",
        "narration": "<나레이션 또는 강조 포인트>"
      }},
      {{ "time": "10~25s", "subtitle": "...", "narration": "..." }},
      {{ "time": "25~45s", "subtitle": "...", "narration": "..." }},
      {{ "time": "45~55s", "subtitle": "...", "narration": "..." }}
    ],
    "cta": "<55~60초 콜투액션, 카톡 ID japanreal2 연결 유도>"
  }}
}}

# 작성 규칙

## 인스타그램 캡션 (instagram_caption)
- 첫 줄: 매물 한 줄 요약 (이모지 1~2개 + 핵심 포인트)
- 본문: 매물 장점 2~3개를 감성적으로 (광고 X, 정보 위주)
- 손님이 공감할 만한 일상 톤
- 매물명·정확한 주소·계약기간은 절대 포함 X
- "DM 또는 카톡 japanreal2" 같은 자연스러운 콜투액션 1줄
- 글머리 기호(•) 사용 OK, 짧은 줄바꿈 활용
- 200~300자

## 인스타그램 해시태그 (instagram_hashtags)
- 8~12개
- 일본 부동산·도쿄·해당 지역·생활 정보 관련
- 인기 태그 + 롱테일 태그 혼합
- 예: #일본부동산 #도쿄월세 #신주쿠원룸 #일본워홀 #도쿄집구하기

## 카카오톡 오픈채팅 (kakao_openchat)
- 첫 줄: 강력한 이모지 + 매물 한 줄 요약
- 정보 3~4줄 (역·월세·방구조·핵심포인트 1개)
- 마지막 줄: 문의 안내 (간결)
- 광고 톤 X, 정보 공유 톤
- 100~150자, 이모지 적극 활용
- 줄바꿈 \\n 사용

## 유튜브 쇼츠 스크립트 (youtube_shorts_script)
- 총 60초 분량
- hook: 첫 3초 시청자 사로잡기 (예: "도쿄 신주쿠 5분 거리 매물!")
- 4개 장면(scenes): 시간 배분 자연스럽게
- 각 장면의 subtitle: 화면에 띄울 짧은 자막 (10~15자)
- 각 장면의 narration: 음성 또는 강조 텍스트 (20~30자)
- cta: 카카오톡 ID japanreal2 자연스럽게 안내

# 공통 주의사항
- 한국어로 작성
- 매물명·정확한 주소·계약기간은 절대 포함 X
- 광고성 강조 문구 X (예: "최고", "절대 강추")
- 정보의 신뢰성 강조 (정직한 톤)
- 추측 정보는 "(현지 확인 필요)" 표시

JSON 출력만, 다른 설명 텍스트 절대 X.
"""


def generate_sns_content(
    property_data: dict,
    blog_post: dict,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    max_retries: int = 3,
) -> dict:
    """
    매물 정보 + 블로그를 바탕으로 SNS 콘텐츠 3종 자동 생성.

    Args:
        property_data: analyzer.py가 추출한 매물 데이터 (dict)
        blog_post: generate_blog_post() 결과 (dict, title/summary_for_chat 포함)
        model: Claude 모델
        api_key: Anthropic API 키
        max_retries: 재시도 횟수

    Returns:
        {
            "instagram_caption": "...",
            "instagram_hashtags": ["#...", ...],
            "kakao_openchat": "...",
            "youtube_shorts_script": {
                "hook": "...",
                "scenes": [...],
                "cta": "..."
            }
        }
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY 가 설정되지 않았습니다."
        )

    client = anthropic.Anthropic(api_key=api_key)

    # 프롬프트 변수 채우기
    property_json = json.dumps(property_data, ensure_ascii=False, indent=2)
    blog_title = blog_post.get("title", "")
    summary_for_chat = blog_post.get("summary_for_chat", "")

    prompt = SNS_PROMPT.format(
        property_json=property_json,
        blog_title=blog_title,
        summary_for_chat=summary_for_chat,
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )

            raw_text = response.content[0].text.strip()
            result = _extract_blog_json(raw_text)

            # 필수 필드 검증
            required = [
                "instagram_caption", "instagram_hashtags",
                "kakao_openchat", "youtube_shorts_script"
            ]
            for k in required:
                if k not in result:
                    raise KeyError(f"필수 필드 누락: {k}")

            return result

        except (json.JSONDecodeError, ValueError, KeyError, IndexError) as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise

    raise RuntimeError(
        f"SNS 콘텐츠 생성 실패 (재시도 {max_retries}회 초과): {last_error}"
    )


def format_sns_for_display(sns_content: dict) -> dict:
    """
    SNS 콘텐츠를 화면 표시·복사용 텍스트로 포맷팅.
    각 채널별로 사용자가 그대로 복사해서 사용할 수 있는 형태.

    Returns:
        {
            "instagram_full": "캡션\\n\\n해시태그",
            "kakao": "메시지",
            "youtube_full": "Hook + 시간별 장면 + CTA",
            "youtube_subtitles_only": "자막만 (영상 편집용)"
        }
    """
    insta_caption = sns_content.get("instagram_caption", "")
    insta_tags = sns_content.get("instagram_hashtags", [])
    instagram_full = (
        insta_caption + "\n\n" + " ".join(insta_tags)
    ).strip()

    kakao = sns_content.get("kakao_openchat", "")

    shorts = sns_content.get("youtube_shorts_script", {})
    hook = shorts.get("hook", "")
    scenes = shorts.get("scenes", [])
    cta = shorts.get("cta", "")

    # 유튜브 쇼츠 전체 스크립트 (사용자 보기용)
    youtube_lines = []
    if hook:
        youtube_lines.append(f"🎬 [0~3초 Hook]")
        youtube_lines.append(f"   {hook}")
        youtube_lines.append("")
    for i, scene in enumerate(scenes, 1):
        time_label = scene.get("time", "")
        subtitle = scene.get("subtitle", "")
        narration = scene.get("narration", "")
        youtube_lines.append(f"📋 [장면 {i} · {time_label}]")
        youtube_lines.append(f"   자막: {subtitle}")
        if narration and narration != subtitle:
            youtube_lines.append(f"   나레이션: {narration}")
        youtube_lines.append("")
    if cta:
        youtube_lines.append(f"📢 [CTA · 55~60초]")
        youtube_lines.append(f"   {cta}")

    youtube_full = "\n".join(youtube_lines)

    # 자막만 (영상 편집 시 그대로 사용)
    subtitle_lines = []
    if hook:
        subtitle_lines.append(hook)
    for scene in scenes:
        subtitle = scene.get("subtitle", "")
        if subtitle:
            subtitle_lines.append(subtitle)
    if cta:
        subtitle_lines.append(cta)
    youtube_subtitles_only = "\n".join(subtitle_lines)

    return {
        "instagram_full": instagram_full,
        "kakao": kakao,
        "youtube_full": youtube_full,
        "youtube_subtitles_only": youtube_subtitles_only,
    }
