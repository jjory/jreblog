"""
db.py
─────────────────────────────────────────────────────────
物件DB (제안용 리스트) — Render Postgres 저장 모듈

역할:
- 환경변수 DATABASE_URL 로 Postgres 에 접속
- 매물 테이블(properties) 생성/관리
- 매물 1건 저장(upsert) / 불러오기 / 목록 / 오래된 것 삭제

핵심 정책:
- 추출 결과(구조화 데이터)는 data(JSONB) 에 통째로 저장 → 재추출 0의 기반
- 직원 수동 입력값은 manual_fields(JSONB) 에 **따로** 저장
  → 같은 도면을 재추출해도 손으로 넣은 사진링크/시키킹 등이 보존됨
- 중복 방지: drive_file_id 로 "이미 추출했는지" 확인
- 정렬: property_number(매물번호) 내림차순 = 최신이 상단

필수 환경변수:
  DATABASE_URL — Render Postgres 의 Internal Database URL

DB 가 설정 안 됐거나 접속 실패 시:
  - is_configured() == False
  - 호출자는 이를 보고 "DB 미연결" 안내를 띄우면 됨 (앱이 죽지 않음)
"""

import os
import json
from contextlib import contextmanager
from typing import Optional

# psycopg(버전 3)는 requirements.txt 의 psycopg[binary] 로 설치됨
try:
    import psycopg
    from psycopg.types.json import Jsonb
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


# ── 테이블 이름 ───────────────────────────────────────────
_TABLE = "properties"

# ── 테이블 생성 SQL ──────────────────────────────────────
# data         : analyzer.py 추출 결과(구조화 JSON) 통째
# manual_fields: 직원 수동 입력(사진링크/시키킹 보정 등) — 재추출해도 보존
_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id              BIGSERIAL PRIMARY KEY,
    property_number TEXT,
    filename        TEXT,
    drive_file_id   TEXT UNIQUE,
    data            JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    manual_fields   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    source          TEXT,
    is_closed       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_prop_num ON {_TABLE} (property_number);
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_created  ON {_TABLE} (created_at);
"""


def is_configured() -> bool:
    """DB 사용 가능 여부 — 라이브러리 설치 + DATABASE_URL 둘 다 있어야 함."""
    return _PSYCOPG_AVAILABLE and bool(os.getenv("DATABASE_URL", "").strip())


def _get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다.")
    return dsn


@contextmanager
def _connect():
    """
    매 작업마다 새 연결을 열고 닫음 (저사용량에선 가장 단순·안전).
    autocommit=True 로 두어 DDL/단건 작업이 즉시 반영되게 함.
    """
    if not _PSYCOPG_AVAILABLE:
        raise RuntimeError("psycopg 라이브러리가 설치되지 않았습니다. requirements.txt 확인.")
    conn = psycopg.connect(_get_dsn(), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """앱 시작 시 1회 호출 — 테이블이 없으면 만든다 (있으면 그대로 둠)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)


def healthcheck() -> dict:
    """
    연결·테이블 상태 점검 (관리자 UI/테스트용).
    Returns:
        {"configured": bool, "connected": bool, "table_ready": bool,
         "row_count": int, "error": str|None}
    """
    result = {"configured": is_configured(), "connected": False,
              "table_ready": False, "row_count": 0, "error": None}
    if not result["configured"]:
        result["error"] = "DATABASE_URL 미설정 또는 psycopg 미설치"
        return result
    try:
        with _connect() as conn:
            result["connected"] = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass(%s) IS NOT NULL", (f"public.{_TABLE}",)
                )
                result["table_ready"] = bool(cur.fetchone()[0])
                if result["table_ready"]:
                    cur.execute(f"SELECT COUNT(*) FROM {_TABLE}")
                    result["row_count"] = int(cur.fetchone()[0])
    except Exception as e:
        result["error"] = str(e)
    return result


def save_property(
    property_number: str = "",
    filename: str = "",
    drive_file_id: Optional[str] = None,
    data: Optional[dict] = None,
    source: str = "",
) -> int:
    """
    매물 1건 저장 (upsert).
    - drive_file_id 가 있고 이미 있으면 → data/filename/property_number 갱신,
      단 manual_fields(직원 수동입력)는 건드리지 않음 (보존).
    - drive_file_id 가 없으면 → 새 행으로 삽입.
    Returns: 저장된 행의 id
    """
    data = data or {}
    with _connect() as conn:
        with conn.cursor() as cur:
            if drive_file_id:
                cur.execute(
                    f"""
                    INSERT INTO {_TABLE}
                        (property_number, filename, drive_file_id, data, source, updated_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (drive_file_id) DO UPDATE SET
                        property_number = EXCLUDED.property_number,
                        filename        = EXCLUDED.filename,
                        data            = EXCLUDED.data,
                        source          = EXCLUDED.source,
                        updated_at      = now()
                    RETURNING id
                    """,
                    (property_number, filename, drive_file_id, Jsonb(data), source),
                )
            else:
                cur.execute(
                    f"""
                    INSERT INTO {_TABLE}
                        (property_number, filename, data, source, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    RETURNING id
                    """,
                    (property_number, filename, Jsonb(data), source),
                )
            return int(cur.fetchone()[0])


