"""
cogs/admin.py
조교 이미숫 - 관리자 전용 명령어 (단어 동기화 / 전체 동기화 / 삭제 / 목록)

⚠️ 모든 슬래시 명령어는 시작 직후 defer()로 응답을 미뤄 3초 타임아웃(10062)을 막습니다.
   defer 이후에는 반드시 interaction.followup.send() 를 사용해야 합니다.
■ /수어목록 은 10개씩 페이지 버튼(◀ 이전 · 1 / N · ▶ 다음)으로 넘겨 봅니다. (utils/paginator.py)
"""
from __future__ import annotations

import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import commands

from database import Database, load_admin_user_ids
from utils.ksl_api import (
    KSLApiError,
    RejectedWord,
    fetch_all_sign_words,
    fetch_many_sign_words,
    fetch_sign_words,
)
from utils.paginator import SignPaginatorView, page_count

log = logging.getLogger(__name__)

MAX_SYNC_KEYWORDS = 5      # /수어동기화 에서 한 번에 처리할 검색어 개수
DEFAULT_MAX_PAGES = 40     # /수어전체동기화 기본 페이지 수 (100건 × 40 = 4,000)
ROWS_PER_PAGE = 100        # 이 API의 페이지당 최대 건수
PROGRESS_INTERVAL = 3.0    # 진행 상황 메시지 수정 간격(초) - 너무 잦으면 요청 제한에 걸립니다
LIST_PAGE_SIZE = 10        # /수어목록 한 페이지에 보여 줄 단어 수

COLOR_ADMIN = discord.Color.dark_teal()
COLOR_FAIL = discord.Color.red()
COLOR_PROGRESS = discord.Color.blurple()


def _clip(text: str, limit: int = 1024) -> str:
    """임베드 필드 길이 제한(1024자)에 맞춰 자릅니다."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summarize(meaning: str, limit: int = 40) -> str:
    """동음이의어를 구분할 수 있을 만큼만 뜻을 요약합니다."""
    text = " ".join((meaning or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _fail_embed(exc: Exception) -> discord.Embed:
    embed = discord.Embed(
        title="❌ 수어사전에서 자료를 가져오지 못했어요",
        description=f"```{exc}```",
        color=COLOR_FAIL,
    )
    embed.set_footer(text="인증키(.env의 KSL_API_KEY)와 네트워크 연결을 확인해 주세요.")
    return embed


class NotBotAdmin(app_commands.CheckFailure):
    """서버 관리자이지만 .env 의 ADMIN_USER_IDS 목록에는 없는 사람이 실행했을 때."""


def bot_admin_only():
    """
    관리자 명령어 2중 검증의 두 번째 관문입니다.
        1차: @app_commands.checks.has_permissions(administrator=True) → 서버 관리자인가?
        2차: @bot_admin_only()                                        → 봇 관리자 목록에 있는가?

    ⚠️ 체크는 함수에 가까운(아래쪽) 데코레이터부터 실행됩니다.
       서버 권한을 먼저 보도록 has_permissions 를 async def 바로 위에 두세요.

    - ADMIN_USER_IDS 가 비어 있으면 통과시킵니다. (서버 관리자 권한만 확인)
    - 목록은 Admin Cog 가 로드될 때 한 번 읽어 둡니다. (Admin.admin_user_ids)
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        cog = interaction.command.binding if interaction.command else None
        if not isinstance(cog, Admin):
            raise NotBotAdmin()  # 목록을 확인할 수 없으면 막습니다 (fail-closed)
        if cog.admin_user_ids and interaction.user.id not in cog.admin_user_ids:
            log.warning(
                "봇 관리자 목록에 없는 사용자의 관리자 명령어 시도: %s (%s) → /%s",
                interaction.user, interaction.user.id, interaction.command.name,
            )
            raise NotBotAdmin()
        return True

    return app_commands.check(predicate)


