"""
utils/media.py
조교 이미숫 - 임베드 사진 · 링크 버튼에 넣을 주소를 다듬고, 디스코드가 거절하면 미디어를 빼고 다시 보냅니다.

■ DB 는 그대로 둡니다
  국립국어원 주소는 http:// 로 저장돼 있습니다. 사람이 브라우저로 여는 링크(버튼 · 제목 · 글 링크)만
  내보낼 때 https:// 로 바꿉니다. (secure_url)
  DB 를 바꾸지 않는 이유: 동기화는 (단어명, 영상 주소) 원본으로 중복을 판정하므로,
  저장된 주소를 바꾸면 다음 동기화 때 같은 단어가 한 번 더 들어옵니다.
■ 임베드 사진은 봇이 직접 받아 첨부 파일로 붙입니다 (PhotoFetcher)
  주소만 넘기면(set_image(url=...)) 디스코드 미디어 프록시가 국립국어원 서버에서 사진을 받아 와야 하는데,
  운영 서버에서 http · https 어느 쪽으로도 사진이 나타나지 않았습니다(2026-10-07).
  그래서 봇이 사진을 받아 attachment://sign.jpg 로 함께 올립니다.
  국립국어원 서버가 매우 느려서(2초 안에 못 받음 · 2026-10-07) 받은 사진은 디스크에 영구 보관합니다.
    1) 메모리 → 2) 디스크(data/cache/images/) → 3) 국립국어원 (한 번에 5초 · 1회 재시도)
  끝내 받지 못하면 사진 칸을 비우고 '사진을 불러오지 못했다'는 안내를 붙입니다.
  (디스코드 프록시도 같은 서버에서 못 받으므로 주소를 넘겨 봐야 빈칸만 남습니다)
■ 디스코드가 거절할 주소는 미리 걸러 냅니다 (빈 문자열 → 사진 · 버튼을 건너뛰고 글과 링크만)
  - http(s) 가 아니거나 호스트가 없는 주소, 공백 · 제어 문자가 섞인 주소
  - 임베드 주소 EMBED_URL_LIMIT(2,048자) · 링크 버튼 주소 BUTTON_URL_LIMIT(512자) 초과
■ 그래도 디스코드가 400 으로 거절하면 사진 · 제목 링크 · 링크 버튼을 빼고 한 번 더 보냅니다.
  (send_with_media_fallback) 퀴즈 보기 버튼처럼 링크가 아닌 버튼은 그대로 둡니다.
■ 주소 다듬기(secure_url 등)는 글자만 보고 네트워크 요청은 보내지 않습니다.
  네트워크 · 디스크를 쓰는 것은 PhotoFetcher 뿐이고, 명령어가 응답을 미룬(defer) 뒤에만 불리므로
  3초 응답 제한에 걸리지 않습니다. 디스크 읽기 · 쓰기는 별도 스레드에서 해서 이벤트 루프를 막지 않습니다.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import aiohttp
import discord

log = logging.getLogger(__name__)

T = TypeVar("T")

IMAGE_EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")

EMBED_URL_LIMIT = 2048  # 임베드 사진 · 제목 링크 주소의 최대 길이
BUTTON_URL_LIMIT = 512  # 링크 버튼 주소의 최대 길이

# 이 도메인(하위 도메인 포함)의 http 주소는 https 로 바꿔서 내보냅니다. (국립국어원)
HTTPS_UPGRADE_DOMAINS = ("korean.go.kr",)

# 주소에 있으면 안 되는 글자: 공백 · 제어 문자 · 꺾쇠 · 큰따옴표
_UNSAFE_CHARS = re.compile(r'[\s\x00-\x1f\x7f<>"]')

# ── 사진 직접 받기 (PhotoFetcher) ──
PHOTO_FETCH_TIMEOUT = 5.0        # 초 - 한 번 받는 데 기다리는 최대 시간 (명령어는 defer 뒤라 여유가 있음)
PHOTO_FETCH_ATTEMPTS = 2         # 시간 초과 · 연결 끊김 · 5xx 일 때 한 번 더 시도
PHOTO_RETRY_BACKOFF = 0.5        # 초 - 다시 시도하기 전 잠깐 쉬는 시간
PHOTO_PREFETCH_TIMEOUT = 20.0    # 초 - 미리 받기(기다리는 사람이 없음)는 한 번에 더 오래 기다립니다
PHOTO_MAX_BYTES = 2 * 1024 * 1024  # 이보다 큰 파일은 받지 않습니다 (수형 사진은 10~30KB)
PHOTO_CACHE_SIZE = 512           # 메모리에 보관할 사진 수 (디스크에는 개수 제한 없이 보관)
PHOTO_RETRY_AFTER = 600.0        # 초 - 끝내 받지 못한 주소는 이 시간 동안 다시 시도하지 않습니다
# 디스크 캐시 위치 (.gitignore 의 data/ 아래라 저장소에 올라가지 않습니다)
PHOTO_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "images"
PHOTO_MISSING_NOTICE = (
    "국립국어원 미디어 서버가 늦게 응답해 사진을 불러오지 못했어요. "
    "아래 🎬 수어 영상 버튼으로 동작을 확인해 주세요."
)
PHOTO_FILENAME = "sign"          # 첨부 파일 이름 (확장자는 받은 파일을 보고 붙임 · 단어명이 드러나지 않음)
# 국립국어원에 사진을 요청할 때의 헤더 - 일반 브라우저와 같은 모양으로 보냅니다.
# ⚠️ 운영 VM(Oracle 오사카)은 헤더와 관계없이 국립국어원에 연결 자체가 안 됩니다. (해외 IP 차단 ·
#    curl 도 브라우저 헤더 · 봇 헤더 모두 15초 연결 시간 초과 · 2026-10-07) 그래서 VM 의 사진은
#    국내 PC 에서 scripts/bulk_download_images.py 로 미리 받아 옮겨 둔 디스크 캐시로 보여 줍니다.
#    이 헤더는 국내 PC 의 일괄 다운로드도 같은 PhotoFetcher 로 받기 때문에 브라우저와 같은 모양으로 둡니다.
PHOTO_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://sldict.korean.go.kr/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}

_reported_unusable: set[str] = set()  # 형식 오류로 건너뛴 사진 주소 (같은 경고를 반복하지 않도록)


def _upgradable(host: str) -> bool:
    host = host.lower()
    return any(host == domain or host.endswith("." + domain) for domain in HTTPS_UPGRADE_DOMAINS)


def secure_url(url: str | None, *, limit: int = EMBED_URL_LIMIT, upgrade: bool = True) -> str:
    """
    디스코드로 내보내도 되는 주소로 다듬습니다. 쓸 수 없는 주소면 빈 문자열을 돌려줍니다.
    upgrade=False 면 검증만 하고 http 를 https 로 바꾸지 않습니다. (디스코드가 직접 받아 오는 사진용)

    >>> secure_url("http://sldict.korean.go.kr/a.jpg")
    'https://sldict.korean.go.kr/a.jpg'
    >>> secure_url("http://sldict.korean.go.kr/a.jpg", upgrade=False)
    'http://sldict.korean.go.kr/a.jpg'
    >>> secure_url("sldict.korean.go.kr/a.jpg")
    ''
    """
    text = (url or "").strip()
    if not text or _UNSAFE_CHARS.search(text):
        return ""
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:  # 'http://[깨진주소' 처럼 해석할 수 없는 주소
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return ""
    if upgrade and scheme == "http" and _upgradable(host):
        text = "https" + text[len(parts.scheme):]
    return text if len(text) <= limit else ""


def _has_extension(url: str, extensions: tuple[str, ...]) -> bool:
    return bool(url) and urlsplit(url).path.lower().endswith(extensions)


def is_image_url(url: str | None) -> bool:
    """임베드에 띄울 수 있는 사진 파일 주소인지."""
    return _has_extension(secure_url(url), IMAGE_EXTENSIONS)


def is_video_url(url: str | None) -> bool:
    """수어 영상 파일 주소인지."""
    return _has_extension(secure_url(url), VIDEO_EXTENSIONS)


def pick_image(image_url: str | None, video_url: str | None) -> str:
    """
    임베드에 띄울 수형 사진을 고릅니다. (검증만 한 원본 주소, 없으면 빈 문자열)
    사진이 없고 영상 자리에 gif 같은 사진이 들어온 단어는 그 사진을 씁니다.
    """
    for url in (image_url, video_url):
        if is_image_url(url):
            return secure_url(url, upgrade=False)
    if image_url and image_url not in _reported_unusable and len(_reported_unusable) < 1000:
        # DB 에 사진 주소가 있는데 쓸 수 없으면 사진 칸이 조용히 빠지므로 한 번은 알립니다.
        _reported_unusable.add(image_url)
        log.warning("저장된 사진 주소의 형식이 올바르지 않아 건너뜁니다: %r", image_url[:300])
    return ""


# ── 사진 직접 받기 ──────────────────────────────────────────────
@dataclass(frozen=True)
class Photo:
    """봇이 받아 둔 사진. 보낼 때마다 to_file() 로 새 파일 객체를 만듭니다. (파일 객체는 한 번 보내면 닫힘)"""

    data: bytes
    filename: str  # 'sign.jpg' 처럼 확장자까지

    def to_file(self) -> discord.File:
        return discord.File(io.BytesIO(self.data), filename=self.filename)


class PhotoError(Exception):
    """사진을 받았지만 쓸 수 없을 때. retryable 이면 잠시 뒤 다시 받으면 될 수도 있는 오류(5xx)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def sniff_image(data: bytes) -> str | None:
    """파일 앞부분을 보고 사진 확장자를 정합니다. 사진이 아니면 None. (오류 안내 HTML 페이지 등)"""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


