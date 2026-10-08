"""
cogs/sentences.py
조교 이미숫 - /문장수어 (문장 수어 · 실전 회화 참여형 학습)

■ 흐름
  1. /문장수어 [카테고리] → 문장 카드를 채널에 올립니다. (원문 · 분류 · 난이도 · 표현 가이드 · 핵심 단어)
  2. 카드 아래 버튼 · 메뉴 (누구나 누를 수 있습니다)
     [✍️ 내 수어문 작성하기]  모달 창에 수어 어순을 적어 제출 → 카드에서 열린 공개 스레드에 올라갑니다.
                               그날(KST) 첫 제출이면 포인트 · 경험치를 줍니다. (utils/rewards.py)
                               제출한 사람에게는 모범 어순을 함께 보여 줘 바로 비교해 볼 수 있게 합니다.
     [💡 모범 수어 어순 보기]  모범 수어문(글로스)과 표현 팁을 누른 사람에게만 보여 줍니다.
     [📚 관련 단어 수형 보기]  (선택 메뉴) 고른 단어의 수형 삽화 · 설명 카드를 누른 사람에게만 보여 줍니다.
                               단어 카드는 cogs/sign_language.py 의 send_word_card 로 그립니다.
  3. 다른 학생들도 스레드에 자유롭게 댓글을 달며 토론합니다.

■ /명언 · /격언 은 이 명령어로 합쳤습니다. (예전 명언 · 사자성어도 '격언 · 명언' 갈래에 옮겨 두었습니다)
■ 봇이 다시 켜져도 예전 카드의 버튼이 동작합니다.
   custom_id 에 문장 번호를 넣고(sentence:write:12), discord.ui.DynamicItem 으로 받아 처리합니다.
■ 문장은 봇이 켜질 때 utils/sentence_seed.py 의 내용으로 DB(sign_sentences)를 맞추고 메모리에 들고 씁니다.
   관련 단어는 시드의 단어명을 이 DB 의 word_id 로 바꿔 넣습니다. 사전에 없는 단어명은 시작 로그로 알려 줍니다.
   (/수어전체동기화 로 단어가 바뀌었다면 봇을 다시 켜면 관련 단어도 다시 연결됩니다)
■ 스레드
   - 처음 제출할 때 카드 메시지에서 공개 스레드('💬 수어 토론: …')를 열고, 다음 제출부터는 그 스레드에 올립니다.
   - 카드가 이미 스레드 안에 있거나 DM 이라 스레드를 열 수 없거나 권한이 없으면 카드에 답장으로 올립니다.
     그마저 안 되면 제출 기록 · 보상만 저장하고 본인에게 알려 줍니다.
   - 봇 역할에 '공개 스레드 만들기' · '스레드에서 메시지 보내기' 권한이 필요합니다.
■ 유저가 적은 글은 마크다운 · 멘션을 무력화해서 올리고, 알림(@everyone 등)도 보내지 않습니다.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from collections.abc import Iterable
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from database import Database, Row
from utils.rewards import SENTENCE_EXP, SENTENCE_POINTS
from utils.sentence_seed import SEED_SENTENCES

log = logging.getLogger(__name__)

# ── 설정값 ──────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))  # 하루 첫 제출 판단 기준 (다른 Cog 와 같은 기준)
COG_NAME = "문장"
SIGN_COG_NAME = "수어"  # 단어 카드를 그려 주는 cogs/sign_language.py 의 Cog 이름

CHOICE_ALL = "전체"
COOLDOWN_SECONDS = 3.0  # /문장수어 연타 방지 (유저당)
COOLDOWN_MESSAGE = f"조교가 조금 바빠요! {COOLDOWN_SECONDS:g}초 후에 다시 시도해 주세요 ⏱️"
SUBMIT_COOLDOWN_SECONDS = 15.0  # 같은 유저가 연달아 제출해 스레드를 도배하지 않도록
SUBMISSION_MIN_LENGTH = 2
SUBMISSION_MAX_LENGTH = 300
SUBMISSION_MAX_LINES = 6
MODAL_TIMEOUT = 3600.0       # 초 - 모달 창을 열어 둔 채 기다려 주는 최대 시간
THREAD_AUTO_ARCHIVE = 10080  # 분 (7일) - 대화가 없으면 이만큼 뒤 스레드가 보관됩니다 (제출하면 다시 열림)
THREAD_NAME_PREFIX = "💬 수어 토론: "
THREAD_NAME_LIMIT = 100      # 디스코드 스레드 이름 최대 길이
ERROR_MESSAGE = "앗, 조교가 잠깐 헷갈렸어요 😵 잠시 후 다시 시도해 주세요!"
MISSING_SENTENCE_MESSAGE = "이 문장을 찾을 수 없어요 🥲 `/문장수어` 로 새 문장을 받아 주세요."

# 디스코드 오류 코드
THREAD_ALREADY_EXISTS = 160004  # 이 메시지에는 이미 스레드가 있음
THREAD_ARCHIVED = 50083         # 보관된 스레드라 바로 보낼 수 없음

# 갈래별 이모지 · 보여 줄 이름 · 색 (DB 값은 utils/sentence_seed.py 의 CATEGORIES)
CATEGORY_META: dict[str, tuple[str, str, discord.Color]] = {
    "속담": ("🧓", "한국 속담", discord.Color.from_rgb(224, 168, 91)),
    "명언": ("💡", "격언 · 명언", discord.Color.from_rgb(122, 198, 160)),
    "일상회화": ("☕", "일상 회화", discord.Color.from_rgb(126, 179, 255)),
    "VRChat": ("🥽", "VRChat 실전 수어", discord.Color.from_rgb(167, 139, 216)),
}
DIFFICULTY_LABELS = {1: "⭐ 입문", 2: "⭐⭐ 초급", 3: "⭐⭐⭐ 중급"}
GUIDE_TEXT = (
    "① 문장의 **핵심 단어**를 고르고\n"
    "② **시간 → 장소 → 주제 → 행동** 순서로 놓고 (의문사 · 부정은 끝으로)\n"
    "③ 질문 · 부정 · 강조는 **표정과 고개**로 더해 보세요!"
)
CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨"


def today_kst() -> date:
    return datetime.now(KST).date()


def category_meta(category: str) -> tuple[str, str, discord.Color]:
    return CATEGORY_META.get(category, ("🗣️", category, discord.Color.blurple()))


def related_ids(sentence: Row) -> list[int]:
    """related_word_ids(JSON 배열)를 읽습니다. 형식이 깨져 있으면 빈 목록."""
    try:
        values = json.loads(sentence["related_word_ids"] or "[]")
        return [int(v) for v in values]
    except (TypeError, ValueError):
        log.warning("문장 %s번의 related_word_ids 형식이 올바르지 않습니다.", sentence["id"])
        return []


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_submission(text: str) -> str:
    """제출한 글을 다듬습니다. (줄마다 공백 정리 · 빈 줄 제거 · 줄 수 · 길이 제한)"""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line][:SUBMISSION_MAX_LINES]
    return "\n".join(lines)[:SUBMISSION_MAX_LENGTH]


def safe_text(text: str) -> str:
    """유저 글을 임베드에 그대로 보이도록 마크다운 · 멘션을 무력화합니다."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def format_gloss(gloss: str) -> str:
    """'나 말 가다 / 너 말 오다' → 절마다 한 줄, 단어마다 칸을 나눠 보여 줍니다."""
    clauses = [clause.strip() for clause in gloss.split("/") if clause.strip()]
    lines = []
    for index, clause in enumerate(clauses):
        words = " ".join(f"`{word}`" for word in clause.replace("`", "'").split())
        mark = CIRCLED_NUMBERS[index] if len(clauses) > 1 and index < len(CIRCLED_NUMBERS) else ""
        lines.append(f"**{mark}** {words}" if mark else words)
    return "\n".join(lines) or "-"


