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
  받지 못하면(2초 초과 · 404 · 사진이 아닌 응답) 원본 주소(http)를 그대로 넘겨 디스코드가 직접 시도하게 둡니다.
  디스코드도 못 받으면 사진 칸 없이 설명 · 링크만 보입니다.
■ 디스코드가 거절할 주소는 미리 걸러 냅니다 (빈 문자열 → 사진 · 버튼을 건너뛰고 글과 링크만)
  - http(s) 가 아니거나 호스트가 없는 주소, 공백 · 제어 문자가 섞인 주소
  - 임베드 주소 EMBED_URL_LIMIT(2,048자) · 링크 버튼 주소 BUTTON_URL_LIMIT(512자) 초과
■ 그래도 디스코드가 400 으로 거절하면 사진 · 제목 링크 · 링크 버튼을 빼고 한 번 더 보냅니다.
  (send_with_media_fallback) 퀴즈 보기 버튼처럼 링크가 아닌 버튼은 그대로 둡니다.
■ 주소 다듬기(secure_url 등)는 글자만 보고 네트워크 요청은 보내지 않습니다.
  네트워크를 쓰는 것은 PhotoFetcher 뿐이고, 명령어가 응답을 미룬(defer) 뒤에만 불리므로
  3초 응답 제한에 걸리지 않습니다. (한 장에 최대 PHOTO_FETCH_TIMEOUT 초)
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
PHOTO_FETCH_TIMEOUT = 2.0        # 초 - 한 장을 받는 데 기다리는 최대 시간
PHOTO_MAX_BYTES = 2 * 1024 * 1024  # 이보다 큰 파일은 받지 않습니다 (수형 사진은 10~30KB)
PHOTO_CACHE_SIZE = 512           # 메모리에 보관할 사진 수 (최대 수십 MB 이내)
PHOTO_RETRY_AFTER = 600.0        # 초 - 받지 못한 주소는 이 시간 동안 다시 시도하지 않습니다
PHOTO_FILENAME = "sign"          # 첨부 파일 이름 (확장자는 받은 파일을 보고 붙임 · 단어명이 드러나지 않음)
PHOTO_USER_AGENT = "Mozilla/5.0 (compatible; LeemisutBot/1.0; +https://github.com/LeeSimYul/Leemisut-bot)"

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
    """사진을 받았지만 쓸 수 없을 때. (HTTP 오류 · 너무 큼 · 사진이 아님)"""


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


class PhotoFetcher:
    """
    임베드 사진을 봇이 직접 받아 첨부 파일로 바꿉니다. (attach)
    - 한 장에 PHOTO_FETCH_TIMEOUT(2초) · PHOTO_MAX_BYTES(2MB) 까지만 받습니다.
    - 받은 사진은 PHOTO_CACHE_SIZE 장까지 메모리에 두고 다시 씁니다.
    - 받지 못한 주소는 PHOTO_RETRY_AFTER(10분) 동안 다시 시도하지 않습니다. (매번 2초씩 늦어지지 않게)
    Cog 가 하나를 만들어 쓰고, Cog 를 내릴 때 close() 합니다.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: OrderedDict[str, Photo] = OrderedDict()
        self._failed_at: dict[str, float] = {}
        self._confirmed = False  # 첫 성공을 로그로 한 번 남겼는지

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": PHOTO_USER_AGENT})
        return self._session

    async def attach(self, embed: discord.Embed) -> Photo | None:
        """
        임베드에 걸린 사진 주소를 받아 와서 attachment:// 로 바꾸고 그 사진을 돌려줍니다.
        보낼 때 send_with_media_fallback(..., photo=사진) 으로 함께 넘겨 주세요.
        받지 못하면 None 이고, 임베드에는 원본 주소가 그대로 남습니다. (디스코드가 직접 시도)
        """
        url = embed.image.url
        if not url or not url.startswith(("http://", "https://")):
            return None
        photo = await self.fetch(url)
        if photo is not None:
            embed.set_image(url=f"attachment://{photo.filename}")
        return photo

    async def fetch(self, url: str) -> Photo | None:
        if url in self._cache:
            self._cache.move_to_end(url)
            return self._cache[url]
        failed_at = self._failed_at.get(url)
        if failed_at is not None and time.monotonic() - failed_at < PHOTO_RETRY_AFTER:
            return None

        started = time.monotonic()
        try:
            photo = await self._download(url)
        except (aiohttp.ClientError, asyncio.TimeoutError, PhotoError) as exc:
            self._failed_at[url] = time.monotonic()
            reason = str(exc) or type(exc).__name__
            if isinstance(exc, asyncio.TimeoutError):
                reason = f"{PHOTO_FETCH_TIMEOUT:g}초 안에 받지 못함"
            log.warning("수형 사진을 직접 받지 못해 주소만 넘깁니다: %s (%s)", url, reason)
            return None

        self._failed_at.pop(url, None)
        self._cache[url] = photo
        if len(self._cache) > PHOTO_CACHE_SIZE:
            self._cache.popitem(last=False)  # 가장 오래 안 쓴 사진부터 버립니다
        if not self._confirmed:
            self._confirmed = True
            log.info(
                "🖼️ 수형 사진 직접 첨부 동작 확인: %s (%.1fKB · %.2f초)",
                url, len(photo.data) / 1024, time.monotonic() - started,
            )
        return photo

    async def _download(self, url: str) -> Photo:
        session = self._ensure_session()
        timeout = aiohttp.ClientTimeout(total=PHOTO_FETCH_TIMEOUT)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise PhotoError(f"HTTP {resp.status}")
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
