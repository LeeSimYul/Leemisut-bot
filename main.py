"""
main.py
조교 이미숫 - 엔트리 포인트 (Cog 자동 로드 + DB 연결)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import Database

BASE_DIR = Path(__file__).resolve().parent
COGS_DIR = BASE_DIR / "cogs"

load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # (선택) 테스트 서버 ID - 설정하면 해당 서버에 즉시 동기화
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "imisut.db"))

log = logging.getLogger("imisut")


class ImisutBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # 특권 인텐트 (개발자 포털에서 활성화 필요)
        intents.members = True          # 특권 인텐트
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(DB_PATH)
        self.ready_count = 0  # on_ready 실행 횟수 (첫 접속인지 재접속인지 구분용)

    async def setup_hook(self) -> None:
        """로그인 직후, 게이트웨이 접속 전에 딱 한 번 실행됩니다."""
        await self.db.connect()
        log.info("🗄️ DB 연결 완료: %s", self.db.path)
        await self.load_cogs()
        await self.sync_commands()

    async def load_cogs(self) -> None:
        """cogs 폴더의 .py 파일을 모두 확장으로 로드합니다. (_로 시작하는 파일 제외)"""
        for file in sorted(COGS_DIR.glob("*.py")):
            if file.stem.startswith("_"):
                continue
            extension = f"cogs.{file.stem}"
            try:
                await self.load_extension(extension)
                log.info("🧩 Cog 로드 완료: %s", extension)
            except Exception:
                log.exception("❌ Cog 로드 실패: %s", extension)

    async def sync_commands(self) -> None:
        try:
            if DEV_GUILD_ID:
                guild = discord.Object(id=int(DEV_GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                scope = f"테스트 서버({DEV_GUILD_ID})"
            else:
                synced = await self.tree.sync()
                scope = "전역"
            log.info("🔗 %s 슬래시 명령어 %d개 동기화", scope, len(synced))
        except discord.HTTPException:
            log.exception("❌ 슬래시 명령어 동기화 실패")

    async def on_ready(self) -> None:
        """
        접속이 끝날 때마다 실행됩니다. (첫 접속 + 끊겼다 붙는 재접속)

        같은 토큰으로 여러 PC·프로세스에서 봇을 켜 두면 하나의 상호작용을 여러 봇이 함께 받아
        /명령어가 10062(만료된 상호작용)로 실패합니다. 콘솔만 보고도 '지금 이 창이 어느 봇이고
        몇 번째 접속인지' 바로 알 수 있도록 신원을 또렷하게 남깁니다.
        """
        self.ready_count += 1
        user = self.user
        name = user.name if user else "이미숫"

        log.info("✨ 수어 연구학 조교 %s(이)가 성공적으로 디스코드에 접속했습니다!", name)
        log.info(
            "🆔 로그인 계정: %s (User ID: %s) · Application ID: %s",
            user,
            user.id if user else "?",
            self.application_id,
        )
        log.info(
            "🖥️ 프로세스 PID: %s · 게이트웨이 세션: %s · 참여 서버: %d곳",
            os.getpid(),
            getattr(self.ws, "session_id", "?"),
            len(self.guilds),
        )

        if self.ready_count == 1:
            log.info(
                "☝️ 봇은 한 번에 한 곳에서만 켜 주세요. 다른 PC나 터미널에서 같은 토큰으로 또 켜면 "
                "슬래시 명령어가 10062 오류로 실패할 수 있습니다."
            )
        else:
            log.warning(
                "🔁 이 프로세스에서 %d번째 접속입니다. 잠깐 끊겼다 붙은 재접속이면 정상이지만, "
                "자주 반복되면 다른 곳에서도 같은 토큰으로 봇이 켜져 있는지 확인해 주세요.",
                self.ready_count,
            )

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.db.close()


def main() -> None:
    if not TOKEN:
        raise SystemExit("❌ .env 파일에 DISCORD_TOKEN이 설정되어 있지 않습니다.")

    discord.utils.setup_logging(level=logging.INFO)  # discord + 우리 봇 로그를 함께 출력
    bot = ImisutBot()
    bot.run(TOKEN, log_handler=None)  # 위에서 로깅을 설정했으므로 중복 설정 방지


if __name__ == "__main__":
    main()