_DISK_EXTENSIONS = ("jpg", "png", "gif", "webp")


class PhotoFetcher:
    """
    임베드 사진을 봇이 직접 받아 첨부 파일로 바꿉니다. (attach)

    찾는 순서: 메모리(최근 PHOTO_CACHE_SIZE 장) → 디스크(cache_dir/{주소 해시}.jpg) → 국립국어원
      - 국립국어원에서는 한 번에 PHOTO_FETCH_TIMEOUT(5초)까지 기다리고, 시간 초과 · 연결 끊김 · 5xx 면
        PHOTO_RETRY_BACKOFF(0.5초) 쉬고 한 번 더 받습니다. 404 · 사진이 아닌 응답은 다시 받지 않습니다.
      - 받은 사진은 디스크에 영구 보관합니다. (임시 파일에 쓴 뒤 이름을 바꿔 반쯤 쓴 파일이 남지 않음)
      - 끝내 받지 못한 주소는 PHOTO_RETRY_AFTER(10분) 동안 다시 시도하지 않습니다.
      - 같은 주소를 동시에 여러 번 찾으면 한 번만 받고 나눠 씁니다. (미리 받기와 명령어가 겹쳐도)
    디스크 폴더를 만들 수 없으면 디스크 캐시 없이(메모리만) 동작합니다.
    Cog 가 하나를 만들어 쓰고, Cog 를 내릴 때 close() 합니다.
    """

    def __init__(self, cache_dir: Path | None = PHOTO_CACHE_DIR) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._memory: OrderedDict[str, Photo] = OrderedDict()
        self._failed_at: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task[Photo | None]] = {}
        self._confirmed = False  # 첫 다운로드 성공을 로그로 한 번 남겼는지
        self.cache_dir: Path | None = None
        if cache_dir is not None:
            try:
                os.makedirs(cache_dir, exist_ok=True)  # 봇이 켜질 때 한 번 (없으면 만듦)
                self.cache_dir = cache_dir
            except OSError as exc:
                log.warning("사진 디스크 캐시 폴더를 만들지 못해 메모리에만 보관합니다: %s (%s)", cache_dir, exc)

    async def close(self) -> None:
        for task in self._inflight.values():
            task.cancel()
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=PHOTO_REQUEST_HEADERS)
        return self._session

    # ── 바깥에서 쓰는 함수 ───────────────────────────────────────
    async def attach(self, embed: discord.Embed) -> Photo | None:
        """
        임베드에 걸린 사진 주소를 받아 와서 attachment:// 로 바꾸고 그 사진을 돌려줍니다.
        보낼 때 send_with_media_fallback(..., photo=사진) 으로 함께 넘겨 주세요.
        끝내 받지 못하면 사진 칸을 비우고 안내 칸(📷 사진)을 붙인 뒤 None 을 돌려줍니다.
        (사진이 원래 없는 단어는 아무것도 바꾸지 않습니다)
        """
        url = embed.image.url
        if not url or not url.startswith(("http://", "https://")):
            return None
        photo = await self.fetch(url)
        if photo is not None:
            embed.set_image(url=f"attachment://{photo.filename}")
        else:
            embed.set_image(url=None)
            embed.add_field(name="📷 사진", value=PHOTO_MISSING_NOTICE, inline=False)
        return photo

    async def fetch(self, url: str, *, timeout: float = PHOTO_FETCH_TIMEOUT) -> Photo | None:
        """
        사진 한 장을 찾습니다. 못 찾으면 None. (이 함수는 예외를 올리지 않습니다)
        timeout 은 국립국어원에 한 번 요청할 때 기다리는 시간입니다. (미리 받기는 더 길게)
        """
        photo = self._memory.get(url)
        if photo is not None:
            self._memory.move_to_end(url)
            return photo
        failed_at = self._failed_at.get(url)
        if failed_at is not None and time.monotonic() - failed_at < PHOTO_RETRY_AFTER:
            return None

        task = self._inflight.get(url)
        if task is None:
            task = asyncio.create_task(self._load(url, timeout))
            self._inflight[url] = task
            task.add_done_callback(lambda _t, url=url: self._inflight.pop(url, None))
        # 이미 받는 중이면(예: 미리 받기) 그 결과를 같이 기다리되, 내 몫의 시간까지만 기다립니다.
        # 기다리던 쪽이 먼저 포기하거나 취소돼도 받기는 끝까지 해서 디스크에 남깁니다.
        limit = timeout * PHOTO_FETCH_ATTEMPTS + PHOTO_RETRY_BACKOFF + 1.0
        try:
            return await asyncio.wait_for(asyncio.shield(task), limit)
        except asyncio.TimeoutError:
            return None

    async def is_cached(self, url: str) -> bool:
        """이미 메모리나 디스크에 있는 사진인지. (국립국어원에 요청하지 않습니다)"""
        return url in self._memory or await self._disk_path(url) is not None

    async def prefetch(self, urls: list[str], *, concurrency: int = 2) -> dict[str, int]:
        """
        사진 여러 장을 미리 받아 디스크에 쌓아 둡니다. (기다리는 사람이 없으므로 시간을 넉넉히)
        반환: {'cached': 이미 있던 장수, 'fetched': 새로 받은 장수, 'failed': 못 받은 장수}
        """
        stats = {"cached": 0, "fetched": 0, "failed": 0}
        gate = asyncio.Semaphore(concurrency)  # 느린 서버에 한꺼번에 몰리지 않도록

        async def one(url: str) -> None:
            if await self.is_cached(url):
                stats["cached"] += 1
                return
            self._failed_at.pop(url, None)  # 미리 받기는 최근 실패 기록과 관계없이 한 번 더 시도
            async with gate:
                photo = await self.fetch(url, timeout=PHOTO_PREFETCH_TIMEOUT)
            stats["fetched" if photo is not None else "failed"] += 1

        await asyncio.gather(*(one(url) for url in dict.fromkeys(urls)))
        return stats

    # ── 내부 처리 ───────────────────────────────────────────────
    async def _load(self, url: str, timeout: float) -> Photo | None:
        """디스크 → 국립국어원 순서로 찾고, 새로 받으면 디스크에 저장합니다."""
        photo = await self._read_disk(url)
        if photo is None:
            started = time.monotonic()
            try:
                photo = await self._download_with_retry(url, timeout)
            except (aiohttp.ClientError, asyncio.TimeoutError, PhotoError) as exc:
                self._failed_at[url] = time.monotonic()
                reason = str(exc) or type(exc).__name__
                if isinstance(exc, asyncio.TimeoutError):
                    reason = f"{timeout:g}초 안에 받지 못함"
                log.warning("수형 사진을 받지 못했습니다 (재시도 포함): %s (%s)", url, reason)
                return None
            except Exception:
                # 예상하지 못한 오류도 명령어까지 올리지 않습니다. (사진 없이 보내면 그만)
                self._failed_at[url] = time.monotonic()
                log.exception("수형 사진을 받다가 예상하지 못한 오류: %s", url)
                return None
            await self._write_disk(url, photo)
            if not self._confirmed:
                self._confirmed = True
                log.info(
                    "🖼️ 수형 사진 직접 첨부 동작 확인: %s (%.1fKB · %.2f초)",
                    url, len(photo.data) / 1024, time.monotonic() - started,
                )

        self._failed_at.pop(url, None)
        self._memory[url] = photo
        self._memory.move_to_end(url)
        if len(self._memory) > PHOTO_CACHE_SIZE:
            self._memory.popitem(last=False)  # 가장 오래 안 쓴 사진부터 메모리에서 뺍니다 (디스크엔 남음)
        return photo

    async def _download_with_retry(self, url: str, timeout: float) -> Photo:
        for attempt in range(1, PHOTO_FETCH_ATTEMPTS + 1):
            try:
                return await self._download(url, timeout)
            except (aiohttp.ClientError, asyncio.TimeoutError, PhotoError) as exc:
                if attempt == PHOTO_FETCH_ATTEMPTS or not _retryable(exc):
                    raise
                log.info("사진 받기 다시 시도 (%s): %s", str(exc) or type(exc).__name__, url)
                await asyncio.sleep(PHOTO_RETRY_BACKOFF * attempt)
        raise AssertionError("unreachable")

    async def _download(self, url: str, timeout: float) -> Photo:
        session = self._ensure_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise PhotoError(f"HTTP {resp.status}", retryable=resp.status >= 500 or resp.status == 429)
            if resp.content_length is not None and resp.content_length > PHOTO_MAX_BYTES:
                raise PhotoError(f"파일이 너무 큼 ({resp.content_length:,}바이트)")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > PHOTO_MAX_BYTES:
                    raise PhotoError(f"파일이 너무 큼 ({PHOTO_MAX_BYTES:,}바이트 초과)")
                chunks.append(chunk)
            content_type = resp.headers.get("Content-Type", "?")

        data = b"".join(chunks)
        extension = sniff_image(data)
        if extension is None:
            raise PhotoError(f"사진 파일이 아님 (Content-Type: {content_type} · {len(data)}바이트)")
        return Photo(data, f"{PHOTO_FILENAME}.{extension}")

    # ── 디스크 캐시 (읽기 · 쓰기는 별도 스레드) ─────────────────
    def _disk_stem(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

    async def _disk_path(self, url: str) -> Path | None:
        stem = self._disk_stem(url)
        if stem is None:
            return None

        def find() -> Path | None:
            for extension in _DISK_EXTENSIONS:
                path = stem.with_suffix(f".{extension}")
                if path.is_file():
                    return path
            return None

        return await asyncio.to_thread(find)

    async def _read_disk(self, url: str) -> Photo | None:
        path = await self._disk_path(url)
        if path is None:
            return None
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            log.warning("디스크에 저장된 사진을 읽지 못했습니다: %s (%s)", path, exc)
            return None
        extension = sniff_image(data)
        if extension is None:  # 깨진 파일은 지우고 다시 받습니다
            await asyncio.to_thread(path.unlink, missing_ok=True)
            return None
        return Photo(data, f"{PHOTO_FILENAME}.{extension}")

    async def _write_disk(self, url: str, photo: Photo) -> None:
        stem = self._disk_stem(url)
        if stem is None:
            return
        path = stem.with_suffix("." + photo.filename.rsplit(".", 1)[-1])

        def write() -> None:
            temp = path.with_name(path.name + ".tmp")
            temp.write_bytes(photo.data)
            os.replace(temp, path)  # 다 쓴 뒤에 이름을 바꿔, 읽는 쪽이 반쯤 쓴 파일을 보지 않게 합니다

        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            log.warning("받은 사진을 디스크에 저장하지 못했습니다: %s (%s)", path, exc)


def _retryable(exc: BaseException) -> bool:
    """잠시 뒤 다시 받으면 될 수도 있는 오류인지. (시간 초과 · 연결 끊김 · 5xx)"""
    if isinstance(exc, PhotoError):
        return exc.retryable
    return isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError, aiohttp.ClientPayloadError))


