"""
utils/paginator.py
조교 이미숫 - 버튼으로 페이지를 넘기는 공용 뷰 (/수어검색, /수어목록 에서 사용)

    [◀ 이전]  [1 / N 페이지]  [▶ 다음]

■ 페이지는 render_page(페이지 번호) 로 필요할 때 한 장씩 만듭니다.
  단어 3,000개짜리 목록도 전부 미리 불러오지 않고, 한 번 만든 페이지는 기억해 둡니다.
■ 명령어를 실행한 사람만 버튼을 누를 수 있습니다.
■ 마지막 조작 후 60초가 지나면 버튼이 모두 잠깁니다. (disabled=True)

⚠️ cogs 끼리 서로 import 하면 확장(extension) 모듈이 두 번 만들어지므로
   여러 Cog 가 함께 쓰는 컴포넌트는 utils 에 둡니다.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

log = logging.getLogger(__name__)

PAGINATOR_TIMEOUT = 60.0  # 초 - 마지막 조작 후 이 시간이 지나면 버튼이 잠깁니다

# 페이지 번호(0부터)를 받아 그 페이지의 임베드를 돌려주는 함수
PageRenderer = Callable[[int], Awaitable[discord.Embed]]


def page_count(total_items: int, page_size: int) -> int:
    """항목 수를 페이지 수로 바꿉니다. (0개여도 최소 1페이지)"""
    return max(1, -(-total_items // page_size))


class SignPaginatorView(discord.ui.View):
    """
    사용 예 (명령어에서 defer() 한 뒤):

        async def render_page(page: int) -> discord.Embed:
            rows = await db.search_words_by_filter(..., limit=10, offset=page * 10)
            return 임베드

        view = SignPaginatorView(interaction.user, page_count(total, 10), render_page)
        await view.start(interaction, ephemeral=True)
    """

    def __init__(
        self,
        owner: discord.abc.User,
        total_pages: int,
        render_page: PageRenderer,
        *,
        timeout: float = PAGINATOR_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.owner = owner
        self.total_pages = max(1, total_pages)
        self.render_page = render_page
        self.page = 0
        # followup.send(wait=True) 로 받은 메시지 (아직 아무도 버튼을 안 눌렀을 때 잠금용)
        self.message: discord.WebhookMessage | None = None
        # 마지막으로 버튼을 누른 상호작용. 토큰이 가장 최근 것이라 시간 초과 시 잠금에 우선 사용합니다.
        self._last_interaction: discord.Interaction | None = None
        self._rendered: dict[int, discord.Embed] = {}  # 한 번 만든 페이지는 다시 조회하지 않음
        self._lock = asyncio.Lock()  # 버튼을 연타해도 페이지가 꼬이지 않도록 한 번에 하나씩 처리
        self._sync_buttons()

    # ── 시작 ───────────────────────────────────────────────────
    async def start(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        """
        첫 페이지를 보냅니다. 명령어에서 interaction.response.defer() 를 한 뒤에 호출해 주세요.
        한 페이지로 끝나면 버튼 없이 보냅니다.
        """
        embed = await self._get_page(0)
        if self.total_pages == 1:
            self.stop()
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
            return
        # wait=True 를 줘야 메시지 객체가 돌아옵니다 (시간 초과 시 버튼 잠금에 필요)
        self.message = await interaction.followup.send(
            embed=embed, view=self, ephemeral=ephemeral, wait=True
        )

    # ── 버튼 ───────────────────────────────────────────────────
    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.primary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[SignPaginatorView]
    ) -> None:
        await self._move(interaction, -1)

    @discord.ui.button(label="1 / 1 페이지", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_indicator(
        self, interaction: discord.Interaction, button: discord.ui.Button[SignPaginatorView]
    ) -> None:
        """현재 위치를 보여 주는 표시용 버튼입니다. (항상 비활성이라 눌리지 않습니다)"""
        await interaction.response.defer()

    @discord.ui.button(label="▶ 다음", style=discord.ButtonStyle.primary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[SignPaginatorView]
    ) -> None:
        await self._move(interaction, +1)

    # ── 내부 처리 ───────────────────────────────────────────────
    def _sync_buttons(self) -> None:
        """현재 페이지에 맞게 버튼 상태와 가운데 표시를 맞춥니다."""
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1
        self.page_indicator.label = f"{self.page + 1} / {self.total_pages} 페이지"

    async def _get_page(self, page: int) -> discord.Embed:
        if page not in self._rendered:
            self._rendered[page] = await self.render_page(page)
        return self._rendered[page]

    async def _move(self, interaction: discord.Interaction, step: int) -> None:
        # 페이지를 만드는 동안 3초 제한에 걸리지 않도록 먼저 응답을 미룹니다. (버튼은 '로딩 중')
        await interaction.response.defer()
        async with self._lock:
            # 이동 폭을 잠금 안에서 적용해야 빠르게 두 번 눌렀을 때 두 칸 넘어갑니다.
            self.page = max(0, min(self.page + step, self.total_pages - 1))
            embed = await self._get_page(self.page)
            self._sync_buttons()
            self._last_interaction = interaction
            # 버튼 상호작용의 원본 응답 = 버튼이 달린 메시지 (ephemeral 메시지도 수정 가능)
            await interaction.edit_original_response(embed=embed, view=self)

    # ── 권한 · 시간 초과 · 오류 ─────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner.id:
            return True
        await interaction.response.send_message(
            f"이 목록은 **{self.owner.display_name}** 님이 불러온 목록이에요! 🙌\n"
            "같은 명령어를 직접 실행하시면 넘겨 볼 수 있어요.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        """마지막 조작 후 60초가 지나면 버튼을 모두 잠급니다."""
        async with self._lock:
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            try:
                if self._last_interaction is not None:
                    await self._last_interaction.edit_original_response(view=self)
                elif self.message is not None:
                    await self.message.edit(view=self)
            except discord.HTTPException:
                pass  # 메시지가 삭제된 경우 등

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("페이지 넘기기 처리 중 오류", exc_info=error)
        msg = "앗, 페이지를 넘기다가 문제가 생겼어요 😢 명령어를 다시 실행해 주세요!"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            log.warning("오류 안내 메시지를 보내지 못했습니다.")
