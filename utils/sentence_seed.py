"""
utils/sentence_seed.py
조교 이미숫 - /문장수어 기본 문장 (한국 속담 · 격언/명언 · 일상 회화 · VRChat 실전 수어)

■ 봇이 켜질 때 이 목록을 DB 의 sign_sentences 에 맞춥니다. (cogs/sentences.py)
    - 같은 번호(id)는 내용을 갱신하고, 새 번호는 추가합니다. 등록일은 처음 값이 남습니다.
    - 목록에서 지운 문장은 DB 에서 지우지 않습니다. (지우려면 DB 에서 직접 DELETE)
    - 번호는 한 번 정하면 바꾸지 마세요. 유저 제출 기록 · 이미 올라간 카드의 버튼이 번호로 문장을 찾습니다.
    - 문장을 새로 넣을 때는 맨 끝 번호 + 1 을 쓰세요. DB 에 직접 넣는 문장은 1000번 이상을 권장합니다.

■ 칸 설명
    korean_text      한국어 원문
    ksl_gloss        참고 예시 수어문 (글로스). 정답이 아니라 여러 표현 중 하나입니다.
                     단어 단위로 띄어 쓰고, 절은 ' / ' 로 나눕니다. 질문은 끝에 '?' 를 붙입니다.
    translation_tip  표현 팁 · 비수지 신호 안내. 아래 '비수지 신호 기준'의 문구(상수)로 씁니다.
    difficulty       1 입문 · 2 초급 · 3 중급
    words            관련 단어 (사전 sign_words 의 단어명). 카드의 '📚 관련 단어 수형 보기' 메뉴가 됩니다.
                     'A|B' 는 A 가 사전에 없으면 B 를 찾습니다. 같은 이름의 단어(동음이의어)는 모두 보여 줍니다.
                     사전에 없는 단어명은 조용히 빠지고, 봇이 켜질 때 로그에 모아서 알려 줍니다.

■ 비수지 신호 기준 (국립국어원 『한국수어 문법』에서 널리 설명하는 수준으로만 씁니다)
    - 판정 의문문(예/아니오 질문) : 눈썹을 올리고 턱을 살짝 듭니다.             → YES_NO_QUESTION
    - 의문사 의문문(무엇 · 어디 …)  : 미간을 찌푸립니다. 의문사는 흔히 문장 끝.   → wh_question()
    - 부정 · 거절                   : 고개를 좌우로 흔듭니다.                    → negation()
    - 감정 · 정도                   : 손동작의 크기 · 속도와 눈 · 입 표정을 맞춥니다. → degree()
    그 밖에는 문법 설명에 흔히 나오는 시간어 앞세우기 · 절 사이 멈춤 · 방향 동사 · 공간 활용만 씁니다.
    특정 손 모양이나 입 모양처럼 사람마다 다를 수 있는 설명은 넣지 않습니다.

⚠️ 참고 예시 수어문 · 팁은 학습용 초안입니다. 수어는 지역 · 사람마다 표현이 다양하므로,
   수업에서 쓰시기 전에 강사님이 검토해 고쳐 주세요. (고친 뒤 봇을 다시 켜면 DB 에 반영됩니다)
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# DB 에 저장하는 분류 값 (카드에 보여 줄 이름 · 색은 cogs/sentences.py 에 있습니다)
CATEGORIES: tuple[str, ...] = ("속담", "명언", "일상회화", "VRChat")
MAX_RELATED_WORDS = 25  # 디스코드 선택 메뉴 한 개에 넣을 수 있는 최대 항목 수


@dataclass(frozen=True)
class SeedSentence:
    id: int
    category: str
    korean_text: str
    ksl_gloss: str
    translation_tip: str
    difficulty: int
    source: str = ""
    words: tuple[str, ...] = ()

    def word_names(self) -> list[str]:
        """'A|B' 대체어까지 펼친 단어명 목록. (DB 에서 한 번에 찾기 위해)"""
        return [name.strip() for entry in self.words for name in entry.split("|") if name.strip()]

    def related_word_ids(self, found: Mapping[str, Sequence[int]]) -> list[int]:
        """
        found(단어명 → word_id 목록)로 관련 단어 ID 를 만듭니다.
        단어 순서를 지키고, 'A|B' 는 먼저 찾은 쪽만 씁니다. 중복은 빼고 최대 MAX_RELATED_WORDS 개.
        """
        ids: list[int] = []
        for entry in self.words:
            for name in (n.strip() for n in entry.split("|")):
                if found.get(name):
                    ids.extend(i for i in found[name] if i not in ids)
                    break
        return ids[:MAX_RELATED_WORDS]

    def unresolved_words(self, found: Mapping[str, Sequence[int]]) -> list[str]:
        """사전에서 하나도 찾지 못한 관련 단어 (로그 안내용)"""
        return [
            entry for entry in self.words
            if not any(found.get(name.strip()) for name in entry.split("|"))
        ]


def validate(sentences: Iterable[SeedSentence]) -> None:
    """번호 중복 · 분류 · 난이도 · 빈 칸을 확인합니다. 문제가 있으면 ValueError."""
    seen: set[int] = set()
    for s in sentences:
        where = f"문장 {s.id}번({s.korean_text[:15]})"
        if s.id <= 0 or s.id in seen:
            raise ValueError(f"{where}: 번호가 0 이하이거나 중복입니다.")
        seen.add(s.id)
        if s.category not in CATEGORIES:
            raise ValueError(f"{where}: 분류 {s.category!r} 는 {CATEGORIES} 중 하나여야 합니다.")
        if s.difficulty not in (1, 2, 3):
            raise ValueError(f"{where}: 난이도는 1~3 이어야 합니다.")
        if not (s.korean_text.strip() and s.ksl_gloss.strip()):
            raise ValueError(f"{where}: 원문과 참고 예시 수어문은 비울 수 없습니다.")


# ── 비수지 신호 · 문법 팁 표준 문구 (팁마다 같은 말을 쓰도록 한곳에 모았습니다) ──
YES_NO_QUESTION = "판정 의문문(예/아니오 질문)이라 문장 끝에서 눈썹을 올리고 턱을 살짝 들어 주세요."
TIME_FIRST = "시간을 나타내는 말은 문장 앞에 두는 경우가 많아요."
PAUSE = "절과 절 사이에 잠깐 멈춤을 두면 흐름이 또렷해져요."


def josa(word: str, with_final: str, without_final: str) -> str:
    """앞말의 받침에 맞는 조사. (무엇 → 을, 어디 → 를)"""
    last = word[-1:] or " "
    has_final = "가" <= last <= "힣" and (ord(last) - ord("가")) % 28 != 0
    return with_final if has_final else without_final


def wh_question(wh: str) -> str:
    return f"의문사 의문문이라 '{wh}'{josa(wh, '을', '를')} 문장 끝에 두고, 미간을 찌푸린 표정과 함께 표현해 주세요."


def negation(word: str) -> str:
    return f"'{word}'에서 고개를 좌우로 흔들어 부정을 나타내요."


def degree(*words: str) -> str:
    quoted = " · ".join(f"'{w}'" for w in words)
    return f"{quoted}처럼 정도 · 감정을 나타내는 말은 손동작의 크기 · 속도와 눈 · 입 표정을 함께 맞춰 주세요."


def directional(word: str, direction: str) -> str:
    return (
        f"'{word}'{josa(word, '은', '는')} {direction} 방향으로 움직여 "
        "누가 누구에게 하는지 나타낼 수 있어요(방향 동사)."
    )


def tip(*parts: str) -> str:
    return " ".join(parts)


SEED_SENTENCES: tuple[SeedSentence, ...] = (
    # ── 🧓 한국 속담 ────────────────────────────────────────────
    SeedSentence(
        1, "속담", "가는 말이 고와야 오는 말이 곱다",
        "나 말 예쁘다 가다 / 너 말 예쁘다 오다",
        tip(directional("가다 · 오다", "나 ↔ 상대"), PAUSE),
        2, "우리 속담", ("말", "예쁘다|곱다", "가다", "오다"),
    ),
    SeedSentence(
        2, "속담", "천 리 길도 한 걸음부터",
        "길 아주 멀다 / 그래도 시작 걸음 하나",
        tip(degree("아주 멀다"), PAUSE),
        2, "우리 속담", ("길", "멀다", "시작", "걷다", "하나"),
    ),
    SeedSentence(
        3, "속담", "백지장도 맞들면 낫다",
        "종이 가볍다 / 그래도 같이 들다 더 좋다",
        tip(degree("가볍다", "더 좋다"), PAUSE),
        2, "우리 속담", ("종이", "가볍다", "같이|함께", "들다", "좋다"),
    ),
    SeedSentence(
        4, "속담", "낮말은 새가 듣고 밤말은 쥐가 듣는다",
        "낮 말 새 듣다 / 밤 말 쥐 듣다 / 말 조심",
        tip("낮 절과 밤 절을 서로 다른 공간(왼쪽 · 오른쪽)에 두면 대비가 분명해져요(공간 활용).", PAUSE),
        3, "우리 속담", ("낮", "밤", "새", "쥐", "듣다", "조심"),
    ),
    SeedSentence(
        5, "속담", "세 살 버릇 여든까지 간다",
        "세 살 습관 / 여든 살 까지 계속",
        tip("숫자(3 · 80)를 또렷하게 보여 주세요.", "'계속'은 동작을 길게 이어 지속되는 뜻을 살려요."),
        2, "우리 속담", ("습관|버릇", "나이", "계속"),
    ),
    SeedSentence(
        6, "속담", "웃는 얼굴에 침 못 뱉는다",
        "웃다 얼굴 / 화내다 못하다",
        tip(negation("못하다"), degree("웃다", "화내다")),
        1, "우리 속담", ("웃다", "얼굴", "화나다|화", "못하다"),
    ),
    SeedSentence(
        7, "속담", "원숭이도 나무에서 떨어진다",
        "원숭이 나무 오르다 잘하다 / 그래도 떨어지다 있다",
        tip("나무의 위치를 공간에 먼저 정해 두면, 그 자리에서 '오르다'와 '떨어지다'를 이어서 표현할 수 있어요(공간 활용).", PAUSE),
        3, "우리 속담", ("원숭이", "나무", "잘하다", "떨어지다"),
    ),
    SeedSentence(
        8, "속담", "티끌 모아 태산",
        "작다 모으다 모으다 / 산 크다 되다",
        tip("'모으다'를 되풀이하면 양이 쌓이는 과정이 드러나요.", degree("작다", "크다")),
        2, "우리 속담", ("작다", "모으다", "산", "크다", "되다"),
    ),
    SeedSentence(
        9, "속담", "소 잃고 외양간 고친다",
        "소 잃어버리다 끝 / 그 다음 집 고치다 / 늦다",
        tip("'끝'으로 이미 일어난 일임을 나타내요.", PAUSE, degree("늦다")),
        3, "우리 속담", ("소", "잃어버리다", "끝", "집", "고치다", "늦다"),
    ),
    SeedSentence(
        10, "속담", "시작이 반이다",
        "시작 / 벌써 반",
        tip("'시작' 뒤에 잠깐 멈춤을 두어 주제와 설명을 나눠 주세요."),
        1, "우리 속담", ("시작", "벌써|이미", "반"),
    ),
    SeedSentence(
        11, "속담", "말 한마디로 천 냥 빚을 갚는다",
        "말 하나 좋다 / 빚 많다 갚다 가능",
        tip(degree("많다"), PAUSE),
        3, "우리 속담", ("말", "하나", "좋다", "빚", "많다", "갚다"),
    ),
    SeedSentence(
        12, "속담", "고생 끝에 낙이 온다",
        "고생 끝 / 행복 오다",
        tip(degree("고생", "행복"), "감정이 바뀌는 문장이라 단어마다 표정을 그 뜻에 맞춰 바꿔 주세요."),
        1, "우리 속담", ("고생|힘들다", "끝", "행복", "오다"),
    ),
    SeedSentence(
        13, "속담", "금강산도 식후경",
        "금강산 구경 좋다 / 그래도 밥 먼저",
        tip("지명 '금강산'은 지문자로 표현할 수 있어요.", PAUSE),
        2, "우리 속담", ("산", "구경", "좋다", "밥", "먼저"),
    ),

    # ── 💡 격언 · 명언 ──────────────────────────────────────────
    SeedSentence(
        14, "명언", "아는 것이 힘이다",
        "알다 / 힘",
        tip("'알다' 뒤에 잠깐 멈춤을 두어 '아는 것은'이라는 주제와 설명 '힘'을 나눠 주세요."),
        1, "프랜시스 베이컨", ("알다", "힘"),
    ),
    SeedSentence(
        15, "명언", "혼자 할 수 있는 일은 적지만, 함께하면 많은 일을 할 수 있다",
        "혼자 일 하다 가능 적다 / 함께 하다 일 많다 가능",
        tip(degree("적다", "많다"), PAUSE),
        3, "헬렌 켈러", ("혼자", "함께|같이", "일", "적다", "많다"),
    ),
    SeedSentence(
        16, "명언", "중요한 것은 눈에 보이지 않아",
        "중요하다 / 눈 보이다 아니다",
        tip(negation("아니다"), "'중요하다' 뒤에는 잠깐 멈춤을 둬요."),
        2, "생텍쥐페리 『어린 왕자』", ("중요하다", "눈", "보이다|보다", "아니다"),
    ),
    SeedSentence(
        17, "명언", "천천히, 그러나 꾸준히",
        "천천히 / 그러나 계속",
        tip(degree("천천히"), "'계속'은 동작을 길게 이어 지속되는 뜻을 살려요."),
        1, "이솝 우화 「토끼와 거북이」", ("천천히", "그러나|하지만", "계속"),
    ),
    SeedSentence(
        18, "명언", "이심전심, 말하지 않아도 마음이 통해요",
        "말하다 없다 / 마음 마음 통하다",
        tip(negation("없다"), PAUSE),
        2, "사자성어 以心傳心", ("말하다", "없다", "마음", "통하다"),
    ),
    SeedSentence(
        19, "명언", "역지사지, 상대의 입장에서 생각해 봐요",
        "나 너 자리 바꾸다 / 생각하다",
        tip("'나'와 '너'의 위치를 공간에 정해 두고 그 사이에서 '바꾸다'를 표현하면 뜻이 분명해져요(공간 활용).", PAUSE),
        3, "사자성어 易地思之", ("자리", "바꾸다", "생각|생각하다"),
    ),
    SeedSentence(
        20, "명언", "백 번 듣는 것보다 한 번 보는 것이 낫다",
        "백 번 듣다 / 한 번 보다 더 좋다",
        tip("숫자(백 · 한)를 또렷하게 보여 주세요.", degree("더 좋다")),
        2, "사자성어 百聞不如一見", ("백", "듣다", "보다", "좋다"),
    ),
    SeedSentence(
        21, "명언", "실패는 성공의 어머니",
        "실패 / 성공 바탕 되다",
        tip("'어머니'를 그대로 옮기기보다 '바탕'처럼 뜻을 풀어 옮기는 방법도 있어요.", PAUSE),
        2, "서양 격언", ("실패", "성공", "어머니"),
    ),
    SeedSentence(
        22, "명언", "오늘 할 일을 내일로 미루지 마라",
        "오늘 일 / 내일 미루다 안 되다",
        tip(TIME_FIRST, negation("안 되다")),
        2, "벤저민 프랭클린", ("오늘", "일", "내일", "미루다"),
    ),
    SeedSentence(
        23, "명언", "배움은 마르지 않는 샘물과 같아요",
        "배우다 / 샘물 같다 / 마르다 없다",
        tip(negation("없다"), PAUSE),
        2, "이미숫 조교의 한마디", ("배우다", "물", "같다", "없다"),
    ),
    SeedSentence(
        24, "명언", "수어는 손으로만 하는 말이 아니라, 몸 전체로 짓는 문장이에요",
        "수어 손 만 말 아니다 / 몸 전체 문장 만들다",
        tip(negation("아니다"), PAUSE),
        3, "이미숫 조교의 한마디", ("수어", "손", "몸", "전체", "말", "만들다"),
    ),
    SeedSentence(
        25, "명언", "표정도 문법이에요",
        "표정 / 문법 같다",
        tip(
            "같은 단어라도 눈썹을 올리고 턱을 살짝 들면 판정 의문, 의문사와 함께 미간을 찌푸리면 의문사 의문,",
            "고개를 좌우로 흔들면 부정이 돼요. 직접 비교해 보세요.",
        ),
        1, "이미숫 조교의 한마디", ("표정", "문법"),
    ),

    # ── ☕ 일상 회화 ─────────────────────────────────────────────
    SeedSentence(
        26, "일상회화", "만나서 반가워요",
        "만나다 반갑다",
        tip(degree("반갑다")),
        1, "일상 회화", ("만나다", "반갑다"),
    ),
    SeedSentence(
        27, "일상회화", "이름이 뭐예요?",
        "너 이름 무엇?",
        tip(wh_question("무엇")),
        1, "일상 회화", ("이름", "무엇"),
    ),
    SeedSentence(
        28, "일상회화", "오늘 날씨가 정말 좋네요",
        "오늘 날씨 좋다",
        tip(TIME_FIRST, degree("정말 좋다")),
        1, "일상 회화", ("오늘", "날씨", "좋다"),
    ),
    SeedSentence(
        29, "일상회화", "밥 먹었어요?",
        "밥 먹다 끝?",
        tip("'끝'으로 이미 한 일(완료)을 나타내요.", YES_NO_QUESTION),
        1, "일상 회화", ("밥", "먹다", "끝"),
    ),
    SeedSentence(
        30, "일상회화", "미안해요, 조금 늦었어요",
        "미안하다 / 나 조금 늦다",
        tip(degree("미안하다", "조금")),
        1, "일상 회화", ("미안하다", "조금", "늦다"),
    ),
    SeedSentence(
        31, "일상회화", "화장실이 어디예요?",
        "화장실 어디?",
        tip(wh_question("어디")),
        1, "일상 회화", ("화장실", "어디"),
    ),
    SeedSentence(
        32, "일상회화", "다시 한 번 천천히 해 주세요",
        "다시 천천히 부탁",
        tip(degree("천천히"), "'부탁'에서는 공손한 표정을 함께해요."),
        1, "일상 회화", ("다시", "천천히", "부탁"),
    ),
    SeedSentence(
        33, "일상회화", "주말에 뭐 해요?",
        "주말 너 하다 무엇?",
        tip(TIME_FIRST, wh_question("무엇")),
        2, "일상 회화", ("주말", "하다", "무엇"),
    ),
    SeedSentence(
        34, "일상회화", "생일 축하해요!",
        "생일 축하",
        tip(degree("축하")),
        1, "일상 회화", ("생일", "축하"),
    ),
    SeedSentence(
        35, "일상회화", "저는 요즘 수어를 배우고 있어요",
        "요즘 나 수어 배우다 계속",
        tip(TIME_FIRST, "계속되는 일은 '배우다'를 되풀이하거나 '계속'을 붙여 나타낼 수 있어요."),
        2, "일상 회화", ("요즘", "수어", "배우다", "계속"),
    ),
    SeedSentence(
        36, "일상회화", "도와줘서 정말 고마워요",
        "너 나 돕다 / 고맙다",
        tip(directional("돕다", "상대 → 나"), degree("고맙다")),
        2, "일상 회화", ("돕다", "고맙다|감사"),
    ),

    # ── 🥽 VRChat 실전 수어 ─────────────────────────────────────
    SeedSentence(
        37, "VRChat", "저는 마이크를 안 써요. 수어로 이야기해요",
        "나 마이크 사용 아니다 / 수어 대화",
        tip(negation("아니다"), PAUSE),
        1, "VRChat 실전", ("마이크", "사용", "아니다", "수어", "대화"),
    ),
    SeedSentence(
        38, "VRChat", "우리 같이 놀래요?",
        "우리 같이 놀다?",
        tip(YES_NO_QUESTION),
        1, "VRChat 실전", ("같이|함께", "놀다"),
    ),
    SeedSentence(
        39, "VRChat", "친구 추가해도 괜찮아요?",
        "친구 추가 괜찮다?",
        tip(YES_NO_QUESTION, "'추가'는 지문자나 '더하다'로 풀어 표현할 수 있어요."),
        2, "VRChat 실전", ("친구", "더하다|추가", "괜찮다"),
    ),
    SeedSentence(
        40, "VRChat", "아바타가 정말 예뻐요!",
        "너 아바타 예쁘다",
        tip("'아바타'는 지문자로 표현할 수 있어요.", degree("정말 예쁘다")),
        1, "VRChat 실전", ("예쁘다",),
    ),
    SeedSentence(
        41, "VRChat", "잠깐 자리 비울게요. 기다려 주세요",
        "나 잠깐 가다 / 기다리다 부탁",
        tip(degree("잠깐"), "'부탁'에서는 공손한 표정을 함께해요."),
        1, "VRChat 실전", ("잠깐", "가다", "기다리다", "부탁"),
    ),
    SeedSentence(
        42, "VRChat", "다른 월드로 이동해요. 저를 따라오세요",
        "우리 다른 곳 가다 / 나 따라오다",
        tip(directional("따라오다", "상대 → 나"), PAUSE),
        2, "VRChat 실전", ("다르다", "곳|장소", "가다", "따라가다|따르다"),
    ),
    SeedSentence(
        43, "VRChat", "손 인식이 자꾸 이상해요",
        "내 손 움직임 계속 이상하다",
        tip("자꾸 일어나는 일은 '계속'을 붙이거나 동작을 되풀이해 나타낼 수 있어요.", degree("이상하다")),
        2, "VRChat 실전", ("손", "계속", "이상하다"),
    ),
    SeedSentence(
        44, "VRChat", "여기 처음 왔어요. 잘 부탁해요",
        "나 여기 처음 오다 / 잘 부탁",
        tip("'여기'는 지금 있는 자리를 가리켜 표현해요.", "'부탁'에서는 공손한 표정을 함께해요."),
        1, "VRChat 실전", ("여기", "처음", "오다", "부탁"),
    ),
    SeedSentence(
        45, "VRChat", "내일 수어 교실에서 또 만나요",
        "내일 수어 교실 다시 만나다",
        tip("시간(내일) → 장소(수어 교실) → 행동(만나다) 순서로 놓아 보세요. 흔히 쓰는 수어 문장의 뼈대예요."),
        1, "VRChat 실전", ("내일", "수어", "교실", "다시", "만나다"),
    ),
    SeedSentence(
        46, "VRChat", "오늘 즐거웠어요, 잘 자요!",
        "오늘 즐겁다 / 잘 자다",
        tip(TIME_FIRST, degree("즐겁다")),
        1, "VRChat 실전", ("오늘", "즐겁다", "자다"),
    ),
)

validate(SEED_SENTENCES)  # 잘못 고치면 봇이 켜질 때 바로 알 수 있도록