def thread_name(sentence: Row) -> str:
    return truncate(THREAD_NAME_PREFIX + sentence["korean_text"], THREAD_NAME_LIMIT)


def word_chips(names: Iterable[str], limit: int = 900) -> str:
    """단어명을 `말` · `가다` 처럼 이어 붙입니다. (같은 이름은 한 번만 · 임베드 칸 길이 안에서)"""
    chips: list[str] = []
    for name in dict.fromkeys(names):
        chip = f"`{name.replace('`', '')}`"
        if len(" · ".join([*chips, chip])) > limit:
            chips.append("…")
            break
        chips.append(chip)
    return " · ".join(chips)


def word_summary(word: Row, limit: int = 60) -> str:
    """선택 메뉴 설명 칸에 넣을 짧은 뜻. (동음이의어를 구분할 만큼)"""
    text = " ".join(html.unescape(word["meaning"] or "").split())
    return truncate(f"[{word['category']}] {text}", limit)


# ── 임베드 ──────────────────────────────────────────────────────
def build_sentence_embed(sentence: Row, words: list[Row]) -> discord.Embed:
    """채널에 올리는 문장 카드."""
    emoji, label, color = category_meta(sentence["category"])
    meta = [f"{emoji} **{label}**", DIFFICULTY_LABELS.get(int(sentence["difficulty"]), "")]
    if sentence["source"]:
        meta.append(sentence["source"])
    embed = discord.Embed(
        title="🤟 문장 수어 도전!",
        description=f"> ### {sentence['korean_text']}\n\n" + " · ".join(m for m in meta if m),
        color=color,
    )
    embed.add_field(name="🧭 표현 가이드", value=GUIDE_TEXT, inline=False)
    if words:
        embed.add_field(
            name="📚 핵심 단어",
            value=word_chips(w["word_name"] for w in words)
                  + "\n아래 메뉴에서 고르면 수형 삽화를 나에게만 보여 줘요.",
            inline=False,
        )
    embed.add_field(
        name="✍️ 참여 방법",
        value=(
            "**[내 수어문 작성하기]** 로 수어 어순을 적으면 이 카드의 토론 스레드에 올라가요.\n"
            f"오늘 첫 참여 보너스 **+{SENTENCE_POINTS} 포인트 · +{SENTENCE_EXP} EXP** 🎁"
        ),
        inline=False,
    )
    embed.set_footer(text=f"문장 #{sentence['id']} · 💡 모범 어순은 버튼을 누른 사람에게만 보여요")
    return embed


