"""
database.py
조교 이미숫 - 비동기 데이터베이스 모듈 (클라우드 PostgreSQL / 로컬 SQLite 이중 지원)

■ 어디에 연결되나요?
    .env 의 DATABASE_URL 이 있으면 → 클라우드 PostgreSQL (asyncpg 커넥션 풀)
    없으면                        → 로컬 SQLite 파일 (aiosqlite, 기본 data/imisut.db)
  바깥에서 쓰는 방법(Database 의 메서드)은 두 경우가 완전히 같습니다. cogs 는 고치지 않아도 됩니다.

■ 왜 이중 지원인가요?
    자취집 PC · 고향 PC · 클라우드 호스팅을 옮겨 다녀도 유저 포인트와 학습 기록이 한곳에 모이도록
    클라우드 DB를 쓰고, 계정이나 인터넷 없이도 봇을 켤 수 있게 로컬 SQLite 도 그대로 남겨 둡니다.

■ 행(row) 읽는 방법
    aiosqlite.Row 와 asyncpg.Record 는 둘 다 row["컬럼명"] 으로 읽을 수 있어서,
    조회 결과를 쓰는 쪽(cogs) 코드는 그대로 동작합니다.
    ⚠️ COUNT(*) 처럼 이름이 없는 값은 반드시 AS 로 이름을 붙여 주세요. (두 DB의 기본 이름이 다릅니다)

■ SQL 은 한 벌만 씁니다
    자리표시자는 ? 로 쓰고, PostgreSQL 로 보낼 때 $1, $2 … 로 자동 변환합니다. (_to_dialect)
    PostgreSQL 에만 필요한 타입 캐스트는 ?::text 처럼 적으면 SQLite 에서는 캐스트가 떨어집니다.

■ sign_words 스키마
    word_id    단어 ID (PK)
    word_name  단어명 (중복 허용 - 동음이의어)
    meaning    수어 설명 전문
    video_url  동영상 파일 주소
    image_url  수형 사진 주소 (임베드에 직접 표시)
    category   분류항목
    detail_url 사전 상세 페이지 (퀴즈 중에는 숨김)
    UNIQUE(word_name, video_url)
      → '배(사물)'과 '배(신체)'처럼 영상이 다르면 각각 저장됩니다.

■ quiz_logs      퀴즈 풀이 기록 (log_id, user_id, word_id, is_correct, solved_at)
■ user_bookmarks 나만의 단어장   (user_id, word_id, created_at)  UNIQUE(user_id, word_id)
    → 두 테이블 모두 sign_words 와 JOIN 해서 읽으므로 삭제된 단어는 자동으로 빠집니다.
    → 외래 키(FOREIGN KEY)는 일부러 걸지 않았습니다. 단어를 지워도 퀴즈 기록은 이력으로 남겨야 합니다.

■ 인메모리 캐시 (_cache)
    전체 단어 수 · 단어명 기준 수 · 분류 목록은 처음 한 번만 세고 메모리에 보관합니다.
    단어를 추가/수정/삭제하는 메서드가 끝나면 캐시를 비우고, 다음 조회 때 다시 채웁니다.
    ⚠️ 여러 PC에서 같은 클라우드 DB에 동시에 붙으면 다른 PC가 바꾼 단어 수가 이 캐시에
       바로 반영되지 않습니다. (봇은 한 번에 한 곳에서만 켜 주세요)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiosqlite

log = logging.getLogger(__name__)

T = TypeVar("T")

# aiosqlite.Row 와 asyncpg.Record 를 함께 가리키는 별칭 (둘 다 row["컬럼명"] 지원)
Row = Any

KST = timezone(timedelta(hours=9))  # 출석 · 일일 보상 기준 (cogs 와 같은 기준)

# LIKE 검색어 최대 길이. 수어 단어명은 길어야 20자 안팎이라 넉넉한 값입니다.
# 패턴이 길수록 행마다 비교 비용이 커지므로 이보다 긴 입력은 잘라서 씁니다.
MAX_LIKE_KEYWORD_LENGTH = 50

# 관리자 명령어 2중 검증용 .env 키 (쉼표로 구분한 디스코드 유저 ID 목록)
ADMIN_USER_IDS_ENV = "ADMIN_USER_IDS"

# 클라우드 PostgreSQL 접속 문자열 .env 키 (없으면 로컬 SQLite)
DATABASE_URL_ENV = "DATABASE_URL"

# 커넥션 풀 크기. 디스코드 봇은 동시 처리량이 크지 않아 작게 잡아도 넉넉하고,
# 무료 플랜의 커넥션 수 제한(Supabase · Neon 모두 빡빡합니다)에도 안전합니다.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5
POOL_COMMAND_TIMEOUT = 30.0  # 초 - 한 질의가 이보다 오래 걸리면 끊습니다
# 초 - 이만큼 쉰 커넥션은 풀이 스스로 버리고 다음에 새로 맺습니다.
# 풀러(pgbouncer)나 중간 방화벽이 먼저 끊어 버리기 전에 우리가 정리하려는 값입니다.
POOL_IDLE_LIFETIME = 180.0
CONNECT_RETRIES = 3          # Neon 등은 절전에서 깨어나며 첫 연결이 실패할 수 있습니다
CONNECT_RETRY_DELAY = 2.0    # 초

# 저장할 컬럼 순서 (튜플 순서와 동일하게 유지해 주세요)
COLUMNS = ("word_name", "meaning", "video_url", "image_url", "category", "detail_url")

# 초기 테스트용 더미 데이터 (word_name, meaning, video_url, image_url, category, detail_url)
# ⚠️ /수어전체동기화 를 하면 실제 자료로 채워집니다.
MOCK_SIGN_WORDS: list[tuple[str, str, str, str, str, str]] = [
    ("안녕하세요", "만나거나 헤어질 때 반갑게 건네는 인사말이에요.",
     "https://example.com/signs/0001.mp4", "", "인사", ""),
    ("감사합니다", "고마운 마음을 전하는 표현이에요.",
     "https://example.com/signs/0002.mp4", "", "인사", ""),
    ("사랑합니다", "소중한 사람에게 애정을 표현하는 말이에요.",
     "https://example.com/signs/0003.mp4", "", "감정", ""),
    ("괜찮아요", "문제없다, 걱정하지 않아도 된다는 뜻이에요.",
     "https://example.com/signs/0004.mp4", "", "일상", ""),
]

# ── 스키마 ──────────────────────────────────────────────────────
# 두 DB의 문법이 달라 따로 적습니다. (PostgreSQL 쪽은 sql/schema_postgres.sql 과 같은 내용)
SQLITE_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id         INTEGER PRIMARY KEY,          -- 디스코드 유저 ID
        points          INTEGER NOT NULL DEFAULT 0,
        exp             INTEGER NOT NULL DEFAULT 0,
        level           INTEGER NOT NULL DEFAULT 1,
        streak          INTEGER NOT NULL DEFAULT 0,   -- 연속 출석일수
        last_quiz_date  TEXT,                         -- 'YYYY-MM-DD' (KST 기준)
        last_daily_date TEXT                          -- /오늘의수어 마지막 확인 날짜
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sign_words (
        word_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        word_name  TEXT    NOT NULL,                 -- 동음이의어 허용 (UNIQUE 아님)
        meaning    TEXT    NOT NULL,
        video_url  TEXT    NOT NULL,
        image_url  TEXT    NOT NULL DEFAULT '',
        category   TEXT    NOT NULL DEFAULT '일반',
        detail_url TEXT    NOT NULL DEFAULT '',
        UNIQUE(word_name, video_url)                 -- 같은 단어라도 영상이 다르면 별개
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sign_words_name ON sign_words(word_name)",
    """
    CREATE TABLE IF NOT EXISTS quiz_logs (
        log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,                 -- 디스코드 유저 ID
        word_id    INTEGER NOT NULL,                 -- sign_words.word_id
        is_correct INTEGER NOT NULL,                 -- 1 정답 / 0 오답 · 시간 초과
        solved_at  TEXT    NOT NULL                  -- ISO 8601 (UTC)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quiz_logs_user_word ON quiz_logs(user_id, word_id)",
    "CREATE INDEX IF NOT EXISTS idx_quiz_logs_user_solved ON quiz_logs(user_id, solved_at)",
    """
    CREATE TABLE IF NOT EXISTS user_bookmarks (
        user_id    INTEGER NOT NULL,
        word_id    INTEGER NOT NULL,                 -- sign_words.word_id
        created_at TEXT    NOT NULL,                 -- ISO 8601 (UTC)
        UNIQUE(user_id, word_id)
    )
    """,
)

