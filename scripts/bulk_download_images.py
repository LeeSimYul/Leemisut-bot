"""
scripts/bulk_download_images.py
조교 이미숫 - 수형 사진 일괄 다운로드 (국내 PC 에서 받아 → 압축 → 운영 VM 으로 옮기기)

■ 왜 필요한가요?
    운영 VM(Oracle 오사카 리전)에서 국립국어원(sldict.korean.go.kr) 사진을 받지 못했습니다.
    국립국어원에 접속되는 PC 에서 이 스크립트로 전체 사진을 받아 압축한 뒤 VM 으로 옮기면,
    봇은 국립국어원에 가지 않고 디스크에 있는 사진을 바로 씁니다.
    ⚠️ 2026-10-08 국내 PC · 일반 브라우저에서도 사진 주소에 연결되지 않았습니다. 이럴 때는
       먼저 --probe 로 진단하고, 서버가 복구된 뒤에 받으면 됩니다.

■ 파일 이름 규칙은 봇과 같습니다
    봇이 쓰는 코드를 그대로 불러 씁니다.
      - 사진 주소 고르기 : utils.media.pick_images (봇의 word_images 와 같은 함수 · 단어마다 여러 장)
      - 받기 · 검사 · 저장 : utils.media.PhotoFetcher (data/cache/images/{주소 해시}.jpg)
    https 로 받더라도 파일 이름은 DB 에 있는 원래 주소 기준이라 봇이 찾는 이름과 똑같습니다.

■ 실행 (저장소 폴더에서)
    python scripts/bulk_download_images.py --probe            # 사진 한 장으로 연결만 진단하고 끝냄
    python scripts/bulk_download_images.py                    # 진단 → 되는 방식(http/https)으로 전체 받기
    python scripts/bulk_download_images.py --scheme https     # 진단 없이 https 로 받기
    python scripts/bulk_download_images.py --concurrency 15   # 동시에 받을 장수 (1~15, 기본 10)
    python scripts/bulk_download_images.py --no-archive       # 압축은 하지 않음

    먼저 사진 한 장으로 DNS · 연결(80/443) · http/https 받기를 진단하고, 둘 다 안 되면 전체 받기를
    시작하지 않고 바로 끝냅니다. (서버가 멈춘 상태에서 수천 장을 기다리지 않도록)
    받는 도중에 서버가 멈추면 남은 사진은 건너뛰고 끝냅니다. 이미 받은 사진은 저장돼 있으므로
    나중에 같은 명령으로 다시 실행하면 이어서 받습니다.
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
import socket
import sys
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))  # 저장소 루트의 database · utils 를 불러오기 위해

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

import aiohttp  # noqa: E402

from database import Database  # noqa: E402
from utils import media  # noqa: E402

DEFAULT_SQLITE_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "imisut.db"))
DEFAULT_ARCHIVE = BASE_DIR / "data" / "images_cache.tar.gz"  # data/ 는 .gitignore 라 커밋되지 않습니다
DEFAULT_TIMEOUT = 5.0    # 초 - 한 번 요청에 기다리는 시간 (연결 자체는 utils.media.PHOTO_CONNECT_TIMEOUT 3초)
PROBE_TIMEOUT = 5.0      # 초 - 진단에서 한 단계(DNS · 연결)마다 기다리는 시간
MAX_CONCURRENCY = 15     # 공공기관 서버에 한꺼번에 몰리지 않도록 상한을 둡니다
BAR_WIDTH = 30
LIVE_WARNINGS = 5        # 받는 도중 바로 화면에 보여 줄 실패 줄 수
SHOWN_FAILURES = 10      # 끝에 화면으로 보여 줄 실패 줄 수 (전체는 파일로)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="수형 사진을 일괄로 받아 봇의 디스크 캐시 규칙대로 저장하고 압축합니다.")
    parser.add_argument("--probe", nargs="?", const="", metavar="URL",
                        help="사진 한 장으로 연결만 진단하고 끝냅니다 (URL 을 생략하면 DB 의 첫 사진)")
    parser.add_argument("--scheme", choices=("auto", "http", "https"), default="auto",
                        help="받는 방식. auto 는 먼저 진단해서 되는 쪽을 고릅니다 (기본 auto)")
    parser.add_argument("--dsn", help="PostgreSQL 주소 (기본: .env 의 DATABASE_URL, 없으면 로컬 SQLite)")
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE_PATH, help="DATABASE_URL 이 없을 때 읽을 SQLite 파일")
    parser.add_argument("--concurrency", type=int, default=10, help=f"동시에 받을 장수 (1~{MAX_CONCURRENCY}, 기본 10)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="한 번 요청에 기다리는 초 (기본 5)")
    parser.add_argument("--cache-dir", type=Path, default=media.PHOTO_CACHE_DIR, help="사진 저장 폴더 (기본: 봇과 같은 곳)")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="압축 파일 경로")
    parser.add_argument("--no-archive", action="store_true", help="압축하지 않습니다")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        parser.error(f"--concurrency 는 1~{MAX_CONCURRENCY} 사이로 지정해 주세요.")
    return args


# ── 진단 ────────────────────────────────────────────────────────
def _with_scheme(url: str, scheme: str) -> str:
    return urlunsplit(urlsplit(url)._replace(scheme=scheme))


async def probe(url: str) -> str | None:
    """
    사진 한 장으로 DNS → 연결(TCP) → http · https 받기를 차례로 확인해 화면에 적습니다.
    받을 수 있는 방식('http' 또는 'https')을 돌려주고, 둘 다 안 되면 None 입니다.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    print(f"🔎 연결 진단: {url}")

    loop = asyncio.get_running_loop()
    started = time.monotonic()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), PROBE_TIMEOUT)
        addresses = ", ".join(sorted({info[4][0] for info in infos}))
        print(f"   1) DNS         ✅ {host} → {addresses}")
    except Exception as exc:  # 진단이므로 어떤 오류든 그대로 보여 줍니다
        print(f"   1) DNS         ❌ {host} → {type(exc).__name__} · {exc} ({time.monotonic() - started:.1f}초)")
        print("   ⇒ 도메인을 찾지 못합니다. 인터넷 연결 · DNS 를 확인하거나, 국립국어원 도메인 장애일 수 있습니다.")
        return None

    tcp_ok = False
    for port in (parts.port or 80, 443):
        started = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), PROBE_TIMEOUT)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            tcp_ok = True
            print(f"   2) 연결 {port:<5}  ✅ {(time.monotonic() - started) * 1000:.0f}ms")
        except asyncio.TimeoutError:
            print(f"   2) 연결 {port:<5}  ❌ TimeoutError · {PROBE_TIMEOUT:g}초 안에 연결되지 않음 (서버 무응답)")
        except Exception as exc:
            print(f"   2) 연결 {port:<5}  ❌ {type(exc).__name__} · {exc} ({time.monotonic() - started:.1f}초)")

    working: str | None = None
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT * 2, sock_connect=PROBE_TIMEOUT)
    async with aiohttp.ClientSession(headers=media.PHOTO_REQUEST_HEADERS) as session:
        for scheme in ("http", "https"):
            candidate = _with_scheme(url, scheme)
            started = time.monotonic()
            try:
                async with session.get(candidate, timeout=timeout) as resp:
                    body = await resp.read()
                    kind = media.sniff_image(body)
                    ok = resp.status == 200 and kind is not None
                    print(
                        f"   3) {scheme.upper():<5} 받기  {'✅' if ok else '⚠️'} HTTP {resp.status} · "
                        f"{resp.headers.get('Content-Type', '?')} · {len(body):,}바이트 · "
                        f"{'사진(' + kind + ')' if kind else '사진 아님'} ({time.monotonic() - started:.1f}초)"
                    )
                    if ok and working is None:
                        working = scheme
            except Exception as exc:
                print(
                    f"   3) {scheme.upper():<5} 받기  ❌ {media.describe_error(exc, PROBE_TIMEOUT * 2)} "
                    f"({time.monotonic() - started:.1f}초)"
                )

    if working == "http":
        print("   ⇒ http 로 받을 수 있습니다.")
    elif working == "https":
        print("   ⇒ https 로만 받을 수 있습니다. (일괄 받기는 https 로 요청하고, 파일 이름은 원래 주소 기준)")
    elif not tcp_ok:
        print("   ⇒ 서버에 연결 자체가 안 됩니다 (80 · 443 모두). 국립국어원 서버 장애이거나 이 네트워크에서 막힌 상태입니다.")
        print("     휴대폰 데이터(LTE/5G) 등 다른 망에서도 같으면 서버 장애입니다. 복구된 뒤 다시 실행해 주세요.")
    else:
        print("   ⇒ 서버와 연결은 되지만 사진을 주지 않습니다. 위 받기 결과의 HTTP 상태를 확인해 주세요.")
    return working


