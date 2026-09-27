"""
main.py
조교 이미숫 - 엔트리 포인트 (Cog 자동 로드 + DB 연결)

오라클 클라우드 VM 에서 systemd 서비스로 상시 가동합니다. 디스코드가 잠깐 5xx 를
돌려주거나 네트워크가 흔들려도 프로세스를 죽이지 않고, 점점 긴 간격으로 다시
접속을 시도합니다. (run_bot_forever) 서비스 재시작보다 조용하고, 끊긴 동안 쌓이는
재접속 요청도 줄어듭니다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import discord
from aiohttp import ClientError
from discord.ext import commands
from dotenv import load_dotenv

from database import Database

BASE_DIR = Path(__file__).resolve().parent
COGS_DIR = BASE_DIR / "cogs"

load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # (선택) 테스트 서버 ID - 설정하면 해당 서버에 즉시 동기화
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "imisut.db"))

# ── 접속 재시도 간격 ───────────────────────────────────────────
RETRY_BASE_DELAY = 30.0        # 첫 재시도까지 기다리는 시간(초). 실패할수록 2배씩 늘립니다
RETRY_MAX_DELAY = 600.0        # 아무리 늘어나도 이 간격(10분)을 넘기지 않습니다
RETRY_COOLDOWN_DELAY = 300.0   # Cloudflare 차단(429)은 금방 풀리지 않아 최소 5분은 쉽니다
RETRY_STABLE_UPTIME = 300.0    # 이만큼(5분) 붙어 있었다면 정상 가동으로 보고 간격을 초기화합니다
# 잠시 뒤 다시 시도해 볼 만한 오류들. 토큰·인텐트처럼 사람이 고쳐야 하는 오류는 넣지 않습니다.
RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    discord.HTTPException,     # 5xx · 429 만 재시도합니다 (_is_retryable_http 로 가립니다)
    discord.GatewayNotFound,  # 게이트웨이 주소를 못 받아온 경우
    discord.ConnectionClosed, # 게이트웨이가 예상 밖 코드로 끊은 경우
    ClientError,              # 연결 실패 · DNS 오류 등 aiohttp 쪽 문제
    OSError,                  # 네트워크가 잠깐 끊긴 경우
    TimeoutError,             # 응답이 오지 않는 경우 (asyncio.TimeoutError 와 같습니다)
)

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


def _is_retryable_http(exc: discord.HTTPException) -> bool:
    """잠시 뒤 다시 시도해 볼 만한 HTTP 오류인가? (디스코드 5xx · Cloudflare 429)"""
    return exc.status >= 500 or exc.status == 429


def _retry_delay(exc: BaseException, delay: float) -> float:
    """이번에 기다릴 시간. 요청 제한에 걸렸다면 더 오래 쉽니다."""
    if isinstance(exc, discord.HTTPException) and exc.status == 429:
        return max(delay, RETRY_COOLDOWN_DELAY)
    return delay


def _one_line(exc: BaseException, limit: int = 200) -> str:
    """
    오류 내용을 로그 한 줄로 줄입니다.

    디스코드가 JSON 대신 Cloudflare 오류 페이지를 돌려주면 그 HTML 전체가 오류 메시지에
    실려 옵니다. 그대로 찍으면 로그가 HTML로 뒤덮여 정작 필요한 줄이 묻히므로 잘라 냅니다.
    """
    try:
        text = " ".join(str(exc).split())
    except Exception:
        # 오류 메시지를 만드는 것조차 실패할 수 있습니다. 로그 때문에 재시도가 멈추면 안 됩니다.
        text = ""
    if not text:
        return type(exc).__name__
    return text if len(text) <= limit else f"{text[:limit]} …(이하 생략)"


async def run_bot_forever() -> None:
    """
    봇을 접속시키고, 일시적인 오류로 끊기면 점점 긴 간격으로 다시 시도합니다.

    systemd 의 자동 재시작(Restart=)에만 맡기지 않는 이유가 있습니다. 재시작은 매번 새
    프로세스로 디스코드에 다시 로그인해 요청 제한·차단을 오히려 길게 만들고, 간격도 고정입니다.
    한 프로세스 안에서 점점 길게 기다리는 편이 조용하고, DB 커넥션 풀도 그대로 살아 있습니다.
    (systemd 재시작은 프로세스 자체가 죽는 사고에 대비한 바깥쪽 안전망으로 남겨 둡니다)

    토큰이 틀렸거나 특권 인텐트가 꺼져 있는 것처럼 '사람이 고쳐야 하는' 문제는
    몇 번 다시 시도해도 똑같으므로, 무엇을 고쳐야 하는지 알리고 바로 멈춥니다.
    """
    delay = RETRY_BASE_DELAY
    attempt = 0

    while True:
        attempt += 1
        bot = ImisutBot()  # 한 번 닫은 봇은 다시 쓸 수 없어 시도마다 새로 만듭니다
        started_at = time.monotonic()
        try:
            async with bot:  # 나갈 때 bot.close() 가 불려 DB 연결까지 정리됩니다
                await bot.start(TOKEN, reconnect=True)
        except discord.LoginFailure:
            log.error(
                "❌ 디스코드가 토큰을 거부했습니다. DISCORD_TOKEN 값을 확인해 주세요. "
                "(따옴표·공백이 섞였거나 토큰을 재발급했을 수 있습니다) 재시도하지 않고 종료합니다."
            )
            raise
        except discord.PrivilegedIntentsRequired:
            log.error(
                "❌ 특권 인텐트가 꺼져 있습니다. 개발자 포털 → Bot → Privileged Gateway Intents 에서 "
                "MESSAGE CONTENT 와 SERVER MEMBERS 를 켜 주세요. 재시도하지 않고 종료합니다."
            )
            raise
        except RETRYABLE_ERRORS as exc:
            if isinstance(exc, discord.HTTPException) and not _is_retryable_http(exc):
                raise  # 401 · 403 처럼 다시 시도해도 결과가 같은 오류는 그대로 알립니다

            if time.monotonic() - started_at >= RETRY_STABLE_UPTIME:
                # 한참 정상 가동한 뒤 끊긴 경우라면 처음 간격부터 다시 셉니다.
                delay, attempt = RETRY_BASE_DELAY, 1

            wait = _retry_delay(exc, delay)
            log.warning(
                "⚠️ 디스코드 접속이 끊겼습니다(%d번째 시도). %.0f초 뒤 다시 붙어 봅니다. · %s: %s",
                attempt,
                wait,
                type(exc).__name__,
                _one_line(exc),
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, RETRY_MAX_DELAY)
        else:
            log.info("🛑 봇이 정상적으로 종료되었습니다.")
            return


def main() -> None:
    if not TOKEN:
        raise SystemExit("❌ .env 파일에 DISCORD_TOKEN이 설정되어 있지 않습니다.")

    discord.utils.setup_logging(level=logging.INFO)  # discord + 우리 봇 로그를 함께 출력
    try:
        asyncio.run(run_bot_forever())
    except KeyboardInterrupt:
        log.info("👋 종료 신호를 받아 봇을 정리했습니다.")


if __name__ == "__main__":
    main()
