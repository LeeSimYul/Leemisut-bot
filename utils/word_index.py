"""
utils/word_index.py
조교 이미숫 - 낱말로 사전 단어(word_id)를 찾는 메모리 색인 (/문장수어)

■ 어디에 쓰나요?
    - 시드 문장의 관련 단어 연결 (utils/sentence_seed.py 의 words → sign_sentences.related_word_ids)
    - 유저가 제출한 수어문에서 단어를 뽑아 '🔎 내가 쓴 단어 수형 확인하기' 메뉴로 보여 주기

■ 이렇게 찾습니다 (앞 단계에서 찾으면 뒤 단계는 보지 않습니다)
    1. 대표 단어명(word_name)이 같은 단어
    2. 표제어에 함께 적힌 다른 이름(aliases)이 같은 단어  (예: '감사' → '고맙다,감사' 단어)
    3. '하다' 를 붙이거나 뗀 형태의 대표 단어명 → 다른 이름 (예: '부탁' ↔ '부탁하다')
    대표 단어명이 있으면 다른 이름은 보지 않습니다. ('떨어지다' 에 '실격,떨어지다' 같은 단어가 섞이지 않게)
    같은 이름의 단어(동음이의어)는 모두 돌려줍니다. 고르는 사람이 뜻 설명을 보고 고릅니다.

■ 수어문 나누기
    '[하나] + [더] + [천천히]', '나 학교 가다?', '다시/천천히/부탁' 처럼 적어도
    한글 · 영문 · 숫자 덩어리만 낱말로 꺼냅니다. (괄호 · 기호 · 화살표는 구분자로 봅니다)

    전체 단어(약 3,700건)의 이름만 메모리에 들고 있어 DB 를 오가지 않고 바로 찾습니다.
    단어가 동기화 · 삭제되면 Database.data_version 이 바뀌므로, 쓰는 쪽에서 새로 만들면 됩니다.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

_TOKEN = re.compile(r"[가-힣A-Za-z0-9]+")
MAX_TOKENS = 40  # 한 수어문에서 살펴볼 최대 낱말 수
HADA = "하다"


def tokenize(text: str) -> list[str]:
    """수어문을 낱말로 나눕니다. (순서 유지 · 중복 제거)"""
    return list(dict.fromkeys(_TOKEN.findall(text or "")))[:MAX_TOKENS]


def variants(term: str) -> list[str]:
    """'하다' 를 붙이거나 뗀 형태. ('부탁' → ['부탁하다'], '부탁하다' → ['부탁'])"""
    if term.endswith(HADA):
        return [term[: -len(HADA)]] if len(term) > len(HADA) else []
    if term.endswith("다"):
        return []  # '가다' + 하다 같은 말은 없습니다
    return [term + HADA]


@dataclass(frozen=True)
class WordMatch:
    term: str     # 유저(또는 시드)가 적은 낱말
    word_id: int
    name: str     # 사전의 대표 단어명
    exact: bool   # 대표 단어명이 그대로 같은가 (아니면 다른 이름 · '하다' 형태로 찾음)

    @property
    def label(self) -> str:
        """선택 메뉴에 보여 줄 이름. 다른 이름으로 찾았으면 '감사 → 고맙다' 처럼 보여 줍니다."""
        return self.name if self.exact or self.term == self.name else f"{self.term} → {self.name}"


class WordIndex:
    def __init__(self, rows: Iterable[Any]) -> None:
        """rows: (word_id, word_name, aliases) 행들. aliases 는 줄바꿈으로 구분한 문자열."""
        self._by_name: dict[str, list[int]] = {}
        self._by_alias: dict[str, list[int]] = {}
        self._names: dict[int, str] = {}
        for row in rows:
            word_id, name = int(row["word_id"]), str(row["word_name"])
            self._names[word_id] = name
            self._by_name.setdefault(name, []).append(word_id)
            for alias in str(row["aliases"] or "").splitlines():
                alias = alias.strip()
                if alias and alias != name and word_id not in self._by_alias.get(alias, []):
                    self._by_alias.setdefault(alias, []).append(word_id)

    def __len__(self) -> int:
        return len(self._names)

    @property
    def alias_count(self) -> int:
        return len(self._by_alias)

    def lookup(self, term: str) -> list[WordMatch]:
        """낱말 하나로 단어를 찾습니다. (대표 이름 → 다른 이름 → '하다' 형태 순 · 없으면 빈 목록)"""
        term = term.strip()
        if not term:
            return []
        for candidate in (term, *variants(term)):
            for table in (self._by_name, self._by_alias):
                ids = table.get(candidate, [])
                if ids:
                    return [WordMatch(term, i, self._names[i], exact=self._names[i] == term) for i in ids]
        return []

    def ids(self, term: str) -> list[int]:
        return [match.word_id for match in self.lookup(term)]

    def match_text(self, text: str, limit: int = 25) -> tuple[list[WordMatch], list[str]]:
        """
        수어문에서 사전 단어를 찾습니다. 반환: (찾은 단어 · 최대 limit 개, 찾지 못한 낱말)
        같은 단어가 여러 낱말에서 나오면 한 번만 담습니다.
        """
        found: list[WordMatch] = []
        seen: set[int] = set()
        missing: list[str] = []
        for term in tokenize(text):
            matches = self.lookup(term)
            if not matches:
                missing.append(term)
            for match in matches:
                if match.word_id not in seen and len(found) < limit:
                    seen.add(match.word_id)
                    found.append(match)
        return found, missing

    def resolve(self, names: Sequence[str]) -> dict[str, list[int]]:
        """단어명 목록 → {단어명: word_id 목록} (찾은 것만). SeedSentence.related_word_ids 에 넘깁니다."""
        return {name: ids for name in dict.fromkeys(names) if (ids := self.ids(name))}
