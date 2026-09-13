"""
cogs/general.py
조교 이미숫 - 기본 명령어 (/안녕, /명언, /격언)

/명언 은 한 문장을 보여 주고 "이걸 수어로 어떻게 옮길까?"를 떠올리게 하는 학습용 명령어입니다.
문구를 추가하시려면 아래 QUOTES 목록에 Quote(...) 한 줄만 더 쓰시면 됩니다.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


# ── 문구 자료 ───────────────────────────────────────────────────
@dataclass(frozen=True)
class Quote:
    text: str                       # 문장
    source: str                     # 출처
    category: str                   # 아래 CATEGORIES 의 키
    keywords: tuple[str, ...] = ()  # 수어로 옮길 때 중심이 되는 단어


CATEGORIES: dict[str, dict[str, object]] = {
    "필적확인란": {"emoji": "✍️", "color": discord.Color.from_rgb(126, 179, 255)},
    "사자성어":   {"emoji": "📖", "color": discord.Color.from_rgb(224, 168, 91)},
    "문학":  {"emoji": "🌙", "color": discord.Color.from_rgb(167, 139, 216)},
    "명언":  {"emoji": "💡", "color": discord.Color.from_rgb(122, 198, 160)},
}

QUOTES: list[Quote] = [
    # ── ✍️ 필적확인란 (역대 수능에서 실제로 쓰인 문구) ──────────
    Quote("흙에서 자란 내 마음 파란 하늘빛",
          "정지용 「향수」 · 2006학년도 수능", "필적확인란", ("흙", "마음", "하늘")),
    Quote("넓은 벌 동쪽 끝으로 옛이야기",
          "정지용 「향수」 · 2007학년도 수능", "필적확인란", ("넓다", "동쪽", "이야기")),
    Quote("손금에 맑은 강물이 흐르고",
          "윤동주 「소년」 · 2008학년도 수능", "필적확인란", ("손", "맑다", "강")),
    Quote("이 많은 별빛이 내린 언덕 위에",
          "윤동주 「별 헤는 밤」 · 2009학년도 수능", "필적확인란", ("별", "많다", "언덕")),
    Quote("맑은 강물처럼 조용하고 은근하며",
          "유안진 「지란지교를 꿈꾸며」 · 2010학년도 수능", "필적확인란", ("강", "조용하다")),
    Quote("날마다 새로우며 깊어지며 넓어진다",
          "정채봉 「첫 마음」 · 2011학년도 수능", "필적확인란", ("날마다", "새롭다", "넓다")),
    Quote("진실로 내가 그대를 사랑하는 까닭은",
          "황동규 「즐거운 편지」 · 2012학년도 수능", "필적확인란", ("진실", "사랑", "까닭")),
    Quote("맑은 햇빛으로 반짝반짝 물들으며",
          "정한모 「가을에」 · 2013학년도 수능", "필적확인란", ("맑다", "해", "반짝반짝")),
    Quote("꽃초롱 불 밝히듯 눈을 밝힐까",
          "박정만 「작은 연가」 · 2014학년도 수능", "필적확인란", ("꽃", "불", "눈")),
    Quote("햇살도 둥글둥글하게 뭉치는 맑은 날",
          "문태준 「돌의 배」 · 2015학년도 수능", "필적확인란", ("해", "뭉치다", "맑다")),
    Quote("넓음과 깊음을 가슴에 채우며",
          "주요한 「청년이여 노래하라」 · 2016학년도 수능", "필적확인란", ("넓다", "깊다", "채우다")),
    Quote("흙에서 자란 내 마음 파아란 하늘빛",
          "정지용 「향수」 · 2017학년도 수능", "필적확인란", ("흙", "파랗다", "하늘")),
    Quote("큰 바다 넓은 하늘을 우리는 가졌노라",
          "김영랑 「바다로 가자」 · 2018학년도 수능", "필적확인란", ("바다", "하늘", "가지다")),
    Quote("그대만큼 사랑스러운 사람을 본 일이 없다",
          "김남조 「편지」 · 2019학년도 수능", "필적확인란", ("사랑", "만큼", "사람")),
    Quote("너무 맑고 초롱한 그 중 하나 별이여",
          "박두진 「별밭에 누워」 · 2020학년도 수능", "필적확인란", ("맑다", "초롱하다", "별")),
    Quote("많고 많은 사람 중에 그대 한 사람",
          "나태주 「들길을 걸으며」 · 2021학년도 수능", "필적확인란", ("많다", "중", "사람")),
    Quote("넓은 하늘로의 비상을 꿈꾸며",
          "이해인 「작은 노래 2」 · 2022학년도 수능", "필적확인란", ("넓다", "하늘", "꿈")),
    Quote("나의 꿈은 맑은 바람이 되어서",
          "한용운 「나의 꿈」 · 2023학년도 수능", "필적확인란", ("꿈", "맑다", "바람")),
    Quote("가장 넓은 길은 언제나 내 마음속에",
          "양광모 「가장 넓은 길」 · 2024학년도 수능", "필적확인란", ("넓다", "길", "마음")),
    Quote("저 넓은 세상에서 큰 꿈을 펼쳐라",
          "곽의영 「하나뿐인 예쁜 딸아」 · 2025학년도 수능", "필적확인란", ("하나", "예쁘다", "딸")),
    Quote("초록 물결이 톡톡 튀는 젊음처럼",
          "안규례 「아침 산책」 · 2026학년도 수능", "필적확인란", ("초록", "물결", "젊다")),

    # ── 📖 사자성어 ────────────────────────────────────────────
    Quote("우공이산 (愚公移山)",
          "쉬지 않고 노력하면 어떤 어려운 일도 끝내 이룰 수 있어요.", "사자성어", ("산", "옮기다", "노력")),
    Quote("이심전심 (以心傳心)",
          "말이 없어도 마음에서 마음으로 뜻이 전해져요.", "사자성어", ("마음", "전하다")),
    Quote("백문불여일견 (百聞不如一見)",
          "백 번 듣는 것보다 한 번 보는 것이 낫습니다.", "사자성어", ("듣다", "보다", "하나")),
    Quote("교학상장 (敎學相長)",
          "가르치는 사람과 배우는 사람이 함께 자랍니다.", "사자성어", ("가르치다", "배우다", "자라다")),
    Quote("대기만성 (大器晩成)",
          "큰 그릇은 늦게 만들어집니다. 조급해하지 않아도 괜찮아요.", "사자성어", ("크다", "그릇", "늦다")),
    Quote("수적천석 (水滴穿石)",
          "작은 물방울도 오래 떨어지면 돌을 뚫습니다.", "사자성어", ("물", "돌", "뚫다")),
    Quote("온고지신 (溫故知新)",
          "옛것을 잘 익혀 새로운 것을 알아 갑니다.", "사자성어", ("옛날", "새롭다", "알다")),
    Quote("역지사지 (易地思之)",
          "처지를 바꾸어 상대의 마음을 헤아려 봅니다.", "사자성어", ("바꾸다", "생각하다")),

    # ── 🌙 문학 · 소설 ─────────────────────────────────────────
    Quote("중요한 것은 눈에 보이지 않아",
          "생텍쥐페리 『어린 왕자』", "문학", ("중요하다", "눈", "보이다")),
    Quote("죽는 날까지 하늘을 우러러 한 점 부끄럼이 없기를",
          "윤동주 「서시」", "문학", ("하늘", "부끄럽다", "없다")),
    Quote("산에는 꽃 피네 꽃이 피네",
          "김소월 「산유화」", "문학", ("산", "꽃", "피다")),
    Quote("나 하나 꽃 피어 풀밭이 달라지겠느냐고 말하지 말아라",
          "조동화 「나 하나 꽃 피어」", "문학", ("하나", "꽃", "달라지다")),
    Quote("천천히, 그러나 꾸준히",
          "이솝 우화 「토끼와 거북이」", "문학", ("천천히", "꾸준하다")),

    # ── 💡 명언 · 격언 ─────────────────────────────────────────
    Quote("천 리 길도 한 걸음부터",
          "우리 속담", "명언", ("길", "하나", "걸음")),
    Quote("아는 것이 힘이다",
          "프랜시스 베이컨", "명언", ("알다", "힘")),
    Quote("가는 말이 고와야 오는 말이 곱다",
          "우리 속담", "명언", ("말", "곱다", "오다")),
    Quote("혼자 할 수 있는 일은 적지만, 함께하면 많은 일을 할 수 있습니다",
          "헬렌 켈러", "명언", ("혼자", "함께", "많다")),
    Quote("배움은 마르지 않는 샘물과 같아서, 퍼낼수록 맑아집니다",
          "이미숫 조교의 한마디", "명언", ("배우다", "샘물", "맑다")),
    Quote("수어는 손으로 하는 말이 아니라, 몸 전체로 짓는 문장입니다",
          "이미숫 조교의 한마디", "명언", ("수어", "손", "몸", "말")),
    Quote("표정도 문법입니다. 눈썹 하나가 문장을 바꿉니다",
          "이미숫 조교의 한마디", "명언", ("표정", "눈썹", "바꾸다")),
]

CHOICE_ALL = "전체"
PRACTICE_TIP = (
    "이 문장을 수어로 표현한다면 중심이 되는 핵심 단어를 "
    "어떻게 시각적으로 배치할지 마음속으로 손짓해 보세요!"
)
FOOTER_TEXT = "오늘도 마음으로 그리는 수어 한 문장 🤟"


def build_quote_embed(quote: Quote) -> discord.Embed:
    """문구 하나를 임베드로 만듭니다."""
    meta = CATEGORIES.get(quote.category, {})
    emoji = str(meta.get("emoji", "📜"))
    color = meta.get("color") or discord.Color.blurple()

    embed = discord.Embed(
        title="📜 오늘의 한 문장 (이미숫 조교의 수어 교실)",
        description=f"> ### {quote.text}\n\n{emoji} **{quote.category}** · {quote.source}",
        color=color,  # type: ignore[arg-type]
    )

    tip = PRACTICE_TIP
    if quote.keywords:
        tip += "\n\n💭 핵심 단어: " + " · ".join(f"`{word}`" for word in quote.keywords)
    embed.add_field(name="🤟 수어 번역 생각하기", value=tip, inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class General(commands.Cog, name="기본"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # 채널마다 바로 직전에 나온 문구를 기억해 두 번 연속 같은 문장이 나오지 않게 합니다.
        self._last_shown: dict[int, str] = {}

    # ── /안녕 ────────────────────────────────────────────────────
    @app_commands.command(name="안녕", description="이미숫 조교와 인사를 나눕니다.")
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
        embed.add_field(
            name="📚 이런 걸 할 수 있어요",
            value=(
                "`/오늘의수어` — 오늘 배울 수어 단어를 알려 드려요\n"
                "`/수어퀴즈` — 수어 동작을 보고 단어를 맞혀 봐요\n"
                "`/명언 or /격언` — 한 문장을 수어로 옮겨 표현력을 길러요"
            ),
            inline=False,
        )
        embed.set_footer(text="궁금한 게 있으면 언제든 불러 주세요! 🤟")
        await interaction.followup.send(embed=embed)

    # ── /명언 ────────────────────────────────────────────────────
    @app_commands.command(
        name="명언",
        description="일상 속 명언, 필적확인란 문구, 사자성어를 통해 수어 번역 사고력을 키워봐요!",
    )
    @app_commands.describe(분류="보고 싶은 갈래가 있으면 골라 주세요. (비우면 전체에서 무작위)")
    @app_commands.choices(분류=[
        app_commands.Choice(name="전체", value=CHOICE_ALL),
        app_commands.Choice(name="✍️ 필적확인란", value="필적확인란"),
        app_commands.Choice(name="📖 사자성어", value="사자성어"),
        app_commands.Choice(name="🌙 문학·소설", value="문학"),
        app_commands.Choice(name="💡 명언·격언", value="명언"),
    ])
    async def quote_command(
        self, interaction: discord.Interaction, 분류: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지
        await self._send_quote(interaction, 분류.value if 분류 else CHOICE_ALL)

    # ── /격언 (/명언 과 같은 기능, 익숙한 쪽으로 부르시라고 함께 둡니다) ──
    @app_commands.command(
        name="격언",
        description="일상 속 명언, 필적확인란 문구, 사자성어를 통해 수어 번역 사고력을 키워봐요!",
    )
    @app_commands.describe(분류="보고 싶은 갈래가 있으면 골라 주세요. (비우면 전체에서 무작위)")
    @app_commands.choices(분류=[
        app_commands.Choice(name="전체", value=CHOICE_ALL),
        app_commands.Choice(name="✍️ 필적확인란", value="필적확인란"),
        app_commands.Choice(name="📖 사자성어", value="사자성어"),
        app_commands.Choice(name="🌙 문학·소설", value="문학"),
        app_commands.Choice(name="💡 명언·격언", value="명언"),
    ])
    async def saying_command(
        self, interaction: discord.Interaction, 분류: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer()  # ← 3초 타임아웃 방지
        await self._send_quote(interaction, 분류.value if 분류 else CHOICE_ALL)

    # ── 공통 처리 ────────────────────────────────────────────────
    async def _send_quote(self, interaction: discord.Interaction, category: str) -> None:
        pool = [q for q in QUOTES if category == CHOICE_ALL or q.category == category]
        if not pool:
            await interaction.followup.send(
                "그 갈래에는 아직 문장이 없어요! 🥲 다른 갈래로 골라 주세요.", ephemeral=True
            )
            return

        quote = self._pick(interaction.channel_id, pool)
        await interaction.followup.send(embed=build_quote_embed(quote))

    def _pick(self, channel_id: int | None, pool: list[Quote]) -> Quote:
        """직전에 나온 문장은 피해서 고릅니다. (고를 게 하나뿐이면 그대로)"""
        last = self._last_shown.get(channel_id or 0)
        candidates = [q for q in pool if q.text != last] or pool
        quote = random.choice(candidates)
        self._last_shown[channel_id or 0] = quote.text
        return quote

    # ── 공통 에러 처리 ───────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, discord.NotFound) and original.code == 10062:
            log.warning("만료된 상호작용 (10062): %s", interaction.command)
            return

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
    await bot.add_cog(General(bot))