class Admin(commands.Cog, name="관리"):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        # 관리자 명령어 2중 검증용 유저 ID 목록 (비어 있으면 서버 관리자 권한만 확인)
        # 값이 잘못 적혀 있으면 ValueError 로 Cog 로드가 실패해 관리자 명령어가 잠깁니다.
        self.admin_user_ids = load_admin_user_ids()
        if self.admin_user_ids:
            log.info("🔐 봇 관리자 %d명 등록 (ADMIN_USER_IDS)", len(self.admin_user_ids))
        else:
            log.info("🔓 ADMIN_USER_IDS 미설정 - 관리자 명령어는 서버 관리자 권한만 확인합니다.")

    # ── /수어동기화 ──────────────────────────────────────────────
    @app_commands.command(
        name="수어동기화",
        description="[관리자] 단어를 검색해 수어 자료를 가져옵니다. (쉼표로 여러 개 입력 가능)",
    )
    @app_commands.describe(
        단어명="예: 사랑  또는  사랑, 학교, 친구",
        미리보기="켜면 DB에 저장하지 않고 결과만 확인합니다.",
    )
    @bot_admin_only()                                         # 2차: 봇 관리자 목록
    @app_commands.checks.has_permissions(administrator=True)  # 1차: 서버 관리자 권한
    async def sync_signs(
        self, interaction: discord.Interaction, 단어명: str, 미리보기: bool = False
    ) -> None:
        await interaction.response.defer(ephemeral=True)  # ← API 호출은 오래 걸리므로 필수

        keywords = [k.strip() for k in 단어명.split(",") if k.strip()][:MAX_SYNC_KEYWORDS]
        if not keywords:
            await interaction.followup.send("검색할 단어를 입력해 주세요! ✏️", ephemeral=True)
            return

        api_key = os.getenv("KSL_API_KEY")
        rejected: list[RejectedWord] = []

        try:
            if len(keywords) == 1:
                rows = await fetch_sign_words(keywords[0], api_key, rejected=rejected)
            else:
                rows = await fetch_many_sign_words(keywords, api_key, rejected=rejected)
        except KSLApiError as exc:
            log.warning("수어사전 동기화 실패: %s", exc)
            await interaction.followup.send(embed=_fail_embed(exc), ephemeral=True)
            return
        except Exception:
            log.exception("수어사전 동기화 중 예상치 못한 오류")
            await interaction.followup.send(
                "동기화 중 예상치 못한 문제가 생겼어요 😢 로그를 확인해 주세요.", ephemeral=True
            )
            return

        saved = 0 if 미리보기 else await self.db.sync_api_words(rows)
        total = await self.db.count_words()

        embed = discord.Embed(
            title="🔍 동기화 미리보기" if 미리보기 else "📥 수어 단어 동기화 결과",
            description="검색어: " + ", ".join(f"`{k}`" for k in keywords),
            color=COLOR_ADMIN,
        )
        if 미리보기:
            summary = f"📗 검증 통과 **{len(rows)}개** (저장하지 않음)\n🚫 필터로 제외 {len(rejected)}개"
        else:
            summary = (
                f"✅ 새로 저장 **{saved}개**\n"
                f"📗 검증 통과 {len(rows)}개 (이미 있던 단어 {len(rows) - saved}개)\n"
                f"🚫 필터로 제외 {len(rejected)}개"
            )
        embed.add_field(name="결과", value=summary, inline=False)

        if rows:
            names = ", ".join(row[0] for row in rows[:15])
            if len(rows) > 15:
                names += f" 외 {len(rows) - 15}개"
            embed.add_field(name="통과한 단어", value=_clip(names), inline=False)
        else:
            # 서버 검색이 결과를 주지 않는 경우가 있어 대안을 안내합니다.
            embed.add_field(
                name="결과가 없나요?",
                value=(
                    "이 API는 단어 검색이 동작하지 않을 때가 있어요.\n"
                    "`/수어전체동기화` 로 전체 자료를 한 번 받아 두시면\n"
                    "`/수어목록` 에서 바로 찾아보실 수 있어요! 📚"
                ),
                inline=False,
            )
        if rejected:
            lines = "\n".join(f"• {r.word_name} — {r.reason}" for r in rejected[:10])
            embed.add_field(name="제외된 항목 (사유)", value=_clip(lines), inline=False)

        embed.set_footer(text=f"현재 총 단어 수: {total}개")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /수어전체동기화 ──────────────────────────────────────────
    @app_commands.command(
        name="수어전체동기화",
        description="[관리자] 일상생활수어 전체(약 3,754건)를 순회하며 DB에 저장합니다.",
    )
    @app_commands.describe(페이지수="가져올 최대 페이지 수 (1페이지 = 100건, 기본 40)")
    @bot_admin_only()                                         # 2차: 봇 관리자 목록
    @app_commands.checks.has_permissions(administrator=True)  # 1차: 서버 관리자 권한
    async def sync_all_signs(
        self, interaction: discord.Interaction, 페이지수: app_commands.Range[int, 1, 100] = DEFAULT_MAX_PAGES
    ) -> None:
        await interaction.response.defer(ephemeral=True)  # ← 수십 초가 걸리므로 필수

        before = await self.db.count_words()
        progress_embed = discord.Embed(
            title="⏳ 전체 수어 자료를 가져오는 중…",
            description="잠시만 기다려 주세요! 조교가 열심히 받아 오고 있어요 🤟",
            color=COLOR_PROGRESS,
        )
        message = await interaction.followup.send(embed=progress_embed, ephemeral=True, wait=True)

        last_edit = time.monotonic()

        async def on_progress(page_no: int, collected: int, total_count: int) -> None:
            """페이지를 한 장 받을 때마다 호출됩니다. (메시지 수정은 간격을 두고)"""
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < PROGRESS_INTERVAL:
                return
            last_edit = now

            done = min(page_no * ROWS_PER_PAGE, total_count or page_no * ROWS_PER_PAGE)
            bar_len = 12
            ratio = (done / total_count) if total_count else 0
            filled = min(bar_len, int(ratio * bar_len))
            bar = "█" * filled + "░" * (bar_len - filled)

            progress_embed.description = (
                f"`{bar}` {int(ratio * 100)}%\n"
                f"📄 {page_no}페이지 · 📗 통과 {collected}건 / 전체 {total_count or '?'}건"
            )
            try:
                await message.edit(embed=progress_embed)
            except discord.HTTPException:
                pass  # 진행 표시 실패는 무시하고 수집을 계속합니다

        rejected: list[RejectedWord] = []
        try:
            rows = await fetch_all_sign_words(
                max_pages=페이지수,
                rows_per_page=ROWS_PER_PAGE,
                api_key=os.getenv("KSL_API_KEY"),
                rejected=rejected,
                progress=on_progress,
            )
        except KSLApiError as exc:
            log.warning("전체 동기화 실패: %s", exc)
            await message.edit(embed=_fail_embed(exc))
            return
        except Exception:
            log.exception("전체 동기화 중 예상치 못한 오류")
            await message.edit(
                embed=discord.Embed(
                    title="❌ 전체 동기화 실패",
                    description="예상치 못한 문제가 생겼어요 😢 로그를 확인해 주세요.",
                    color=COLOR_FAIL,
                )
            )
            return

        saved = await self.db.sync_api_words(rows)  # INSERT OR IGNORE - 중복은 자동으로 건너뜁니다
        after = await self.db.count_words()

        embed = discord.Embed(
            title="📚 전체 수어 동기화 완료!",
            description=f"최대 {페이지수}페이지 × {ROWS_PER_PAGE}건 기준으로 수집했어요.",
            color=COLOR_ADMIN,
        )
        embed.add_field(
            name="결과",
            value=(
                f"✅ 새로 저장 **{saved}개**\n"
                f"📗 검증 통과 {len(rows)}개 (이미 있던 단어 {len(rows) - saved}개)\n"
                f"🚫 필터로 제외 {len(rejected)}개"
            ),
            inline=False,
        )
        embed.add_field(name="DB 단어 수", value=f"{before}개 → **{after}개**", inline=False)
        if rejected:
            lines = "\n".join(f"• {r.word_name} — {r.reason}" for r in rejected[:5])
            embed.add_field(name="제외된 항목 예시", value=_clip(lines), inline=False)
        embed.set_footer(text="필터 기준은 utils/ksl_api.py 상단의 '필터 설정'에서 조정할 수 있어요.")

        await message.edit(embed=embed)

    # ── /수어삭제 ────────────────────────────────────────────────
    @app_commands.command(name="수어삭제", description="[관리자] 잘못 저장된 수어 단어를 삭제합니다.")
    @app_commands.describe(단어명="삭제할 단어명 (입력하면 후보가 자동으로 뜹니다)")
    @bot_admin_only()                                         # 2차: 봇 관리자 목록
    @app_commands.checks.has_permissions(administrator=True)  # 1차: 서버 관리자 권한
    async def delete_sign(self, interaction: discord.Interaction, 단어명: str) -> None:
        await interaction.response.defer(ephemeral=True)  # ← 3초 타임아웃 방지

        name = 단어명.strip()
        word = await self.db.get_word_by_name(name)
        if word is None:
            similar = await self.db.search_words(name, limit=5)
            hint = (
                "\n비슷한 단어: " + ", ".join(f"`{w['word_name']}`" for w in similar)
                if similar else ""
            )
            await interaction.followup.send(
                f"`{name}` 은(는) DB에 없는 단어예요! 🔍{hint}", ephemeral=True
            )
            return

        same_name = await self.db.get_words_by_name(name)
        deleted = await self.db.delete_word_by_name(name)
        if not deleted:
            await interaction.followup.send(f"`{name}` 삭제에 실패했어요 😢", ephemeral=True)
            return

        if deleted > 1:  # 동음이의어가 함께 지워진 경우 무엇이 사라졌는지 알려 줍니다
            body = "\n".join(f"• {w['category']} / {_summarize(w['meaning'])}" for w in same_name)
            description = f"**{name}** 항목 {deleted}건을 지웠어요.\n{body}"
        else:
            description = (
                f"**{word['word_name']}** (분류: {word['category']})\n"
                f"> {_summarize(word['meaning'], 200)}"
            )

        embed = discord.Embed(
            title="🗑️ 단어를 삭제했어요",
            description=description,
            color=COLOR_ADMIN,
        )
        embed.set_footer(text=f"남은 단어 수: {await self.db.count_words()}개")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @delete_sign.autocomplete("단어명")
    async def delete_sign_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """자동완성은 defer가 불가능하므로, 가볍고 빠른 조회만 수행합니다."""
        try:
            words = await self.db.search_words(current, limit=25)
        except Exception:
            log.exception("자동완성 조회 실패")
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

    # ── /수어목록 ────────────────────────────────────────────────
    @app_commands.command(name="수어목록", description="[관리자] 저장된 수어 단어를 확인합니다.")
    @app_commands.describe(검색="비워 두면 전체 목록을 10개씩 넘겨 볼 수 있어요.")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_signs(self, interaction: discord.Interaction, 검색: str = "") -> None:
        await interaction.response.defer(ephemeral=True)  # ← 3초 타임아웃 방지

        keyword = 검색.strip()
        matched = await self.db.count_words_by_filter(keyword)  # 검색어가 비면 전체
        total = await self.db.count_words()                     # 캐시
        distinct = await self.db.count_distinct_words()         # 캐시
        if matched == 0:
            await interaction.followup.send(
                f"조건에 맞는 단어가 없어요! 📭 (전체 {total}개)", ephemeral=True
            )
            return

        header = f"🔍 검색어 `{keyword}` · {matched}개 찾음\n\n" if keyword else ""
        footer = f"전체 {total}개 (단어명 기준 {distinct}개)"

        async def render_page(page: int) -> discord.Embed:
            """버튼을 누를 때마다 그 페이지의 10개만 DB에서 가져옵니다."""
            offset = page * LIST_PAGE_SIZE
            words = await self.db.search_words_by_filter(
                keyword, limit=LIST_PAGE_SIZE, offset=offset
            )
            # 같은 단어명이 여러 개면 뜻 요약을 덧붙여 구분합니다. ('배' 같은 동음이의어)
            # 페이지 경계에 걸려 나뉘어도 알 수 있도록 DB 전체 기준으로 확인합니다.
            homonyms = await self.db.get_homonym_names([w["word_name"] for w in words])

            lines = []
            for number, w in enumerate(words, start=offset + 1):
                line = f"`{number}.` **{w['word_name']}** — {w['category']}"
                if w["word_name"] in homonyms:
                    line += f" / {_summarize(w['meaning'])}"
                lines.append(line)

            body = "\n".join(lines) or "이 페이지의 단어가 방금 삭제되었어요 🥲"
            embed = discord.Embed(
                title="📚 저장된 수어 단어",
                description=_clip(header + body, 4000),
                color=COLOR_ADMIN,
            )
            embed.set_footer(text=footer)
            return embed

        view = SignPaginatorView(interaction.user, page_count(matched, LIST_PAGE_SIZE), render_page)
        await view.start(interaction, ephemeral=True)

    # ── 공통 에러 처리 ───────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(error, app_commands.MissingPermissions):
            msg = "이 명령어는 서버 관리자만 사용할 수 있어요! 🔒"
        elif isinstance(error, NotBotAdmin):
            msg = "이 명령어는 등록된 봇 관리자만 사용할 수 있어요! 🔐"
        elif isinstance(original, discord.NotFound) and original.code == 10062:
            log.warning("만료된 상호작용 (10062): %s", interaction.command)
            return
        else:
            log.exception("관리자 명령어 처리 중 오류", exc_info=error)
            msg = "앗, 조교가 잠깐 헷갈렸어요 😵 로그를 확인해 주세요."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            log.warning("오류 안내 메시지를 보내지 못했습니다.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot, bot.db))  # type: ignore[attr-defined]
