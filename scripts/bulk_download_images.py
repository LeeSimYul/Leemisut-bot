"""
scripts/bulk_download_images.py
조교 이미숫 - 수형 사진 일괄 다운로드 (국내 PC 에서 받아 → 압축 → 운영 VM 으로 옮기기)

■ 왜 필요한가요?
    국립국어원(sldict.korean.go.kr)이 해외 IP 를 막아, 운영 VM(Oracle 오사카 리전)은 사진을
    직접 받을 수 없습니다. (curl 도 15초 연결 시간 초과 · 2026-10-07)
    국내 인터넷(통신사 IP)을 쓰는 PC 에서 이 스크립트로 전체 사진을 받아 압축한 뒤 VM 으로 옮기면,
    봇은 국립국어원에 가지 않고 디스크에 있는 사진을 바로 씁니다.

■ 파일 이름 규칙은 봇과 같습니다
    봇이 쓰는 코드를 그대로 불러 씁니다.
      - 사진 주소 고르기 : utils.media.pick_image  (봇의 word_image 와 같은 함수)
      - 받기 · 검사 · 저장 : utils.media.PhotoFetcher (data/cache/images/{주소 해시}.jpg)
    그래서 여기서 저장한 파일 이름이 봇이 찾는 이름과 똑같습니다.

■ 실행 (저장소 폴더에서)
    python scripts/bulk_download_images.py                    # .env 의 DATABASE_URL (없으면 로컬 SQLite)
    python scripts/bulk_download_images.py --concurrency 15   # 동시에 받을 장수 (1~15, 기본 10)
    python scripts/bulk_download_images.py --dsn "postgresql://..."   # .env 대신 직접 지정
    python scripts/bulk_download_images.py --no-archive       # 압축은 하지 않음

    이미 받은 사진은 건너뛰므로, 중간에 끊기거나 실패한 사진이 있으면 다시 실행해 이어 받으면 됩니다.
    끝나면 data/images_cache.tar.gz 로 압축합니다. (압축 안에는 images/ 폴더 하나)
    ⚠️ 이 스크립트는 DB 를 읽기만 하고 디스코드에는 접속하지 않습니다.
       PC 에서 main.py(봇)를 켜지는 마세요. VM 의 봇과 같은 토큰이라 명령어가 10062 로 실패합니다.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import tarfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))  # 저장소 루트의 database · utils 를 불러오기 위해

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

from database import Database  # noqa: E402
from utils import media  # noqa: E402

DEFAULT_SQLITE_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "imisut.db"))
DEFAULT_ARCHIVE = BASE_DIR / "data" / "images_cache.tar.gz"  # data/ 는 .gitignore 라 커밋되지 않습니다
DEFAULT_TIMEOUT = 15.0   # 초 - 한 번 요청에 기다리는 시간 (국내에서는 보통 1초 안에 끝납니다)
MAX_CONCURRENCY = 15     # 공공기관 서버에 한꺼번에 몰리지 않도록 상한을 둡니다
BAR_WIDTH = 30
SHOWN_FAILURES = 10      # 끝에 화면으로 보여 줄 실패 줄 수 (전체는 파일로)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="수형 사진을 일괄로 받아 봇의 디스크 캐시 규칙대로 저장하고 압축합니다.")
    parser.add_argument("--dsn", help="PostgreSQL 주소 (기본: .env 의 DATABASE_URL, 없으면 로컬 SQLite)")
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE_PATH, help="DATABASE_URL 이 없을 때 읽을 SQLite 파일")
    parser.add_argument(
        "--concurrency", type=int, default=10,
        help=f"동시에 받을 장수 (1~{MAX_CONCURRENCY}, 기본 10)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="한 번 요청에 기다리는 초 (기본 15)")
    parser.add_argument("--cache-dir", type=Path, default=media.PHOTO_CACHE_DIR, help="사진 저장 폴더 (기본: 봇과 같은 곳)")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="압축 파일 경로")
    parser.add_argument("--no-archive", action="store_true", help="압축하지 않습니다")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        parser.error(f"--concurrency 는 1~{MAX_CONCURRENCY} 사이로 지정해 주세요.")
    return args


class FailureLog(logging.Handler):
    """utils.media 가 남기는 실패 경고를 모아 둡니다. (진행 막대가 깨지지 않게 화면에는 바로 찍지 않음)"""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class Progress:
    """한 줄 진행 막대. 터미널이 아니면(파일로 저장 등) 10% 마다 한 줄씩 찍습니다."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0
        self.stats = {"cached": 0, "fetched": 0, "failed": 0}
        self.started = time.monotonic()
        self.interactive = sys.stdout.isatty()
        self._last_draw = 0.0
        self._last_decile = -1

    def add(self, outcome: str) -> None:
        self.done += 1
        self.stats[outcome] += 1
        self.draw()

    def draw(self, *, force: bool = False) -> None:
        ratio = self.done / self.total if self.total else 1.0
        now = time.monotonic()
        if self.interactive:
            if not force and now - self._last_draw < 0.1 and self.done < self.total:
                return
            self._last_draw = now
            filled = int(ratio * BAR_WIDTH)
            bar = "█" * filled + "░" * (BAR_WIDTH - filled)
            sys.stdout.write(f"\r[{bar}] {self.line(ratio, now)}   ")
            sys.stdout.flush()
        else:
            decile = int(ratio * 10)
            if decile != self._last_decile or (force and self.done < self.total):
                self._last_decile = decile
                print(self.line(ratio, now), flush=True)

    def line(self, ratio: float, now: float) -> str:
        s = self.stats
        return (
            f"{ratio * 100:5.1f}% {self.done}/{self.total} · 새로 받음 {s['fetched']} · "
            f"이미 있음 {s['cached']} · 실패 {s['failed']} · {now - self.started:.0f}초"
        )


