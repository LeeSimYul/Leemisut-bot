"""
migrate_to_cloud.py
조교 이미숫 - 로컬 SQLite(data/imisut.db) → 클라우드 PostgreSQL 일괄 이전 스크립트

■ 사용법
    python migrate_to_cloud.py --dry-run     무엇을 옮길지 세어만 봅니다. (아무것도 쓰지 않음)
    python migrate_to_cloud.py               실제로 옮깁니다.
    python migrate_to_cloud.py --verify-only 이미 옮긴 결과가 맞는지 비교만 합니다.
    python migrate_to_cloud.py --sqlite data/백업.db --yes

■ 무엇을 옮기나요?
    sign_words(수어 단어) · users(포인트 · 출석) · quiz_logs(퀴즈 기록) · user_bookmarks(단어장)
    · user_quiz_notes(오답노트) 다섯 테이블을 전부 옮깁니다. (원본에 없는 테이블 · 컬럼은 건너뜁니다) word_id · log_id 같은 ID도 그대로 옮겨서,
    퀴즈 기록과 단어장이 가리키는 단어가 어긋나지 않게 합니다.

■ 안전장치
    · 원본 SQLite 는 읽기 전용으로만 엽니다. (원본은 절대 바뀌지 않습니다)
    · 여러 번 실행해도 안전합니다. 이미 있는 행은 ON CONFLICT DO NOTHING 으로 건너뜁니다.
    · 옮긴 뒤 시퀀스를 최대 ID 에 맞춰, 봇이 새로 넣는 행의 ID 가 충돌하지 않게 합니다.
    · 대상 DB에 이미 데이터가 있으면 한 번 더 물어봅니다. (--yes 로 건너뛸 수 있습니다)

■ 옮긴 다음
    .env 의 DATABASE_URL 을 그대로 두고 봇을 켜면 클라우드 DB로 붙습니다.
    로컬 파일로 되돌리려면 DATABASE_URL 을 주석 처리하면 됩니다. (데이터는 양쪽에 그대로 남습니다)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from dotenv import load_dotenv

from database import DATABASE_URL_ENV, Database

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_PATH = BASE_DIR / "data" / "imisut.db"
DEFAULT_BATCH_SIZE = 500

log = logging.getLogger("migrate")


@dataclass(frozen=True)
class TableSpec:
    """옮길 테이블 하나. columns 순서대로 읽어 그대로 넣습니다."""

    name: str
    columns: tuple[str, ...]
    id_column: str | None = None  # 시퀀스를 맞춰야 하는 자동 증가 ID
    check_sums: tuple[str, ...] = field(default=())  # 옮긴 뒤 합계까지 비교할 컬럼


# 순서 주의: 단어를 먼저 넣어야 기록 · 단어장이 가리킬 대상이 생깁니다.
TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        "sign_words",
        ("word_id", "word_name", "meaning", "video_url", "image_url", "category", "detail_url"),
        id_column="word_id",
    ),
    TableSpec(
        "users",
        ("user_id", "points", "exp", "level", "streak", "last_quiz_date", "last_daily_date"),
        check_sums=("points", "exp"),
    ),
    TableSpec(
        "quiz_logs",
        ("log_id", "user_id", "word_id", "is_correct", "solved_at", "quiz_type"),
        id_column="log_id",
    ),
    TableSpec(
        "user_bookmarks",
        ("user_id", "word_id", "created_at"),
    ),
    TableSpec(
        "user_quiz_notes",
        ("id", "user_id", "word_id", "wrong_count", "is_mastered", "last_wrong_at", "mastered_at"),
        id_column="id",
    ),
)


def insert_sql(table: str, columns: Sequence[str]) -> str:
    """
    이전용 INSERT 문. 이미 있는 행(같은 ID · 같은 단어+영상 등)은 건너뜁니다.
    자리표시자는 ? 로 쓰고, database.py 의 백엔드가 PostgreSQL 용 $1, $2 … 로 바꿔 줍니다.
    """
    placeholders = ", ".join("?" * len(columns))
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    )


def sequence_sql(table: str, id_column: str) -> str:
    """
    자동 증가 ID의 다음 값을 '지금 들어 있는 최대 ID + 1' 로 맞추는 문장.
    이렇게 해 두지 않으면 봇이 새 단어를 넣을 때 1번부터 다시 시작해 충돌합니다.
    """
    return (
        f"SELECT setval(pg_get_serial_sequence('{table}', '{id_column}'), "
        f"COALESCE((SELECT MAX({id_column}) FROM {table}), 0) + 1, false)"
    )


class MigrationTarget(Protocol):
    """옮겨 넣을 곳. (테스트에서는 가짜 대상을 끼워 넣습니다)"""

    async def ensure_schema(self) -> None: ...

    async def insert_many(
        self, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
    ) -> None: ...

    async def count(self, table: str) -> int: ...

    async def sum_column(self, table: str, column: str) -> int: ...

    async def sync_sequence(self, table: str, id_column: str) -> None: ...

    def describe(self) -> str: ...


class PostgresTarget:
    """실제 클라우드 PostgreSQL. database.py 의 백엔드를 그대로 재사용합니다."""

    def __init__(self, dsn: str) -> None:
        # seed_mock=False : 더미 단어 4개가 먼저 들어가 ID가 밀리는 일을 막습니다.
        self.db = Database(dsn=dsn, seed_mock=False)

    async def __aenter__(self) -> PostgresTarget:
        await self.db.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.db.close()

    async def ensure_schema(self) -> None:
        pass  # connect() 안의 init_db() 가 이미 테이블을 만들어 둡니다

    async def insert_many(
        self, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
    ) -> None:
        async with self.db.backend.transaction():
            await self.db.backend.execute_many(insert_sql(table, columns), rows)

    async def count(self, table: str) -> int:
        row = await self.db.backend.fetch_one(f"SELECT COUNT(*) AS cnt FROM {table}")
        return int(row["cnt"]) if row else 0

    async def sum_column(self, table: str, column: str) -> int:
        row = await self.db.backend.fetch_one(
            f"SELECT COALESCE(SUM({column}), 0) AS total FROM {table}"
        )
        return int(row["total"]) if row else 0

    async def sync_sequence(self, table: str, id_column: str) -> None:
        await self.db.backend.execute(sequence_sql(table, id_column))

    def describe(self) -> str:
        return self.db.describe()


@dataclass
class TableReport:
    """테이블 한 개의 이전 결과."""

    name: str
    source_rows: int
    target_before: int
    target_after: int
    sums: dict[str, tuple[int, int]] = field(default_factory=dict)  # 컬럼: (원본, 대상)

    @property
    def inserted(self) -> int:
        return self.target_after - self.target_before

    @property
    def skipped(self) -> int:
        return max(0, self.source_rows - self.inserted)

    @property
    def ok(self) -> bool:
        """대상에 원본 이상이 들어 있고, 합계 비교도 맞는지."""
        counts_ok = self.target_after >= self.source_rows
        sums_ok = all(source == target for source, target in self.sums.values())
        return counts_ok and sums_ok


def read_sqlite_table(conn: sqlite3.Connection, spec: TableSpec) -> tuple[list[str], list[tuple]]:
    """
    원본에서 한 테이블을 읽습니다. 반환: (실제로 읽은 컬럼, 행 목록)

    아주 예전 DB에 없는 컬럼이 있을 수 있으니, 원본에 실제로 있는 컬럼만 골라 옮깁니다.
    (빠진 컬럼은 대상 테이블의 기본값이 들어갑니다)
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({spec.name})")}
    if not existing:
        return [], []  # 테이블 자체가 없는 아주 옛 DB

    columns = [column for column in spec.columns if column in existing]
    rows = conn.execute(f"SELECT {', '.join(columns)} FROM {spec.name}").fetchall()
    return columns, [tuple(row) for row in rows]


