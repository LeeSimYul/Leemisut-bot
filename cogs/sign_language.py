"""
cogs/sign_language.py
조교 이미숫 - '오늘의 수어' · '수어 퀴즈' · '수어 검색' · '나만의 단어장'

관리자용 명령어(/수어동기화, /수어전체동기화, /수어삭제, /수어목록)는 cogs/admin.py 에 있습니다.

■ /오늘의수어 는 유저마다 다른 단어를 배정하고, 하루 한 번 출석 보상을 줍니다.
■ 링크 표기 원칙
   - 메시지 본문(content)에는 주소를 넣지 않습니다. 긴 URL이 그대로 보이기 때문입니다.
   - 영상·사전 링크는 임베드 안에서 마스크 링크([보이는 글](주소))로만 보여 줍니다.
■ 정답 은닉 원칙
   사전 상세 페이지는 주소를 누르면 단어명이 보이므로 퀴즈가 끝난 뒤에만 붙입니다.
■ 모든 슬래시 명령어는 시작 직후 defer()로 응답을 미뤄 3초 타임아웃(10062)을 막습니다.
■ /수어검색 결과가 여러 개면 5개씩 페이지 버튼(◀ 이전 · 1 / N · ▶ 다음)으로 넘겨 봅니다.
   (공용 컴포넌트: utils/paginator.py)
■ /수어퀴즈 는 30% 확률로 '틀린 뒤 아직 못 맞힌 단어'를 정답으로 내는 복습 문제를 냅니다.
   풀이 결과(정답 · 오답 · 시간 초과)는 모두 quiz_logs 에 기록됩니다.
■ 퀴즈 카드·버튼·채점 결과는 명령어를 부른 본인에게만 보입니다. (ephemeral · 채널 도배 방지)
■ 퀴즈 보상은 하루 MAX_DAILY_QUIZ_REWARDS 회까지만 지급합니다.
   상한을 채운 뒤에도 문제 풀이와 학습 기록(quiz_logs)은 제한 없이 계속됩니다.
■ /단어장저장 · /수어단어장 으로 나만의 단어장을 관리합니다. (본인에게만 보이는 메시지)
"""
from __future__ import annotations

import logging
import random
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from database import Database
from utils.paginator import SignPaginatorView, page_count

log = logging.getLogger(__name__)

# ── 설정값 ──────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))  # zoneinfo는 Windows에서 tzdata가 필요해서 고정 오프셋 사용
QUIZ_CHOICES = 4
QUIZ_TIMEOUT = 45  # 초
REWARD_POINTS = 10
REWARD_EXP = 10
# 포인트 어뷰징 방지: 하루에 퀴즈 보상을 받을 수 있는 최대 횟수 (10회 = 최대 100pt / 100exp)
# 상한을 채워도 퀴즈 풀이·오답 복습 기록은 제한 없이 계속 쌓입니다.
MAX_DAILY_QUIZ_REWARDS = 10
DAILY_POINTS = 5   # /오늘의수어 출석 보상
DAILY_EXP = 5
SEARCH_PAGE_SIZE = 5    # /수어검색 결과 목록 한 페이지에 보여 줄 건수
BOOKMARK_PAGE_SIZE = 5  # /수어단어장 한 페이지에 보여 줄 건수

# 오답 복습: /수어퀴즈 에서 이 확률로 '틀린 뒤 아직 못 맞힌 단어'를 정답으로 냅니다.
REVIEW_PROBABILITY = 0.3
REVIEW_POOL_SIZE = 10   # 복습 후보로 볼 오답 수 (많이 틀린 → 최근에 틀린 순)
REVIEW_NOTICE = "🔁 틀린 단어는 나중에 복습 문제로 다시 나올 수 있어요."

# /단어장저장 자동완성 후보를 고르면 들어오는 값 ('#단어ID') - 동음이의어를 정확히 구분합니다.
BOOKMARK_ID_PATTERN = re.compile(r"#(\d{1,19})")

# 연타 방지: 유저당 COOLDOWN_SECONDS 초에 1회 (명령어마다 따로 계산)
COOLDOWN_SECONDS = 3.0
COOLDOWN_MESSAGE = f"조교가 조금 바빠요! {COOLDOWN_SECONDS:g}초 후에 다시 시도해 주세요 ⏱️"

# True로 바꾸면 본문에 영상 주소를 넣어 디스코드 플레이어가 뜹니다.
# 대신 긴 주소가 글로 보입니다. (기본값 False = 깔끔한 마스크 링크만)
SHOW_VIDEO_PLAYER = False