async def load_image_urls(args: argparse.Namespace) -> tuple[dict[str, str], int, int, str]:
    """
    DB 에서 단어를 읽어 봇과 같은 규칙으로 사진 주소를 고릅니다.
    반환: ({사진 주소: 단어명}, 전체 단어 수, 사진 주소가 저장된 단어 수, DB 설명)
    """
    db = Database(args.sqlite, dsn=args.dsn, seed_mock=False)  # 더미 단어를 넣지 않습니다
    await db.connect()
    try:
        rows = await db.backend.fetch_all(
            "SELECT word_id, word_name, image_url, video_url FROM sign_words ORDER BY word_id"
        )
        described = db.describe()
    finally:
        await db.close()

    urls: dict[str, str] = {}
    with_image = 0
    for row in rows:
        if row["image_url"]:
            with_image += 1
        url = media.pick_image(row["image_url"], row["video_url"])  # 봇의 word_image() 와 같은 규칙
        if url and url not in urls:
            urls[url] = row["word_name"]
    return urls, len(rows), with_image, described


async def download_all(urls: list[str], args: argparse.Namespace) -> Progress:
    fetcher = media.PhotoFetcher(args.cache_dir)
    if fetcher.cache_dir is None:
        raise SystemExit(f"❌ 사진 저장 폴더를 만들 수 없습니다: {args.cache_dir}")
    progress = Progress(len(urls))
    gate = asyncio.Semaphore(args.concurrency)

    async def one(url: str) -> None:
        if await fetcher.is_cached(url):
            progress.add("cached")
            return
        async with gate:
            photo = await fetcher.fetch(url, timeout=args.timeout)
        progress.add("fetched" if photo is not None else "failed")

    try:
        progress.draw(force=True)
        await asyncio.gather(*(one(url) for url in urls))
        progress.draw(force=True)
        if progress.interactive:
            print()
    finally:
        await fetcher.close()
    return progress


def make_archive(cache_dir: Path, archive: Path) -> tuple[int, int]:
    """cache_dir 의 사진만 images/ 폴더로 묶어 tar.gz 로 압축합니다. 반환: (파일 수, 바이트)"""
    files = sorted(
        path for path in cache_dir.iterdir()
        if path.is_file() and path.suffix.lstrip(".") in ("jpg", "png", "gif", "webp")
    )
    archive.parent.mkdir(parents=True, exist_ok=True)
    temp = archive.with_name(archive.name + ".tmp")
    with tarfile.open(temp, "w:gz") as tar:
        for path in files:
            tar.add(path, arcname=f"images/{path.name}")
    os.replace(temp, archive)
    return len(files), archive.stat().st_size


async def main() -> int:
    args = parse_args()
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")  # 콘솔 인코딩이 달라도 막대 문자 때문에 멈추지 않게

    failures = FailureLog()
    media_log = logging.getLogger("utils.media")
    media_log.addHandler(failures)
    media_log.setLevel(logging.WARNING)
    media_log.propagate = False  # 진행 막대 사이에 경고가 끼어들지 않게 (끝에 모아서 보여 줌)

    print("📚 DB 에서 단어를 읽는 중…")
    urls, total_words, with_image, described = await load_image_urls(args)
    print(f"🗄️ {described}")
    print(f"   단어 {total_words}개 · 사진 주소 저장 {with_image}개 · 받을 사진(중복 제외) {len(urls)}장")
    print(f"📁 저장 위치: {args.cache_dir}")
    if not urls:
        print("❌ 받을 사진 주소가 없습니다. DATABASE_URL(.env)이 운영 DB 를 가리키는지 확인해 주세요.")
        return 1

    print(f"⬇️ 동시 {args.concurrency}장씩 받습니다. (한 번에 {args.timeout:g}초 · 실패하면 1번 더 시도)")
    progress = await download_all(list(urls), args)
    stats = progress.stats

    cached_now = stats["cached"] + stats["fetched"]
    print(
        f"✅ 끝: 새로 받음 {stats['fetched']} · 이미 있음 {stats['cached']} · 실패 {stats['failed']} "
        f"→ 저장된 사진 {cached_now}/{len(urls)}장 ({time.monotonic() - progress.started:.0f}초)"
    )

    failed_list = args.cache_dir.parent / "failed_images.txt"
    if failures.lines:
        failed_list.write_text("\n".join(failures.lines) + "\n", encoding="utf-8")
        print(f"⚠️ 경고 {len(failures.lines)}건 (받지 못한 사진 {stats['failed']}장) - 처음 {min(SHOWN_FAILURES, len(failures.lines))}줄:")
        for line in failures.lines[:SHOWN_FAILURES]:
            print(f"   {line}")
        print(f"   전체 목록: {failed_list}")
        print("   💡 잠시 뒤 같은 명령을 다시 실행하면 실패한 사진만 다시 받습니다.")
    elif failed_list.exists():
        failed_list.unlink()

    if args.no_archive:
        return 0
    print("🗜️ 압축하는 중…")
    count, size = make_archive(args.cache_dir, args.archive)
    print(f"📦 {args.archive} ({count}장 · {size / 1024 / 1024:.1f}MB)")
    print(f"   VM 에서 풀고 나면 'ls ~/Leemisut-bot/data/cache/images | wc -l' 이 {count} 이상이어야 합니다.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n⏹️ 중단했습니다. 받은 사진은 저장돼 있으니 다시 실행하면 이어서 받습니다.")
        sys.exit(130)
