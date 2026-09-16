"""
database.py
조교 이미숫 - aiosqlite 기반 비동기 데이터베이스 모듈

봇 전체에서 커넥션 하나를 공유합니다. (main.py에서 bot.db 로 접근)

■ sign_words 스키마
    word_id    INTEGER  PK
    word_name  TEXT     단어명 (중복 허용 - 동음이의어)
    meaning    TEXT     수어 설명 전문
    video_url  TEXT     동영상 파일 주소
    image_url  TEXT     수형 사진 주소 (임베드에 직접 표시)
    category   TEXT     분류항목
    detail_url TEXT     사전 상세 페이지 (퀴즈 중에는 숨김)
    UNIQUE(word_name, video_url)
      → '배(사물)'과 '배(신체)'처럼 영상이 다르면 각각 저장됩니다.

■ quiz_logs      퀴즈 풀이 기록 (log_id, user_id, word_id, is_correct, solved_at)
    → 정답(is_correct=1) 기록 수로 '오늘 받은 퀴즈 보상 횟수'(일일 상한선)를 셉니다.
■ user_bookmarks 나만의 단어장   (user_id, word_id, created_at)  UNIQUE(user_id, word_id)
    → 두 테이블 모두 sign_words 와 JOIN 해서 읽으므로 삭제된 단어는 자동으로 빠집니다.
    → 기존 DB에도 봇을 켤 때 CREATE TABLE IF NOT EXISTS 로 자동 추가됩니다. (기존 데이터 유지)

■ 인메모리 캐시 (_cache)
    전체 단어 수 · 단어명 기준 수 · 분류 목록은 처음 한 번만 DB에서 세고 메모리에 보관합니다.
    단어를 추가/수정/삭제하는 메서드가 끝나면 캐시를 비우고, 다음 조회 때 다시 채웁니다.
    ⚠️ 봇이 켜진 채로 DB 파일을 외부 프로그램으로 고치면 캐시에 반영되지 않습니다. (재시작 필요)
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

log = logging.getLogger(__name__)

T = TypeVar("T")

# 하루 경계를 따질 때 쓰는 시간대. cogs 와 같은 고정 오프셋을 씁니다.
# (quiz_logs.solved_at 은 UTC 로 남으므로, KST 하루를 UTC 구간으로 바꿔 조회합니다)
KST = timezone(timedelta(hours=9))

# LIKE 검색어 최대 길이. 수어 단어명은 길어야 20자 안팎이라 넉넉한 값입니다.
# 패턴이 길수록 행마다 비교 비용이 커지므로 이보다 긴 입력은 잘라서 씁니다.
MAX_LIKE_KEYWORD_LENGTH = 50

# 관리자 명령어 2중 검증용 .env 키 (쉼표로 구분한 디스코드 유저 ID 목록)
ADMIN_USER_IDS_ENV = "ADMIN_USER_IDS"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,          -- 디스코드 유저 ID
    points         INTEGER NOT NULL DEFAULT 0,
    exp            INTEGER NOT NULL DEFAULT 0,
    level          INTEGER NOT NULL DEFAULT 1,
    streak         INTEGER NOT NULL DEFAULT 0,   -- 연속 출석일수
    last_quiz_date TEXT,                         -- 'YYYY-MM-DD' (KST 기준)
    last_daily_date TEXT                         -- /오늘의수어 마지막 확인 날짜
);

CREATE TABLE IF NOT EXISTS sign_words (
    word_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    word_name  TEXT    NOT NULL,                 -- 동음이의어 허용 (UNIQUE 아님)
    meaning    TEXT    NOT NULL,
    video_url  TEXT    NOT NULL,
    image_url  TEXT    NOT NULL DEFAULT '',
    category   TEXT    NOT NULL DEFAULT '일반',
    detail_url TEXT    NOT NULL DEFAULT '',
    UNIQUE(word_name, video_url)                 -- 같은 단어라도 영상이 다르면 별개
);

CREATE INDEX IF NOT EXISTS idx_sign_words_name ON sign_words(word_name);

-- 퀴즈 풀이 기록 (오답 복습 출제용). 시간 초과는 오답(0)으로 남깁니다.
CREATE TABLE IF NOT EXISTS quiz_logs (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,                 -- 디스코드 유저 ID
    word_id    INTEGER NOT NULL,                 -- sign_words.word_id
    is_correct INTEGER NOT NULL,                 -- 1 정답 / 0 오답·시간 초과
    solved_at  TEXT    NOT NULL                  -- ISO 8601 (UTC)
);
CREATE INDEX IF NOT EXISTS idx_quiz_logs_user_word ON quiz_logs(user_id, word_id);

-- 나만의 단어장. UNIQUE 가 같은 단어 중복 저장을 막고, user_id 조회 인덱스 역할도 합니다.
CREATE TABLE IF NOT EXISTS user_bookmarks (
    user_id    INTEGER NOT NULL,
    word_id    INTEGER NOT NULL,                 -- sign_words.word_id
    created_at TEXT    NOT NULL,                 -- ISO 8601 (UTC)
    UNIQUE(user_id, word_id)
);
"""

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
    """기록 시각 (UTC, ISO 8601). 예: 2026-09-15T03:04:05+00:00"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        # 자주 조회하지만 잘 바뀌지 않는 집계값 (count_words, count_distinct_words, get_all_categories)
        self._cache: dict[str, Any] = {}
        # 캐시를 비울 때마다 1씩 올립니다. 조회 도중 데이터가 바뀐 경우를 알아채는 데 씁니다.
        self._cache_generation = 0

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DB가 아직 연결되지 않았습니다. connect()를 먼저 호출하세요.")
        return self._conn

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
        # 읽는 사이(await 중)에 동기화·삭제가 끝났다면 방금 값은 옛날 값이므로 저장하지 않습니다.
        if generation == self._cache_generation:
            self._cache[key] = value
        return value

    # ── 연결 / 초기화 ─────────────────────────────────────────────
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row  # row["컬럼명"] 으로 접근 가능
        # WAL: 읽기와 쓰기가 서로를 막지 않습니다. (DB 파일에 저장되어 계속 유지)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # WAL 모드에서는 NORMAL 로도 DB가 깨지지 않으며, 커밋마다 디스크 동기화(fsync)를
        # 하지 않아 쓰기가 빨라집니다. (정전 시 마지막 몇 건의 커밋만 되돌려질 수 있음)
        # ※ synchronous 는 연결마다 기본값(FULL)으로 돌아가므로 connect() 에서 매번 설정합니다.
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self.init_db()

    async def init_db(self) -> None:
        """
        테이블 생성 → 마이그레이션 → (비어 있을 때만) 더미 데이터 삽입.

        ⚠️ 더미를 비어 있을 때만 넣는 이유: 매번 INSERT 하면 /수어삭제 로 지운 단어가
           봇을 재시작할 때마다 되살아납니다.
        """
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        if await self.count_words() == 0:
            await self.conn.executemany(
                "INSERT OR IGNORE INTO sign_words "
                "(word_name, meaning, video_url, image_url, category, detail_url) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                MOCK_SIGN_WORDS,
            )
        await self.conn.commit()
        self._invalidate_cache()  # 마이그레이션 · 더미 삽입으로 단어 수가 바뀌었을 수 있음

    async def _migrate(self) -> None:
        """
        예전 스키마를 새 스키마로 옮깁니다. (데이터와 유저 포인트는 그대로 유지)

        1) sign_words 에 image_url · detail_url 컬럼이 없으면 추가
        2) users 에 last_daily_date 컬럼이 없으면 추가 (출석 체크용)
        3) word_name 에 걸려 있던 UNIQUE 제약을 UNIQUE(word_name, video_url) 로 교체
           - SQLite는 제약 조건만 떼어낼 수 없어서 테이블을 다시 만들고 데이터를 옮깁니다.
        4) quiz_logs · user_bookmarks 는 새 테이블이라 SCHEMA 의 CREATE TABLE IF NOT EXISTS 가
           init_db() 에서 알아서 만들어 줍니다. (여기서 따로 할 일 없음)
        """
        async with self.conn.execute("PRAGMA table_info(sign_words)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

        for column in ("image_url", "detail_url"):
            if column not in columns:
                await self.conn.execute(
                    f"ALTER TABLE sign_words ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )

        # users 테이블: 출석 기록 컬럼 (기본값 없이 NULL 허용)
        async with self.conn.execute("PRAGMA table_info(users)") as cur:
            user_columns = {row[1] for row in await cur.fetchall()}
        if "last_daily_date" not in user_columns:
            await self.conn.execute("ALTER TABLE users ADD COLUMN last_daily_date TEXT")
        await self.conn.commit()

        # 현재 테이블 정의에 복합 UNIQUE 가 들어 있는지 확인
        async with self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sign_words'"
        ) as cur:
            row = await cur.fetchone()
        table_sql = (row[0] if row else "") or ""
        if "UNIQUE(word_name, video_url)" in table_sql.replace(" ,", ","):
            return  # 이미 새 스키마입니다

        # 테이블 재생성 (동음이의어 허용)
        await self.conn.executescript(
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
                (word_name, meaning, video_url, image_url, category, detail_url)
            SELECT word_name, meaning, video_url, image_url, category, detail_url
            FROM sign_words;
            DROP TABLE sign_words;
            ALTER TABLE sign_words_new RENAME TO sign_words;
            CREATE INDEX IF NOT EXISTS idx_sign_words_name ON sign_words(word_name);
            COMMIT;
            PRAGMA foreign_keys=on;
            """
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ── 수어 단어 ────────────────────────────────────────────────
    async def count_words(self) -> int:
        """전체 단어 수. (캐시 - /오늘의수어 가 부를 때마다 COUNT 하지 않습니다)"""

        async def load() -> int:
            async with self.conn.execute("SELECT COUNT(*) FROM sign_words") as cur:
                (count,) = await cur.fetchone()
            return count

        return await self._cached("count_words", load)

    async def count_distinct_words(self) -> int:
        """동음이의어를 하나로 세었을 때의 단어 수. (캐시)"""

        async def load() -> int:
            async with self.conn.execute(
                "SELECT COUNT(DISTINCT word_name) FROM sign_words"
            ) as cur:
                (count,) = await cur.fetchone()
            return count

        return await self._cached("count_distinct_words", load)

    async def get_daily_word(self, day: date) -> aiosqlite.Row | None:
        """날짜를 기준으로 단어를 골라, 같은 날에는 모두에게 같은 단어를 보여줍니다."""
        count = await self.count_words()
        if count == 0:
            return None

        offset = day.toordinal() % count
        async with self.conn.execute(
            "SELECT * FROM sign_words ORDER BY word_id LIMIT 1 OFFSET ?", (offset,)
        ) as cur:
            return await cur.fetchone()

    async def get_daily_word_for_user(self, user_id: int, day: date) -> aiosqlite.Row | None:
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
        async with self.conn.execute(
            "SELECT * FROM sign_words ORDER BY word_id LIMIT 1 OFFSET ?", (offset,)
        ) as cur:
            return await cur.fetchone()

    async def get_all_categories(self) -> list[tuple[str, int]]:
        """
        DB에 실제로 등록된 분류 목록을 (분류명, 단어 수) 로 돌려줍니다. (캐시)
        분류 자동완성은 글자를 칠 때마다 불리므로 GROUP BY 를 매번 하지 않습니다.
        """

        async def load() -> tuple[tuple[str, int], ...]:
            async with self.conn.execute(
                "SELECT category, COUNT(*) AS cnt FROM sign_words "
                "WHERE category <> '' GROUP BY category ORDER BY cnt DESC, category"
            ) as cur:
                return tuple((row["category"], row["cnt"]) for row in await cur.fetchall())

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
        clauses: list[str] = []
        params: list = []
        if keyword.strip():
            clauses.append("word_name LIKE ? ESCAPE '\\'")
            params.append(self._like_pattern(keyword))
        if category.strip():
            clauses.append("category LIKE ? ESCAPE '\\'")
            params.append(self._like_pattern(category))
        where = " AND ".join(clauses) if clauses else "1=1"
        return where, params

    async def search_words_by_filter(
        self, keyword: str = "", category: str = "", limit: int = 10, offset: int = 0
    ) -> list[aiosqlite.Row]:
        """
        단어명과 분류를 함께 걸어 검색합니다. (둘 다 비어 있으면 전체)
        정확히 일치하는 단어명 → 짧은 단어 → 가나다 순으로 정렬합니다.

        offset 은 페이지 버튼용입니다. (3페이지 = offset 20, limit 10)
        마지막에 word_id 로 한 번 더 정렬해, 단어명·분류가 같은 동음이의어도
        페이지를 오갈 때 순서가 바뀌거나 두 번 나오지 않게 합니다.
        """
        where, params = self._build_filter(keyword, category)
        async with self.conn.execute(
            f"SELECT * FROM sign_words WHERE {where} "
            "ORDER BY (word_name = ?) DESC, LENGTH(word_name), word_name, category, word_id "
            "LIMIT ? OFFSET ?",
            (*params, keyword.strip(), limit, offset),
        ) as cur:
            return list(await cur.fetchall())

    async def count_words_by_filter(self, keyword: str = "", category: str = "") -> int:
        """검색 조건에 맞는 전체 건수. (limit 과 무관한 실제 총계)"""
        where, params = self._build_filter(keyword, category)
        async with self.conn.execute(
            f"SELECT COUNT(*) FROM sign_words WHERE {where}", params
        ) as cur:
            (count,) = await cur.fetchone()
        return count

    async def get_homonym_names(self, names: list[str]) -> set[str]:
        """
        주어진 단어명 중 DB에 2건 이상 있는(동음이의어) 이름만 돌려줍니다.
        목록을 페이지로 나누면 '배' 두 건이 서로 다른 페이지에 걸릴 수 있어 DB 기준으로 확인합니다.
        """
        unique = list(dict.fromkeys(names))
        if not unique:
            return set()
        placeholders = ",".join("?" * len(unique))  # 값은 모두 ? 로 바인딩합니다
        async with self.conn.execute(
            f"SELECT word_name FROM sign_words WHERE word_name IN ({placeholders}) "
            "GROUP BY word_name HAVING COUNT(*) > 1",
            unique,
        ) as cur:
            return {row["word_name"] for row in await cur.fetchall()}

    async def get_random_words(self, limit: int) -> list[aiosqlite.Row]:
        """
        무작위 단어를 뽑되 단어명이 겹치지 않게 합니다.
        (퀴즈 보기에 '배'가 두 개 뜨면 고를 수 없으므로)
        """
        async with self.conn.execute(
            "SELECT * FROM sign_words GROUP BY word_name ORDER BY RANDOM() LIMIT ?", (limit,)
        ) as cur:
            return list(await cur.fetchall())

    async def get_word_by_name(self, word_name: str) -> aiosqlite.Row | None:
        """단어명으로 1건을 찾습니다. (동음이의어가 있으면 첫 번째)"""
        async with self.conn.execute(
            "SELECT * FROM sign_words WHERE word_name = TRIM(?) ORDER BY word_id LIMIT 1",
            (word_name,),
        ) as cur:
            return await cur.fetchone()

    async def get_words_by_name(self, word_name: str) -> list[aiosqlite.Row]:
        """같은 단어명을 가진 항목을 모두 가져옵니다. (동음이의어 확인용)"""
        async with self.conn.execute(
            "SELECT * FROM sign_words WHERE word_name = TRIM(?) ORDER BY word_id", (word_name,)
        ) as cur:
            return list(await cur.fetchall())

    async def get_word_by_id(self, word_id: int) -> aiosqlite.Row | None:
        """단어 ID로 정확히 1건을 찾습니다. (동음이의어를 구분해야 할 때)"""
        async with self.conn.execute(
            "SELECT * FROM sign_words WHERE word_id = ?", (word_id,)
        ) as cur:
            return await cur.fetchone()

    async def search_words(self, keyword: str, limit: int = 25) -> list[aiosqlite.Row]:
        """
        단어명 부분 일치 검색. 동음이의어는 각각 따로 나옵니다.
        정확히 일치하는 단어 → 짧은 단어 순으로 정렬합니다.
        """
        # 자동완성에서 글자를 칠 때마다 호출되므로 이스케이프·길이 제한을 꼭 거칩니다.
        async with self.conn.execute(
            "SELECT * FROM sign_words WHERE word_name LIKE ? ESCAPE '\\' "
            "ORDER BY (word_name = ?) DESC, LENGTH(word_name), word_name, category LIMIT ?",
            (self._like_pattern(keyword), keyword.strip(), limit),
        ) as cur:
            return list(await cur.fetchall())

    async def delete_word_by_name(self, word_name: str) -> int:
        """
        단어명이 같은 항목을 모두 삭제하고, 삭제된 행 수를 반환합니다.
        (동음이의어가 여러 건이면 함께 지워집니다)
        """
        # 지워질 단어를 담아 둔 단어장 항목도 함께 정리합니다. (퀴즈 기록은 이력으로 남김)
        await self.conn.execute(
            "DELETE FROM user_bookmarks WHERE word_id IN "
            "(SELECT word_id FROM sign_words WHERE word_name = TRIM(?))",
            (word_name,),
        )
        cur = await self.conn.execute(
            "DELETE FROM sign_words WHERE word_name = TRIM(?)", (word_name,)
        )
        await self.conn.commit()  # 두 DELETE 를 한 번에 확정합니다
        self._invalidate_cache()  # 단어 수 · 분류 목록이 바뀌었으므로
        return cur.rowcount

    async def delete_word_by_id(self, word_id: int) -> int:
        """동음이의어 중 하나만 골라 지울 때 사용합니다."""
        await self.conn.execute("DELETE FROM user_bookmarks WHERE word_id = ?", (word_id,))
        cur = await self.conn.execute("DELETE FROM sign_words WHERE word_id = ?", (word_id,))
        await self.conn.commit()
        self._invalidate_cache()  # 단어 수 · 분류 목록이 바뀌었으므로
        return cur.rowcount

    async def sync_api_words(self, words_data: list[tuple[str, ...]]) -> int:
        """
        API에서 받아 온 단어들을 저장합니다.
        (word_name, meaning, video_url[, image_url, category, detail_url]) 형태를 모두 받습니다.

        중복 판정은 (word_name, video_url) 복합 기준입니다.
        같은 단어라도 영상이 다르면 동음이의어로 보고 따로 저장하고,
        영상까지 같으면 뜻풀이·주소·분류를 최신 값으로 갱신합니다.

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
            await self.conn.executemany(
                """
                INSERT INTO sign_words
                    (word_name, meaning, video_url, image_url, category, detail_url)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(word_name, video_url) DO UPDATE SET
                    meaning    = excluded.meaning,
                    image_url  = excluded.image_url,
                    category   = excluded.category,
                    detail_url = excluded.detail_url
                """,
                rows,
            )
            await self.conn.commit()
        finally:
            # 분류가 갱신만 되어도 분류 목록이 바뀌고, 중간에 실패해도 일부 행이
            # 들어갔을 수 있으므로 성공·실패와 관계없이 항상 캐시를 비웁니다.
            self._invalidate_cache()
        return await self.count_words() - before  # 캐시가 비었으므로 새로 센 값

    # ── 유저 ─────────────────────────────────────────────────────
    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()

    async def claim_daily(
        self, user_id: int, today: date, points: int, exp: int
    ) -> tuple[bool, aiosqlite.Row]:
        """
        오늘 첫 /오늘의수어 이면 출석을 인정하고 보상을 지급합니다.

        반환: (이번에 새로 출석했는지, 최신 유저 정보)

        같은 날 두 번 눌러도 보상이 두 번 나가지 않도록, 날짜 조건을 UPDATE 문 안에
        넣어 한 번의 질의로 처리합니다. (동시에 두 번 눌러도 안전)
        """
        today_str = today.isoformat()
        yesterday_str = date.fromordinal(today.toordinal() - 1).isoformat()

        await self.conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        cur = await self.conn.execute(
            """
            UPDATE users SET
                points          = points + ?,
                exp             = exp + ?,
                streak          = CASE WHEN last_daily_date = ? THEN streak + 1 ELSE 1 END,
                last_daily_date = ?
            WHERE user_id = ?
              AND (last_daily_date IS NULL OR last_daily_date <> ?)
            """,
            (points, exp, yesterday_str, today_str, user_id, today_str),
        )
        await self.conn.commit()

        user = await self.get_user(user_id)
        assert user is not None
        return cur.rowcount > 0, user

    async def add_reward(self, user_id: int, points: int, exp: int) -> aiosqlite.Row:
        """포인트/경험치 지급 (유저가 없으면 새로 생성) 후 최신 정보를 반환합니다."""
        await self.conn.execute(
            """
            INSERT INTO users (user_id, points, exp) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                points = points + excluded.points,
                exp    = exp    + excluded.exp
            """,
            (user_id, points, exp),
        )
        await self.conn.commit()
        user = await self.get_user(user_id)
        assert user is not None
        return user

    # ── 퀴즈 기록 · 오답 복습 ────────────────────────────────────
    async def log_quiz_attempt(self, user_id: int, word_id: int, is_correct: bool) -> None:
        """퀴즈 한 문제의 결과를 남깁니다. (시간 초과는 오답으로 기록해 주세요)"""
        await self.conn.execute(
            "INSERT INTO quiz_logs (user_id, word_id, is_correct, solved_at) VALUES (?, ?, ?, ?)",
            (user_id, word_id, int(is_correct), _now_iso()),
        )
        await self.conn.commit()

    @staticmethod
    def _kst_day_bounds(today: date) -> tuple[str, str]:
        """
        KST 하루(00:00~24:00)를 quiz_logs.solved_at 과 비교할 UTC 문자열 구간으로 바꿉니다.

        solved_at 은 UTC ISO 8601 문자열이고 _now_iso() 가 늘 같은 형식('+00:00', 초 단위)으로
        남기므로, 사전순 문자열 비교만으로도 정확히 하루를 잘라낼 수 있습니다.
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
        async with self.conn.execute(
            """
            SELECT COUNT(*) FROM quiz_logs
            WHERE user_id = ? AND is_correct = 1
              AND solved_at >= ? AND solved_at < ?
            """,
            (user_id, start_utc, end_utc),
        ) as cur:
            (count,) = await cur.fetchone()
        return int(count)

    async def record_quiz_reward(
        self,
        user_id: int,
        word_id: int,
        today: date,
        *,
        points: int,
        exp: int,
        max_daily_rewards: int,
    ) -> tuple[bool, int, aiosqlite.Row | None]:
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

        반환: (보상 지급 여부, 오늘 사용한 보상 횟수, 최신 유저 정보 or None)
        """
        start_utc, end_utc = self._kst_day_bounds(today)

        cur = await self.conn.execute(
            "INSERT INTO quiz_logs (user_id, word_id, is_correct, solved_at) VALUES (?, ?, 1, ?)",
            (user_id, word_id, _now_iso()),
        )
        log_id = cur.lastrowid

        # 유저 행이 없을 수도 있으니 먼저 만들어 둡니다. (보상은 아래 UPDATE 에서만 나갑니다)
        await self.conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        cur = await self.conn.execute(
            """
            UPDATE users SET
                points = points + ?,
                exp    = exp + ?
            WHERE user_id = ?
              AND (SELECT COUNT(*) FROM quiz_logs
                   WHERE user_id = ? AND is_correct = 1
                     AND solved_at >= ? AND solved_at < ?
                     AND log_id < ?) < ?
            """,
            (points, exp, user_id, user_id, start_utc, end_utc, log_id, max_daily_rewards),
        )
        granted = cur.rowcount > 0
        await self.conn.commit()

        # 표시용 횟수도 log_id 기준으로 셉니다. ('내 앞의 정답 수 + 1' = 내 차례)
        # log_id 는 계속 커지기만 하므로, 동시에 여러 문제를 풀어도 이 값은 흔들리지 않습니다.
        # (전체 개수를 다시 세면 같은 순간의 다른 풀이까지 들어가 모두 같은 숫자로 보입니다)
        async with self.conn.execute(
            """
            SELECT COUNT(*) FROM quiz_logs
            WHERE user_id = ? AND is_correct = 1
              AND solved_at >= ? AND solved_at < ?
              AND log_id < ?
            """,
            (user_id, start_utc, end_utc, log_id),
        ) as cur:
            (earlier,) = await cur.fetchone()

        # 상한을 넘긴 뒤의 정답도 기록에는 남으므로, 막힌 경우에는 상한값으로 보여 줍니다.
        used = earlier + 1 if granted else max_daily_rewards
        user = await self.get_user(user_id) if granted else None
        return granted, used, user

    async def get_user_wrong_words(self, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        """
        유저가 틀린 뒤 아직 다시 맞히지 못한 단어를 돌려줍니다. (오답 복습 출제용)

        - 단어별 '마지막 풀이'가 오답인 것만 고릅니다.
          복습에서 맞히면 목록에서 빠지고, 다시 틀리면 돌아옵니다.
        - 많이 틀린 단어 → 최근에 틀린 단어 순으로 정렬합니다.
        - 순서 비교는 solved_at 대신 log_id 로 합니다. (같은 초에 기록돼도 앞뒤가 정확)
        - 삭제된 단어는 sign_words 와 JOIN 되지 않으므로 자동으로 빠집니다.
        - 각 행에는 단어 정보(sign_words 컬럼 전체)와 wrong_count(틀린 횟수)가 들어 있습니다.
        """
        async with self.conn.execute(
            """
            SELECT w.*, s.wrong_count
            FROM (
                SELECT word_id,
                       SUM(is_correct = 0)                           AS wrong_count,
                       MAX(CASE WHEN is_correct = 0 THEN log_id END) AS last_wrong_id,
                       MAX(CASE WHEN is_correct = 1 THEN log_id END) AS last_correct_id
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
        ) as cur:
            return list(await cur.fetchall())

    # ── 나만의 단어장 ────────────────────────────────────────────
    async def toggle_bookmark(self, user_id: int, word_id: int) -> bool:
        """
        단어장에 없으면 담고(True), 이미 있으면 뺍니다(False).
        사전에 없는 word_id 면 LookupError 를 냅니다.
        """
        cur = await self.conn.execute(
            "DELETE FROM user_bookmarks WHERE user_id = ? AND word_id = ?", (user_id, word_id)
        )
        if cur.rowcount > 0:
            await self.conn.commit()
            return False

        # 실제로 있는 단어일 때만 들어갑니다. (SELECT 결과가 없으면 아무것도 넣지 않음)
        # OR IGNORE: 같은 순간 두 번 처리돼 이미 들어가 있어도 UNIQUE 오류 없이 넘어갑니다.
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO user_bookmarks (user_id, word_id, created_at) "
            "SELECT ?, word_id, ? FROM sign_words WHERE word_id = ?",
            (user_id, _now_iso(), word_id),
        )
        await self.conn.commit()
        if cur.rowcount == 0 and await self.get_word_by_id(word_id) is None:
            raise LookupError(f"word_id={word_id} 단어가 사전에 없습니다.")
        return True

    async def get_user_bookmarks(self, user_id: int) -> list[aiosqlite.Row]:
        """
        유저가 담아 둔 단어를 최근에 담은 순서로 돌려줍니다.
        각 행에는 단어 정보(sign_words 컬럼 전체)와 bookmarked_at(담은 시각)이 들어 있습니다.
        """
        async with self.conn.execute(
            """
            SELECT w.*, b.created_at AS bookmarked_at
            FROM user_bookmarks AS b
            JOIN sign_words AS w ON w.word_id = b.word_id
            WHERE b.user_id = ?
            ORDER BY b.created_at DESC, b.rowid DESC
            """,
            (user_id,),
        ) as cur:
            return list(await cur.fetchall())
