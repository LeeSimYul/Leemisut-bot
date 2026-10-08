"""
cogs/general.py
조교 이미숫 - 기본 명령어 (/안녕, /내정보)

/안녕 의 명령어 안내판은 HELP_TEXT 에 있습니다. 일반 유저용 명령어를 추가하면 여기도 함께 고쳐 주세요.
/내정보 는 레벨 · 경험치 · 포인트 · 연속 출석 · 단어장 개수 · 오늘의 보상 현황을 한눈에 보여 줍니다.

※ 예전 /명언 · /격언 은 /문장수어 (cogs/sentences.py)로 합쳤습니다.
   문장 · 모범 수어문은 utils/sentence_seed.py 에서 관리합니다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from database import Database, Row
# 보상 밸런스는 utils/rewards.py 한 곳에서 관리합니다.
# (cogs 끼리 import 하면 확장 모듈이 두 번 만들어지므로 공용 값은 utils 에 둡니다)
from utils.rewards import MAX_DAILY_QUIZ_REWARDS, SENTENCE_EXP, SENTENCE_POINTS

log = logging.getLogger(__name__)


# 연타 방지: 유저당 COOLDOWN_SECONDS 초에 1회 (명령어마다 따로 계산)
COOLDOWN_SECONDS = 3.0
COOLDOWN_MESSAGE = f"조교가 조금 바빠요! {COOLDOWN_SECONDS:g}초 후에 다시 시도해 주세요 ⏱️"

# /안녕 안내판 - 일반 유저용 명령어를 갈래별로 모았습니다. (임베드 필드 1칸 = 최대 1024자)
HELP_TEXT = (
    "**🤟 수어 학습**\n"
    "`/오늘의수어` — 나에게 맞춘 오늘의 단어 & 출석 체크\n"
    "`/수어검색` — 단어 · 분야 조건으로 수어 사전 검색\n"
    "\n**🧩 퀴즈 & 복습**\n"
    "`/수어퀴즈` — 수어 퀴즈 풀기 (틀린 단어는 오답노트에 자동 저장)\n"
    "`/오답노트` · `/복습퀴즈` — 틀린 단어 모아 보기 · 다시 풀어 해결하기\n"
    "`/단어장저장` · `/수어단어장` — 나만의 단어장에 담고 모아 보기\n"
    "\n**📜 문장 수어 · 실전 회화**\n"
    "`/문장수어` — 속담 · 명언 · 일상/VRChat 회화를 수어 어순으로 옮기고 스레드에서 토론하기\n"
    "\n**👤 개인 프로필**\n"
    "`/내정보` — 내 레벨 · 포인트 · 연속 출석일 · 오늘 퀴즈 보상 확인"
)

# /내정보 설정
KST = timezone(timedelta(hours=9))  # 출석 날짜 기준 (cogs/sign_language.py 의 KST 와 같아야 합니다)
EXP_PER_LEVEL = 100                 # 경험치 100마다 레벨 1 상승 (30 exp → 레벨 1, 130 exp → 레벨 2)
LEVEL_BAR_LENGTH = 10
COLOR_PROFILE = discord.Color.from_rgb(126, 179, 255)


def level_progress(exp: int) -> tuple[int, int]:
    """
    경험치로 레벨을 계산합니다. 반환: (레벨, 이번 레벨에서 쌓은 경험치)
    ※ DB 의 users.level 은 지금까지 올려 주는 곳이 없어 늘 1이므로, 표시할 때 경험치로 계산합니다.
    """
    exp = max(0, exp)
    return exp // EXP_PER_LEVEL + 1, exp % EXP_PER_LEVEL


def streak_text(user: Row | None, today: date) -> str:
    """
    연속 출석 표시. DB 의 streak 은 출석할 때만 바뀌므로 마지막 출석일을 함께 봅니다.
    (/오늘의수어 와 같은 규칙: 어제 출석했으면 이어지고, 그보다 오래됐으면 끊긴 것)
    """
    last = user["last_daily_date"] if user else None
    if not last:
        return "아직 출석 기록이 없어요\n`/오늘의수어` 로 첫 출석!"
    if last == today.isoformat():
        return f"**{user['streak']}**일차 ✅\n오늘 출석 완료!"
    if last == (today - timedelta(days=1)).isoformat():
        return f"**{user['streak']}**일차\n오늘 `/오늘의수어` 로 이어 가요!"
    return "끊겼어요 😢\n`/오늘의수어` 로 다시 시작!"


def build_profile_embed(
    name: str,
    avatar_url: str | None,
    user: Row | None,
    bookmark_count: int,
    quiz_rewards_today: int,
    today: date,
    sentence_done: bool = False,
) -> discord.Embed:
    """/내정보 임베드. 아직 한 번도 활동하지 않은 유저(user=None)는 0으로 보여 줍니다."""
    exp = user["exp"] if user else 0
    points = user["points"] if user else 0
    level, gained = level_progress(exp)
    filled = gained * LEVEL_BAR_LENGTH // EXP_PER_LEVEL
    bar = "▰" * filled + "▱" * (LEVEL_BAR_LENGTH - filled)

    embed = discord.Embed(title=f"👤 {name} 님의 수어 학습 프로필", color=COLOR_PROFILE)
    if user is None:
        embed.description = "아직 학습 기록이 없어요! `/오늘의수어` 로 첫걸음을 떼 보세요 🌱"
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    embed.add_field(
        name="🎖️ 레벨 · 🧪 경험치",
        value=(
            f"레벨 **{level}** · 경험치 **{exp}** exp\n"
            f"`{bar}` 다음 레벨까지 **{EXP_PER_LEVEL - gained}** exp"
        ),
        inline=False,
    )
    embed.add_field(name="🪙 보유 포인트", value=f"**{points}** pt", inline=True)
    embed.add_field(name="🔥 연속 출석", value=streak_text(user, today), inline=True)
    embed.add_field(
        name="📚 단어장",
        value=f"**{bookmark_count}**개 저장 중\n"
              + ("`/수어단어장` 에서 보기" if bookmark_count else "`/단어장저장` 으로 담아 보세요"),
        inline=True,
    )
    remaining = max(0, MAX_DAILY_QUIZ_REWARDS - quiz_rewards_today)
    embed.add_field(
        name="🎯 오늘 퀴즈 보상",
        value=f"**{quiz_rewards_today}** / {MAX_DAILY_QUIZ_REWARDS}회\n"
              + (f"**{remaining}**회 더 받을 수 있어요" if remaining else "오늘은 다 받았어요! 🌙"),
        inline=True,
    )
    embed.add_field(
        name="✍️ 오늘 문장 수어",
        value="참여 완료 ✅\n토론 스레드도 둘러봐요!" if sentence_done
              else f"아직이에요\n`/문장수어` 참여 시 +{SENTENCE_POINTS}pt · +{SENTENCE_EXP}exp",
        inline=True,
    )
    embed.set_footer(text="경험치는 /오늘의수어 출석 · /수어퀴즈 정답 · /문장수어 참여로 쌓여요 🤟")
    return embed


class General(commands.Cog, name="기본"):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db  # /내정보 에서 유저 기록을 읽습니다

    # ── /안녕 ────────────────────────────────────────────────────
    @app_commands.command(name="안녕", description="이미숫 조교와 인사를 나눕니다.")
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def hello(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지

        embed = discord.Embed(
            title="🤟 안녕하세요! 보조선생님 이미숫입니다",
            description=(
                "한국대학교 한국수어연구학 조교 **이미숫**이에요. 만나서 반갑습니다!\n"
                "오늘도 한 단어씩, 천천히 같이 익혀 봐요."
            ),
            color=discord.Color.from_rgb(255, 183, 77),
        )
        embed.add_field(name="📚 이런 걸 할 수 있어요", value=HELP_TEXT, inline=False)
        embed.set_footer(text="궁금한 게 있으면 언제든 불러 주세요! 🤟")
        await interaction.followup.send(embed=embed)

    # ── /내정보 ──────────────────────────────────────────────────
    @app_commands.command(
        name="내정보",
        description="나의 수어 학습 레벨, 포인트, 연속 출석일수, 단어장 현황을 확인합니다.",
    )
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def my_profile(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지

        user_id = interaction.user.id
        today = datetime.now(KST).date()
        user = await self.db.get_user(user_id)  # 한 번도 활동 안 했으면 None (새로 만들지 않음)
        bookmarks = await self.db.get_user_bookmarks(user_id)
        # 오늘 퀴즈 보상을 몇 번 받았는지 (상한선을 넘긴 정답도 기록에는 남으므로 잘라서 표시)
        quiz_rewards_today = min(
            await self.db.get_today_quiz_reward_count(user_id, today), MAX_DAILY_QUIZ_REWARDS
        )
        sentence_done = await self.db.has_sentence_submission_on(user_id, today)

        embed = build_profile_embed(
            interaction.user.display_name,
            interaction.user.display_avatar.url,
            user,
            len(bookmarks),
            quiz_rewards_today,
            today,
            sentence_done,
        )
        await interaction.followup.send(embed=embed)

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
            log.warning("만료된 상호작용 (10062): %s", interaction.command)
            return
        else:
            log.exception("기본 명령어 처리 중 오류", exc_info=error)
            msg = "앗, 조교가 잠깐 헷갈렸어요 😵 잠시 후 다시 시도해 주세요!"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            log.warning("오류 안내 메시지를 보내지 못했습니다.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot, bot.db))  # type: ignore[attr-defined]
