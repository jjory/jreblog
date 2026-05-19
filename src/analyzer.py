"""
analyzer.py
─────────────────────────────────────────────────────────
일본 부동산 도면(マイソク/間取り図)을 Claude Opus 4.7 Vision API로
구조화된 JSON으로 변환하는 모듈.

지원 형식: JPG, JPEG, PNG, WEBP, GIF, PDF
 - PDF는 pypdfium2로 페이지를 고해상도 이미지로 변환 후 분석
   (pypdfium2는 별도 외부 프로그램 설치 불필요 → Windows에서 안정적)
"""

import base64
import io
import json
import re
import time
from pathlib import Path
from typing import Optional

import anthropic

# Opus 4.7 — 시각 정확도 98.5%, 마이소크처럼 정보 밀도 높은 이미지에 최적.
DEFAULT_MODEL = "claude-opus-4-7"

EXTRACTION_PROMPT = """\
あなたは日本の不動産マイソク（物件図面・間取り図）の解析専門家です。
提供された画像を精密に分析し、以下のスキーマに従って JSON のみを出力してください。
画像から読み取れない項目は null としてください。憶測や推測はしないこと。

# 重要な注意点
- 賃料表記が「8.5万円」の場合は 85000 として整数(円)で出力
- 「○○駅 徒歩○分」は最寄駅と分数を別フィールドに分離
- 設備アイコンも全て読み取ること (エアコン・バルコニー・洗濯機置場 等)
- 間取り表記 (1R, 1K, 1DK, 1LDK, 2K, 2LDK 等) は半角で

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

# 분석 가능한 이미지 확장자
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
SUPPORTED_EXTS = IMAGE_EXTS | {".pdf"}


def _pdf_to_image_blocks(pdf_path: str, max_pages: int = 3) -> list[dict]:
    """
    PDF를 고해상도 PNG 이미지로 변환해 Claude 이미지 블록 리스트로 반환.
    pypdfium2 사용 (외부 프로그램 설치 불필요).
    """
    import pypdfium2 as pdfium

    blocks = []
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            # scale 2.8 ≈ 200 DPI 수준. 마이소크의 작은 글씨까지 인식 가능
            bitmap = page.render(scale=2.8)
            pil_image = bitmap.to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            encoded = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": encoded,
                },
            })
    finally:
        pdf.close()

    if not blocks:
        raise ValueError("PDF에서 페이지를 추출하지 못했습니다.")
    return blocks


def _image_to_block(image_path: str) -> dict:
    """일반 이미지 파일을 Claude 이미지 블록으로 변환."""
    path = Path(image_path)
    ext = path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    }
    if ext not in media_types:
        raise ValueError(f"지원하지 않는 이미지 형식: {ext}")

    with open(path, "rb") as f:
        encoded = base64.standard_b64encode(f.read()).decode("utf-8")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_types[ext],
            "data": encoded,
        },
    }


def _build_image_blocks(file_path: str) -> list[dict]:
    """파일 경로 → Claude 이미지 블록 리스트 (PDF/이미지 자동 판별)."""
    ext = Path(file_path).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(
            f"지원하지 않는 형식: {ext}\n"
            f"지원 형식: JPG, PNG, WEBP, GIF, PDF"
        )
    if ext == ".pdf":
        return _pdf_to_image_blocks(file_path)
    return [_image_to_block(file_path)]


def _extract_json(text: str) -> dict:
    """Claude 응답에서 JSON 부분만 안전하게 추출."""
    text = text.strip()
    # 코드 블록 제거
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON을 찾을 수 없습니다:\n{text[:300]}")
    return json.loads(text[start : end + 1])


def analyze_property_sheet(
    file_path: str,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    max_retries: int = 3,
) -> dict:
    """
    마이소크 이미지/PDF → 구조화된 부동산 데이터(dict)

    Args:
        file_path: 마이소크 파일 경로 (JPG/PNG/WEBP/GIF/PDF)
        model: 사용할 Claude 모델
        api_key: Anthropic API 키 (None이면 환경변수 사용)
        max_retries: 실패 시 재시도 횟수

    Returns:
        구조화된 부동산 정보 딕셔너리
    """
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    image_blocks = _build_image_blocks(file_path)

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
            # 과부하·일시적 오류는 재시도
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

    raise RuntimeError(f"도면 분석 실패 (재시도 {max_retries}회 초과): {last_error}")