IMAGE_EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")

EMBED_FIELD_LIMIT = 1024  # 디스코드 임베드 필드 한 칸의 최대 길이
MAX_FIELD_CHUNKS = 4      # 임베드 전체 6000자 제한을 넘지 않도록 한 설명의 최대 칸 수
                          # (실제 수어 설명은 100~200자라 여기에 걸릴 일은 거의 없습니다)

COLOR_DAILY = discord.Color.from_rgb(255, 183, 77)
COLOR_SEARCH = discord.Color.from_rgb(126, 179, 255)
COLOR_QUIZ = discord.Color.blurple()
COLOR_CORRECT = discord.Color.green()
COLOR_WRONG = discord.Color.red()
COLOR_TIMEOUT = discord.Color.light_grey()
COLOR_BOOKMARK = discord.Color.from_rgb(122, 198, 160)


def today_kst() -> date:
    return datetime.now(KST).date()


def _row_get(row: aiosqlite.Row, key: str, default: str = "") -> str:
    """예전 DB에 없는 컬럼(image_url, detail_url)을 안전하게 읽습니다."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return value if value else default


def _is_http(url: str) -> bool:
    return bool(url) and url.startswith(("http://", "https://"))


def is_image_url(url: str) -> bool:
    return _is_http(url) and urlsplit(url).path.lower().endswith(IMAGE_EXTENSIONS)


def is_video_url(url: str) -> bool:
    return _is_http(url) and urlsplit(url).path.lower().endswith(VIDEO_EXTENSIONS)


def has_quiz_media(word: aiosqlite.Row) -> bool:
    """퀴즈 문제로 보여 줄 수형 사진이나 영상이 있는지 확인합니다."""
    return is_image_url(_row_get(word, "image_url")) or is_video_url(word["video_url"])


def masked(label: str, url: str) -> str:
    """디스코드 마스크 링크. 주소가 비어 있으면 빈 문자열을 돌려줍니다."""
    return f"[{label}]({url})" if _is_http(url) else ""


def summarize(meaning: str, limit: int = 45) -> str:
    """목록에서 동음이의어를 구분할 수 있을 만큼만 뜻을 줄입니다."""
    text = " ".join((meaning or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def add_full_text_field(embed: discord.Embed, name: str, text: str) -> None:
    """
    긴 글을 잘라내지 않고 넣습니다.
    한 칸(1024자)을 넘으면 문장 단위로 나눠 '(이어서)' 칸을 추가합니다.
    """
    text = (text or "").strip()
    if not text:
        return

    chunks: list[str] = []
    remaining = text
    while len(remaining) > EMBED_FIELD_LIMIT:
        window = remaining[:EMBED_FIELD_LIMIT]
        # 문장이 끊기지 않도록 마침표 → 공백 순으로 자를 자리를 찾습니다.
        cut = max(window.rfind(". "), window.rfind("다. "), window.rfind("\n"))
        if cut < EMBED_FIELD_LIMIT // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = EMBED_FIELD_LIMIT
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    chunks.append(remaining)

    # 임베드 전체 길이 제한(6000자)을 넘지 않도록 안전장치를 둡니다.
    if len(chunks) > MAX_FIELD_CHUNKS:
        chunks = chunks[:MAX_FIELD_CHUNKS]
        chunks[-1] += "\n…(이어지는 내용은 아래 사전 링크에서 확인해 주세요)"

    for index, chunk in enumerate(chunks):
        embed.add_field(name=name if index == 0 else f"{name} (이어서)", value=chunk, inline=False)


def set_media(embed: discord.Embed, image_url: str, video_url: str) -> str | None:
    """
    임베드에 보여 줄 미디어를 정합니다.
    반환값은 메시지 본문에 넣을 내용입니다. (기본 설정에서는 항상 None)
    """
    if is_image_url(image_url):
        embed.set_image(url=image_url)
    elif is_image_url(video_url):  # 영상 자리에 gif가 들어온 경우
        embed.set_image(url=video_url)

    if SHOW_VIDEO_PLAYER and is_video_url(video_url):
        return video_url
    return None


def link_field_value(word: aiosqlite.Row, *, include_detail: bool) -> str:
    """영상·사전 링크를 한 줄로 모읍니다."""
    links = [masked("🎬 수어 영상", word["video_url"])]
    if include_detail:
        links.append(masked("📖 국립국어원 사전", _row_get(word, "detail_url")))
    return " · ".join(link for link in links if link)


def build_word_embed(
    word: aiosqlite.Row, *, title: str, color: discord.Color
) -> tuple[discord.Embed, str | None]:
    """
    단어 하나를 자세히 보여 주는 임베드를 만듭니다. (/오늘의수어, /수어검색 공용)
    반환: (임베드, 메시지 본문에 넣을 내용)
    """
    detail_url = _row_get(word, "detail_url")
    embed = discord.Embed(title=title, url=detail_url or None, color=color)
    add_full_text_field(embed, "✋ 수어 설명", word["meaning"])

    links = link_field_value(word, include_detail=True)
    if links:
        embed.add_field(name="🔗 바로가기", value=links, inline=False)

    content = set_media(embed, _row_get(word, "image_url"), word["video_url"])
    return embed, content


def build_search_page_embed(
    words: list[aiosqlite.Row], *, conditions: str, total: int, offset: int
) -> discord.Embed:
    """/수어검색 결과 목록의 한 페이지. (번호는 전체 결과 기준으로 이어서 매깁니다)"""
    lines = [
        f"`{number}.` **{w['word_name']}** [{w['category']}] — {summarize(w['meaning'])}"
        for number, w in enumerate(words, start=offset + 1)
    ]
    body = "\n".join(lines) or "이 페이지의 단어가 방금 삭제되었어요 🥲"
    embed = discord.Embed(
        title=f"🔍 수어 검색 결과 (총 {total}개)",
        description=f"{conditions}\n\n{body}",
        color=COLOR_SEARCH,
    )
    embed.set_footer(text="자세히 보려면 단어명을 정확히 넣어 다시 검색해 주세요 🤟")
    return embed


def build_bookmark_page_embed(
    words: list[aiosqlite.Row], *, owner_name: str, total: int, offset: int
) -> discord.Embed:
    """/수어단어장 한 페이지. 단어마다 영상·사전 링크를 붙여 바로 복습할 수 있게 합니다."""
    lines = []
    for number, w in enumerate(words, start=offset + 1):
        line = f"`{number}.` **{w['word_name']}** [{w['category']}] — {summarize(w['meaning'])}"
        links = link_field_value(w, include_detail=True)
        if links:
            line += f"\n└ {links}"
        lines.append(line)

    embed = discord.Embed(
        title=f"📒 {owner_name} 님의 수어 단어장 (총 {total}개)",
        description="\n".join(lines),
        color=COLOR_BOOKMARK,
    )
    embed.set_footer(text="최근에 담은 순서 · 빼려면 /단어장저장 에서 같은 단어를 한 번 더 🤟")
    return embed


# ── 퀴즈 UI ─────────────────────────────────────────────────────
class QuizChoiceButton(discord.ui.Button["SignQuizView"]):
    def __init__(self, word_id: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.word_id = word_id

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.handle_answer(interaction, self)


class SignQuizView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        owner: discord.abc.User,
        answer: aiosqlite.Row,
        choices: list[aiosqlite.Row],
        embed: discord.Embed,
        *,
        is_review: bool = False,
    ) -> None:
        super().__init__(timeout=QUIZ_TIMEOUT)
        self.db = db
        self.owner = owner
        self.answer = answer
        self.embed = embed
        self.is_review = is_review  # 오답 복습으로 나온 문제인지 (결과 문구가 달라집니다)
        # followup.send(wait=True) 로 받은 메시지 (시간 초과 시 수정용)
        self.message: discord.Message | discord.WebhookMessage | None = None
        self.answered = False
        # 정답이 드러나는 주소는 여기에만 보관하고, 끝난 뒤에 꺼냅니다.
        self.detail_url = _row_get(answer, "detail_url")

        for word in choices:
            self.add_item(QuizChoiceButton(word["word_id"], word["word_name"]))

    def _reveal(self, chosen: QuizChoiceButton | None = None) -> None:
        """버튼을 모두 잠그고 정답(초록)/내가 고른 오답(빨강)을 표시합니다."""
        for item in self.children:
            if isinstance(item, QuizChoiceButton):
                item.disabled = True
                if item.word_id == self.answer["word_id"]:
                    item.style = discord.ButtonStyle.success
                elif item is chosen:
                    item.style = discord.ButtonStyle.danger

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner.id:
            return True
        # 버튼 클릭은 별개의 상호작용이라 defer 없이 바로 응답해도 됩니다.
        await interaction.response.send_message(
            f"이 문제는 **{self.owner.display_name}** 님의 퀴즈예요! 🙌\n"
            "`/수어퀴즈` 로 직접 도전해 보세요!",
            ephemeral=True,
        )
        return False

    async def handle_answer(self, interaction: discord.Interaction, chosen: QuizChoiceButton) -> None:
        # 버튼 연타로 보상이 두 번 들어가는 것 방지 (await 전에 플래그를 세움)
        if self.answered:
            await interaction.response.defer()
            return
        self.answered = True
        self.stop()
        self._reveal(chosen)

        name = self.answer["word_name"]
        is_correct = chosen.word_id == self.answer["word_id"]
        # 기록과 보상을 한 번에 처리합니다. (상한선 확인은 DB 안에서 원자적으로 이뤄집니다)
        granted, used_today, user = await self._record(is_correct)

        if is_correct:
            self.embed.color = COLOR_CORRECT
            self.embed.title = f"🎉 정답이에요! — {name} [{self.answer['category']}]"
            praise = (
                "🎓 **복습 성공!** 전에 틀렸던 단어를 이번엔 맞히셨어요."
                if self.is_review else "눈썰미가 대단하시네요 👏"
            )
            if granted and user is not None:
                reward_line = (
                    f"✨ 포인트 **+{REWARD_POINTS}** · 경험치 **+{REWARD_EXP}** "
                    f"(오늘 보상 {used_today}/{MAX_DAILY_QUIZ_REWARDS}회)\n"
                    f"현재 포인트 {user['points']} · 경험치 {user['exp']}"
                )
            elif used_today < 0:
                # DB 오류로 기록조차 남기지 못한 경우 (상한선과 무관) - 솔직하게 알려 줍니다.
                reward_line = (
                    "⚠️ 기록을 저장하지 못해 이번에는 보상을 드리지 못했어요.\n"
                    "잠시 후 다시 도전해 주세요!"
                )
            else:
                # 상한선 도달: 보상은 0이지만 학습 기록은 _record 에서 이미 남겼습니다.
                reward_line = (
                    f"🎯 오늘의 퀴즈 보상 상한선"
                    f"({MAX_DAILY_QUIZ_REWARDS}/{MAX_DAILY_QUIZ_REWARDS}회)에 달성하여 "
                    "보상 없이 학습 기록만 남습니다.\n"
                    "내일 다시 도전하면 보상을 받을 수 있어요! 🌙"
                )
            self.embed.description = f"{praise}\n{reward_line}"
        else:
            self.embed.color = COLOR_WRONG
            self.embed.title = f"😅 아쉬워요! — 정답은 {name} [{self.answer['category']}]"
            self.embed.description = (
                f"고르신 답은 **{chosen.label}** 였어요.\n"
                "손 모양을 한 번 더 따라 해 보고 다시 도전해 봐요! 💪\n"
                f"{REVIEW_NOTICE}"
            )

        self._finish_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def on_timeout(self) -> None:
        if self.answered:
            return
        self.answered = True
        self._reveal()

        self.embed.color = COLOR_TIMEOUT
        self.embed.title = (
            f"⏰ 시간 초과! — 정답은 {self.answer['word_name']} [{self.answer['category']}]"
        )
        self.embed.description = f"다음엔 꼭 맞혀 봐요! 🤟\n{REVIEW_NOTICE}"
        self._finish_embed()
        await self._record(False)  # 시간 초과도 오답으로 기록합니다

        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed, view=self)
            except discord.HTTPException:
                pass  # 메시지가 삭제된 경우 등

    async def _record(self, is_correct: bool) -> tuple[bool, int, aiosqlite.Row | None]:
        """
        풀이 결과를 quiz_logs 에 남기고, 정답이면 일일 상한선 안에서 보상까지 처리합니다.
        (handle_answer · on_timeout 모두 answered 플래그 뒤에서 불러 한 문제에 한 번만 기록)

        반환: (보상 지급 여부, 오늘 사용한 보상 횟수, 최신 유저 정보)
              DB 오류로 기록하지 못하면 (False, -1, None) - 상한선 도달과 구분하기 위한 값입니다.
        """
        try:
            if is_correct:
                return await self.db.record_quiz_reward(
                    self.owner.id,
                    self.answer["word_id"],
                    today_kst(),
                    points=REWARD_POINTS,
                    exp=REWARD_EXP,
                    max_daily_rewards=MAX_DAILY_QUIZ_REWARDS,
                )
            await self.db.log_quiz_attempt(self.owner.id, self.answer["word_id"], False)
            return False, 0, None
        except Exception:
            log.exception(
                "퀴즈 기록 저장 실패 (user=%s, word=%s)", self.owner.id, self.answer["word_id"]
            )
            return False, -1, None

    def _finish_embed(self) -> None:
        """정답 공개 후에만 설명 전문과 링크를 붙입니다."""
        self.embed.clear_fields()
        # 제목을 누르면 사전 상세 페이지로 이동합니다. (정답 공개 후이므로 안전)
        if _is_http(self.detail_url):
            self.embed.url = self.detail_url
        add_full_text_field(self.embed, "✋ 수어 설명", self.answer["meaning"])
        links = link_field_value(self.answer, include_detail=True)
        if links:
            self.embed.add_field(name="🔗 바로가기", value=links, inline=False)
        self.embed.set_footer(text="한 문제 더 풀어 볼까요? /수어퀴즈 🤟")

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("수어 퀴즈 처리 중 오류", exc_info=error)
        msg = "앗, 채점하다가 문제가 생겼어요 😢 잠시 후 다시 시도해 주세요!"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# ── Cog ─────────────────────────────────────────────────────────
class SignLanguage(commands.Cog, name="수어"):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    # ── /오늘의수어 ──────────────────────────────────────────────
    @app_commands.command(
        name="오늘의수어",
        description="오늘 나에게 배정된 수어 단어를 확인하고 출석 도장을 받아요!",
    )
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def daily_sign(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지 (공개 메시지)

        today = today_kst()
        # 유저마다 다른 단어를 배정합니다. (유저 ID + 날짜) % 전체 단어 수
        word = await self.db.get_daily_word_for_user(interaction.user.id, today)
        if word is None:
            await interaction.followup.send(
                "앗, 아직 등록된 수어 단어가 없어요! 🥲 `/수어전체동기화` 로 단어를 채워 주세요.",
                ephemeral=True,
            )
            return

        # 오늘 처음이면 출석 인정, 이미 확인했으면 보상 없이 안내만
        claimed, user = await self.db.claim_daily(
            interaction.user.id, today, DAILY_POINTS, DAILY_EXP
        )

        embed, content = build_word_embed(
            word,
            title=f"🤟 오늘의 수어 : {word['word_name']} [{word['category']}]",
            color=COLOR_DAILY,
        )

        if claimed:
            embed.description = (
                f"🎯 **출석 완료!** 🔥 연속 **{user['streak']}일차**\n"
                f"✨ 포인트 **+{DAILY_POINTS}** · 경험치 **+{DAILY_EXP}** "
                f"(현재 {user['points']}점 · {user['exp']}exp)"
            )
        else:
            embed.description = (
                f"오늘의 수어를 이미 확인하셨어요! (오늘 배운 단어: **{word['word_name']}**)\n"
                f"🔥 연속 **{user['streak']}일차** · 출석 보상은 하루에 한 번만 드려요 😊"
            )

        embed.set_footer(
            text=f"{today:%Y년 %m월 %d일} · {interaction.user.display_name} 님의 오늘 단어예요 🤟"
        )
        await interaction.followup.send(content=content, embed=embed)

    # ── /수어검색 ────────────────────────────────────────────────
    @app_commands.command(
        name="수어검색",
        description="단어명이나 분야(카테고리) 조건으로 수어 사전을 검색합니다.",
    )
    @app_commands.describe(
        단어명="찾고 싶은 단어 (일부만 입력해도 돼요)",
        분류="분야로 좁히기 (입력하면 후보가 자동으로 떠요)",
    )
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def search_sign(
        self, interaction: discord.Interaction, 단어명: str = "", 분류: str = ""
    ) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지

        keyword, category = 단어명.strip(), 분류.strip()
        if not keyword and not category:
            categories = await self.db.get_all_categories()
            sample = " · ".join(f"`{name}`" for name, _ in categories[:8]) or "(없음)"
            await interaction.followup.send(
                "찾을 조건을 하나는 알려 주세요! 🔍\n"
                "예시: `/수어검색 단어명:사랑` · `/수어검색 분류:감정`\n"
                f"\n등록된 분야: {sample}",
                ephemeral=True,
            )
            return

        total = await self.db.count_words_by_filter(keyword, category)

        conditions = " · ".join(
            part for part in (f"단어명 `{keyword}`" if keyword else "",
                              f"분류 `{category}`" if category else "") if part
        )

        if total == 0:
            await interaction.followup.send(
                f"{conditions} 조건에 맞는 수어를 찾지 못했어요 🥲\n"
                "단어의 일부만 넣어 보시거나, 분류를 비워 두고 다시 찾아 보세요!",
                ephemeral=True,
            )
            return

        # 결과가 하나면 바로 자세히 보여 줍니다.
        if total == 1:
            (word,) = await self.db.search_words_by_filter(keyword, category, limit=1)
            embed, content = build_word_embed(
                word,
                title=f"🔍 수어 검색 : {word['word_name']} [{word['category']}]",
                color=COLOR_SEARCH,
            )
            embed.description = f"{conditions} 으로 찾은 결과예요."
            embed.set_footer(text="다른 단어도 찾아볼까요? /수어검색 🤟")
            await interaction.followup.send(content=content, embed=embed)
            return

        # 여러 개면 SEARCH_PAGE_SIZE 개씩 나눠 버튼(◀ 이전 · 1 / N · ▶ 다음)으로 넘겨 봅니다.
        async def render_page(page: int) -> discord.Embed:
            """버튼을 누를 때마다 그 페이지의 단어만 DB에서 가져옵니다."""
            offset = page * SEARCH_PAGE_SIZE
            words = await self.db.search_words_by_filter(
                keyword, category, limit=SEARCH_PAGE_SIZE, offset=offset
            )
            return build_search_page_embed(words, conditions=conditions, total=total, offset=offset)

        view = SignPaginatorView(interaction.user, page_count(total, SEARCH_PAGE_SIZE), render_page)
        await view.start(interaction)

    @search_sign.autocomplete("분류")
    async def category_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """DB에 실제로 있는 분류만 후보로 띄웁니다. (자동완성은 defer가 안 되므로 가볍게)"""
        try:
            categories = await self.db.get_all_categories()
        except Exception:
            log.exception("분류 자동완성 조회 실패")
            return []

        current = current.strip()
        return [
            app_commands.Choice(name=f"{name} ({count}개)"[:100], value=name)
            for name, count in categories
            if not current or current in name
        ][:25]

    @search_sign.autocomplete("단어명")
    async def word_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """입력 중인 글자로 시작하는 단어를 후보로 띄웁니다."""
        if not current.strip():
            return []
        try:
            words = await self.db.search_words(current, limit=25)
        except Exception:
            log.exception("단어 자동완성 조회 실패")
            return []

        seen: set[str] = set()
        choices: list[app_commands.Choice[str]] = []
        for w in words:
            if w["word_name"] in seen:  # 선택값이 같으므로 한 번만 노출
                continue
            seen.add(w["word_name"])
            choices.append(
                app_commands.Choice(
                    name=f"{w['word_name']} ({w['category']})"[:100], value=w["word_name"]
                )
            )
        return choices

    # ── /수어퀴즈 ────────────────────────────────────────────────
    @app_commands.command(name="수어퀴즈", description="수어 동작을 보고 알맞은 단어를 골라 보세요!")
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def sign_quiz(self, interaction: discord.Interaction) -> None:
        # 퀴즈 카드는 부른 사람에게만 보여 채널 도배를 막습니다. (ephemeral)
        await interaction.response.defer(ephemeral=True)  # ← 3초 타임아웃 방지

        # 30% 확률로 '틀린 뒤 아직 못 맞힌 단어'에서 정답을 고릅니다. (오답 복습)
        answer = await self._pick_review_answer(interaction.user.id)
        is_review = answer is not None
        if answer is not None:
            # 보기 = 복습 단어 + 이름이 다른 무작위 단어들 (같은 이름이 보기에 두 번 뜨지 않게)
            others = await self.db.get_random_words(QUIZ_CHOICES)
            distractors = [w for w in others if w["word_name"] != answer["word_name"]]
            choices = [answer, *distractors[: QUIZ_CHOICES - 1]]
            random.shuffle(choices)  # 정답이 늘 첫 번째 버튼에 오지 않도록 섞습니다
        else:
            # 보기에 같은 단어명이 두 번 나오지 않도록 단어명 기준으로 뽑습니다.
            choices = await self.db.get_random_words(QUIZ_CHOICES)

        if len(choices) < 2:
            await interaction.followup.send(
                "퀴즈를 내려면 단어가 최소 2개는 있어야 해요! 📚", ephemeral=True
            )
            return

        if answer is None:
            # 정답은 '보여 줄 미디어가 있는' 단어 중에서 고릅니다.
            playable = [w for w in choices if has_quiz_media(w)]
            if not playable:
                await interaction.followup.send(
                    "보여 드릴 수어 자료를 찾지 못했어요 😢 잠시 후 다시 시도해 주세요!",
                    ephemeral=True,
                )
                return
            answer = random.choice(playable)

        # 정답이 드러날 만한 정보(단어명·분류·사전 주소)는 넣지 않습니다.
        intro = "🔁 **복습 문제!** 전에 틀렸던 단어가 나왔어요.\n" if is_review else ""
        embed = discord.Embed(
            title="🧩 수어 퀴즈!",
            description=(
                f"{intro}이 수어 동작은 어떤 뜻일까요?\n"
                f"**{QUIZ_TIMEOUT}초** 안에 아래 버튼에서 골라 주세요!"
            ),
            color=COLOR_QUIZ,
        )
        content = set_media(embed, _row_get(answer, "image_url"), answer["video_url"])

        # 영상 링크는 마스크 링크로만 (주소에 단어가 드러나지 않습니다)
        video_link = masked("🎬 영상으로 문제 보기", answer["video_url"])
        if video_link:
            embed.add_field(name="문제 영상", value=video_link, inline=False)
        embed.set_footer(
            text=f"정답 시 포인트 +{REWARD_POINTS} · 경험치 +{REWARD_EXP}"
                 f" · 보상은 하루 {MAX_DAILY_QUIZ_REWARDS}회까지"
        )

        view = SignQuizView(self.db, interaction.user, answer, choices, embed, is_review=is_review)
        # wait=True 를 줘야 메시지 객체가 돌아옵니다 (시간 초과 시 수정에 필요)
        # ephemeral=True 로 보내도 채점 화면까지 계속 비공개로 유지됩니다.
        #   - 버튼 클릭: interaction.response.edit_message() 가 같은 비공개 메시지를 고침
        #   - 시간 초과: 여기서 받은 WebhookMessage.edit() 가 상호작용 토큰으로 같은 메시지를 고침
        #     (토큰 유효 15분 > QUIZ_TIMEOUT 45초라 안전)
        view.message = await interaction.followup.send(
            content=content, embed=embed, view=view, ephemeral=True, wait=True
        )

    async def _pick_review_answer(self, user_id: int) -> aiosqlite.Row | None:
        """
        REVIEW_PROBABILITY(30%) 확률로 오답 복습 문제의 정답을 고릅니다.
        오답 이력이 없거나, 보여 줄 미디어가 있는 오답이 없으면 None → 기존처럼 무작위 출제.
        """
        if random.random() >= REVIEW_PROBABILITY:
            return None  # 70% 는 DB를 조회하지 않고 바로 무작위 출제
        wrong_words = await self.db.get_user_wrong_words(user_id, limit=REVIEW_POOL_SIZE)
        playable = [w for w in wrong_words if has_quiz_media(w)]
        return random.choice(playable) if playable else None

    # ── /단어장저장 ──────────────────────────────────────────────
    @app_commands.command(
        name="단어장저장",
        description="수어 단어를 내 단어장에 담습니다. 이미 담긴 단어면 단어장에서 뺍니다.",
    )
    @app_commands.describe(
        단어명="담을(또는 뺄) 단어 — 입력하면 후보가 떠요. 뜻이 여러 개인 단어는 후보에서 골라 주세요."
    )
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def bookmark_save(
        self, interaction: discord.Interaction, 단어명: app_commands.Range[str, 1, 100]
    ) -> None:
        await interaction.response.defer(ephemeral=True)  # ← 3초 타임아웃 방지 (나만 보이게)

        word = await self._resolve_bookmark_word(interaction, 단어명)
        if word is None:
            return  # 안내 메시지는 _resolve_bookmark_word 에서 보냈습니다

        added = await self.db.toggle_bookmark(interaction.user.id, word["word_id"])
        label = f"**{word['word_name']}** [{word['category']}]"
        if added:
            msg = f"📌 {label} 을(를) 내 단어장에 담았어요!\n`/수어단어장` 에서 모아 볼 수 있어요."
        else:
            msg = f"🗑️ {label} 을(를) 내 단어장에서 뺐어요.\n다시 담으려면 `/단어장저장` 을 한 번 더 해 주세요."
        await interaction.followup.send(msg, ephemeral=True)

    async def _resolve_bookmark_word(
        self, interaction: discord.Interaction, raw: str
    ) -> aiosqlite.Row | None:
        """
        입력값을 단어 한 건으로 바꿉니다. 못 정하면 안내를 보내고 None 을 돌려줍니다.
          - 자동완성 후보를 고르면 '#단어ID' 가 들어옵니다. (동음이의어도 정확히 구분)
          - 직접 친 단어명은 이름으로 찾고, 뜻이 여럿이면 후보에서 고르도록 안내합니다.
        """
        text = raw.strip()
        if not text:
            await interaction.followup.send("담을 단어를 입력해 주세요! ✏️", ephemeral=True)
            return None

        if match := BOOKMARK_ID_PATTERN.fullmatch(text):
            word = await self.db.get_word_by_id(int(match.group(1)))
            if word is None:
                await interaction.followup.send(
                    "앗, 그 단어는 사전에서 지워졌어요 🥲 다시 검색해서 골라 주세요!", ephemeral=True
                )
            return word

        words = await self.db.get_words_by_name(text)
        if len(words) == 1:
            return words[0]
        if len(words) > 1:
            lines = "\n".join(
                f"• {w['word_name']} [{w['category']}] — {summarize(w['meaning'])}" for w in words[:10]
            )
            await interaction.followup.send(
                f"**{text}** 은(는) 뜻이 다른 단어가 {len(words)}개 있어요! 🤔\n"
                f"입력칸에 단어를 치면 뜨는 후보에서 원하는 뜻을 골라 주세요.\n{lines}",
                ephemeral=True,
            )
            return None

        similar = await self.db.search_words(text, limit=5)
        hint = "\n비슷한 단어: " + ", ".join(f"`{w['word_name']}`" for w in similar) if similar else ""
        await interaction.followup.send(f"`{text}` 은(는) 사전에 없는 단어예요! 🔍{hint}", ephemeral=True)
        return None

    @bookmark_save.autocomplete("단어명")
    async def bookmark_word_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """
        동음이의어('배')도 따로 고를 수 있게 단어 한 건마다 후보를 띄웁니다. (값은 '#단어ID')
        아무것도 치지 않았으면 내 단어장에 담긴 단어를 띄워 바로 뺄 수 있게 합니다.
        """
        try:
            if current.strip():
                words, prefix = await self.db.search_words(current, limit=25), ""
            else:
                words, prefix = (await self.db.get_user_bookmarks(interaction.user.id))[:25], "📌 "
        except Exception:
            log.exception("단어장 자동완성 조회 실패")
            return []

        return [
            app_commands.Choice(
                name=f"{prefix}{w['word_name']} [{w['category']}] — {summarize(w['meaning'], 30)}"[:100],
                value=f"#{w['word_id']}",
            )
            for w in words
        ]

    # ── /수어단어장 ──────────────────────────────────────────────
    @app_commands.command(name="수어단어장", description="내 단어장에 담아 둔 수어 단어를 모아 봅니다.")
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def bookmark_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)  # ← 3초 타임아웃 방지 (나만 보이게)

        words = await self.db.get_user_bookmarks(interaction.user.id)
        if not words:
            await interaction.followup.send(
                "아직 단어장이 비어 있어요! 📭\n"
                "`/단어장저장 단어명:사랑` 처럼 마음에 드는 단어를 담아 보세요 📌",
                ephemeral=True,
            )
            return

        owner_name = interaction.user.display_name

        async def render_page(page: int) -> discord.Embed:
            """처음에 한 번 불러온 단어장을 BOOKMARK_PAGE_SIZE 개씩 잘라 보여 줍니다."""
            offset = page * BOOKMARK_PAGE_SIZE
            return build_bookmark_page_embed(
                words[offset: offset + BOOKMARK_PAGE_SIZE],
                owner_name=owner_name, total=len(words), offset=offset,
            )

        view = SignPaginatorView(
            interaction.user, page_count(len(words), BOOKMARK_PAGE_SIZE), render_page
        )
        await view.start(interaction, ephemeral=True)

    # ── 공통 에러 처리 ───────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(error, app_commands.CommandOnCooldown):
            # 연타 방지 - 오류가 아니므로 로그 없이 본인에게만 안내합니다.
            # (쿨다운은 명령어 실행 전에 걸리므로 defer 전이라 send_message 로 응답됩니다)
            msg = COOLDOWN_MESSAGE
        elif isinstance(original, discord.NotFound) and original.code == 10062:
            # 이미 만료된 상호작용 - 응답을 보낼 대상이 없으므로 로그만 남깁니다.
            log.warning("만료된 상호작용 (10062): %s", interaction.command)
            return
        else:
            log.exception("슬래시 명령어 처리 중 오류", exc_info=error)
            msg = "앗, 조교가 잠깐 헷갈렸어요 😵 잠시 후 다시 시도해 주세요!"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            log.warning("오류 안내 메시지를 보내지 못했습니다.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SignLanguage(bot, bot.db))  # type: ignore[attr-defined]