# ── 링크 버튼 ───────────────────────────────────────────────────
def link_buttons(
    *, video_url: str | None = "", detail_url: str | None = "", row: int | None = None
) -> list[discord.ui.Button]:
    """'🎬 수어 영상 보기' · '📖 국립국어원 사전' 버튼. 주소를 쓸 수 없는 버튼은 만들지 않습니다."""
    buttons: list[discord.ui.Button] = []
    for label, emoji, raw in (
        ("수어 영상 보기", "🎬", video_url),
        ("국립국어원 사전", "📖", detail_url),
    ):
        url = secure_url(raw, limit=BUTTON_URL_LIMIT)
        if url:
            buttons.append(discord.ui.Button(label=label, emoji=emoji, url=url, row=row))
    return buttons


def link_view(*, video_url: str | None = "", detail_url: str | None = "") -> discord.ui.View | None:
    """링크 버튼만 담은 뷰. 붙일 버튼이 없으면 None 을 돌려줍니다."""
    buttons = link_buttons(video_url=video_url, detail_url=detail_url)
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)  # 링크 버튼은 봇이 처리할 일이 없어 시간 제한이 필요 없습니다
    for button in buttons:
        view.add_item(button)
    return view


# ── 400 거절 대비 ───────────────────────────────────────────────
def _link_items(view: discord.ui.View | None) -> list[discord.ui.Button]:
    if view is None:
        return []
    return [item for item in view.children if isinstance(item, discord.ui.Button) and item.url]