def open_source(path: Path) -> sqlite3.Connection:
    """원본 SQLite 를 읽기 전용으로 엽니다. (실수로 쓰는 일을 막습니다)"""
    if not path.exists():
        raise SystemExit(f"❌ 원본 DB를 찾지 못했습니다: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


async def migrate(
    source_path: Path,
    target: MigrationTarget,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    verify_only: bool = False,
) -> list[TableReport]:
    """원본 SQLite 를 읽어 대상(PostgreSQL)에 넣고, 테이블별 결과를 돌려줍니다."""
    conn = open_source(source_path)
    conn.row_factory = None
    reports: list[TableReport] = []

    try:
        await target.ensure_schema()

        for spec in TABLES:
            columns, rows = read_sqlite_table(conn, spec)
            before = await target.count(spec.name)

            if rows and columns and not dry_run and not verify_only:
                for start in range(0, len(rows), batch_size):
                    batch = rows[start:start + batch_size]
                    await target.insert_many(spec.name, columns, batch)
                    log.info(
                        "  %s: %d/%d 건 전송", spec.name, min(start + batch_size, len(rows)), len(rows)
                    )
                if spec.id_column:
                    await target.sync_sequence(spec.name, spec.id_column)

            after = before if (dry_run or verify_only) else await target.count(spec.name)
            report = TableReport(spec.name, len(rows), before, after)

            for column in spec.check_sums:
                if column not in columns:
                    continue
                source_total = conn.execute(
                    f"SELECT COALESCE(SUM({column}), 0) FROM {spec.name}"
                ).fetchone()[0]
                report.sums[column] = (int(source_total or 0), await target.sum_column(spec.name, column))

            reports.append(report)
    finally:
        conn.close()

    return reports


def print_report(reports: list[TableReport], *, dry_run: bool, verify_only: bool) -> bool:
    """결과를 표로 보여 주고, 전부 정상인지 돌려줍니다."""
    if dry_run:
        title = "🔍 미리보기 (아무것도 쓰지 않았습니다)"
    elif verify_only:
        title = "🔎 대조 결과"
    else:
        title = "📦 이전 결과"

    print(f"\n{title}")
    print(f"{'테이블':<16}{'원본':>8}{'옮기기 전':>10}{'옮긴 후':>9}{'새로 들어감':>12}   상태")
    print("-" * 68)

    all_ok = True
    for report in reports:
        state = "✅" if report.ok else "⚠️"
        if not report.ok:
            all_ok = False
        print(
            f"{report.name:<16}{report.source_rows:>8}{report.target_before:>10}"
            f"{report.target_after:>9}{report.inserted:>12}   {state}"
        )
        for column, (source_total, target_total) in report.sums.items():
            mark = "✅" if source_total == target_total else "⚠️"
            print(f"  └ {column} 합계: 원본 {source_total} · 대상 {target_total} {mark}")

    if dry_run:
        print("\n미리보기였습니다. 실제로 옮기려면 --dry-run 없이 다시 실행해 주세요.")
    elif all_ok:
        print("\n🎉 모든 테이블이 정상적으로 대조되었습니다.")
        print("   이제 .env 의 DATABASE_URL 을 그대로 두고 봇을 켜면 클라우드 DB를 씁니다.")
    else:
        print("\n⚠️ 숫자가 맞지 않는 테이블이 있습니다. 위 표의 ⚠️ 줄을 확인해 주세요.")
        print("   (이미 대상에 다른 데이터가 있었다면 '옮기기 전' 숫자가 0이 아닐 수 있습니다)")

    return all_ok


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="로컬 SQLite 의 이미숫 봇 데이터를 클라우드 PostgreSQL 로 옮깁니다.",
    )
    parser.add_argument(
        "--sqlite", type=Path, default=DEFAULT_SQLITE_PATH, help="원본 SQLite 파일 (기본: data/imisut.db)"
    )
    parser.add_argument(
        "--dsn", default=None, help=f"대상 PostgreSQL 주소 (기본: .env 의 {DATABASE_URL_ENV})"
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="한 번에 보낼 행 수 (기본 500)"
    )
    parser.add_argument("--dry-run", action="store_true", help="세어만 보고 쓰지 않습니다")
    parser.add_argument("--verify-only", action="store_true", help="옮기지 않고 숫자만 대조합니다")
    parser.add_argument("--yes", action="store_true", help="확인 질문을 건너뜁니다")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    load_dotenv(BASE_DIR / ".env")
    import os

    dsn = args.dsn or os.getenv(DATABASE_URL_ENV, "")
    if not dsn.strip():
        print(
            f"❌ 대상 주소가 없습니다. .env 에 {DATABASE_URL_ENV} 을 적거나 --dsn 으로 넘겨 주세요.\n"
            "   예) DATABASE_URL=postgresql://postgres:비밀번호@db.xxxx.supabase.co:5432/postgres"
        )
        return 2

    print(f"📤 원본: SQLite {args.sqlite}")
    async with PostgresTarget(dsn) as target:
        print(f"📥 대상: {target.describe()}")

        if not args.dry_run and not args.verify_only and not args.yes:
            existing = sum([await target.count(spec.name) for spec in TABLES])
            if existing:
                print(f"\n⚠️ 대상 DB에 이미 {existing}행이 있습니다. (이미 있는 행은 건너뜁니다)")
                if not sys.stdin.isatty():
                    print("   확인을 받을 수 없어 중단합니다. 계속하려면 --yes 를 붙여 주세요.")
                    return 3
                if input("   계속할까요? [y/N] ").strip().lower() not in {"y", "yes"}:
                    print("   취소했습니다.")
                    return 1

        reports = await migrate(
            args.sqlite,
            target,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )

    return 0 if print_report(reports, dry_run=args.dry_run, verify_only=args.verify_only) else 4


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