def build_gloss_embed(sentence: Row, *, mine: str = "") -> discord.Embed:
    """모범 수어 어순 (본인에게만). mine 이 있으면 내 수어문을 위에 함께 보여 줍니다."""
    _, _, color = category_meta(sentence["category"])
    embed = discord.Embed(
        title="💡 모범 수어 어순", description=f"> {sentence['korean_text']}", color=color
    )
    if mine:
        embed.add_field(name="✍️ 내 수어문", value=safe_text(mine), inline=False)
    embed.add_field(name="🤟 모범 수어문 (글로스)", value=format_gloss(sentence["ksl_gloss"]), inline=False)
    if sentence["translation_tip"]:
        embed.add_field(
            name="🙂 표현 팁 · 비수지 신호", value=truncate(sentence["translation_tip"], 1024), inline=False
        )
    embed.set_footer(text="모범 어순은 하나의 예시예요. 지역 · 사람마다 다른 표현도 있어요 🤟")
    return embed


def build_submission_embed(
    author: discord.abc.User, text: str, sentence: Row, ordinal: int
) -> discord.Embed:
    """스레드에 올리는 수어문."""
    _, _, color = category_meta(sentence["category"])
    embed = discord.Embed(
        description="\n".join(f"> **{safe_text(line)}**" for line in text.splitlines()),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=f"{author.display_name} 님의 수어문", icon_url=author.display_avatar.url)
    embed.set_footer(text=truncate(f"{ordinal}번째 수어문 · {sentence['korean_text']}", 200))
    return embed