POSTGRES_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id         BIGINT  PRIMARY KEY,
        points          INTEGER NOT NULL DEFAULT 0,
        exp             INTEGER NOT NULL DEFAULT 0,
        level           INTEGER NOT NULL DEFAULT 1,
        streak          INTEGER NOT NULL DEFAULT 0,
        last_quiz_date  TEXT,
        last_daily_date TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sign_words (
        word_id    BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        word_name  TEXT NOT NULL,
        meaning    TEXT NOT NULL,
        video_url  TEXT NOT NULL,
        image_url  TEXT NOT NULL DEFAULT '',
        category   TEXT NOT NULL DEFAULT '일반',
        detail_url TEXT NOT NULL DEFAULT '',
        CONSTRAINT sign_words_name_video_key UNIQUE (word_name, video_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sign_words_name ON sign_words(word_name)",
    """
    CREATE TABLE IF NOT EXISTS quiz_logs (
        log_id     BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        user_id    BIGINT   NOT NULL,
        word_id    BIGINT   NOT NULL,
        is_correct SMALLINT NOT NULL,
        solved_at  TEXT     NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quiz_logs_user_word ON quiz_logs(user_id, word_id)",
    "CREATE INDEX IF NOT EXISTS idx_quiz_logs_user_solved ON quiz_logs(user_id, solved_at)",
    """
    CREATE TABLE IF NOT EXISTS user_bookmarks (
        user_id    BIGINT NOT NULL,
        word_id    BIGINT NOT NULL,
        created_at TEXT   NOT NULL,
        CONSTRAINT user_bookmarks_user_word_key UNIQUE (user_id, word_id)
    )
    """,
)


def load_admin_user_ids() -> frozenset[int]:
    """
    .env 의 ADMIN_USER_IDS (쉼표로 구분한 디스코드 유저 ID 목록)를 읽습니다.
        ADMIN_USER_IDS=123456789012345678,234567890123456789

    - 비어 있거나 없으면 빈 집합 → 관리자 명령어는 '서버 관리자 권한'만 확인합니다.
    - 숫자가 아닌 항목은 경고를 남기고 건너뜁니다.
    - 값은 적혀 있는데 올바른 ID가 하나도 없으면 ValueError 를 냅니다.
      (오타 하나로 2중 검증이 조용히 꺼지는 일을 막기 위해서입니다)

    ⚠️ main.py 의 load_dotenv() 이후에 호출해야 합니다. 모듈 상단에서 미리 읽으면
       .env 가 아직 로드되지 않아 항상 빈 값이 됩니다.
    """
    raw = os.getenv(ADMIN_USER_IDS_ENV, "")
    ids: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue  # "123,,456" 이나 끝의 쉼표는 조용히 무시
        if token.isascii() and token.isdigit() and int(token) > 0:
            ids.add(int(token))
        else:
            log.warning("%s 에 올바르지 않은 항목이 있어 건너뜁니다: %r", ADMIN_USER_IDS_ENV, token)

    if raw.strip() and not ids:
        raise ValueError(
            f"{ADMIN_USER_IDS_ENV} 값({raw!r})에서 올바른 디스코드 유저 ID를 찾지 못했습니다. "
            "숫자 ID를 쉼표로 구분해 적거나, 서버 관리자 권한만 쓰시려면 비워 두세요."
        )
    return frozenset(ids)


def _now_iso() -> str:
    """
    기록 시각 (UTC, ISO 8601). 예: 2026-09-27T03:04:05.123456+00:00

    마이크로초까지 남기는 이유: 같은 초에 담은 단어장 항목의 순서를 created_at 만으로
    가릴 수 있게 하기 위해서입니다. (SQLite 의 rowid 는 PostgreSQL 에 없습니다)
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ── SQL 방언 변환 ───────────────────────────────────────────────
_CAST_PATTERN = re.compile(r"::(?:text|bigint|int|smallint)\b")


def _split_literals(sql: str) -> list[tuple[str, bool]]:
    """SQL을 (조각, 문자열 리터럴인가) 목록으로 자릅니다. ('...' 안은 건드리지 않기 위해)"""
    parts: list[tuple[str, bool]] = []
    buffer: list[str] = []
    index, length = 0, len(sql)

    while index < length:
        if sql[index] != "'":
            buffer.append(sql[index])
            index += 1
            continue

        parts.append(("".join(buffer), False))
        buffer = []
        end = index + 1
        while end < length:
            if sql[end] == "'":
                if end + 1 < length and sql[end + 1] == "'":  # '' 는 리터럴 안의 따옴표
                    end += 2
                    continue
                break
            end += 1
        parts.append((sql[index:end + 1], True))
        index = end + 1

    parts.append(("".join(buffer), False))
    return parts


def _to_dialect(sql: str, dialect: str) -> str:
    r"""
    한 벌로 쓴 SQL을 각 DB 문법에 맞게 바꿉니다.
      - PostgreSQL: 자리표시자 ? → $1, $2 … (asyncpg 는 번호 방식만 받습니다)
      - SQLite:     PostgreSQL 전용 타입 캐스트(::text 등)를 떼어 냅니다.
    문자열 리터럴 안의 ? 와 :: 는 그대로 둡니다. (예: ESCAPE '\' 또는 '물음표?')
    """
    pieces: list[str] = []
    counter = 0
    for text, is_literal in _split_literals(sql):
        if is_literal:
            pieces.append(text)
        elif dialect == "postgres":
            chunk: list[str] = []
            for char in text:
                if char == "?":
                    counter += 1
                    chunk.append(f"${counter}")
                else:
                    chunk.append(char)
            pieces.append("".join(chunk))
        else:
            pieces.append(_CAST_PATTERN.sub("", text))
    return "".join(pieces)


# ── 백엔드 ──────────────────────────────────────────────────────
class Backend(ABC):
    """DB 한 종류와 이야기하는 최소한의 창구. SQL은 ? 자리표시자로 받습니다."""

    dialect: str = ""
    like_operator: str = "LIKE"  # 부분 일치 검색에 쓸 연산자
    schema: tuple[str, ...] = ()

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]: ...

    @abstractmethod
    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None: ...

    @abstractmethod
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """실행하고 바뀐 행 수를 돌려줍니다."""

    @abstractmethod
    async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None: ...

    @abstractmethod
    async def insert_returning_id(
        self, sql: str, params: Sequence[Any], id_column: str
    ) -> int:
        """INSERT 하고 새로 만들어진 ID를 돌려줍니다. (SQLite: lastrowid / PG: RETURNING)"""

    @abstractmethod
    async def execute_script(self, statements: Sequence[str]) -> None:
        """DDL 여러 개를 순서대로 실행합니다."""

    @abstractmethod
    def transaction(self) -> Any:
        """async with 로 쓰는 트랜잭션. 여러 문장을 한 번에 확정합니다. (중첩 허용)"""

    @abstractmethod
    def describe(self) -> str:
        """로그에 남길 사람이 읽을 설명. 비밀번호는 절대 넣지 않습니다."""

    def sql(self, sql: str) -> str:
        return _to_dialect(sql, self.dialect)


class SQLiteBackend(Backend):
    """로컬 파일 DB. 계정이나 인터넷 없이 바로 쓸 수 있는 기본값입니다."""

    dialect = "sqlite"
    like_operator = "LIKE"  # SQLite 의 LIKE 는 ASCII 대소문자를 구분하지 않습니다
    schema = SQLITE_SCHEMA

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        # 커넥션이 하나뿐이라 트랜잭션은 한 번에 하나만 열 수 있습니다.
        # (동시에 BEGIN 을 두 번 보내면 "cannot start a transaction within a transaction")
        self._tx_lock = asyncio.Lock()
        # 같은 작업 안에서 트랜잭션이 중첩됐는지 표시. (중첩이면 BEGIN 을 다시 걸지 않습니다)
        self._depth: ContextVar[int] = ContextVar("sqlite_tx_depth", default=0)

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DB가 아직 연결되지 않았습니다. connect()를 먼저 호출하세요.")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None : 한 문장씩 바로 확정(autocommit). 여러 문장을 묶을 때만
        # transaction() 이 BEGIN/COMMIT 을 직접 걸어 PostgreSQL 쪽과 동작을 맞춥니다.
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row  # row["컬럼명"] 으로 접근 가능
        # WAL: 읽기와 쓰기가 서로를 막지 않습니다. (DB 파일에 저장되어 계속 유지)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # WAL 모드에서는 NORMAL 로도 DB가 깨지지 않으며, 커밋마다 디스크 동기화(fsync)를
        # 하지 않아 쓰기가 빨라집니다. (정전 시 마지막 몇 건의 커밋만 되돌려질 수 있음)
        # ※ synchronous 는 연결마다 기본값(FULL)으로 돌아가므로 connect() 에서 매번 설정합니다.
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        # 다른 프로세스가 쓰는 중이면 곧바로 실패하지 않고 잠시 기다립니다.
        await self._conn.execute("PRAGMA busy_timeout=5000")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self.conn.execute(self.sql(sql), tuple(params)) as cur:
            return list(await cur.fetchall())

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        async with self.conn.execute(self.sql(sql), tuple(params)) as cur:
            return await cur.fetchone()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = await self.conn.execute(self.sql(sql), tuple(params))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        await self.conn.executemany(self.sql(sql), [tuple(row) for row in rows])

    async def insert_returning_id(
        self, sql: str, params: Sequence[Any], id_column: str
    ) -> int:
        cur = await self.conn.execute(self.sql(sql), tuple(params))
        return int(cur.lastrowid or 0)

    async def execute_script(self, statements: Sequence[str]) -> None:
        for statement in statements:
            await self.conn.execute(statement)

    @asynccontextmanager
    async def transaction(self) -> Any:
        if self._depth.get():  # 이미 트랜잭션 안이면 그대로 참여합니다
            yield
            return
        # 다른 작업이 트랜잭션 중이면 끝날 때까지 기다립니다. (퀴즈 여러 개를 동시에 채점해도 안전)
        async with self._tx_lock:
            token = self._depth.set(1)
            await self.conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()
            finally:
                self._depth.reset(token)

    def describe(self) -> str:
        return f"SQLite 파일 {self.path}"


class PostgresBackend(Backend):
    """클라우드 PostgreSQL (Supabase · Neon 등). asyncpg 커넥션 풀을 씁니다."""

    dialect = "postgres"
    like_operator = "ILIKE"  # SQLite 의 LIKE 와 대소문자 동작을 맞추기 위해
    schema = POSTGRES_SCHEMA

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = POOL_MIN_SIZE,
        max_size: int = POOL_MAX_SIZE,
    ) -> None:
        self.dsn, self.uses_pooler = normalize_postgres_dsn(dsn)
        self.min_size = min_size
        self.max_size = max_size
        self._pool: Any = None
        # 트랜잭션 동안 한 커넥션을 붙잡아 둡니다. (풀에서 매번 다른 커넥션을 받으면
        # BEGIN 과 COMMIT 이 서로 다른 커넥션으로 흩어져 원자성이 깨집니다)
        self._tx_conn: ContextVar[Any] = ContextVar("pg_tx_conn", default=None)
        # connect() 에서 채웁니다. 빈 튜플은 '아무 예외도 잡지 않음' 을 뜻합니다.
        self._lost_errors: tuple[type[BaseException], ...] = ()

    @property
    def pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("DB가 아직 연결되지 않았습니다. connect()를 먼저 호출하세요.")
        return self._pool

    async def connect(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:  # 설치 안내
            raise RuntimeError(
                "DATABASE_URL 이 설정되어 있는데 asyncpg 가 없습니다. "
                "pip install -r requirements.txt 로 설치해 주세요."
            ) from exc

        # 트랜잭션 풀러(Supabase 6543 등) 뒤에서는 prepared statement 를 재사용할 수 없습니다.
        statement_cache_size = 0 if self.uses_pooler else 100
        # 커넥션이 끊겼을 때 알아볼 예외들. (asyncpg 를 불러온 뒤에야 알 수 있습니다)
        self._lost_errors = (
            asyncpg.exceptions.PostgresConnectionError,  # 커넥션이 사라짐 · 거부됨
            asyncpg.exceptions.AdminShutdownError,       # 서버·풀러가 커넥션을 끊음
            asyncpg.exceptions.InterfaceError,           # 이미 닫힌 커넥션을 쓰려 함
            ConnectionError,                             # 소켓이 끊김 (OSError 계열)
        )

        last_error: Exception | None = None
        for attempt in range(1, CONNECT_RETRIES + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    self.dsn,
                    min_size=self.min_size,
                    max_size=self.max_size,
                    command_timeout=POOL_COMMAND_TIMEOUT,
                    max_inactive_connection_lifetime=POOL_IDLE_LIFETIME,
                    statement_cache_size=statement_cache_size,
                    server_settings={"application_name": "leemisut-bot"},
                )
                return
            except Exception as exc:  # 절전 중인 DB가 깨어날 때까지 몇 번 더 시도합니다
                last_error = exc
                if attempt == CONNECT_RETRIES:
                    break
                log.warning(
                    "PostgreSQL 연결 실패 (%d/%d) - %s초 후 다시 시도합니다: %s",
                    attempt, CONNECT_RETRIES, CONNECT_RETRY_DELAY, exc,
                )
                await asyncio.sleep(CONNECT_RETRY_DELAY)

        raise RuntimeError(
            f"PostgreSQL 에 연결하지 못했습니다 ({self.describe()}). "
            "DATABASE_URL 과 네트워크를 확인해 주세요."
        ) from last_error

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def _connection(self) -> Any:
        """트랜잭션 중이면 붙잡아 둔 커넥션, 아니면 풀에서 하나 빌립니다."""
        pinned = self._tx_conn.get()
        if pinned is not None:
            yield pinned
            return
        async with self.pool.acquire() as conn:
            yield conn

    async def _read(self, action: Any, label: str) -> Any:
        """
        조회를 실행하되, 끊긴 커넥션을 잡았다면 새 커넥션으로 딱 한 번만 다시 시도합니다.

        봇이 새벽처럼 한참 조용하다가 첫 명령을 받을 때, 풀러나 중간 방화벽이 이미 조용히
        끊어 둔 커넥션을 잡는 일이 있습니다. 그대로 두면 그 명령 하나가 통째로 실패합니다.

        ⚠️ 다시 시도하는 것은 '조회'뿐입니다. 쓰기는 서버가 이미 실행한 뒤에 응답만 못 받았을
           수도 있어서, 다시 보내면 포인트가 두 번 지급되거나 단어장 토글이 되돌아갑니다.
           어차피 명령어들은 쓰기 전에 조회를 먼저 하므로, 그때 커넥션이 새것으로 바뀝니다.
        ⚠️ 트랜잭션 안에서도 다시 시도하지 않습니다. 붙잡아 둔 커넥션이 죽었다면 그 트랜잭션은
           이미 깨진 것이라, 일부만 되살리는 것보다 통째로 되돌리는 편이 안전합니다.
        """
        try:
            async with self._connection() as conn:
                return await action(conn)
        except self._lost_errors as exc:
            if self._tx_conn.get() is not None:
                raise
            log.warning(
                "DB 커넥션이 끊겨 있어 새로 연결해 다시 조회합니다 (%s): %s",
                label,
                exc,
            )
        async with self._connection() as conn:
            return await action(conn)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        query = self.sql(sql)
        rows = await self._read(lambda conn: conn.fetch(query, *params), "목록 조회")
        return list(rows)

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        query = self.sql(sql)
        return await self._read(lambda conn: conn.fetchrow(query, *params), "단건 조회")

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._connection() as conn:
            status = await conn.execute(self.sql(sql), *params)
        return _rowcount_from_status(status)

    async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        async with self._connection() as conn:
            await conn.executemany(self.sql(sql), [tuple(row) for row in rows])

    async def insert_returning_id(
        self, sql: str, params: Sequence[Any], id_column: str
    ) -> int:
        async with self._connection() as conn:
            value = await conn.fetchval(f"{self.sql(sql)} RETURNING {id_column}", *params)
        return int(value or 0)

    async def execute_script(self, statements: Sequence[str]) -> None:
        async with self._connection() as conn:
            for statement in statements:
                await conn.execute(statement)

    @asynccontextmanager
    async def transaction(self) -> Any:
        if self._tx_conn.get() is not None:  # 이미 트랜잭션 안이면 그대로 참여합니다
            yield
            return
        async with self.pool.acquire() as conn:
            token = self._tx_conn.set(conn)
            try:
                async with conn.transaction():
                    yield
            finally:
                self._tx_conn.reset(token)

    def describe(self) -> str:
        parts = urlsplit(self.dsn)
        host = parts.hostname or "?"
        port = f":{parts.port}" if parts.port else ""
        name = (parts.path or "/").lstrip("/") or "?"
        pooler = " · 풀러(prepared statement 끔)" if self.uses_pooler else ""
        return f"PostgreSQL {host}{port}/{name}{pooler}"


_STATUS_PATTERN = re.compile(r"(\d+)\s*$")


def _rowcount_from_status(status: str | None) -> int:
    """asyncpg 의 상태 문자열('UPDATE 1', 'DELETE 2')에서 바뀐 행 수를 꺼냅니다."""
    match = _STATUS_PATTERN.search(status or "")
    return int(match.group(1)) if match else 0


# 트랜잭션 풀러(pgbouncer)를 쓰는 주소인지 알아보는 표시들
_POOLER_HINTS = ("pgbouncer=true", "pooler.supabase.com", "-pooler.")
# libpq · asyncpg 가 모르는, 다른 도구들이 붙이는 옵션 (있으면 연결이 실패합니다)
_UNSUPPORTED_QUERY_KEYS = {"pgbouncer", "schema", "connection_limit", "pool_timeout", "supa"}


def normalize_postgres_dsn(dsn: str) -> tuple[str, bool]:
    """
    접속 문자열을 asyncpg 가 이해하는 형태로 다듬습니다. 반환: (주소, 풀러 사용 여부)

      - postgresql+asyncpg:// 처럼 붙은 드라이버 표기를 떼어 냅니다. (SQLAlchemy 형식)
      - Prisma 등이 붙이는 옵션(pgbouncer, schema …)을 떼어 냅니다. asyncpg 가 모르는 값입니다.
      - 클라우드 주소인데 sslmode 가 없으면 require 를 넣습니다. (무료 플랜은 TLS 필수)
      - 6543 포트나 pooler 주소는 '풀러 사용'으로 표시해 prepared statement 를 끄게 합니다.
    """
    raw = (dsn or "").strip()
    if not raw:
        raise ValueError("DATABASE_URL 이 비어 있습니다.")

    lowered = raw.lower()
    uses_pooler = any(hint in lowered for hint in _POOLER_HINTS)

    parts = urlsplit(raw)
    scheme = parts.scheme.split("+", 1)[0] or "postgresql"
    if scheme == "postgres":
        scheme = "postgresql"
    if scheme != "postgresql":
        raise ValueError(
            f"PostgreSQL 주소가 아닙니다: {scheme}:// (postgresql:// 로 시작해야 합니다)"
        )

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _UNSUPPORTED_QUERY_KEYS
    ]
    keys = {key.lower() for key, _ in query}

    host = (parts.hostname or "").lower()
    is_local = host in {"localhost", "127.0.0.1", "::1", ""} or host.endswith(".localhost")
    if not is_local and "sslmode" not in keys and "ssl" not in keys:
        query.append(("sslmode", "require"))

    if parts.port == 6543:
        uses_pooler = True

    rebuilt = urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return rebuilt, uses_pooler


