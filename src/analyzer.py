"""
analyzer.py
─────────────────────────────────────────────────────────
일본 부동산 도면(マイソク/間取り図)을 분석해 구조화된 JSON으로 변환.

엔진 옵션 (engine 파라미터):
- "hybrid"  : Gemini 2.5 Flash (무료) → 자신도 낮으면 Claude Opus 4.7 자동 재시도 ⭐ 추천
- "gemini"  : Gemini 2.5 Flash (무료)만 사용
- "claude"  : Claude Opus 4.7 (유료, 최고 정확도)만 사용

지원 형식: JPG, JPEG, PNG, WEBP, GIF, PDF
PDF는 pypdfium2로 페이지를 고해상도 이미지로 변환 후 분석.
"""

import base64
import io
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic

DEFAULT_CLAUDE_MODEL = "claude-opus-4-7"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

EXTRACTION_PROMPT = """\
あなたは日本の不動産マイソク（物件図面・間取り図）の解析専門家です。
提供された画像を精密に分析し、以下のスキーマに従って JSON のみを出力してください。
画像から読み取れない項目は null としてください。憶測や推測はしないこと。

# 重要な注意点
- 賃料表記が「8.5万円」の場合は 85000 として整数(円)で出力
- 「○○駅 徒歩○分」は最寄駅と分数を別フィールドに分離
- 設備アイコンも全て読み取ること (エアコン・バルコニー・洗濯機置場 等)
- 間取り表記 (1R, 1K, 1DK, 1LDK, 2K, 2LDK 等) は半角で
- extraction_confidence は読み取り精度を厳密に評価:
  * high: 主要項目(駅・賃料・間取り・面積)がすべて明確に読み取れた
  * medium: 一部の項目が不鮮明だが、主要項目は読み取れた
  * low: 画像が不鮮明、または主要項目の半数以上が読み取れない

# 出力スキーマ (JSON のみ、コードブロックなし)
{
  "property_name": "物件名（マンション・アパート名）",
  "address": "所在地（番地まで）",
  "nearest_station": {
    "line": "路線名",
    "station": "駅名",
    "walk_minutes": 数値
  },
  "additional_stations": [
    {"line": "...", "station": "...", "walk_minutes": 0}
  ],
  "rent_yen": 月額賃料(円, 整数),
  "management_fee_yen": 管理費・共益費(円),
  "layout": "間取り (例: 1LDK)",
  "area_sqm": 専有面積(m², 数値),
  "building_age_years": 築年数(数値),
  "construction_year": 築年(西暦, 例: 2018),
  "structure": "構造 (木造/軽量鉄骨/重量鉄骨/RC/SRC)",
  "floor": 入居予定階,
  "total_floors": 建物総階数,
  "facing_direction": "向き (例: 南向き、東南角)",
  "rooms": [
    {"name": "洋室", "size_jou": 6.0, "size_sqm": 9.93}
  ],
  "facilities": {
    "air_conditioner": true/false,
    "separate_bath_toilet": true/false,
    "washing_machine_indoor": true/false,
    "balcony": true/false,
    "auto_lock": true/false,
    "delivery_box": true/false,
    "elevator": true/false,
    "internet_free": true/false,
    "gas_type": "都市ガス/プロパン/null",
    "kitchen_burners": コンロ口数,
    "intercom_monitor": true/false,
    "other_facilities": ["記載されたその他設備"]
  },
  "conditions": {
    "foreigners_ok": true/false/null,
    "pets_ok": true/false/null,
    "instruments_ok": true/false/null,
    "corporate_contract_ok": true/false/null,
    "office_use_ok": true/false/null,
    "contract_period_years": 契約期間
  },
  "available_from": "入居可能日",
  "agency_notes": "備考・特筆事項 (原文ママ)",
  "extraction_confidence": "high/medium/low"
}
"""

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
SUPPORTED_EXTS = IMAGE_EXTS | {".pdf"}

MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
}


