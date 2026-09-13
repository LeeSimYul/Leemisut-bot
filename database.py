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
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,          -- 디스코드 유저 ID
    points         INTEGER NOT NULL DEFAULT 0,
    exp            INTEGER NOT NULL DEFAULT 0,
    level          INTEGER NOT NULL DEFAULT 1,
    streak         INTEGER NOT NULL DEFAULT 0,   -- 연속 퀴즈 참여 일수
    last_quiz_date TEXT                          -- 'YYYY-MM-DD' (KST 기준)
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


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DB가 아직 연결되지 않았습니다. connect()를 먼저 호출하세요.")
        return self._conn

    # ── 연결 / 초기화 ─────────────────────────────────────────────
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row  # row["컬럼명"] 으로 접근 가능
        await self._conn.execute("PRAGMA journal_mode=WAL")
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

    async def _migrate(self) -> None:
        """
        예전 스키마를 새 스키마로 옮깁니다. (데이터와 유저 포인트는 그대로 유지)

        1) image_url · detail_url 컬럼이 없으면 추가
        2) word_name 에 걸려 있던 UNIQUE 제약을 UNIQUE(word_name, video_url) 로 교체
           - SQLite는 제약 조건만 떼어낼 수 없어서 테이블을 다시 만들고 데이터를 옮깁니다.
        """
        async with self.conn.execute("PRAGMA table_info(sign_words)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

        for column in ("image_url", "detail_url"):
            if column not in columns:
                await self.conn.execute(
                    f"ALTER TABLE sign_words ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
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
        async with self.conn.execute("SELECT COUNT(*) FROM sign_words") as cur:
            (count,) = await cur.fetchone()
        return count

    async def count_distinct_words(self) -> int:
        """동음이의어를 하나로 세었을 때의 단어 수."""
        async with self.conn.execute("SELECT COUNT(DISTINCT word_name) FROM sign_words") as cur:
            (count,) = await cur.fetchone()
        return count

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

    async def search_words(self, keyword: str, limit: int = 25) -> list[aiosqlite.Row]:
        """
        단어명 부분 일치 검색. 동음이의어는 각각 따로 나옵니다.
        정확히 일치하는 단어 → 짧은 단어 순으로 정렬합니다.
        """
        cleaned = keyword.strip()
        escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        async with self.conn.execute(
            "SELECT * FROM sign_words WHERE word_name LIKE ? ESCAPE '\\' "
            "ORDER BY (word_name = ?) DESC, LENGTH(word_name), word_name, category LIMIT ?",
            (pattern, cleaned, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def delete_word_by_name(self, word_name: str) -> int:
        """
        단어명이 같은 항목을 모두 삭제하고, 삭제된 행 수를 반환합니다.
        (동음이의어가 여러 건이면 함께 지워집니다)
        """
        cur = await self.conn.execute(
            "DELETE FROM sign_words WHERE word_name = TRIM(?)", (word_name,)
        )
        await self.conn.commit()
        return cur.rowcount

    async def delete_word_by_id(self, word_id: int) -> int:
        """동음이의어 중 하나만 골라 지울 때 사용합니다."""
        cur = await self.conn.execute("DELETE FROM sign_words WHERE word_id = ?", (word_id,))
        await self.conn.commit()
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
        return await self.count_words() - before

    # ── 유저 ─────────────────────────────────────────────────────
    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()

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
