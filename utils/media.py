"""
utils/media.py
조교 이미숫 - 임베드 사진 · 링크 버튼에 넣을 주소를 다듬고, 디스코드가 거절하면 미디어를 빼고 다시 보냅니다.

■ DB 는 그대로 둡니다
  국립국어원 주소는 http:// 로 저장돼 있어서 화면에 내보낼 때만 https:// 로 바꿉니다. (secure_url)
  DB 를 바꾸지 않는 이유: 동기화는 (단어명, 영상 주소) 원본으로 중복을 판정하므로,
  저장된 주소를 바꾸면 다음 동기화 때 같은 단어가 한 번 더 들어옵니다.
■ 디스코드가 거절할 주소는 미리 걸러 냅니다 (빈 문자열 → 사진 · 버튼을 건너뛰고 글과 링크만)
  - http(s) 가 아니거나 호스트가 없는 주소, 공백 · 제어 문자가 섞인 주소
  - 임베드 주소 EMBED_URL_LIMIT(2,048자) · 링크 버튼 주소 BUTTON_URL_LIMIT(512자) 초과
■ 그래도 디스코드가 400 으로 거절하면 사진 · 제목 링크 · 링크 버튼을 빼고 한 번 더 보냅니다.
  (send_with_media_fallback) 퀴즈 보기 버튼처럼 링크가 아닌 버튼은 그대로 둡니다.
■ 주소를 글자로만 다듬고 네트워크 요청은 보내지 않습니다. (3초 응답 제한에 영향 없음)
  주소 형식은 맞는데 파일이 없는(404) 사진은 디스코드가 오류 없이 받아 주므로 전송은 실패하지 않습니다.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

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


def _upgradable(host: str) -> bool:
    host = host.lower()
    return any(host == domain or host.endswith("." + domain) for domain in HTTPS_UPGRADE_DOMAINS)


def secure_url(url: str | None, *, limit: int = EMBED_URL_LIMIT) -> str:
    """
    디스코드로 내보내도 되는 주소로 다듬습니다. 쓸 수 없는 주소면 빈 문자열을 돌려줍니다.

    >>> secure_url("http://sldict.korean.go.kr/a.jpg")
    'https://sldict.korean.go.kr/a.jpg'
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
    if scheme == "http" and _upgradable(host):
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
    임베드에 띄울 수형 사진을 고릅니다. (https 로 다듬은 주소, 없으면 빈 문자열)
    사진이 없고 영상 자리에 gif 같은 사진이 들어온 단어는 그 사진을 씁니다.
    """
    for url in (image_url, video_url):
        if is_image_url(url):
            return secure_url(url)
    return ""


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
    **kwargs: Any,
) -> T:
    """
    send(embed=..., view=..., **kwargs) 를 부르고, 디스코드가 400 으로 거절하면
    미디어를 빼고 딱 한 번 더 보냅니다. 두 번째도 실패하면 그 오류를 그대로 올립니다.

    send 로는 followup.send · response.edit_message · message.edit 를 넘깁니다.
    (응답이 거절되면 그 상호작용은 아직 응답 전이라 같은 함수로 다시 보낼 수 있습니다)
    view 가 None 이거나 비어 있으면 view 없이 보냅니다.
    ⚠️ 수정(edit)에 쓸 때는 링크를 빼도 버튼이 남는 뷰(퀴즈 보기 등)만 넘겨 주세요.
       view 를 생략한 수정은 '기존 버튼 유지'라서 빼려던 링크 버튼이 그대로 남습니다.
    """

    def view_kwargs() -> dict[str, Any]:
        return {"view": view} if view is not None and view.children else {}

    try:
        return await send(embed=embed, **view_kwargs(), **kwargs)
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