class Database:
    """
    봇 전체가 함께 쓰는 DB 창구. (main.py 에서 bot.db 로 접근)

    path      : 로컬 SQLite 파일 경로 (DATABASE_URL 이 없을 때만 씁니다)
    dsn       : PostgreSQL 접속 문자열. 생략하면 .env 의 DATABASE_URL 을 봅니다.
    seed_mock : 단어가 하나도 없을 때 테스트용 더미 4개를 넣을지 (이전 스크립트는 False)
    """

    def __init__(
        self,
        path: str | Path = "data/imisut.db",
        *,
        dsn: str | None = None,
        seed_mock: bool = True,
    ) -> None:
        self.path = Path(path)
        self.seed_mock = seed_mock
        self._dsn = dsn
        self._backend: Backend | None = None
        # 자주 조회하지만 잘 바뀌지 않는 집계값 (count_words, count_distinct_words, get_all_categories)
        self._cache: dict[str, Any] = {}
        # 캐시를 비울 때마다 1씩 올립니다. 조회 도중 데이터가 바뀐 경우를 알아채는 데 씁니다.
        self._cache_generation = 0

    # ── 백엔드 선택 / 연결 ───────────────────────────────────────
    @property
    def backend(self) -> Backend:
        if self._backend is None:
            raise RuntimeError("DB가 아직 연결되지 않았습니다. connect()를 먼저 호출하세요.")
        return self._backend

    @property
    def dialect(self) -> str:
        """'postgres' 또는 'sqlite'"""
        return self.backend.dialect

    def describe(self) -> str:
        """지금 어디에 연결되어 있는지 사람이 읽을 설명. (비밀번호는 들어가지 않습니다)"""
        return self.backend.describe() if self._backend else "아직 연결되지 않음"

    def _create_backend(self) -> Backend:
        dsn = self._dsn if self._dsn is not None else os.getenv(DATABASE_URL_ENV, "")
        if dsn and dsn.strip():
            return PostgresBackend(dsn)
        return SQLiteBackend(self.path)

    async def connect(self) -> None:
        self._backend = self._create_backend()
        await self._backend.connect()
        await self.init_db()

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None

    async def init_db(self) -> None:
        """
        테이블 생성 → (SQLite 만) 옛 스키마 마이그레이션 → 비어 있을 때만 더미 데이터 삽입.

        ⚠️ 더미를 비어 있을 때만 넣는 이유: 매번 INSERT 하면 /수어삭제 로 지운 단어가
           봇을 재시작할 때마다 되살아납니다.
        """
        await self.backend.execute_script(self.backend.schema)
        await self._migrate_sqlite_legacy()

        if self.seed_mock and await self.count_words() == 0:
            async with self.backend.transaction():
                await self.backend.execute_many(
                    "INSERT INTO sign_words "
                    "(word_name, meaning, video_url, image_url, category, detail_url) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    MOCK_SIGN_WORDS,
                )
            log.info("🌱 단어가 없어 테스트용 더미 %d개를 넣었습니다.", len(MOCK_SIGN_WORDS))

        self._invalidate_cache()  # 마이그레이션 · 더미 삽입으로 단어 수가 바뀌었을 수 있음

    async def _migrate_sqlite_legacy(self) -> None:
        """
        예전 SQLite 스키마를 새 스키마로 옮깁니다. (데이터와 유저 포인트는 그대로 유지)

        1) sign_words 에 image_url · detail_url 컬럼이 없으면 추가
        2) users 에 last_daily_date 컬럼이 없으면 추가 (출석 체크용)
        3) word_name 에 걸려 있던 UNIQUE 제약을 UNIQUE(word_name, video_url) 로 교체
           - SQLite는 제약 조건만 떼어낼 수 없어서 테이블을 다시 만들고 데이터를 옮깁니다.
           - word_id 도 함께 옮겨, 퀴즈 기록 · 단어장이 가리키는 ID가 흐트러지지 않게 합니다.

        ※ 클라우드 PostgreSQL 은 이 봇이 처음부터 새 스키마로 만들기 때문에 할 일이 없습니다.
           (옛 SQLite 데이터는 migrate_to_cloud.py 로 옮깁니다)
        """
        backend = self.backend
        if not isinstance(backend, SQLiteBackend):
            return

        conn = backend.conn
        async with conn.execute("PRAGMA table_info(sign_words)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

        for column in ("image_url", "detail_url"):
            if column not in columns:
                await conn.execute(
                    f"ALTER TABLE sign_words ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )

        # users 테이블: 출석 기록 컬럼 (기본값 없이 NULL 허용)
        async with conn.execute("PRAGMA table_info(users)") as cur:
            user_columns = {row[1] for row in await cur.fetchall()}
        if "last_daily_date" not in user_columns:
            await conn.execute("ALTER TABLE users ADD COLUMN last_daily_date TEXT")

        # 현재 테이블 정의에 복합 UNIQUE 가 들어 있는지 확인
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sign_words'"
        ) as cur:
            row = await cur.fetchone()
        table_sql = (row[0] if row else "") or ""
        if "UNIQUE(word_name, video_url)" in table_sql.replace(" ,", ","):
            return  # 이미 새 스키마입니다

        # 테이블 재생성 (동음이의어 허용)
        await conn.executescript(
            """
            PRAGMA foreign_keys=off;
            BEGIN;
            CREATE TABLE sign_words_new (
                word_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                word_name  TEXT    NOT NULL,
                meaning    TEXT    NOT NULL,
                video_url  TEXT    NOT NULL,
                image_url  TEXT    NOT NULL DEFAULT '',
                category   TEXT    NOT NULL DEFAULT '일반',
                detail_url TEXT    NOT NULL DEFAULT '',
                UNIQUE(word_name, video_url)
            );
            INSERT OR IGNORE INTO sign_words_new
                (word_id, word_name, meaning, video_url, image_url, category, detail_url)
            SELECT word_id, word_name, meaning, video_url, image_url, category, detail_url
            FROM sign_words;
            DROP TABLE sign_words;
            ALTER TABLE sign_words_new RENAME TO sign_words;
            CREATE INDEX IF NOT EXISTS idx_sign_words_name ON sign_words(word_name);
            COMMIT;
            PRAGMA foreign_keys=on;
            """
        )

    # ── 인메모리 캐시 ────────────────────────────────────────────
    def _invalidate_cache(self) -> None:
        """단어가 추가/수정/삭제된 뒤 호출합니다. 다음 조회 때 DB에서 새로 채웁니다."""
        self._cache.clear()
        self._cache_generation += 1

    async def _cached(self, key: str, loader: Callable[[], Awaitable[T]]) -> T:
        """캐시에 있으면 즉시 돌려주고, 없으면 loader() 로 DB에서 읽어 저장합니다."""
        if key in self._cache:
            return self._cache[key]
        generation = self._cache_generation
        value = await loader()
        # 읽는 사이(await 중)에 동기화 · 삭제가 끝났다면 방금 값은 옛날 값이므로 저장하지 않습니다.
        if generation == self._cache_generation:
            self._cache[key] = value
        return value

    # ── 수어 단어 ────────────────────────────────────────────────
    async def count_words(self) -> int:
        """전체 단어 수. (캐시 - /오늘의수어 가 부를 때마다 COUNT 하지 않습니다)"""

        async def load() -> int:
            row = await self.backend.fetch_one("SELECT COUNT(*) AS cnt FROM sign_words")
            return int(row["cnt"]) if row else 0

        return await self._cached("count_words", load)

    async def count_distinct_words(self) -> int:
        """동음이의어를 하나로 세었을 때의 단어 수. (캐시)"""

        async def load() -> int:
            row = await self.backend.fetch_one(
                "SELECT COUNT(DISTINCT word_name) AS cnt FROM sign_words"
            )
            return int(row["cnt"]) if row else 0

        return await self._cached("count_distinct_words", load)

    async def get_daily_word(self, day: date) -> Row | None:
        """날짜를 기준으로 단어를 골라, 같은 날에는 모두에게 같은 단어를 보여줍니다."""
        count = await self.count_words()
        if count == 0:
            return None

        offset = day.toordinal() % count
        return await self.backend.fetch_one(
            "SELECT * FROM sign_words ORDER BY word_id LIMIT 1 OFFSET ?", (offset,)
        )

    async def get_daily_word_for_user(self, user_id: int, day: date) -> Row | None:
        """
        유저마다 다른 단어를 배정합니다.
            (유저 ID + 날짜) % 전체 단어 수

        같은 사람은 하루 종일 같은 단어를, 다른 사람은 다른 단어를 받습니다.
        (단어가 새로 동기화되어 전체 수가 바뀌면 배정도 바뀝니다)
        """
        count = await self.count_words()
        if count == 0:
            return None

        offset = (user_id + day.toordinal()) % count
        return await self.backend.fetch_one(
            "SELECT * FROM sign_words ORDER BY word_id LIMIT 1 OFFSET ?", (offset,)
        )

    async def get_all_categories(self) -> list[tuple[str, int]]:
        """
        DB에 실제로 등록된 분류 목록을 (분류명, 단어 수) 로 돌려줍니다. (캐시)
        분류 자동완성은 글자를 칠 때마다 불리므로 GROUP BY 를 매번 하지 않습니다.
        """

        async def load() -> tuple[tuple[str, int], ...]:
            rows = await self.backend.fetch_all(
                "SELECT category, COUNT(*) AS cnt FROM sign_words "
                "WHERE category <> '' GROUP BY category ORDER BY cnt DESC, category"
            )
            return tuple((row["category"], int(row["cnt"])) for row in rows)

        # 캐시는 튜플로 보관하고 새 리스트로 돌려줍니다. (받는 쪽이 고쳐도 캐시는 그대로)
        return list(await self._cached("categories", load))

    @staticmethod
    def _like_pattern(value: str) -> str:
        r"""
        LIKE 검색용 '부분 일치' 패턴을 만듭니다. (SQL 에서 반드시 ESCAPE '\' 와 함께 사용)

        사용자가 넣은 %, _ 가 와일드카드로 동작하면 '%_%_%_…' 같은 입력 하나로
        행마다 비교량이 폭증해(DoS) 봇 전체가 멈출 수 있습니다. 그래서
          1) 길이를 MAX_LIKE_KEYWORD_LENGTH 로 먼저 자르고  ← 이스케이프 뒤에 자르면 '\' 만 남을 수 있음
          2) 이스케이프 문자 \ 를 \\ 로 바꾼 다음           ← 순서가 바뀌면 \% 가 \\% 로 이중 변환됨
          3) % → \%,  _ → \_ 로 바꿔 글자 그대로 찾게 합니다.
        결과적으로 패턴 안의 와일드카드는 앞뒤에 붙인 % 두 개뿐입니다.
        """
        cleaned = value.strip()[:MAX_LIKE_KEYWORD_LENGTH]
        escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def _build_filter(self, keyword: str, category: str) -> tuple[str, list]:
        """단어명 · 분류 조건을 WHERE 절과 인자 목록으로 만듭니다."""
        like = self.backend.like_operator  # SQLite: LIKE / PostgreSQL: ILIKE
        clauses: list[str] = []
        params: list = []
        if keyword.strip():
            clauses.append(f"word_name {like} ? ESCAPE '\\'")
            params.append(self._like_pattern(keyword))
        if category.strip():
            clauses.append(f"category {like} ? ESCAPE '\\'")
            params.append(self._like_pattern(category))
        where = " AND ".join(clauses) if clauses else "1=1"
        return where, params

    async def search_words_by_filter(
        self, keyword: str = "", category: str = "", limit: int = 10, offset: int = 0
    ) -> list[Row]:
        """
        단어명과 분류를 함께 걸어 검색합니다. (둘 다 비어 있으면 전체)
        정확히 일치하는 단어명 → 짧은 단어 → 가나다 순으로 정렬합니다.

        offset 은 페이지 버튼용입니다. (3페이지 = offset 20, limit 10)
        마지막에 word_id 로 한 번 더 정렬해, 단어명 · 분류가 같은 동음이의어도
        페이지를 오갈 때 순서가 바뀌거나 두 번 나오지 않게 합니다.
        """
        where, params = self._build_filter(keyword, category)
        return await self.backend.fetch_all(
            f"SELECT * FROM sign_words WHERE {where} "
            "ORDER BY (word_name = ?::text) DESC, LENGTH(word_name), word_name, category, word_id "
            "LIMIT ? OFFSET ?",
            (*params, keyword.strip(), limit, offset),
        )

    async def count_words_by_filter(self, keyword: str = "", category: str = "") -> int:
        """검색 조건에 맞는 전체 건수. (limit 과 무관한 실제 총계)"""
        where, params = self._build_filter(keyword, category)
        row = await self.backend.fetch_one(
            f"SELECT COUNT(*) AS cnt FROM sign_words WHERE {where}", params
        )
        return int(row["cnt"]) if row else 0

    async def get_homonym_names(self, names: list[str]) -> set[str]:
        """
        주어진 단어명 중 DB에 2건 이상 있는(동음이의어) 이름만 돌려줍니다.
        목록을 페이지로 나누면 '배' 두 건이 서로 다른 페이지에 걸릴 수 있어 DB 기준으로 확인합니다.
        """
        unique = list(dict.fromkeys(names))
        if not unique:
            return set()
        placeholders = ",".join("?" * len(unique))  # 값은 모두 자리표시자로 바인딩합니다
        rows = await self.backend.fetch_all(
            f"SELECT word_name FROM sign_words WHERE word_name IN ({placeholders}) "
            "GROUP BY word_name HAVING COUNT(*) > 1",
            unique,
        )
        return {row["word_name"] for row in rows}

    async def get_random_words(self, limit: int) -> list[Row]:
        """
        무작위 단어를 뽑되 단어명이 겹치지 않게 합니다.
        (퀴즈 보기에 '배'가 두 개 뜨면 고를 수 없으므로)

        단어명마다 가장 작은 word_id 하나만 후보로 두고 그중에서 섞습니다.
        (PostgreSQL 은 GROUP BY 에 없는 컬럼을 그냥 SELECT 할 수 없어 이 방식으로 맞췄습니다)
        """
        return await self.backend.fetch_all(
            "SELECT * FROM sign_words WHERE word_id IN "
            "(SELECT MIN(word_id) FROM sign_words GROUP BY word_name) "
            "ORDER BY RANDOM() LIMIT ?",
            (limit,),
        )

    async def get_word_by_name(self, word_name: str) -> Row | None:
        """단어명으로 1건을 찾습니다. (동음이의어가 있으면 첫 번째)"""
        return await self.backend.fetch_one(
            "SELECT * FROM sign_words WHERE word_name = TRIM(?::text) ORDER BY word_id LIMIT 1",
            (word_name,),
        )

    async def get_words_by_name(self, word_name: str) -> list[Row]:
        """같은 단어명을 가진 항목을 모두 가져옵니다. (동음이의어 확인용)"""
        return await self.backend.fetch_all(
            "SELECT * FROM sign_words WHERE word_name = TRIM(?::text) ORDER BY word_id",
            (word_name,),
        )

    async def get_word_by_id(self, word_id: int) -> Row | None:
        """단어 ID로 정확히 1건을 찾습니다. (동음이의어를 구분해야 할 때)"""
        return await self.backend.fetch_one(
            "SELECT * FROM sign_words WHERE word_id = ?", (word_id,)
        )

    async def search_words(self, keyword: str, limit: int = 25) -> list[Row]:
        """
        단어명 부분 일치 검색. 동음이의어는 각각 따로 나옵니다.
        정확히 일치하는 단어 → 짧은 단어 순으로 정렬합니다.
        """
        # 자동완성에서 글자를 칠 때마다 호출되므로 이스케이프 · 길이 제한을 꼭 거칩니다.
        like = self.backend.like_operator
        return await self.backend.fetch_all(
            f"SELECT * FROM sign_words WHERE word_name {like} ? ESCAPE '\\' "
            "ORDER BY (word_name = ?::text) DESC, LENGTH(word_name), word_name, category LIMIT ?",
            (self._like_pattern(keyword), keyword.strip(), limit),
        )

    async def delete_word_by_name(self, word_name: str) -> int:
        """
        단어명이 같은 항목을 모두 삭제하고, 삭제된 행 수를 반환합니다.
        (동음이의어가 여러 건이면 함께 지워집니다)
        """
        async with self.backend.transaction():
            # 지워질 단어를 담아 둔 단어장 항목도 함께 정리합니다. (퀴즈 기록은 이력으로 남김)
            await self.backend.execute(
                "DELETE FROM user_bookmarks WHERE word_id IN "
                "(SELECT word_id FROM sign_words WHERE word_name = TRIM(?::text))",
                (word_name,),
            )
            deleted = await self.backend.execute(
                "DELETE FROM sign_words WHERE word_name = TRIM(?::text)", (word_name,)
            )
        self._invalidate_cache()  # 단어 수 · 분류 목록이 바뀌었으므로
        return deleted

    async def delete_word_by_id(self, word_id: int) -> int:
        """동음이의어 중 하나만 골라 지울 때 사용합니다."""
        async with self.backend.transaction():
            await self.backend.execute(
                "DELETE FROM user_bookmarks WHERE word_id = ?", (word_id,)
            )
            deleted = await self.backend.execute(
                "DELETE FROM sign_words WHERE word_id = ?", (word_id,)
            )
        self._invalidate_cache()  # 단어 수 · 분류 목록이 바뀌었으므로
        return deleted

    async def sync_api_words(self, words_data: list[tuple[str, ...]]) -> int:
        """
        API에서 받아 온 단어들을 저장합니다.
        (word_name, meaning, video_url[, image_url, category, detail_url]) 형태를 모두 받습니다.

        중복 판정은 (word_name, video_url) 복합 기준입니다.
        같은 단어라도 영상이 다르면 동음이의어로 보고 따로 저장하고,
        영상까지 같으면 뜻풀이 · 주소 · 분류를 최신 값으로 갱신합니다.

        반환값은 '새로 추가된' 행 수입니다. 갱신은 세지 않습니다.
        """
        if not words_data:
            return 0

        rows: list[tuple[str, ...]] = []
        for row in words_data:
            row = tuple(row)
            if len(row) == 4:  # 예전 형식 (name, meaning, video, category)
                row = (row[0], row[1], row[2], "", row[3], "")
            elif len(row) == 5:  # (name, meaning, video, category, detail)
                row = (row[0], row[1], row[2], "", row[3], row[4])
            rows.append(row + ("",) * (6 - len(row)))

        before = await self.count_words()
        try:
            async with self.backend.transaction():
                await self.backend.execute_many(
                    """
                    INSERT INTO sign_words
                        (word_name, meaning, video_url, image_url, category, detail_url)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (word_name, video_url) DO UPDATE SET
                        meaning    = excluded.meaning,
                        image_url  = excluded.image_url,
                        category   = excluded.category,
                        detail_url = excluded.detail_url
                    """,
                    rows,
                )
        finally:
            # 분류가 갱신만 되어도 분류 목록이 바뀌고, 중간에 실패해도 일부 행이
            # 들어갔을 수 있으므로 성공 · 실패와 관계없이 항상 캐시를 비웁니다.
            self._invalidate_cache()
        return await self.count_words() - before  # 캐시가 비었으므로 새로 센 값

    # ── 유저 ─────────────────────────────────────────────────────
    async def get_user(self, user_id: int) -> Row | None:
        return await self.backend.fetch_one(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )

    async def claim_daily(
        self, user_id: int, today: date, points: int, exp: int
    ) -> tuple[bool, Row]:
        """
        오늘 첫 /오늘의수어 이면 출석을 인정하고 보상을 지급합니다.

        반환: (이번에 새로 출석했는지, 최신 유저 정보)

        같은 날 두 번 눌러도 보상이 두 번 나가지 않도록, 날짜 조건을 UPDATE 문 안에
        넣어 한 번의 질의로 처리합니다. (동시에 두 번 눌러도 안전)
        """
        today_str = today.isoformat()
        yesterday_str = date.fromordinal(today.toordinal() - 1).isoformat()

        async with self.backend.transaction():
            await self.backend.execute(
                "INSERT INTO users (user_id) VALUES (?) ON CONFLICT DO NOTHING", (user_id,)
            )
            updated = await self.backend.execute(
                """
                UPDATE users SET
                    points          = points + ?,
                    exp             = exp + ?,
                    streak          = CASE WHEN last_daily_date = ?::text
                                           THEN streak + 1 ELSE 1 END,
                    last_daily_date = ?::text
                WHERE user_id = ?
                  AND (last_daily_date IS NULL OR last_daily_date <> ?::text)
                """,
                (points, exp, yesterday_str, today_str, user_id, today_str),
            )

        user = await self.get_user(user_id)
        assert user is not None
        return updated > 0, user

    async def add_reward(self, user_id: int, points: int, exp: int) -> Row:
        """포인트/경험치 지급 (유저가 없으면 새로 생성) 후 최신 정보를 반환합니다."""
        await self.backend.execute(
            """
            INSERT INTO users (user_id, points, exp) VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                points = users.points + excluded.points,
                exp    = users.exp    + excluded.exp
            """,
            (user_id, points, exp),
        )
        user = await self.get_user(user_id)
        assert user is not None
        return user

    # ── 퀴즈 기록 · 오답 복습 ────────────────────────────────────
    async def log_quiz_attempt(self, user_id: int, word_id: int, is_correct: bool) -> None:
        """퀴즈 한 문제의 결과를 남깁니다. (시간 초과는 오답으로 기록해 주세요)"""
        await self.backend.execute(
            "INSERT INTO quiz_logs (user_id, word_id, is_correct, solved_at) "
            "VALUES (?, ?, ?, ?::text)",
            (user_id, word_id, int(is_correct), _now_iso()),
        )

    @staticmethod
    def _kst_day_bounds(today: date) -> tuple[str, str]:
        """
        KST 하루(00:00~24:00)를 quiz_logs.solved_at 과 비교할 UTC 문자열 구간으로 바꿉니다.

        solved_at 은 UTC ISO 8601 문자열이고 _now_iso() 가 늘 같은 형식으로 남기므로,
        사전순 문자열 비교만으로도 정확히 하루를 잘라낼 수 있습니다.
        (경계값은 초 단위로 만들고, 기록은 마이크로초까지 있어 항상 경계보다 뒤에 옵니다)
        """
        day_start_kst = datetime(today.year, today.month, today.day, tzinfo=KST)
        start_utc = day_start_kst.astimezone(timezone.utc).isoformat(timespec="seconds")
        end_utc = (day_start_kst + timedelta(days=1)).astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )
        return start_utc, end_utc

    async def get_today_quiz_reward_count(self, user_id: int, today: date) -> int:
        """
        오늘(KST 기준) 퀴즈 정답으로 보상을 받은 횟수를 셉니다.
        (/내정보 표시용. 보상 지급 판단은 record_quiz_reward 안에서 원자적으로 합니다)

        보상은 하루 중 먼저 맞힌 문제부터 순서대로 나가므로,
        '오늘의 정답 기록 수' 가 곧 '오늘 받은 보상 횟수' 입니다.
        (상한을 넘긴 뒤의 정답도 기록은 남으므로, 표시할 때는 상한으로 잘라 주세요)
        """
        start_utc, end_utc = self._kst_day_bounds(today)
        row = await self.backend.fetch_one(
            """
            SELECT COUNT(*) AS cnt FROM quiz_logs
            WHERE user_id = ? AND is_correct = 1
              AND solved_at >= ?::text AND solved_at < ?::text
            """,
            (user_id, start_utc, end_utc),
        )
        return int(row["cnt"]) if row else 0

    async def record_quiz_reward(
        self,
        user_id: int,
        word_id: int,
        today: date,
        *,
        points: int,
        exp: int,
        max_daily_rewards: int,
    ) -> tuple[bool, int, Row | None]:
        """
        퀴즈 정답을 기록하고, 일일 상한선 안에서만 보상을 지급합니다.

        ■ 동시성(Race Condition) 방지
          '파이썬에서 횟수를 세고 → 보상을 준다' 로 나누면 그 사이에 다른 퀴즈가 끼어들어
          상한을 넘겨 지급될 수 있습니다. 그래서 횟수 확인을 UPDATE 문의 WHERE 안에 넣어
          '확인과 지급'을 한 문장에서 끝냅니다.
          기준은 시간이 아니라 방금 남긴 기록의 log_id 입니다.
            - 내 기록보다 '앞선' 오늘 정답 수가 상한 미만일 때만 지급
            - 두 문제를 같은 순간에 맞혀도 log_id 순서로 줄이 서므로,
              중복 지급도, 둘 다 막히는 일도 없습니다.
          기록 · 지급 · 횟수 확인을 한 트랜잭션으로 묶어 중간에 끊겨도 어긋나지 않게 합니다.

        반환: (보상 지급 여부, 오늘 사용한 보상 횟수, 최신 유저 정보 or None)
        """
        start_utc, end_utc = self._kst_day_bounds(today)

        async with self.backend.transaction():
            log_id = await self.backend.insert_returning_id(
                "INSERT INTO quiz_logs (user_id, word_id, is_correct, solved_at) "
                "VALUES (?, ?, 1, ?::text)",
                (user_id, word_id, _now_iso()),
                "log_id",
            )

            # 유저 행이 없을 수도 있으니 먼저 만들어 둡니다. (보상은 아래 UPDATE 에서만 나갑니다)
            await self.backend.execute(
                "INSERT INTO users (user_id) VALUES (?) ON CONFLICT DO NOTHING", (user_id,)
            )
            granted = await self.backend.execute(
                """
                UPDATE users SET
                    points = points + ?,
                    exp    = exp + ?
                WHERE user_id = ?
                  AND (SELECT COUNT(*) FROM quiz_logs
                       WHERE user_id = ? AND is_correct = 1
                         AND solved_at >= ?::text AND solved_at < ?::text
                         AND log_id < ?) < ?::int
                """,
                (points, exp, user_id, user_id, start_utc, end_utc, log_id, max_daily_rewards),
            ) > 0

            # 표시용 횟수도 log_id 기준으로 셉니다. ('내 앞의 정답 수 + 1' = 내 차례)
            # log_id 는 계속 커지기만 하므로, 동시에 여러 문제를 풀어도 이 값은 흔들리지 않습니다.
            # (전체 개수를 다시 세면 같은 순간의 다른 풀이까지 들어가 모두 같은 숫자로 보입니다)
            row = await self.backend.fetch_one(
                """
                SELECT COUNT(*) AS cnt FROM quiz_logs
                WHERE user_id = ? AND is_correct = 1
                  AND solved_at >= ?::text AND solved_at < ?::text
                  AND log_id < ?
                """,
                (user_id, start_utc, end_utc, log_id),
            )

        earlier = int(row["cnt"]) if row else 0
        # 상한을 넘긴 뒤의 정답도 기록에는 남으므로, 막힌 경우에는 상한값으로 보여 줍니다.
        used = earlier + 1 if granted else max_daily_rewards
        user = await self.get_user(user_id) if granted else None
        return granted, used, user

    async def get_user_wrong_words(self, user_id: int, limit: int = 10) -> list[Row]:
        """
        유저가 틀린 뒤 아직 다시 맞히지 못한 단어를 돌려줍니다. (오답 복습 출제용)

        - 단어별 '마지막 풀이'가 오답인 것만 고릅니다.
          복습에서 맞히면 목록에서 빠지고, 다시 틀리면 돌아옵니다.
        - 많이 틀린 단어 → 최근에 틀린 단어 순으로 정렬합니다.
        - 순서 비교는 solved_at 대신 log_id 로 합니다. (같은 초에 기록돼도 앞뒤가 정확)
        - 삭제된 단어는 sign_words 와 JOIN 되지 않으므로 자동으로 빠집니다.
        - 각 행에는 단어 정보(sign_words 컬럼 전체)와 wrong_count(틀린 횟수)가 들어 있습니다.
        """
        return await self.backend.fetch_all(
            """
            SELECT w.*, s.wrong_count
            FROM (
                SELECT word_id,
                       SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) AS wrong_count,
                       MAX(CASE WHEN is_correct = 0 THEN log_id END)   AS last_wrong_id,
                       MAX(CASE WHEN is_correct = 1 THEN log_id END)   AS last_correct_id
                FROM quiz_logs
                WHERE user_id = ?
                GROUP BY word_id
            ) AS s
            JOIN sign_words AS w ON w.word_id = s.word_id
            WHERE s.last_wrong_id IS NOT NULL
              AND (s.last_correct_id IS NULL OR s.last_correct_id < s.last_wrong_id)
            ORDER BY s.wrong_count DESC, s.last_wrong_id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )

    # ── 나만의 단어장 ────────────────────────────────────────────
    async def toggle_bookmark(self, user_id: int, word_id: int) -> bool:
        """
        단어장에 없으면 담고(True), 이미 있으면 뺍니다(False).
        사전에 없는 word_id 면 LookupError 를 냅니다.
        """
        async with self.backend.transaction():
            removed = await self.backend.execute(
                "DELETE FROM user_bookmarks WHERE user_id = ? AND word_id = ?",
                (user_id, word_id),
            )
            if removed > 0:
                return False

            # 실제로 있는 단어일 때만 들어갑니다. (SELECT 결과가 없으면 아무것도 넣지 않음)
            # ON CONFLICT DO NOTHING: 같은 순간 두 번 처리돼도 UNIQUE 오류 없이 넘어갑니다.
            inserted = await self.backend.execute(
                "INSERT INTO user_bookmarks (user_id, word_id, created_at) "
                "SELECT ?::bigint, word_id, ?::text FROM sign_words WHERE word_id = ? "
                "ON CONFLICT DO NOTHING",
                (user_id, _now_iso(), word_id),
            )
            if inserted == 0 and await self.get_word_by_id(word_id) is None:
                raise LookupError(f"word_id={word_id} 단어가 사전에 없습니다.")
        return True

    async def get_user_bookmarks(self, user_id: int) -> list[Row]:
        """
        유저가 담아 둔 단어를 최근에 담은 순서로 돌려줍니다.
        각 행에는 단어 정보(sign_words 컬럼 전체)와 bookmarked_at(담은 시각)이 들어 있습니다.

        created_at 은 마이크로초까지 기록되므로 담은 순서가 그대로 드러납니다.
        (같은 시각이 겹치는 아주 드문 경우만 word_id 로 가릅니다)
        """
        return await self.backend.fetch_all(
            """
            SELECT w.*, b.created_at AS bookmarked_at
            FROM user_bookmarks AS b
            JOIN sign_words AS w ON w.word_id = b.word_id
            WHERE b.user_id = ?
            ORDER BY b.created_at DESC, b.word_id DESC
            """,
            (user_id,),
        )
