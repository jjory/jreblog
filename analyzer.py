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
- 敷金(shikikin)・礼金(reikin) はマイソクの表記をそのまま読み取る:
  * 「1ヶ月」「1.0ヶ月分」のような月数表記 → {"value": 1.0, "unit": "months"}
  * 「120,000円」のような金額表記 → {"value": 120000, "unit": "yen"}
  * 「なし」「0」「ゼロ」 → {"value": 0, "unit": "months"}
  * 読み取れない場合 → {"value": null, "unit": null}（憶測しない）
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
  "shikikin": {"value": 数値 or null, "unit": "months / yen / null"},
  "reikin": {"value": 数値 or null, "unit": "months / yen / null"},
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
def _pdf_to_image_bytes_list(pdf_path: str, max_pages: int = 2) -> list[tuple[bytes, str]]:
    """
    PDF → 페이지별 (JPEG bytes, mime_type) 리스트.

    메모리 효율 최적화:
    - max_pages=2: 마이소크는 보통 1페이지, 안전 마진으로 2페이지까지만
    - scale=2.0 (약 144 DPI): 마이소크 분석에 충분, 200 DPI 대비 약 50% 절감
    - JPEG quality=85: PNG 대비 약 70% 크기 감소, AI 분석 정확도 영향 거의 없음
    - Streamlit Cloud 무료 1GB 메모리 환경에서 안정 작동
    """
    import pypdfium2 as pdfium

    results = []
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            bitmap = page.render(scale=2.0)  # 약 144 DPI (분석에 충분)
            pil_image = bitmap.to_pil()
            # RGBA → RGB 변환 (JPEG는 알파 채널 미지원)
            if pil_image.mode in ("RGBA", "LA", "P"):
                pil_image = pil_image.convert("RGB")
            buf = io.BytesIO()
            pil_image.save(buf, format="JPEG", quality=85, optimize=True)
            results.append((buf.getvalue(), "image/jpeg"))
            # 메모리 즉시 해제
            del bitmap, pil_image
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

    # Claude 이미지 블록 형식 (이미지는 매번 다름 → user 메시지)
    image_blocks = []
    for img_bytes, mime in image_data:
        encoded = base64.standard_b64encode(img_bytes).decode("utf-8")
        image_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": encoded},
        })
    # 이미지 + 짧은 user 지시 (실제 추출 규칙은 system에 캐싱)
    user_content = image_blocks + [
        {"type": "text", "text": "위 마이소크(物件図面)를 system 프롬프트의 추출 규칙에 따라 분석하여 JSON으로 출력하세요."}
    ]

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                # ⭐ Prompt Caching: 안정 추출 규칙(EXTRACTION_PROMPT)을 system에 두고 캐시
                #    매물 5개 병렬 중 4건은 cache hit → 비용 90%↓, TTFT↓
                system=[
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
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

    # ③ Hybrid: Gemini 우선, 자신도가 high가 아니면 Claude (품질 우선 모드)
    has_gemini_key = bool(gemini_api_key or os.getenv("GEMINI_API_KEY"))

    if has_gemini_key:
        try:
            gemini_result = _analyze_with_gemini(file_path, gemini_api_key, max_retries=2)
            actual_conf = _compute_actual_confidence(gemini_result)
            gemini_result["extraction_confidence"] = actual_conf

            # 자신도가 high면 Gemini 결과 채택 (무료)
            # low/medium이면 Claude로 재분석 (품질 우선 — 정확도 향상)
            if actual_conf == "high":
                gemini_result["_engine_used"] = "gemini"
                return gemini_result
            # high가 아니면 아래에서 Claude로 재시도
        except Exception:
            # Gemini 실패 → 조용히 Claude로 폴백
            pass

    # Claude 폴백
    result = _analyze_with_claude(file_path, model, api_key, max_retries)
    if has_gemini_key:
        result["_engine_used"] = "claude (정확도 향상 모드)"
    else:
        result["_engine_used"] = "claude (GEMINI_API_KEY 미설정)"
    return result


# ──────────────────────────────────────────────────────
# 에러 메시지를 사용자 친화적인 한국어로 변환
# ──────────────────────────────────────────────────────
def format_error_korean(e: Exception, filename: str = "") -> str:
    """
    API/시스템 에러를 직원이 알아보기 쉬운 한국어 메시지로 변환.
    어떻게 대처해야 할지까지 안내.
    """
    err = str(e).lower()
    prefix = f"[{filename}] " if filename else ""

    # 인증/권한
    if any(x in err for x in ("authentication", "401", "unauthorized", "invalid_api_key")):
        return (
            f"{prefix}🔑 **API 키 인증 실패**\n"
            "→ Streamlit Cloud Settings → Secrets 의 ANTHROPIC_API_KEY가 "
            "정확한지 확인하세요."
        )
    if any(x in err for x in ("permission", "403", "forbidden")):
        return (
            f"{prefix}🔒 **API 권한 부족**\n"
            "→ Anthropic 계정의 결제 상태 또는 API 키 권한을 확인하세요."
        )

    # 크레딧·결제
    if any(x in err for x in ("credit balance", "billing", "payment", "insufficient")):
        return (
            f"{prefix}💳 **API 잔액 부족** — 가장 흔한 원인입니다.\n"
            "→ https://console.anthropic.com → Billing 에서 크레딧 충전 ($5~)"
        )

    # 호출 한도
    if any(x in err for x in ("429", "rate", "quota", "too many requests")):
        return (
            f"{prefix}⏱️ **API 호출 한도 초과**\n"
            "→ 1~2분 기다린 후 다시 시도하세요. "
            "여러 명이 동시에 처리할 때 자주 발생합니다."
        )

    # 파일 크기
    if any(x in err for x in ("request_too_large", "413", "payload too large")):
        return (
            f"{prefix}📦 **파일 크기 초과**\n"
            "→ PDF는 30MB 이하로 줄여서 다시 업로드하세요. "
            "(PDF는 페이지 분리, 이미지는 화질 압축)"
        )

    # 서버 일시 장애
    if any(x in err for x in ("500", "502", "503", "529", "overloaded", "internal_server")):
        return (
            f"{prefix}🌐 **AI 서버 일시 장애**\n"
            "→ 1~2분 후 다시 시도하세요. Anthropic/Google 서버 측 이슈로 "
            "자동 복구됩니다."
        )

    # 타임아웃
    if any(x in err for x in ("timeout", "timed out", "deadline")):
        return (
            f"{prefix}⏰ **응답 시간 초과**\n"
            "→ 파일이 크거나 네트워크가 느릴 수 있습니다. 다시 시도하세요."
        )

    # JSON 파싱 (AI 응답 형식 오류)
    if any(x in err for x in ("json", "decode", "expecting value")):
        return (
            f"{prefix}🤖 **AI 응답 형식 오류**\n"
            "→ AI가 가끔 JSON 형식이 아닌 답을 보냅니다. "
            "다시 시도하면 보통 성공합니다."
        )

    # Gemini 키 미설정
    if "gemini_api_key" in err or ("gemini" in err and "미설정" in err):
        return (
            f"{prefix}🔑 **Gemini API 키 미설정**\n"
            "→ Streamlit Secrets에 GEMINI_API_KEY 추가하면 무료 분석 가능. "
            "지금은 Claude만 사용됩니다."
        )

    # 파일 형식 오류
    if any(x in err for x in ("지원하지 않는 형식", "unsupported", "invalid image")):
        return (
            f"{prefix}📄 **파일 형식 오류**\n"
            "→ JPG/PNG/WEBP/GIF/PDF만 지원합니다. 다른 형식이면 변환 필요."
        )

    # PDF 추출 실패
    if any(x in err for x in ("pdf", "page")) and "추출" in err:
        return (
            f"{prefix}📄 **PDF 페이지 추출 실패**\n"
            "→ PDF가 손상되었거나 스캔 품질이 낮을 수 있습니다. "
            "원본을 다시 받아서 시도하세요."
        )

    # 자신도 low (분석 결과는 나옴)
    if "분석 실패" in err and "재시도" in err:
        return (
            f"{prefix}🔁 **분석 재시도 한도 초과**\n"
            "→ 도면 품질이 좋지 않거나 API 일시 장애. "
            "1~2분 후 다시 시도하거나, 더 선명한 도면으로 교체하세요."
        )

    # 알 수 없는 에러
    return (
        f"{prefix}⚠️ **예상치 못한 오류**\n"
        f"→ 메시지: {str(e)[:200]}\n"
        "→ 다시 시도 후에도 같은 오류면 관리자에게 알려주세요."
    )