def update_manual_fields(row_id: int, manual_fields: dict) -> None:
    """직원 수동 입력값 저장/갱신 (사진링크·시키킹 보정 등). 추출 데이터는 안 건드림."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {_TABLE} SET manual_fields = %s, updated_at = now() WHERE id = %s",
                (Jsonb(manual_fields or {}), row_id),
            )


def drive_file_exists(drive_file_id: str) -> bool:
    """이미 추출한 드라이브 파일인지 확인 (중복 추출 방지용)."""
    if not drive_file_id:
        return False
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {_TABLE} WHERE drive_file_id = %s LIMIT 1",
                (drive_file_id,),
            )
            return cur.fetchone() is not None


def _row_to_dict(row, cols) -> dict:
    return {col: row[i] for i, col in enumerate(cols)}


_SELECT_COLS = (
    "id", "property_number", "filename", "drive_file_id",
    "data", "manual_fields", "source", "is_closed", "created_at", "updated_at",
)


def get_property(row_id: int) -> Optional[dict]:
    """매물 1건 불러오기 (id 기준)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_SELECT_COLS)} FROM {_TABLE} WHERE id = %s",
                (row_id,),
            )
            row = cur.fetchone()
            return _row_to_dict(row, _SELECT_COLS) if row else None


def list_properties(include_closed: bool = False, limit: int = 1000) -> list:
    """
    매물 목록 — 매물번호(property_number) 내림차순 = 최신 상단.
    include_closed=False 면 모집종료(is_closed=True) 매물은 제외.
    """
    where = "" if include_closed else "WHERE is_closed = FALSE"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(_SELECT_COLS)} FROM {_TABLE}
                {where}
                ORDER BY property_number DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [_row_to_dict(r, _SELECT_COLS) for r in cur.fetchall()]


def mark_closed(row_id: int, closed: bool = True) -> None:
    """모집종료 체크/해제."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {_TABLE} SET is_closed = %s, updated_at = now() WHERE id = %s",
                (closed, row_id),
            )


def delete_property(row_id: int) -> None:
    """매물 1건 삭제."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE} WHERE id = %s", (row_id,))


def delete_old(days: int = 14) -> int:
    """
    오래된 매물 자동삭제 (기본 2주).
    Returns: 삭제된 건수
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {_TABLE} WHERE created_at < now() - (%s || ' days')::interval",
                (str(days),),
            )
            return cur.rowcount


# ── 단독 실행 시 자가 테스트 ─────────────────────────────
# 로컬/Render Shell 에서  python -m src.db  로 실행하면
# 연결 → 테이블 생성 → 저장 → 조회 → 삭제 까지 한 번에 점검한다.
if __name__ == "__main__":
    print("=== db.py 자가 테스트 ===")
    print("1) is_configured:", is_configured())
    if not is_configured():
        print("   ⚠️ DATABASE_URL 미설정 — Render Shell 또는 .env 에서 설정 후 다시 실행하세요.")
        raise SystemExit(0)

    print("2) init_db (테이블 생성)...")
    init_db()
    print("   OK")

    print("3) healthcheck:", healthcheck())

    print("4) 테스트 매물 저장...")
    test_id = save_property(
        property_number="9999999",
        filename="9999999_테스트도면.jpg",
        drive_file_id="TEST_DRIVE_ID_DELETE_ME",
        data={"rent_yen": 80000, "layout": "1K", "address": "東京都新宿区테스트"},
        source="selftest",
    )
    print("   저장된 id:", test_id)

    print("5) 불러오기:", get_property(test_id))

    print("6) 수동입력 저장...")
    update_manual_fields(test_id, {"photo_link": "https://example.com/test"})
    print("   다시 불러오기:", get_property(test_id))

    print("7) 중복 확인 (True 나와야 정상):", drive_file_exists("TEST_DRIVE_ID_DELETE_ME"))

    print("8) 테스트 매물 삭제...")
    delete_property(test_id)
    print("   삭제 후 조회 (None 나와야 정상):", get_property(test_id))

    print("=== 테스트 완료 — 모두 정상이면 위에 오류 없이 출력됨 ===")