# ── 화면 표시 ───────────────────────────────────────────────────
class FailureLog(logging.Handler):
    """utils.media 의 경고를 모읍니다. 처음 LIVE_WARNINGS 줄은 받는 도중에 바로 보여 줍니다."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[str] = []
        self.printer: Callable[[str], None] = print

    def emit(self, record: logging.LogRecord) -> None:
        line = record.getMessage()
        self.lines.append(line)
        if len(self.lines) <= LIVE_WARNINGS or line.startswith("🚧"):
            self.printer(f"   ⚠️ {line}")
            if len(self.lines) == LIVE_WARNINGS:
                self.printer("   … (이후 실패는 끝에 모아서 보여 드립니다)")


class Progress:
    """한 줄 진행 막대. 터미널이 아니면(파일로 저장 등) 10% 마다 한 줄씩 찍습니다."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0
        self.stats = {"cached": 0, "fetched": 0, "failed": 0, "skipped": 0}
        self.started = time.monotonic()
        self.interactive = sys.stdout.isatty()
        self._last_draw = 0.0
        self._last_decile = -1

    def add(self, outcome: str) -> None:
        self.done += 1
        self.stats[outcome] += 1
        self.draw()

    def print_above(self, text: str) -> None:
        """진행 막대를 지우고 한 줄 찍은 뒤 막대를 다시 그립니다."""
        if self.interactive:
            sys.stdout.write("\r" + " " * (BAR_WIDTH + 100) + "\r")
        print(text, flush=True)
        if self.interactive:
            self.draw(force=True)

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
        skipped = f" · 건너뜀 {s['skipped']}" if s["skipped"] else ""
        return (
            f"{ratio * 100:5.1f}% {self.done}/{self.total} · 새로 받음 {s['fetched']} · "
            f"이미 있음 {s['cached']} · 실패 {s['failed']}{skipped} · {now - self.started:.0f}초"
        )