# ──────────────────────────────────────────────────────
# 공통: 파일 → 이미지 바이트 리스트 (PDF는 페이지별로 분리)
# ──────────────────────────────────────────────────────
def _pdf_to_image_bytes_list(pdf_path: str, max_pages: int = 3) -> list[tuple[bytes, str]]:
    """PDF → 페이지별 (PNG bytes, mime_type) 리스트."""
    import pypdfium2 as pdfium

    results = []
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            bitmap = page.render(scale=2.8)  # 약 200 DPI
            pil_image = bitmap.to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            results.append((buf.getvalue(), "image/png"))
    finally:
        pdf.close()

    if not results:
        raise ValueError("PDF에서 페이지를 추출하지 못했습니다.")
    return results


def _file_to_image_bytes_list(file_path: str) -> list[tuple[bytes, str]]:
    """모든 파일 → (이미지 bytes, mime_type) 리스트로 통일."""
    ext = Path(file_path).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(
            f"지원하지 않는 형식: {ext}\n지원 형식: JPG, PNG, WEBP, GIF, PDF"
        )
    if ext == ".pdf":
        return _pdf_to_image_bytes_list(file_path)
    with open(file_path, "rb") as f:
        return [(f.read(), MEDIA_TYPES[ext])]


# ──────────────────────────────────────────────────────
# JSON 안전 추출
# ──────────────────────────────────────────────────────
def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON을 찾을 수 없습니다:\n{text[:300]}")
    return json.loads(text[start : end + 1])


def _compute_actual_confidence(result: dict) -> str:
    """
    추출 결과의 실제 신뢰도를 재계산. AI가 'high'라고 해도 핵심 필드가 비어 있으면 'low'로 강등.
    하이브리드 모드에서 Claude 재시도 트리거에 사용.
    """
    declared = (result.get("extraction_confidence") or "").lower()

    station = result.get("nearest_station") or {}
    critical_fields = [
        station.get("station"),
        result.get("rent_yen"),
        result.get("layout"),
        result.get("area_sqm"),
    ]
    missing = sum(1 for v in critical_fields if not v)

    if missing >= 2:
        return "low"  # 4개 중 2개 이상 누락 → 강제 low
    if missing == 1 and declared == "high":
        return "medium"  # 1개 누락 → high는 과대평가
    return declared if declared in ("high", "medium", "low") else "medium"


# ──────────────────────────────────────────────────────
# Claude Opus 4.7 분석기 (유료, 최고 정확도)
# ──────────────────────────────────────────────────────
def _analyze_with_claude(
    file_path: str,
    model: str = DEFAULT_CLAUDE_MODEL,
    api_key: Optional[str] = None,
    max_retries: int = 3,
) -> dict:
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    image_data = _file_to_image_bytes_list(file_path)

    # Claude 이미지 블록 형식
    image_blocks = []
    for img_bytes, mime in image_data:
        encoded = base64.standard_b64encode(img_bytes).decode("utf-8")
        image_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": encoded},
        })
    content = image_blocks + [{"type": "text", "text": EXTRACTION_PROMPT}]

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
            )
            raw_text = response.content[0].text
            return _extract_json(raw_text)
        except (json.JSONDecodeError, ValueError, IndexError) as e:
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

    raise RuntimeError(f"Claude 분석 실패 (재시도 {max_retries}회 초과): {last_error}")


