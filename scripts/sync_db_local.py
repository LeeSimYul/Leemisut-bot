"""
scripts/sync_db_local.py
조교 이미숫 - 수어사전 전체 동기화를 PC 에서 직접 실행합니다. (디스코드 /수어전체동기화 와 같은 동작)

■ 왜 필요한가요?
    운영 VM(Oracle 오사카)에서는 api.kcisa.kr 주소를 찾지 못해(DNS) /수어전체동기화 가 실패합니다.
    국내 PC 에서는 수어 API 에 접속되므로, PC 에서 API 를 받아 .env 의 운영 DB(DATABASE_URL)에 바로 저장합니다.

■ 봇 명령어와 같은 코드를 씁니다
    - 수집 · 검증 : utils.ksl_api.fetch_all_sign_words  (/수어전체동기화 와 같은 필터)
    - 저장       : Database.sync_api_words           (/수어전체동기화 와 같은 저장 방식)
    같은 단어 · 같은 영상이면 같은 행을 갱신합니다. (설명 · 대표 사진 · 분류 · 사전 주소 · 수형 이미지 목록)
    새 단어는 추가하고, 유저 포인트 · 출석 · 퀴즈 기록 · 단어장 · 오답노트는 건드리지 않습니다.
    수형 이미지 목록(image_urls)은 봇이 읽는 형식 그대로 '줄바꿈으로 구분한 주소'로 저장합니다.

■ 실행 (저장소 폴더 · .env 에 DATABASE_URL 과 KSL_API_KEY 필요)
    python scripts/sync_db_local.py --dry-run     # 받아서 운영 DB 와 비교만 (저장하지 않음)
    python scripts/sync_db_local.py               # 받아서 비교 → 확인 질문 → 저장
    python scripts/sync_db_local.py --yes         # 확인 질문 없이 저장
    ⚠️ PC 에서 main.py(봇)를 켜지는 마세요. 이 스크립트는 디스코드에 접속하지 않습니다.
    저장한 뒤에는 VM 의 봇을 한 번 다시 시작해 주세요. (단어 수 · 분류 목록 캐시를 새로 읽도록)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))  # 저장소 루트의 database · utils 를 불러오기 위해

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

from database import Database  # noqa: E402
from utils import media  # noqa: E402
from utils.ksl_api import (  # noqa: E402
    DEFAULT_MAX_PAGES,
    DEFAULT_NUM_OF_ROWS,
    KSLApiError,
    RejectedWord,
    fetch_all_sign_words,
)

DEFAULT_SQLITE_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "imisut.db"))
BAR_WIDTH = 30
# 기존 단어와 짝이 맞지 않는 '새 단어'가 이보다 많으면 영상 주소가 바뀌었을 수 있어 크게 경고합니다.
# (같은 단어 · 같은 영상으로 판단하므로, 영상 주소가 바뀌면 갱신 대신 새 행이 생겨 중복이 됩니다)
NEW_WORDS_WARN_RATIO = 0.10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="수어사전 전체 동기화를 PC 에서 실행합니다. (/수어전체동기화 와 같은 동작)")
    parser.add_argument("--dry-run", action="store_true", help="받아서 비교만 하고 저장하지 않습니다")
    parser.add_argument("--yes", action="store_true", help="저장 전 확인 질문을 건너뜁니다")
    parser.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"가져올 최대 페이지 수 (1페이지 = {DEFAULT_NUM_OF_ROWS}건, 기본 {DEFAULT_MAX_PAGES})")
    parser.add_argument("--dsn", help="PostgreSQL 주소 (기본: .env 의 DATABASE_URL, 없으면 로컬 SQLite)")
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE_PATH, help="DATABASE_URL 이 없을 때 쓸 SQLite 파일")
    args = parser.parse_args()
    if not 1 <= args.pages <= 100:
        parser.error("--pages 는 1~100 사이로 지정해 주세요.")
    return args


def draw_progress(page_no: int, collected: int, total: int, started: float) -> None:
    done = min(page_no * DEFAULT_NUM_OF_ROWS, total) if total else page_no * DEFAULT_NUM_OF_ROWS
    ratio = done / total if total else 0.0
    filled = int(min(ratio, 1.0) * BAR_WIDTH)
    line = (
        f"[{'█' * filled}{'░' * (BAR_WIDTH - filled)}] {ratio * 100:5.1f}% "
        f"📄 {page_no}페이지 · 통과 {collected}건 / 전체 {total or '?'}건 · {time.monotonic() - started:.0f}초"
    )
    if sys.stdout.isatty():
        sys.stdout.write("\r" + line + "   ")
        sys.stdout.flush()
    else:
        print(line, flush=True)


def summarize_changes(rows: list[tuple[str, ...]], existing: dict[tuple[str, str], tuple[str, str]]) -> dict[str, int]:
    """받은 단어를 운영 DB 와 비교합니다. (저장하기 전 미리보기)"""
    stats = {"matched": 0, "new": 0, "image_changed": 0, "to_illustration": 0, "with_list": 0, "multi": 0}
    for row in rows:
        name, video, image_url, image_urls = row[0], row[2], row[3], row[6]
        if image_urls:
            stats["with_list"] += 1
            if len(image_urls.splitlines()) >= 2:
                stats["multi"] += 1
        old = existing.get((name, video))
        if old is None:
            stats["new"] += 1
            continue
        stats["matched"] += 1
        old_image, _old_list = old
        if old_image != image_url:
            stats["image_changed"] += 1
            if media.is_video_frame(old_image) and image_url and not media.is_video_frame(image_url):
                stats["to_illustration"] += 1
    return stats


async def main() -> int:
    args = parse_args()
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.WARNING, format="   ⚠️ %(message)s")  # 페이지별 INFO 로그는 진행 막대로 대신

    db = Database(args.sqlite, dsn=args.dsn, seed_mock=False)  # 더미 단어를 넣지 않습니다
    await db.connect()
    try:
        print(f"🗄️ 저장 대상: {db.describe()}")
        old_rows = await db.backend.fetch_all("SELECT word_name, video_url, image_url, image_urls FROM sign_words")
        existing = {(r["word_name"], r["video_url"]): (r["image_url"], r["image_urls"]) for r in old_rows}
        before_lists = sum(1 for image, lst in existing.values() if lst)
        print(f"   지금 단어 {len(existing)}개 · 수형 이미지 목록이 있는 단어 {before_lists}개")

        print(f"⬇️ 수어 API 에서 최대 {args.pages}페이지를 받습니다…")
        started = time.monotonic()
        rejected: list[RejectedWord] = []

        async def on_progress(page_no: int, collected: int, total: int) -> None:
            draw_progress(page_no, collected, total, started)

        try:
            rows = await fetch_all_sign_words(max_pages=args.pages, rejected=rejected, progress=on_progress)
        except KSLApiError as exc:
            if sys.stdout.isatty():
                print()
            print(f"❌ 수어 API 에서 받지 못했습니다: {exc}")
            return 2
        if sys.stdout.isatty():
            print()
        print(f"📗 받은 단어 {len(rows)}개 (필터로 제외 {len(rejected)}개 · {time.monotonic() - started:.0f}초)")
        if not rows:
            print("❌ 받은 단어가 없어 저장하지 않습니다.")
            return 2

        s = summarize_changes(rows, existing)
        print("🔎 운영 DB 와 비교")
        print(f"   기존 단어와 같은 행(갱신) {s['matched']}개 · 새 단어(추가) {s['new']}개")
        print(f"   수형 이미지 목록 {s['with_list']}개 (여러 장 {s['multi']}개)")
        print(f"   대표 사진이 바뀌는 단어 {s['image_changed']}개 (영상 캡처 → 삽화 {s['to_illustration']}개)")
        if existing and s["new"] > max(50, len(existing) * NEW_WORDS_WARN_RATIO):
            print(
                f"   🚨 새 단어가 {s['new']}개로 너무 많습니다. 영상 주소가 바뀌었다면 갱신 대신 새 행이 생겨\n"
                "      같은 단어가 두 번 들어갈 수 있습니다. --dry-run 결과를 확인한 뒤 진행해 주세요."
            )

        if args.dry_run:
            print("🧪 --dry-run: 저장하지 않고 끝냅니다.")
            return 0
        if not args.yes:
            try:
                answer = input("💾 위 내용으로 운영 DB 에 저장할까요? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes", "ㅇ"):
                print("⏹️ 저장하지 않고 끝냅니다.")
                return 1

        added = await db.sync_api_words(rows)  # 한 번의 트랜잭션으로 저장 (중간에 실패하면 모두 취소)
        total = await db.count_words()
        with_lists = await db.count_words_with_image_urls()
        print(f"✅ 저장 완료: 새로 추가 {added}개 · 갱신 {len(rows) - added}개 → 전체 단어 {total}개")
        print(f"🖼️ 수형 이미지 목록이 있는 단어: {before_lists}개 → {with_lists}개")
        print("👉 다음: VM 에서 봇을 다시 시작하고, 새 삽화를 미리 받아 두세요.")
        print("   sudo systemctl restart leemisut && venv/bin/python scripts/bulk_download_images.py --no-archive")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n⏹️ 중단했습니다. (저장 전이었다면 DB 는 그대로입니다)")
        sys.exit(130)