# ── 받기 ────────────────────────────────────────────────────────
async def load_image_urls(args: argparse.Namespace) -> tuple[dict[str, str], int, int, str]:
    """
    DB 에서 단어를 읽어 봇과 같은 규칙으로 사진 주소를 고릅니다. (수형 이미지가 여러 장이면 모두)
    반환: ({사진 주소: 단어명}, 전체 단어 수, 사진 주소가 저장된 단어 수, DB 설명)
    """
    db = Database(args.sqlite, dsn=args.dsn, seed_mock=False)  # 더미 단어를 넣지 않습니다
    await db.connect()
    try:
        rows = await db.backend.fetch_all(
            "SELECT word_id, word_name, image_url, image_urls, video_url FROM sign_words ORDER BY word_id"
        )
        described = db.describe()
    finally:
        await db.close()

    urls: dict[str, str] = {}
    with_image = 0
    for row in rows:
        if row["image_url"] or row["image_urls"]:
            with_image += 1
        # 봇의 word_images() 와 같은 규칙 (스토리보드는 봇이 받은 이미지로 그때그때 만듭니다)
        for url in media.pick_images(row["image_urls"], row["image_url"], row["video_url"]):
            urls.setdefault(url, row["word_name"])
    return urls, len(rows), with_image, described


async def download_all(urls: list[str], args: argparse.Namespace, *, https: bool, failures: FailureLog) -> Progress:
    # 여러 장을 동시에 받으므로 몇 장 실패했다고 서버가 멈췄다고 보지 않습니다. (완전히 멈췄을 때만 건너뜀)
    fetcher = media.PhotoFetcher(
        args.cache_dir, https_download=https, breaker_threshold=max(10, args.concurrency * 2)
    )
    if fetcher.cache_dir is None:
        raise SystemExit(f"❌ 사진 저장 폴더를 만들 수 없습니다: {args.cache_dir}")
    progress = Progress(len(urls))
    failures.printer = progress.print_above
    gate = asyncio.Semaphore(args.concurrency)

    async def one(url: str) -> None:
        if await fetcher.is_cached(url):
            progress.add("cached")
            return
        async with gate:
            if fetcher.host_down(url):  # 서버가 멈춘 것으로 보이면 기다리지 않고 넘어갑니다
                progress.add("skipped")
                return
            photo = await fetcher.fetch(url, timeout=args.timeout)
        progress.add("fetched" if photo is not None else "failed")

    try:
        progress.draw(force=True)
        await asyncio.gather(*(one(url) for url in urls))
        progress.draw(force=True)
        if progress.interactive:
            print()
    finally:
        failures.printer = print
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
    media_log.propagate = False  # 진행 막대 사이에 경고가 끼어들지 않게

    if args.probe:  # --probe URL : DB 없이 그 주소만 진단
        return 0 if await probe(args.probe) else 2

    print("📚 DB 에서 단어를 읽는 중…")
    urls, total_words, with_image, described = await load_image_urls(args)
    print(f"🗄️ {described}")
    print(f"   단어 {total_words}개 · 사진 주소 저장 {with_image}개 · 받을 사진(중복 제외) {len(urls)}장")
    if not urls:
        print("❌ 받을 사진 주소가 없습니다. DATABASE_URL(.env)이 운영 DB 를 가리키는지 확인해 주세요.")
        return 1

    if args.probe is not None:  # --probe (주소 생략) : DB 의 첫 사진으로 진단만
        return 0 if await probe(next(iter(urls))) else 2

    scheme = args.scheme
    if scheme == "auto":
        scheme = await probe(next(iter(urls)))
        if scheme is None:
            print("⏹️ 진단에서 사진을 받을 수 없어 전체 받기를 시작하지 않았습니다. (위 진단 결과 참고)")
            return 2
    print(f"📁 저장 위치: {args.cache_dir}")
    print(
        f"⬇️ {scheme} 로 동시 {args.concurrency}장씩 받습니다. "
        f"(연결 {media.PHOTO_CONNECT_TIMEOUT:g}초 · 한 번에 {args.timeout:g}초 · 실패하면 1번 더 시도)"
    )
    progress = await download_all(list(urls), args, https=scheme == "https", failures=failures)
    stats = progress.stats

    cached_now = stats["cached"] + stats["fetched"]
    print(
        f"✅ 끝: 새로 받음 {stats['fetched']} · 이미 있음 {stats['cached']} · 실패 {stats['failed']} · "
        f"건너뜀 {stats['skipped']} → 저장된 사진 {cached_now}/{len(urls)}장 "
        f"({time.monotonic() - progress.started:.0f}초)"
    )
    if stats["skipped"]:
        print("🚧 받는 도중 서버가 응답하지 않아 남은 사진을 건너뛰었습니다. 서버가 복구되면 같은 명령으로 이어 받으세요.")

    failed_list = args.cache_dir.parent / "failed_images.txt"
    if failures.lines:
        failed_list.write_text("\n".join(failures.lines) + "\n", encoding="utf-8")
        shown = min(SHOWN_FAILURES, len(failures.lines))
        print(f"⚠️ 경고 {len(failures.lines)}건 (받지 못한 사진 {stats['failed']}장) - 처음 {shown}줄:")
        for line in failures.lines[:SHOWN_FAILURES]:
            print(f"   {line}")
        print(f"   전체 목록: {failed_list}")
        print("   💡 잠시 뒤 같은 명령을 다시 실행하면 받지 못한 사진만 다시 받습니다.")
    elif failed_list.exists():
        failed_list.unlink()

    if args.no_archive:
        return 0
    if cached_now == 0:
        print("📦 저장된 사진이 없어 압축하지 않았습니다.")
        return 2
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