# ── 카드 버튼 · 메뉴 (봇을 다시 켜도 동작하는 DynamicItem) ─────────
def sentences_cog(interaction: discord.Interaction) -> Sentences | None:
    cog = interaction.client.get_cog(COG_NAME)  # type: ignore[attr-defined]
    return cog if isinstance(cog, Sentences) else None


async def send_error(interaction: discord.Interaction, message: str = ERROR_MESSAGE) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.warning("오류 안내 메시지를 보내지 못했습니다.")


async def run_safely(interaction: discord.Interaction, action: str, coro: Any) -> None:
    """버튼 · 메뉴 처리 중 오류가 나면 로그를 남기고 누른 사람에게만 알립니다."""
    try:
        await coro
    except discord.NotFound as error:
        if error.code == 10062:  # 만료된 상호작용 - 알릴 곳이 없습니다
            log.warning("만료된 상호작용 (10062): %s", action)
            return
        log.exception("문장 수어 %s 처리 중 오류", action)
        await send_error(interaction)
    except Exception:
        log.exception("문장 수어 %s 처리 중 오류", action)
        await send_error(interaction)


class WriteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sentence:write:(?P<id>\d+)"):
    """[✍️ 내 수어문 작성하기] → 모달 창"""

    def __init__(self, sentence_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="내 수어문 작성하기",
                emoji="✍️",
                style=discord.ButtonStyle.success,
                custom_id=f"sentence:write:{sentence_id}",
                row=0,
            )
        )
        self.sentence_id = sentence_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> WriteButton:
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = sentences_cog(interaction)
        if cog is not None:
            await run_safely(interaction, "작성", cog.open_submission_modal(interaction, self.sentence_id))


class GlossButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sentence:gloss:(?P<id>\d+)"):
    """[💡 모범 수어 어순 보기] → 나에게만"""

    def __init__(self, sentence_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="모범 수어 어순 보기",
                emoji="💡",
                style=discord.ButtonStyle.secondary,
                custom_id=f"sentence:gloss:{sentence_id}",
                row=0,
            )
        )
        self.sentence_id = sentence_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> GlossButton:
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = sentences_cog(interaction)
        if cog is not None:
            await run_safely(interaction, "모범 어순", cog.show_gloss(interaction, self.sentence_id))


class WordSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"sentence:word:(?P<id>\d+)"):
    """[📚 관련 단어 수형 보기] 메뉴 → 고른 단어 카드를 나에게만"""

    def __init__(self, sentence_id: int, options: list[discord.SelectOption]) -> None:
        super().__init__(
            discord.ui.Select(
                custom_id=f"sentence:word:{sentence_id}",
                placeholder="📚 관련 단어 수형 보기 (나에게만 보여요)",
                options=options,
                row=1,
            )
        )
        self.sentence_id = sentence_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Select, match: re.Match[str]
    ) -> WordSelect:
        return cls(int(match["id"]), list(item.options))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = sentences_cog(interaction)
        if cog is not None and self.item.values:
            await run_safely(
                interaction, "관련 단어",
                cog.show_related_word(interaction, self.sentence_id, int(self.item.values[0])),
            )


DYNAMIC_ITEMS = (WriteButton, GlossButton, WordSelect)


def build_card_view(sentence_id: int, words: list[Row]) -> discord.ui.View:
    """카드 아래 버튼 2개 + (관련 단어가 있으면) 선택 메뉴."""
    view = discord.ui.View(timeout=None)
    view.add_item(WriteButton(sentence_id))
    view.add_item(GlossButton(sentence_id))
    options = [
        discord.SelectOption(
            label=truncate(word["word_name"], 100),
            value=str(word["word_id"]),
            description=word_summary(word),
            emoji="📚",
        )
        for word in words[:25]
    ]
    if options:
        view.add_item(WordSelect(sentence_id, options))
    return view


