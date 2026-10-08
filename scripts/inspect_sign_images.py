"""
scripts/inspect_sign_images.py
조교 이미숫 - 수어 API 의 수형 이미지(signImages)에 실제로 무엇이 들어 있는지 확인합니다.

■ 왜 필요한가요?
    봇은 signImages 에서 '영상 캡처가 아닌 이미지(삽화)'가 있으면 그것만, 없으면 전부를
    순서대로 이어 붙여 보여 줍니다. (utils.media.pick_images) 이 규칙이 실제 데이터에 맞는지,
    단어마다 몇 장이 오는지 /수어전체동기화 전에 눈으로 확인하는 용도입니다.

■ 실행 (저장소 폴더 · .env 의 KSL_API_KEY 필요 · DB 는 건드리지 않습니다)
    python scripts/inspect_sign_images.py                # 첫 페이지 10단어
    python scripts/inspect_sign_images.py --rows 50      # 첫 페이지 50단어 (최대 100)
    python scripts/inspect_sign_images.py --page 7       # 7페이지
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

from utils import media  # noqa: E402
from utils.ksl_api import KSLApiClient, KSLApiError, SignWord, evaluate_word  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="수어 API 의 signImages 내용을 확인합니다. (DB 는 건드리지 않음)")
    parser.add_argument("--page", type=int, default=1, help="볼 페이지 (기본 1)")
    parser.add_argument("--rows", type=int, default=10, help="한 페이지에서 볼 단어 수 (1~100, 기본 10)")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    try:
        client = KSLApiClient()
    except KSLApiError as exc:
        print(f"❌ {exc}")
        return 1
    try:
        page = await client.fetch_page(args.page, num_of_rows=max(1, min(args.rows, 100)))
    except KSLApiError as exc:
        print(f"❌ 수어 API 를 부르지 못했습니다: {exc}")
        return 2
    finally:
        await client.close()

    print(f"📄 {args.page}페이지 · {len(page.items)}건 (전체 {page.total_count}건)\n")
    counts: Counter[int] = Counter()
    with_illustration = 0
    for item in page.items:
        word = evaluate_word(item)
        title = item.get("title", "?")
        raw = item.get("signImages", "")
        if not isinstance(word, SignWord):
            print(f"• {title} - 제외됨 ({word.reason})")
            continue
        counts[len(word.image_urls)] += 1
        frames = [url for url in word.image_urls if media.is_video_frame(url)]
        others = [url for url in word.image_urls if not media.is_video_frame(url)]
        if others:
            with_illustration += 1
        chosen = media.pick_images("\n".join(word.image_urls), word.image_url, word.video_url)
        print(f"• {word.word_name} [{word.category}] - signImages {len(word.image_urls)}장 "
              f"(영상 캡처 모양 {len(frames)} · 그 밖의 이미지 {len(others)}) → 봇이 보여 줄 {len(chosen)}장")
        for url in word.image_urls:
            mark = "🎞️ 영상 캡처" if media.is_video_frame(url) else "🖼️ 그 밖"
            pick = " ✅" if url in chosen else ""
            print(f"    {mark}{pick}  {url}")
        if not word.image_urls and raw:
            print(f"    (signImages 원문: {raw[:200]!r})")

    print("\n📊 단어별 signImages 장수:", ", ".join(f"{n}장 {c}개" for n, c in sorted(counts.items())) or "없음")
    print(f"   영상 캡처가 아닌 이미지(삽화 후보)가 있는 단어: {with_illustration}개")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
