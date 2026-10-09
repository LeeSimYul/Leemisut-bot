"""
utils/hangul.py
조교 이미숫 - 한글 조사 고르기 ('무엇'을 · '어디'를 · '사랑'이 · '배'가)
"""
from __future__ import annotations


def has_final_consonant(word: str) -> bool:
    """마지막 글자에 받침이 있는지. (한글이 아니면 받침 없음으로 봅니다)"""
    last = word.strip()[-1:] or " "
    return "가" <= last <= "힣" and (ord(last) - ord("가")) % 28 != 0


def josa(word: str, with_final: str, without_final: str) -> str:
    """앞말의 받침에 맞는 조사. josa('무엇', '을', '를') → '을'"""
    return with_final if has_final_consonant(word) else without_final