# ──────────────────────────────────────────────────────
# Gemini 2.5 Flash 분석기 (무료, Google AI Studio)
# ──────────────────────────────────────────────────────
def _analyze_with_gemini(
    file_path: str,
    api_key: Optional[str] = None,
    max_retries: int = 2,
) -> dict:
    """
    Gemini 2.5 Flash 무료 티어를 이용한 분석.
    Google AI Studio에서 무료로 API 키 발급 가능: https://aistudio.google.com/apikey
    """
    from google import genai
    from google.genai import types as gtypes

    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "GEMINI_API_KEY 미설정. "
            "https://aistudio.google.com/apikey 에서 무료 발급 후 환경변수에 설정하세요."
        )

    client = genai.Client(api_key=key)
    image_data = _file_to_image_bytes_list(file_path)

    contents = []
    for img_bytes, mime in image_data:
        contents.append(gtypes.Part.from_bytes(data=img_bytes, mime_type=mime))
    contents.append(EXTRACTION_PROMPT)

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=DEFAULT_GEMINI_MODEL,
                contents=contents,
                config=gtypes.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            raw_text = response.text or ""
            return _extract_json(raw_text)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1.5)
                continue
        except Exception as e:
            last_error = e
            # 429 (rate limit) 등은 지수 백오프
            err_str = str(e).lower()
            if any(x in err_str for x in ("429", "rate", "quota", "503")):
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            raise

    raise RuntimeError(f"Gemini 분석 실패 (재시도 {max_retries}회 초과): {last_error}")


# ──────────────────────────────────────────────────────
# 메인 디스패처: 엔진 선택 + 하이브리드 폴백
# ──────────────────────────────────────────────────────
def analyze_property_sheet(
    file_path: str,
    engine: str = "hybrid",
    model: str = DEFAULT_CLAUDE_MODEL,
    api_key: Optional[str] = None,
    gemini_api_key: Optional[str] = None,
    max_retries: int = 3,
) -> dict:
    """
    마이소크 → 구조화된 부동산 데이터(dict)

    Args:
        file_path: 도면 파일 경로 (JPG/PNG/WEBP/GIF/PDF)
        engine: "hybrid" | "gemini" | "claude"
            - hybrid: Gemini 무료 → 자신도 낮으면 Claude 재시도 (추천)
            - gemini: Gemini 2.5 Flash 무료만 사용
            - claude: Claude Opus 4.7 유료만 사용
        model: Claude 모델명 (engine이 claude 또는 hybrid 폴백 시)
        api_key: Anthropic API 키 (None이면 환경변수)
        gemini_api_key: Gemini API 키 (None이면 환경변수 GEMINI_API_KEY)
        max_retries: 재시도 횟수

    Returns:
        dict — '_engine_used' 필드로 실제 사용된 엔진 표시
    """
    engine = (engine or "hybrid").lower()

    # ① Claude만 사용
    if engine == "claude":
        result = _analyze_with_claude(file_path, model, api_key, max_retries)
        result["_engine_used"] = "claude"
        return result

    # ② Gemini만 사용
    if engine == "gemini":
        result = _analyze_with_gemini(file_path, gemini_api_key, max_retries=2)
        result["_engine_used"] = "gemini"
        # 실제 자신도 강등이 있으면 표시
        actual = _compute_actual_confidence(result)
        if actual != (result.get("extraction_confidence") or "").lower():
            result["extraction_confidence"] = actual
        return result

    # ③ Hybrid: Gemini 우선, 자신도 낮거나 실패 시 Claude
    has_gemini_key = bool(gemini_api_key or os.getenv("GEMINI_API_KEY"))

    if has_gemini_key:
        try:
            gemini_result = _analyze_with_gemini(file_path, gemini_api_key, max_retries=2)
            actual_conf = _compute_actual_confidence(gemini_result)
            gemini_result["extraction_confidence"] = actual_conf

            # 자신도가 low가 아니면 Gemini 결과 채택 (무료)
            if actual_conf != "low":
                gemini_result["_engine_used"] = "gemini"
                return gemini_result
            # low면 아래에서 Claude로 재시도
        except Exception:
            # Gemini 실패 → 조용히 Claude로 폴백
            pass

    # Claude 폴백
    result = _analyze_with_claude(file_path, model, api_key, max_retries)
    if has_gemini_key:
        result["_engine_used"] = "claude (gemini→claude 폴백)"
    else:
        result["_engine_used"] = "claude (GEMINI_API_KEY 미설정)"
    return result