class SubmissionModal(discord.ui.Modal):
    """수어문을 적는 모달 창. (제출하면 Sentences.handle_submission)"""

    def __init__(self, cog: Sentences, sentence: Row, card: discord.Message | None) -> None:
        super().__init__(title="✍️ 내 수어문 작성하기", timeout=MODAL_TIMEOUT)
        self.cog = cog
        self.sentence = sentence
        self.card = card
        self.text: discord.ui.TextInput[SubmissionModal] = discord.ui.TextInput(
            label=truncate(f"📝 {sentence['korean_text']}", 45),  # 라벨은 45자까지
            style=discord.TextStyle.paragraph,
            placeholder="내가 생각한 수어 어순을 단어 단위로 띄어 적어 보세요 (예: 나 학교 가다)",
            min_length=SUBMISSION_MIN_LENGTH,
            max_length=SUBMISSION_MAX_LENGTH,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_submission(interaction, self.sentence, str(self.text.value), self.card)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("수어문 제출 처리 중 오류", exc_info=error)
        await send_error(interaction)


# ── Cog ─────────────────────────────────────────────────────────
class Sentences(commands.Cog, name=COG_NAME):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._sentences: dict[int, Row] = {}       # 문장 번호 → 행 (켜질 때 DB 에서 읽어 둠)
        self._last_shown: dict[int, int] = {}      # 채널별 직전 문장 (두 번 연속 같은 문장 방지)
        self._last_submit: dict[int, float] = {}   # 유저별 마지막 제출 시각 (도배 방지)
        self._thread_locks: dict[int, asyncio.Lock] = {}  # 카드별 스레드 만들기 잠금

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*DYNAMIC_ITEMS)
        try:
            await self.sync_seed()
        except Exception:
            # 시드를 맞추지 못해도 이미 저장된 문장으로 계속 동작합니다.
            log.exception("문장 수어 시드를 DB 에 맞추지 못했습니다. (저장된 문장으로 계속합니다)")
            log.info("🗣️ 문장 수어: 저장된 문장 %d개로 시작합니다.", await self._reload_quietly())

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(*DYNAMIC_ITEMS)

    # ── 문장 준비 ────────────────────────────────────────────────
    async def sync_seed(self) -> None:
        """utils/sentence_seed.py 의 문장을 DB 에 맞추고, 관련 단어를 이 DB 의 word_id 로 연결합니다."""
        names = [name for s in SEED_SENTENCES for name in s.word_names()]
        found = await self.db.find_word_ids_by_names(names)
        rows = [
            (s.id, s.category, s.korean_text, s.ksl_gloss, s.translation_tip, s.difficulty,
             s.source, s.related_word_ids(found))
            for s in SEED_SENTENCES
        ]
        total = await self.db.sync_sentences(rows)
        await self.reload()

        missing = sorted({w for s in SEED_SENTENCES for w in s.unresolved_words(found)})
        log.info(
            "🗣️ 문장 수어: 문장 %d개 준비 · 관련 단어 %d개 연결%s",
            total, sum(len(row[7]) for row in rows),
            f" · 사전에 없는 단어 {len(missing)}개: {', '.join(missing)}" if missing else "",
        )

    async def reload(self) -> None:
        self._sentences = {int(row["id"]): row for row in await self.db.get_sentences()}

    async def _reload_quietly(self) -> int:
        try:
            await self.reload()
        except Exception:
            log.exception("저장된 문장도 읽지 못했습니다. (/문장수어 를 쓸 때 다시 시도합니다)")
        return len(self._sentences)

    async def get_sentence(self, sentence_id: int) -> Row | None:
        """메모리에 없으면(DB 에 직접 넣은 문장 등) DB 에서 찾아 기억해 둡니다."""
        sentence = self._sentences.get(sentence_id)
        if sentence is None:
            sentence = await self.db.get_sentence(sentence_id)
            if sentence is not None:
                self._sentences[sentence_id] = sentence
        return sentence

    async def pick(self, channel_id: int | None, category: str) -> Row | None:
        """갈래에서 무작위로 하나 고릅니다. 같은 채널에서 직전에 나온 문장은 피합니다."""
        if not self._sentences:
            await self._reload_quietly()
        pool = [
            s for s in self._sentences.values() if category == CHOICE_ALL or s["category"] == category
        ]
        if not pool:
            return None
        last = self._last_shown.get(channel_id or 0)
        sentence = random.choice([s for s in pool if int(s["id"]) != last] or pool)
        self._last_shown[channel_id or 0] = int(sentence["id"])
        return sentence

    # ── /문장수어 ────────────────────────────────────────────────
    @app_commands.command(
        name="문장수어",
        description="속담 · 명언 · 일상/VRChat 회화 문장을 수어 어순으로 옮겨 보고 스레드에서 함께 토론해요!",
    )
    @app_commands.describe(카테고리="보고 싶은 갈래를 골라 주세요. (비우면 전체에서 무작위)")
    @app_commands.choices(카테고리=[
        app_commands.Choice(name="🎲 전체 (랜덤)", value=CHOICE_ALL),
        app_commands.Choice(name="🧓 한국 속담", value="속담"),
        app_commands.Choice(name="💡 격언 · 명언", value="명언"),
        app_commands.Choice(name="☕ 일상 회화", value="일상회화"),
        app_commands.Choice(name="🥽 VRChat 실전 수어", value="VRChat"),
    ])
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def sentence_command(
        self, interaction: discord.Interaction, 카테고리: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지

        sentence = await self.pick(interaction.channel_id, 카테고리.value if 카테고리 else CHOICE_ALL)
        if sentence is None:
            await interaction.followup.send(
                "그 갈래에는 아직 문장이 없어요! 🥲 다른 갈래로 골라 주세요.", ephemeral=True
            )
            return

        words = await self.db.get_words_by_ids(related_ids(sentence))
        await interaction.followup.send(
            embed=build_sentence_embed(sentence, words),
            view=build_card_view(int(sentence["id"]), words),
        )

    # ── 카드 버튼 · 메뉴 처리 ────────────────────────────────────
    async def show_gloss(self, interaction: discord.Interaction, sentence_id: int) -> None:
        sentence = await self.get_sentence(sentence_id)
        if sentence is None:
            await interaction.response.send_message(MISSING_SENTENCE_MESSAGE, ephemeral=True)
            return
        await interaction.response.send_message(embed=build_gloss_embed(sentence), ephemeral=True)

    async def open_submission_modal(self, interaction: discord.Interaction, sentence_id: int) -> None:
        # 모달은 3초 안에 띄워야 해서 응답을 미룰 수 없습니다. 문장은 메모리에서 바로 꺼냅니다.
        sentence = await self.get_sentence(sentence_id)
        if sentence is None:
            await interaction.response.send_message(MISSING_SENTENCE_MESSAGE, ephemeral=True)
            return
        await interaction.response.send_modal(SubmissionModal(self, sentence, interaction.message))

    async def show_related_word(
        self, interaction: discord.Interaction, sentence_id: int, word_id: int
    ) -> None:
        # 메뉴를 처음 상태로 되돌려 같은 단어를 다시 골라도 열리게 합니다. (카드 내용은 그대로)
        # 멈춘(stop) 뷰로 보내야 이 메시지용 뷰가 봇 메모리에 따로 쌓이지 않습니다.
        if interaction.message is not None:
            reset = discord.ui.View.from_message(interaction.message, timeout=None)
            reset.stop()
            await interaction.response.edit_message(view=reset)
        else:
            await interaction.response.defer()

        word = await self.db.get_word_by_id(word_id)
        if word is None:
            await interaction.followup.send(
                "이 단어는 사전에서 찾을 수 없어요 🥲 (사전이 새로 동기화됐을 수 있어요)", ephemeral=True
            )
            return

        sentence = await self.get_sentence(sentence_id)
        title = f"📚 관련 단어 : {word['word_name']} [{word['category']}]"
        footer = f"「{truncate(sentence['korean_text'], 40)}」의 관련 단어예요 🤟" if sentence else ""
        sign_cog = self.bot.get_cog(SIGN_COG_NAME)
        send_word_card = getattr(sign_cog, "send_word_card", None)
        if send_word_card is not None:
            await send_word_card(interaction, word, title=title, footer=footer)
            return

        # 수어 Cog 가 꺼져 있으면 설명만 보여 줍니다.
        embed = discord.Embed(
            title=title, description=truncate(html.unescape(word["meaning"] or ""), 4000),
            color=category_meta(sentence["category"] if sentence else "")[2],
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── 수어문 제출 ──────────────────────────────────────────────
    async def handle_submission(
        self,
        interaction: discord.Interaction,
        sentence: Row,
        raw_text: str,
        card: discord.Message | None,
    ) -> None:
        text = clean_submission(raw_text)
        if len(text) < SUBMISSION_MIN_LENGTH:
            await interaction.response.send_message(
                f"수어문을 {SUBMISSION_MIN_LENGTH}글자 이상 적어 주세요! ✍️", ephemeral=True
            )
            return

        user_id = interaction.user.id
        waited = time.monotonic() - self._last_submit.get(user_id, float("-inf"))
        if waited < SUBMIT_COOLDOWN_SECONDS:
            # 적은 내용을 돌려줘서 다시 붙여 넣을 수 있게 합니다.
            await interaction.response.send_message(
                f"조금만 천천히! ⏱️ {SUBMIT_COOLDOWN_SECONDS - waited:.0f}초 뒤에 다시 제출해 주세요.\n"
                f"적으신 내용: {safe_text(text)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        rewarded, ordinal, user = await self.db.record_sentence_submission(
            user_id, int(sentence["id"]), text, today_kst(),
            points=SENTENCE_POINTS, exp=SENTENCE_EXP,
        )
        self._remember_submit(user_id)

        posted = await self.post_submission(
            card or interaction.message, sentence,
            build_submission_embed(interaction.user, text, sentence, ordinal),
        )

        lines = []
        if rewarded and user is not None:
            lines.append(
                f"🎉 **참여 완료!** 오늘의 문장 수어 보너스 **+{SENTENCE_EXP} EXP · +{SENTENCE_POINTS} 포인트** "
                f"(현재 {user['points']}점 · {user['exp']}exp)"
            )
        else:
            lines.append("✅ **수어문을 등록했어요!** 오늘 참여 보너스는 이미 받았어요. 내일 또 도전해 주세요 🌙")
        if posted is not None:
            lines.append(f"💬 [토론 스레드에서 보기]({posted.jump_url}) · 다른 분들의 수어문에도 의견을 남겨 보세요!")
        else:
            lines.append(
                "⚠️ 토론 스레드에 올리지 못했어요. 서버 관리자에게 봇의 **'공개 스레드 만들기' · "
                "'스레드에서 메시지 보내기'** 권한을 확인해 달라고 알려 주세요. (제출 기록과 보상은 저장됐어요)"
            )
        lines.append("👇 내 수어문과 모범 어순을 비교해 보세요!")
        await interaction.followup.send(
            "\n".join(lines), embed=build_gloss_embed(sentence, mine=text), ephemeral=True
        )

    def _remember_submit(self, user_id: int) -> None:
        now = time.monotonic()
        self._last_submit[user_id] = now
        if len(self._last_submit) > 1000:  # 오래된 기록은 정리합니다
            self._last_submit = {
                uid: at for uid, at in self._last_submit.items() if now - at < SUBMIT_COOLDOWN_SECONDS
            }

    async def post_submission(
        self, card: discord.Message | None, sentence: Row, embed: discord.Embed
    ) -> discord.Message | None:
        """수어문을 카드의 토론 스레드에 올립니다. 스레드가 안 되면 카드에 답장으로. 실패하면 None."""
        if card is None:
            return None
        quiet = discord.AllowedMentions.none()

        thread, created = await self.discussion_thread(card, sentence)
        if thread is not None:
            content = (
                "💬 **수어 토론방**이 열렸어요! 이 문장을 수어로 어떻게 옮길지 자유롭게 이야기해 주세요.\n"
                "카드의 **[✍️ 내 수어문 작성하기]** 로 내 어순을 올리거나, 여기에 바로 댓글로 의견을 남겨도 좋아요 🤟"
            ) if created else None
            try:
                return await self._send_in_thread(thread, content=content, embed=embed, allowed_mentions=quiet)
            except discord.HTTPException as error:
                log.warning("토론 스레드에 수어문을 올리지 못해 카드에 답장으로 올립니다. (%s)", _describe(error))

        try:
            return await card.reply(embed=embed, mention_author=False, allowed_mentions=quiet)
        except discord.HTTPException as error:
            log.warning("수어문을 카드에 답장으로도 올리지 못했습니다. (%s)", _describe(error))
            return None

    async def discussion_thread(
        self, card: discord.Message, sentence: Row
    ) -> tuple[discord.Thread | None, bool]:
        """
        카드 메시지의 공개 스레드를 찾거나 새로 엽니다. 반환: (스레드 or None, 방금 열었는지)
        스레드 ID 는 카드 메시지 ID 와 같습니다. 같은 카드에 동시에 제출해도 스레드는 하나만 열립니다.
        """
        if card.guild is None or isinstance(card.channel, discord.Thread):
            return None, False  # DM 이거나 이미 스레드 안 - 카드에 답장으로 올립니다

        lock = self._thread_locks.setdefault(card.id, asyncio.Lock())
        async with lock:
            thread = card.guild.get_thread(card.id)
            if thread is not None:
                return thread, False
            try:
                thread = await card.create_thread(
                    name=thread_name(sentence), auto_archive_duration=THREAD_AUTO_ARCHIVE
                )
                return thread, True
            except discord.HTTPException as error:
                if error.code != THREAD_ALREADY_EXISTS:
                    log.warning("수어 토론 스레드를 열지 못했습니다. (%s)", _describe(error))
                    return None, False
            # 이미 있는데 봇 메모리에 없는 경우 (보관된 스레드 · 봇 재시작 등)
            try:
                channel = await self.bot.fetch_channel(card.id)
            except discord.HTTPException as error:
                log.warning("기존 수어 토론 스레드를 찾지 못했습니다. (%s)", _describe(error))
                return None, False
            return (channel, False) if isinstance(channel, discord.Thread) else (None, False)

    @staticmethod
    async def _send_in_thread(thread: discord.Thread, **kwargs: Any) -> discord.Message:
        """보관(archived)된 스레드면 다시 열고 보냅니다."""
        try:
            return await thread.send(**kwargs)
        except discord.HTTPException as error:
            if error.code != THREAD_ARCHIVED:
                raise
        await thread.edit(archived=False)
        return await thread.send(**kwargs)

    # ── 공통 에러 처리 ───────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(error, app_commands.CommandOnCooldown):
            msg = COOLDOWN_MESSAGE  # 연타 방지 - 오류가 아니므로 로그 없이 본인에게만 안내합니다.
        elif isinstance(original, discord.NotFound) and original.code == 10062:
            log.warning("만료된 상호작용 (10062): %s", interaction.command)
            return
        else:
            log.exception("문장 수어 명령어 처리 중 오류", exc_info=error)
            msg = ERROR_MESSAGE
        await send_error(interaction, msg)


def _describe(error: discord.HTTPException) -> str:
    return f"HTTP {error.status} · 코드 {error.code} · {error.text or error.__class__.__name__}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Sentences(bot, bot.db))  # type: ignore[attr-defined]