def has_media(embed: discord.Embed, view: discord.ui.View | None) -> bool:
    """디스코드가 주소를 검사하는 부분(사진 · 썸네일 · 제목 링크 · 링크 버튼)이 있는지."""
    return bool(embed.image.url or embed.thumbnail.url or embed.url or _link_items(view))


def strip_media(embed: discord.Embed, view: discord.ui.View | None) -> None:
    """사진 · 썸네일 · 제목 링크 · 링크 버튼을 뺍니다. 설명 칸 안의 글 링크는 남습니다."""
    embed.set_image(url=None)
    embed.set_thumbnail(url=None)
    embed.url = None
    for item in _link_items(view):
        view.remove_item(item)  # type: ignore[union-attr]


async def send_with_media_fallback(
    send: Callable[..., Awaitable[T]],
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
    photo: Photo | None = None,
    **kwargs: Any,
) -> T:
    """
    send(embed=..., view=..., **kwargs) 를 부르고, 디스코드가 400 으로 거절하면
    미디어를 빼고 딱 한 번 더 보냅니다. 두 번째도 실패하면 그 오류를 그대로 올립니다.

    photo 는 PhotoFetcher.attach() 가 돌려준 사진입니다. 첨부 파일(file=)로 함께 보내고,
    다시 보낼 때는 사진을 뺐으므로 첨부하지 않습니다.
    수정(edit)에는 photo 를 넘기지 마세요. 처음 보낼 때 올린 첨부 파일은 수정해도 그대로 남고,
    임베드의 attachment:// 주소도 그 파일을 계속 가리킵니다.

    send 로는 followup.send · response.edit_message · message.edit 를 넘깁니다.
    (응답이 거절되면 그 상호작용은 아직 응답 전이라 같은 함수로 다시 보낼 수 있습니다)
    view 가 None 이거나 비어 있으면 view 없이 보냅니다.
    ⚠️ 수정(edit)에 쓸 때는 링크를 빼도 버튼이 남는 뷰(퀴즈 보기 등)만 넘겨 주세요.
       view 를 생략한 수정은 '기존 버튼 유지'라서 빼려던 링크 버튼이 그대로 남습니다.
    """

    def view_kwargs() -> dict[str, Any]:
        return {"view": view} if view is not None and view.children else {}

    file_kwargs = {"file": photo.to_file()} if photo is not None else {}
    try:
        return await send(embed=embed, **view_kwargs(), **file_kwargs, **kwargs)
    except discord.HTTPException as error:
        if error.status != 400 or not has_media(embed, view):
            raise
        log.warning(
            "디스코드가 사진 · 링크가 담긴 메시지를 거절해(400) 미디어를 빼고 다시 보냅니다. "
            "(제목: %s · 사진: %s) %s",
            embed.title, embed.image.url or "없음", error.text or error,
        )
        strip_media(embed, view)
        return await send(embed=embed, **view_kwargs(), **kwargs)
