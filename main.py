"""
main.py
조교 이미숫 - 엔트리 포인트 (Cog 자동 로드 + DB 연결 + 헬스 체크 서버)

Render 같은 웹 서비스 호스팅은 '열려 있는 HTTP 포트'가 있어야 배포를 살아 있다고 판단합니다.
그래서 봇과 함께 아주 가벼운 웹 서버를 띄워 / 와 /health 요청에 200 OK 로 답합니다.
(웹 서버는 discord.py 가 이미 쓰는 aiohttp 로 만들어, 추가 패키지가 필요하지 않습니다)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

from database import Database

BASE_DIR = Path(__file__).resolve().parent
COGS_DIR = BASE_DIR / "cogs"

load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # (선택) 테스트 서버 ID - 설정하면 해당 서버에 즉시 동기화
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "imisut.db"))
# 호스팅이 지정해 주는 포트. Render 는 PORT 를 넣어 주고, 로컬에서는 10000 을 씁니다.
HEALTH_PORT = int(os.getenv("PORT", "10000"))
HEALTH_HOST = "0.0.0.0"  # 호스팅 바깥에서 들어오는 헬스 체크를 받으려면 모든 주소에서 들어야 합니다

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
        # 클라우드(PostgreSQL)인지 로컬 파일(SQLite)인지 콘솔에서 바로 알 수 있게 남깁니다.
        log.info("🗄️ DB 연결 완료: %s", self.db.describe())
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


async def health_check(request: web.Request) -> web.Response:
    """호스팅의 헬스 체크 요청에 답합니다. (GET / · GET /health)"""
    return web.Response(text="Leemisut Bot is running!", status=200)


async def start_health_server() -> web.AppRunner | None:
    """
    헬스 체크용 HTTP 서버를 띄웁니다. (봇을 막지 않고 백그라운드로 돕니다)

    포트를 열지 못해도 봇 본체는 계속 켭니다. 디스코드에서 쓰는 데에는 지장이 없고,
    호스팅이 포트를 못 찾는 문제는 아래 경고 로그로 확인할 수 있습니다.
    """
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app, access_log=None)  # 헬스 체크 요청까지 로그로 남기면 시끄럽습니다
    await runner.setup()
    try:
        await web.TCPSite(runner, HEALTH_HOST, HEALTH_PORT).start()
    except OSError:
        await runner.cleanup()
        log.warning(
            "⚠️ 헬스 체크 서버가 %s:%d 포트를 열지 못했습니다. (이미 쓰는 중일 수 있어요) "
            "봇은 그대로 실행합니다.",
            HEALTH_HOST,
            HEALTH_PORT,
        )
        return None

    log.info("🌐 헬스 체크 서버 시작: %s:%d (GET / · /health)", HEALTH_HOST, HEALTH_PORT)
    return runner


async def run_all() -> None:
    """
    헬스 체크 서버를 먼저 열고 나서 봇을 접속시킵니다.

    순서가 중요합니다. 디스코드 로그인이나 DB 연결이 늦어져도 포트는 이미 열려 있어서,
    호스팅이 'No open ports detected' 로 배포를 실패 처리하지 않습니다.
    """
    runner = await start_health_server()
    bot = ImisutBot()
    try:
        async with bot:  # 나갈 때 bot.close() 가 불려 DB 연결까지 정리됩니다
            await bot.start(TOKEN, reconnect=True)
    finally:
        if runner is not None:
            await runner.cleanup()


def main() -> None:
    if not TOKEN:
        raise SystemExit("❌ .env 파일에 DISCORD_TOKEN이 설정되어 있지 않습니다.")

    discord.utils.setup_logging(level=logging.INFO)  # discord + 우리 봇 로그를 함께 출력
    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        log.info("👋 종료 신호를 받아 봇과 헬스 체크 서버를 정리했습니다.")


if __name__ == "__main__":
    main()
