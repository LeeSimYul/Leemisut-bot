"""
cogs/sign_language.py
조교 이미숫 - '오늘의 수어' · '수어 퀴즈'

관리자용 명령어(/수어동기화, /수어전체동기화, /수어삭제, /수어목록)는 cogs/admin.py 에 있습니다.

■ 링크 표기 원칙
   - 메시지 본문(content)에는 주소를 넣지 않습니다. 긴 URL이 그대로 보이기 때문입니다.
   - 영상·사전 링크는 임베드 안에서 마스크 링크([보이는 글](주소))로만 보여 줍니다.
   - 수형 사진이 있으면 임베드 이미지로 띄웁니다. 주소가 글로 보이지 않습니다.

■ 정답 은닉 원칙
   사전 상세 페이지는 주소를 누르면 단어명이 그대로 보입니다.
   그래서 퀴즈가 '끝난 뒤에만' 결과 임베드에 붙입니다.

■ 모든 슬래시 명령어는 시작 직후 defer()로 응답을 미뤄 3초 타임아웃(10062)을 막습니다.
   defer 이후에는 반드시 interaction.followup.send() 를 사용해야 합니다.
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from database import Database

log = logging.getLogger(__name__)

# ── 설정값 ──────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))  # zoneinfo는 Windows에서 tzdata가 필요해서 고정 오프셋 사용
QUIZ_CHOICES = 4
QUIZ_TIMEOUT = 30  # 초
REWARD_POINTS = 10
REWARD_EXP = 10

# True로 바꾸면 본문에 영상 주소를 넣어 디스코드 플레이어가 뜹니다.
# 대신 긴 주소가 글로 보입니다. (기본값 False = 깔끔한 마스크 링크만)
SHOW_VIDEO_PLAYER = False

IMAGE_EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")

EMBED_FIELD_LIMIT = 1024  # 디스코드 임베드 필드 한 칸의 최대 길이
MAX_FIELD_CHUNKS = 4      # 임베드 전체 6000자 제한을 넘지 않도록 한 설명의 최대 칸 수
                          # (실제 수어 설명은 100~200자라 여기에 걸릴 일은 거의 없습니다)

COLOR_DAILY = discord.Color.from_rgb(255, 183, 77)
COLOR_QUIZ = discord.Color.blurple()
COLOR_CORRECT = discord.Color.green()
COLOR_WRONG = discord.Color.red()
COLOR_TIMEOUT = discord.Color.light_grey()


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


def masked(label: str, url: str) -> str:
    """디스코드 마스크 링크. 주소가 비어 있으면 빈 문자열을 돌려줍니다."""
    return f"[{label}]({url})" if _is_http(url) else ""


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
    ) -> None:
        super().__init__(timeout=QUIZ_TIMEOUT)
        self.db = db
        self.owner = owner
        self.answer = answer
        self.embed = embed
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

        if chosen.word_id == self.answer["word_id"]:
            user = await self.db.add_reward(interaction.user.id, REWARD_POINTS, REWARD_EXP)
            self.embed.color = COLOR_CORRECT
            self.embed.title = f"🎉 정답이에요! — {name} [{self.answer['category']}]"
            self.embed.description = (
                "눈썰미가 대단하시네요 👏\n"
                f"✨ 포인트 **+{REWARD_POINTS}** · 경험치 **+{REWARD_EXP}**\n"
                f"현재 포인트 {user['points']} · 경험치 {user['exp']}"
            )
        else:
            self.embed.color = COLOR_WRONG
            self.embed.title = f"😅 아쉬워요! — 정답은 {name} [{self.answer['category']}]"
            self.embed.description = (
                f"고르신 답은 **{chosen.label}** 였어요.\n"
                "손 모양을 한 번 더 따라 해 보고 다시 도전해 봐요! 💪"
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
        self.embed.description = "다음엔 꼭 맞혀 봐요! 🤟"
        self._finish_embed()

        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed, view=self)
            except discord.HTTPException:
                pass  # 메시지가 삭제된 경우 등

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
    @app_commands.command(name="오늘의수어", description="이미숫 조교가 오늘 함께 배울 수어 단어를 알려 드려요!")
    async def daily_sign(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지 (공개 메시지)

        today = today_kst()
        word = await self.db.get_daily_word(today)
        if word is None:
            await interaction.followup.send(
                "앗, 아직 등록된 수어 단어가 없어요! 🥲 `/수어전체동기화` 로 단어를 채워 주세요.",
                ephemeral=True,
            )
            return

        detail_url = _row_get(word, "detail_url")

        # 제목 전체가 사전 상세 페이지로 가는 링크가 됩니다.
        embed = discord.Embed(
            title=f"🤟 오늘의 수어 : {word['word_name']} [{word['category']}]",
            url=detail_url or None,
            color=COLOR_DAILY,
        )
        add_full_text_field(embed, "✋ 수어 설명", word["meaning"])

        links = link_field_value(word, include_detail=True)
        if links:
            embed.add_field(name="🔗 바로가기", value=links, inline=False)

        embed.set_footer(text=f"{today:%Y년 %m월 %d일} · 오늘도 한 단어씩, 천천히 같이 익혀 봐요!")
        content = set_media(embed, _row_get(word, "image_url"), word["video_url"])

        await interaction.followup.send(content=content, embed=embed)

    # ── /수어퀴즈 ────────────────────────────────────────────────
    @app_commands.command(name="수어퀴즈", description="수어 동작을 보고 알맞은 단어를 골라 보세요!")
    async def sign_quiz(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지

        # 보기에 같은 단어명이 두 번 나오지 않도록 단어명 기준으로 뽑습니다.
        choices = await self.db.get_random_words(QUIZ_CHOICES)
        if len(choices) < 2:
            await interaction.followup.send(
                "퀴즈를 내려면 단어가 최소 2개는 있어야 해요! 📚", ephemeral=True
            )
            return

        # 정답은 '보여 줄 미디어가 있는' 단어 중에서 고릅니다.
        playable = [
            w for w in choices
            if is_image_url(_row_get(w, "image_url")) or is_video_url(w["video_url"])
        ]
        if not playable:
            await interaction.followup.send(
                "보여 드릴 수어 자료를 찾지 못했어요 😢 잠시 후 다시 시도해 주세요!", ephemeral=True
            )
            return
        answer = random.choice(playable)

        # 정답이 드러날 만한 정보(단어명·분류·사전 주소)는 넣지 않습니다.
        embed = discord.Embed(
            title="🧩 수어 퀴즈!",
            description=f"이 수어 동작은 어떤 뜻일까요?\n**{QUIZ_TIMEOUT}초** 안에 아래 버튼에서 골라 주세요!",
            color=COLOR_QUIZ,
        )
        content = set_media(embed, _row_get(answer, "image_url"), answer["video_url"])

        # 영상 링크는 마스크 링크로만 (주소에 단어가 드러나지 않습니다)
        video_link = masked("🎬 영상으로 문제 보기", answer["video_url"])
        if video_link:
            embed.add_field(name="문제 영상", value=video_link, inline=False)
        embed.set_footer(text=f"정답 시 포인트 +{REWARD_POINTS} · 경험치 +{REWARD_EXP}")

        view = SignQuizView(self.db, interaction.user, answer, choices, embed)
        # wait=True 를 줘야 메시지 객체가 돌아옵니다 (시간 초과 시 수정에 필요)
        view.message = await interaction.followup.send(
            content=content, embed=embed, view=view, wait=True
        )

    # ── 공통 에러 처리 ───────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(original, discord.NotFound) and original.code == 10062:
            # 이미 만료된 상호작용 - 응답을 보낼 대상이 없으므로 로그만 남깁니다.
            log.warning("만료된 상호작용 (10062): %s", interaction.command)
            return

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
