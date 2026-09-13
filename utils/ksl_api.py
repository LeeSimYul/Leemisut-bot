"""
utils/ksl_api.py
조교 이미숫 - 문화공공데이터광장(KCISA) 국립국어원 일상생활수어 API 연동 모듈

■ 사용하는 API
    GET https://api.kcisa.kr/openapi/service/rest/meta13/getCTE01701
        ?serviceKey={인증키}&numOfRows=100&pageNo=1&keyword=

    ※ keyword 는 값이 비어 있어도 반드시 붙여서 호출해야 합니다. (제공처 주의사항)
    ※ 전체 데이터는 약 3,754건이며, 페이지당 100건씩 약 38페이지입니다.

■ 외부 공개 함수 (cogs/admin.py 에서 사용)
    fetch_sign_words(keyword)       → 단어 하나 검색
    fetch_many_sign_words(keywords) → 여러 단어 검색
    fetch_all_sign_words()          → 전체 데이터를 페이지 순회로 수집
    KSLApiError                     → 이 모듈이 던지는 모든 오류의 상위 클래스

    세 함수 모두 [(word_name, meaning, video_url, image_url, category, detail_url), ...]
    형태를 돌려줍니다. database.sync_api_words() 에 그대로 넣으시면 됩니다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence
from urllib.parse import unquote, urlsplit

import aiohttp

log = logging.getLogger(__name__)

__all__ = [
    # 오류
    "KSLApiError",
    "KSLApiKeyMissing",
    "KSLApiAuthError",
    "KSLApiRequestError",
    "KSLApiParseError",
    # 자료형
    "SignWord",
    "RejectedWord",
    # 최상위 함수 (권장 진입점)
    "fetch_sign_words",
    "fetch_many_sign_words",
    "fetch_all_sign_words",
    # 클라이언트 및 유틸
    "KSLApiClient",
    "normalize_word_name",
    "normalize_meaning",
    "evaluate_word",
    "filter_words",
    "is_media_url",
    "pick_image_url",
    "BASE_URL",
]


# ── 오류 정의 ───────────────────────────────────────────────────
class KSLApiError(Exception):
    """수어 API 관련 오류의 상위 클래스."""


class KSLApiKeyMissing(KSLApiError):
    """인증키가 설정되지 않았을 때."""


class KSLApiAuthError(KSLApiError):
    """인증키가 거부되었을 때 (401 / 403 / 인증 관련 resultCode)."""


class KSLApiRequestError(KSLApiError):
    """네트워크 오류, DNS 실패, HTTP 404/500 등."""


class KSLApiParseError(KSLApiError):
    """응답을 해석하지 못했을 때."""


# ── API 설정 ────────────────────────────────────────────────────
DEFAULT_ENDPOINT = "https://api.kcisa.kr/openapi/service/rest/meta13/getCTE01701"
BASE_URL = os.getenv("KSL_API_ENDPOINT") or DEFAULT_ENDPOINT

DEFAULT_NUM_OF_ROWS = 100  # 이 API의 페이지당 최대 건수
DEFAULT_MAX_PAGES = 40     # 100 × 40 = 4,000 ≥ 전체 3,754건
REQUEST_TIMEOUT = 20       # 초
PAGE_DELAY = 0.2           # 페이지 사이 간격 (서버 부담 완화)

# 단어 검색에 쓸 파라미터 후보. 실제로 결과를 주는 이름을 찾아 기억해 둡니다.
SEARCH_PARAM_CANDIDATES = ("keyword", "title", "searchWrd", "q")
_working_search_param: str | None = None  # 한 번 찾으면 이후 호출에서 재사용

# 정상 처리로 보는 resultCode 값들
SUCCESS_CODES = {"0000", "000", "00", "0", "OK", "SUCCESS"}

# 인증 실패로 보는 resultCode 키워드
AUTH_FAIL_HINTS = ("SERVICE_KEY", "SERVICEKEY", "인증", "KEY_IS_NOT", "UNREGISTERED", "EXPIRED")

# ClientConnectorDNSError 는 aiohttp 3.11+ 에만 있어서, 없으면 상위 클래스로 대체합니다.
_DNS_ERROR = getattr(aiohttp, "ClientConnectorDNSError", aiohttp.ClientConnectorError)

DEFAULT_MEANING = "국립국어원 표준 수어 표현입니다."
DEFAULT_CATEGORY = "일반"

# ── 필터 설정 ───────────────────────────────────────────────────
MAX_WORD_LENGTH = 12   # 표제어 최대 글자 수 (공백 포함)
MAX_WORD_TOKENS = 2    # 띄어쓰기로 나눈 최대 어절 수 ('헌법 재판소' ✅ / '사랑을 나누는 남녀' ❌)
MAX_LENGTH_RATIO = 2.0  # 검색어 대비 허용 배율 (사랑 → 사랑하다 ✅)

# 검색어 뒤에 붙어도 같은 말로 인정할 어미
ALLOWED_SUFFIXES = ("하다", "되다", "시키다", "스럽다", "롭다", "적", "히", "이", "기")

# 표제어에 이런 말이 섞여 있으면 전문 용어·유물명으로 보고 제외
BLOCKED_NAME_KEYWORDS = (
    "박물관", "미술관", "유물", "고분", "토기", "불상", "석탑",
    "국보", "소장품", "작품명", "조각상",
)

# 분류가 이 중 하나면 제외 (일상생활수어 API에는 거의 없지만 안전장치로 둡니다)
BLOCKED_CATEGORIES = ("박물관", "유물", "고고")

# 표제어에 허용할 문자: 한글 · 영문 · 숫자 · 공백 (그 외 기호는 제외 사유)
INVALID_CHARS = re.compile(r"[^가-힣ㄱ-ㅎㅏ-ㅣa-zA-Z0-9 ]")

# 정제 단계에서 잘라내는 패턴들
_PAREN = re.compile(r"[(（\[{][^)）\]}]*[)）\]}]")    # (신체의)등 → 등
_HTML_TAG = re.compile(r"<[^>]+>")                      # <b>사랑</b> → 사랑
_TRAILING_NUM = re.compile(r"(?<=\D)0[0-9]$")            # 사랑01 → 사랑 (코로나19·G20은 보존)
_SPLIT_CHARS = re.compile(r"[,，/·|;:∼~…]")             # "복장,입다" → "복장"

# ── KCISA 응답 필드 매핑 (활용 명세 기준) ───────────────────────
# 앞에 있는 태그부터 순서대로 찾습니다.
# 이 API는 필드 용도가 이름과 다릅니다. 실제 응답을 확인한 결과:
#   title           → 단어명            (예: 허사)
#   description     → 동영상 mp4 주소   ← '뜻풀이'가 아닙니다!
#   url             → 사전 상세 페이지  (signContentsView.do)
#   signDescription → 수형 설명 (사람이 읽는 글)
#   signImages      → 수형 사진 주소들 (쉼표 구분)
#   categoryType    → 분류항목
# 그래서 태그 이름만 믿지 않고, '값의 생김새'로 용도를 가려냅니다.
_KEY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "word_name": ("title", "subTitle", "alternativeTitle"),
    "category": ("categoryType", "subjectCategory", "subjectKeyword"),
}

# 값을 찾아볼 태그들 (용도별 우선순위)
_MEANING_FIELDS = (
    "signDescription",   # 수형 설명 (사람이 읽는 글) - 가장 유용
    "description",       # 이 API에서는 mp4 주소가 들어옵니다 (주소면 건너뜀)
    "context", "mean", "subDescription", "abstract", "definition",
)
_URL_FIELDS = ("description", "url", "referenceIdentifier", "signImages", "context", "subDescription")

_VIDEO_EXT = (".mp4", ".webm", ".mov", ".avi", ".mpg", ".mpeg", ".wmv")
_IMAGE_EXT = (".gif", ".png", ".jpg", ".jpeg", ".webp")


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _has_ext(value: str, extensions: tuple[str, ...]) -> bool:
    path = urlsplit(value).path.lower()
    return path.endswith(extensions)


def is_media_url(value: str) -> bool:
    """동영상·이미지 '파일' 주소인지. (사전 상세 페이지는 False)"""
    return bool(value) and _is_url(value) and _has_ext(value, _VIDEO_EXT + _IMAGE_EXT)


# ── 1) 정제 ─────────────────────────────────────────────────────
def normalize_word_name(raw: str) -> str:
    """
    API 표제어를 대표 단어 하나로 정리합니다.

    >>> normalize_word_name("복장,입다")
    '복장'
    >>> normalize_word_name("(신체의)등")
    '등'
    >>> normalize_word_name("사랑, -애")
    '사랑'
    """
    if not raw:
        return ""

    name = _HTML_TAG.sub("", str(raw))
    name = _PAREN.sub("", name)          # 괄호 안 부연 설명 제거
    name = _SPLIT_CHARS.split(name)[0]   # 쉼표·가운뎃점 앞의 대표 단어만 사용
    name = name.strip()
    name = name.strip("-–—‘’“”\"'`.^*")  # 접사 표시(-애) 및 따옴표 제거
    # 동형어 번호(사랑01, 사랑02)만 제거합니다. 앞자리가 0인 두 자리 숫자만 대상이라
    # 코로나19·G20·119 같은 이름은 그대로 남습니다.
    stripped = _TRAILING_NUM.sub("", name).strip()
    if len(stripped) >= 2:
        name = stripped
    return " ".join(name.split()).strip()


def normalize_meaning(raw: str, fallback: str = DEFAULT_MEANING) -> str:
    """뜻풀이에서 HTML 태그와 군더더기 공백을 제거합니다. 비어 있으면 기본 문구를 씁니다."""
    if not raw:
        return fallback
    text = _HTML_TAG.sub("", str(raw))
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = " ".join(text.split()).strip()
    return text or fallback


def _first_media_url(raw: str) -> str:
    """signImages 처럼 쉼표로 여러 건이 올 수 있으므로 첫 번째 http(s) 주소만 씁니다."""
    if not raw:
        return ""
    for candidate in str(raw).split(","):
        url = candidate.strip()
        if url.startswith(("http://", "https://")):
            return url
    return ""


# ── 2) 검증 ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class SignWord:
    word_name: str
    meaning: str
    video_url: str        # 동영상 파일 주소 (퀴즈에서 노출해도 안전)
    category: str
    image_url: str = ""   # 수형 사진 (임베드에 직접 표시 - 본문에 주소가 안 보임)
    detail_url: str = ""  # 사전 상세 페이지 (정답이 보이므로 퀴즈 중에는 숨김)

    def as_row(self) -> tuple[str, str, str, str, str, str]:
        """database.sync_api_words() 가 받는 튜플 형태로 변환합니다."""
        return (
            self.word_name, self.meaning, self.video_url,
            self.image_url, self.category, self.detail_url,
        )


@dataclass(frozen=True)
class RejectedWord:
    word_name: str
    reason: str


def _matches_keyword(name: str, keyword: str) -> bool:
    """표제어가 검색어와 같은 말인지 판단합니다. (검색어가 없으면 무조건 통과)"""
    if not keyword:
        return True
    if name == keyword:
        return True
    if not name.startswith(keyword):
        return False  # '사랑을 나누는 남녀'처럼 검색어가 중간에 낀 복합어는 제외

    tail = name[len(keyword):]
    if tail in ALLOWED_SUFFIXES:  # 사랑 + 하다 → 허용
        return True
    return len(name) <= max(len(keyword) + 1, len(keyword) * MAX_LENGTH_RATIO)


def evaluate_word(item: dict[str, Any], keyword: str = "") -> SignWord | RejectedWord:
    """
    API 결과 1건을 정제·검증합니다.
    통과하면 SignWord, 걸러지면 이유를 담은 RejectedWord를 돌려줍니다.
    """
    raw_name = _pick(item, "word_name")
    name = normalize_word_name(raw_name)
    label = raw_name or "(이름 없음)"

    if not name:
        return RejectedWord(label, "표제어를 읽을 수 없음")

    if INVALID_CHARS.search(name):
        return RejectedWord(label, "표제어에 기호가 섞여 있음")

    if len(name.split()) > MAX_WORD_TOKENS:
        return RejectedWord(label, f"어절이 너무 많음 (문장형 표현으로 추정)")

    if len(name) > MAX_WORD_LENGTH:
        return RejectedWord(label, f"표제어가 너무 긺 ({len(name)}자 > {MAX_WORD_LENGTH}자)")

    if any(bad in name for bad in BLOCKED_NAME_KEYWORDS):
        return RejectedWord(label, "전문 용어·유물명으로 추정되는 표제어")

    keyword = normalize_word_name(keyword)
    if not _matches_keyword(name, keyword):
        return RejectedWord(label, f"검색어 '{keyword}' 와 일치하지 않음")

    category = normalize_word_name(_pick(item, "category")) or DEFAULT_CATEGORY
    if any(bad in category for bad in BLOCKED_CATEGORIES):
        return RejectedWord(label, f"제외 분류에 해당 ({category})")

    video_url = pick_media_url(item)
    if not video_url:
        return RejectedWord(label, "수어 영상·이미지 주소가 없음")

    return SignWord(
        word_name=name,
        meaning=pick_meaning(item),
        video_url=video_url,
        category=category,
        image_url=pick_image_url(item),
        detail_url=pick_detail_url(item),
    )


def filter_words(
    items: Iterable[dict[str, Any]],
    keyword: str = "",
    seen: set[tuple[str, str]] | None = None,
) -> tuple[list[SignWord], list[RejectedWord]]:
    """
    여러 건을 한꺼번에 정제·검증합니다.

    중복 판정은 (단어명, 영상주소) 복합 기준입니다.
    '배(사물)'과 '배(신체)'처럼 영상이 다르면 동음이의어로 보고 둘 다 남깁니다.
    seen 집합을 넘기면 여러 페이지에 걸쳐 중복을 이어서 걸러 냅니다.

    반환: (통과한 단어들, 걸러진 단어들)
    """
    accepted: list[SignWord] = []
    rejected: list[RejectedWord] = []
    if seen is None:
        seen = set()

    for item in items:
        result = evaluate_word(item, keyword)
        if isinstance(result, RejectedWord):
            rejected.append(result)
            continue
        key = (result.word_name, result.video_url)
        if key in seen:
            rejected.append(RejectedWord(result.word_name, "같은 단어·같은 영상이 이미 있음"))
            continue
        seen.add(key)
        accepted.append(result)

    return accepted, rejected


def _pick(item: dict[str, Any], field: str) -> str:
    """응답 태그 이름이 조금 달라도 값을 찾아내기 위한 헬퍼."""
    for key in _KEY_CANDIDATES[field]:
        value = item.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        if value not in (None, ""):
            return str(value)
    return ""


def _values(
    item: dict[str, Any], fields: tuple[str, ...], *, split_commas: bool = False
) -> list[str]:
    """
    지정한 태그들의 값을 우선순위 순서로 모읍니다.

    split_commas=True 는 signImages 처럼 '쉼표로 구분된 주소 목록'에만 씁니다.
    설명 텍스트에 쓰면 문장 안의 쉼표에서 잘리므로 기본값은 False 입니다.
    """
    out: list[str] = []
    for key in fields:
        raw = item.get(key)
        if isinstance(raw, list):
            raw = ",".join(str(x) for x in raw)
        if raw in (None, ""):
            continue
        parts = str(raw).split(",") if split_commas else [str(raw)]
        for part in parts:
            part = part.strip()
            if part:
                out.append(part)
    return out


def pick_media_url(item: dict[str, Any]) -> str:
    """
    동영상·이미지 '파일' 주소를 고릅니다. 동영상을 우선하고, 없으면 이미지를 씁니다.
    사전 상세 페이지(signContentsView.do)는 파일이 아니므로 절대 선택되지 않습니다.
    """
    values = _values(item, _URL_FIELDS, split_commas=True)
    for value in values:
        if _is_url(value) and _has_ext(value, _VIDEO_EXT):
            return value
    for value in values:
        if _is_url(value) and _has_ext(value, _IMAGE_EXT):
            return value
    return ""


def pick_detail_url(item: dict[str, Any]) -> str:
    """사전 상세 페이지 주소를 고릅니다. (미디어 파일이 아닌 http 주소)"""
    for value in _values(item, _URL_FIELDS, split_commas=True):
        if _is_url(value) and not is_media_url(value):
            return value
    return ""


def pick_image_url(item: dict[str, Any]) -> str:
    """수형 사진 주소를 고릅니다. (임베드에 그대로 띄울 수 있는 이미지)"""
    for value in _values(item, _URL_FIELDS, split_commas=True):
        if _is_url(value) and _has_ext(value, _IMAGE_EXT):
            return value
    return ""


def pick_meaning(item: dict[str, Any]) -> str:
    """
    사람이 읽는 설명을 '자를 수 있는 데까지 자르지 않고' 모읍니다.

    - description 자리에 mp4 주소가 들어오는 API라서, 주소처럼 생긴 값은 건너뜁니다.
    - 여러 태그에 설명이 나뉘어 있으면 모두 이어 붙입니다.
    - 이미 담은 문장에 포함되는 짧은 값은 중복이므로 버립니다.
    """
    kept: list[str] = []
    for value in _values(item, _MEANING_FIELDS):
        if _is_url(value):
            continue
        text = normalize_meaning(value, fallback="")
        if not text:
            continue
        if any(text in existing for existing in kept):
            continue  # 이미 들어 있는 내용
        kept = [existing for existing in kept if existing not in text]  # 더 긴 쪽을 남김
        kept.append(text)
    return "\n".join(kept) if kept else DEFAULT_MEANING


# ── 3) XML 응답 파싱 ────────────────────────────────────────────
def _tag(element: ET.Element) -> str:
    """네임스페이스가 붙어 있으면 떼어 냅니다."""
    return element.tag.split("}")[-1]


def _flatten(element: ET.Element, into: dict[str, str]) -> None:
    """<item> 아래의 자식 태그를 평평한 딕셔너리로 만듭니다. (중첩 태그도 포함)"""
    for child in element:
        text = (child.text or "").strip()
        name = _tag(child)
        if text and name not in into:
            into[name] = text
        _flatten(child, into)


@dataclass
class ApiPage:
    """응답 한 페이지."""
    items: list[dict[str, str]]
    total_count: int
    result_code: str
    result_msg: str


def _parse_response(body: str) -> ApiPage:
    """
    <response><header>...</header><body><items><item>...</item></items>
    <totalCount>3754</totalCount></body></response> 를 해석합니다.
    """
    text = body.strip()
    if not text:
        raise KSLApiParseError("서버가 빈 응답을 보냈습니다.")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        snippet = text[:200].replace("\n", " ")
        raise KSLApiParseError(f"XML 응답을 해석하지 못했습니다: {exc} / 응답 앞부분: {snippet}") from exc

    result_code = result_msg = ""
    total_count = 0
    for node in root.iter():
        name = _tag(node)
        value = (node.text or "").strip()
        if name == "resultCode" and not result_code:
            result_code = value
        elif name == "resultMsg" and not result_msg:
            result_msg = value
        elif name == "totalCount" and not total_count and value.isdigit():
            total_count = int(value)

    items: list[dict[str, str]] = []
    for node in root.iter():
        if _tag(node).lower() == "item":
            data: dict[str, str] = {}
            _flatten(node, data)
            if data:
                items.append(data)

    return ApiPage(items=items, total_count=total_count, result_code=result_code, result_msg=result_msg)


# ── 4) API 클라이언트 ───────────────────────────────────────────
class KSLApiClient:
    """문화공공데이터광장(KCISA) 수어 API 비동기 클라이언트."""

    def __init__(
        self,
        api_key: str | None = None,
        session: aiohttp.ClientSession | None = None,
        endpoint: str | None = None,
    ) -> None:
        key = api_key or os.getenv("KSL_API_KEY")
        if not key:
            raise KSLApiKeyMissing(
                "KSL_API_KEY가 설정되어 있지 않습니다. .env 파일을 확인해 주세요."
            )
        # 포털에서 'Encoding' 키를 복사한 경우 이중 인코딩을 막기 위해 한 번 풀어 둡니다.
        self.api_key = unquote(key.strip()) if "%" in key else key.strip()
        self.endpoint = endpoint or BASE_URL
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "KSLApiClient":
        self._ensure_session()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ── 요청 ─────────────────────────────────────────────────────
    def _build_params(
        self, num_of_rows: int, page_no: int, keyword: str = "", search_param: str = "keyword"
    ) -> dict[str, str]:
        """
        keyword 는 빈 값이라도 반드시 포함해야 합니다. (제공처 주의사항)
        다른 이름(title 등)으로 검색할 때는 keyword= 를 함께 붙입니다.
        """
        params = {
            "serviceKey": self.api_key,
            "numOfRows": str(num_of_rows),
            "pageNo": str(page_no),
            "keyword": "",
        }
        if keyword:
            params[search_param] = keyword
        return params

    async def _request(self, params: dict[str, str]) -> ApiPage:
        session = self._ensure_session()
        try:
            async with session.get(self.endpoint, params=params) as resp:
                body = await resp.text()
                self._check_status(resp.status, body)
        except _DNS_ERROR as exc:
            raise KSLApiRequestError(
                f"'{self.endpoint}' 도메인을 찾을 수 없습니다. 주소와 네트워크 연결을 확인해 주세요."
            ) from exc
        except aiohttp.ClientError as exc:
            raise KSLApiRequestError(f"API 서버에 연결하지 못했습니다: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise KSLApiRequestError(
                f"API 서버 응답이 {REQUEST_TIMEOUT}초 안에 오지 않았습니다."
            ) from exc

        page = _parse_response(body)
        self._check_result_code(page.result_code, page.result_msg)
        return page

    def _check_status(self, status: int, body: str) -> None:
        """HTTP 상태 코드별 안내 메시지."""
        if status == 200:
            return
        if status in (401, 403):
            raise KSLApiAuthError(
                f"인증에 실패했습니다 (HTTP {status}). "
                "KSL_API_KEY 값과 해당 API 활용 승인 상태를 확인해 주세요."
            )
        if status == 404:
            raise KSLApiRequestError(
                f"요청 주소를 찾을 수 없습니다 (HTTP 404).\n{self.endpoint}\n"
                "신청하신 API의 '요청 URL'과 같은지 확인해 주세요. "
                "다른 주소라면 .env 에 KSL_API_ENDPOINT 로 지정할 수 있습니다."
            )
        if status == 429:
            raise KSLApiRequestError("호출 한도를 초과했습니다 (HTTP 429). 잠시 후 다시 시도해 주세요.")
        if 500 <= status < 600:
            raise KSLApiRequestError(
                f"API 서버에 문제가 있습니다 (HTTP {status}). 잠시 후 다시 시도해 주세요."
            )
        raise KSLApiRequestError(f"예상치 못한 응답입니다 (HTTP {status}). {body[:150]}")

    @staticmethod
    def _check_result_code(result_code: str, result_msg: str) -> None:
        """본문 안의 resultCode 검사 (HTTP 200이어도 실패일 수 있습니다)."""
        if not result_code or result_code in SUCCESS_CODES:
            return
        upper = f"{result_code} {result_msg}".upper()
        if any(hint in upper for hint in AUTH_FAIL_HINTS):
            raise KSLApiAuthError(
                f"인증키가 거부되었습니다. [{result_code}] {result_msg or '메시지 없음'}"
            )
        raise KSLApiRequestError(f"API 오류 [{result_code}] {result_msg or '메시지 없음'}")

    # ── 페이지 단위 조회 ─────────────────────────────────────────
    async def fetch_page(
        self, page_no: int, *, num_of_rows: int = DEFAULT_NUM_OF_ROWS, keyword: str = ""
    ) -> ApiPage:
        """페이지 하나를 그대로 가져옵니다. (검색어 없이 전체 조회용)"""
        params = self._build_params(num_of_rows, page_no, keyword)
        return await self._request(params)

    async def search_raw(
        self, keyword: str, *, num_of_rows: int = DEFAULT_NUM_OF_ROWS, page_no: int = 1
    ) -> ApiPage:
        """
        단어 검색. 어느 파라미터 이름이 실제로 동작하는지 모르므로
        keyword → title → searchWrd → q 순서로 시도하고, 성공한 이름을 기억합니다.
        """
        global _working_search_param

        candidates = (
            (_working_search_param,) if _working_search_param
            else SEARCH_PARAM_CANDIDATES
        )

        last_page: ApiPage | None = None
        for param in candidates:
            page = await self._request(self._build_params(num_of_rows, page_no, keyword, param))
            last_page = page
            if page.items:
                if _working_search_param != param:
                    log.info("[KSL] 검색 파라미터 '%s' 사용 (결과 %d건)", param, len(page.items))
                    _working_search_param = param
                return page
            log.debug("[KSL] 파라미터 '%s' 로는 결과가 없습니다.", param)

        # 기억해 둔 파라미터가 갑자기 안 되면 다음 호출에서 다시 탐색하도록 초기화
        _working_search_param = None
        return last_page or ApiPage([], 0, "", "")


# ── 5) 최상위 함수 ──────────────────────────────────────────────
ProgressCallback = Callable[[int, int, int], Awaitable[None]]
"""진행 상황 콜백: (현재 페이지, 지금까지 수집한 건수, 전체 건수)"""


async def fetch_sign_words(
    keyword: str,
    api_key: str | None = None,
    *,
    num_of_rows: int = DEFAULT_NUM_OF_ROWS,
    page_no: int = 1,
    rejected: list[RejectedWord] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> list[tuple[str, ...]]:
    """
    검색어 하나로 수어 단어를 가져옵니다.
    'title' 등 여러 파라미터 이름을 자동으로 시도합니다.

    반환: [(word_name, meaning, video_url, image_url, category, detail_url), ...]
    Raises: KSLApiError
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise KSLApiError("검색어가 비어 있습니다.")

    client = KSLApiClient(api_key, session=session)
    try:
        page = await client.search_raw(keyword, num_of_rows=num_of_rows, page_no=page_no)
    finally:
        await client.close()

    if not page.items:
        log.warning(
            "[KSL] '%s' - 서버 검색이 결과를 주지 않았습니다 (전체 %d건). "
            "전체 동기화 후 로컬에서 찾는 방법을 권합니다.",
            keyword, page.total_count,
        )

    accepted, rejected_words = filter_words(page.items, keyword)
    if page.items and not accepted:
        log.warning(
            "[KSL] '%s' - %d건을 받았지만 모두 제외되었습니다. 실제 태그: %s",
            keyword, len(page.items), sorted(page.items[0].keys()),
        )
    log.info(
        "[KSL] '%s' 검색: 수집 %d건 → 통과 %d건 / 제외 %d건",
        keyword, len(page.items), len(accepted), len(rejected_words),
    )

    if rejected is not None:
        rejected.extend(rejected_words)
    return [w.as_row() for w in accepted]


async def fetch_many_sign_words(
    keywords: Sequence[str],
    api_key: str | None = None,
    *,
    num_of_rows: int = DEFAULT_NUM_OF_ROWS,
    rejected: list[RejectedWord] | None = None,
) -> list[tuple[str, ...]]:
    """
    여러 검색어를 차례대로 조회합니다.
    일부가 실패해도 나머지는 진행하고, 전부 실패한 경우에만 예외를 던집니다.
    """
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        raise KSLApiError("검색어가 비어 있습니다.")

    rows: list[tuple[str, ...]] = []
    seen: set[str] = set()
    errors: list[BaseException] = []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    ) as session:
        for keyword in keywords:
            try:
                found = await fetch_sign_words(
                    keyword, api_key, num_of_rows=num_of_rows,
                    rejected=rejected, session=session,
                )
            except KSLApiError as exc:
                log.warning("[KSL] '%s' 검색 실패: %s", keyword, exc)
                errors.append(exc)
                if rejected is not None:
                    rejected.append(RejectedWord(keyword, f"검색 실패 ({type(exc).__name__})"))
                continue

            for row in found:
                key = (row[0], row[2])  # (단어명, 영상주소)
                if key in seen:  # 검색어끼리 겹치는 결과 제거
                    continue
                seen.add(key)
                rows.append(row)

    if errors and len(errors) == len(keywords):
        raise errors[0]  # 원인(404 · 인증 실패 등)을 그대로 전달
    return rows


async def fetch_all_sign_words(
    max_pages: int = DEFAULT_MAX_PAGES,
    rows_per_page: int = DEFAULT_NUM_OF_ROWS,
    *,
    api_key: str | None = None,
    rejected: list[RejectedWord] | None = None,
    progress: ProgressCallback | None = None,
) -> list[tuple[str, ...]]:
    """
    검색어 없이 pageNo 를 1부터 순회하여 전체 수어 데이터를 수집합니다.
    (약 3,754건 / 페이지당 100건 → 38페이지 정도)

    Parameters
    ----------
    max_pages     : 최대 페이지 수 (안전장치)
    rows_per_page : numOfRows (이 API는 100이 최대)
    rejected      : 리스트를 넘기면 걸러진 항목이 여기에 채워집니다.
    progress      : async 콜백 (page_no, 수집 건수, 전체 건수) - 진행 표시용

    Raises
    ------
    KSLApiError : 첫 페이지부터 실패한 경우
                  (중간 페이지 실패는 로그만 남기고 계속 진행합니다)
    """
    rows: list[tuple[str, ...]] = []
    seen: set[str] = set()
    total_count = 0

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    ) as session:
        client = KSLApiClient(api_key, session=session)

        for page_no in range(1, max_pages + 1):
            try:
                page = await client.fetch_page(page_no, num_of_rows=rows_per_page)
            except KSLApiError as exc:
                if page_no == 1:
                    raise  # 첫 페이지부터 안 되면 설정 문제이므로 그대로 알림
                log.warning("[KSL] %d페이지 수집 실패, 건너뜁니다: %s", page_no, exc)
                continue

            if page.total_count:
                total_count = page.total_count

            if not page.items:
                log.info("[KSL] %d페이지에서 데이터가 끝났습니다.", page_no)
                break

            accepted, rejected_words = filter_words(page.items, "", seen)
            rows.extend(w.as_row() for w in accepted)
            if rejected is not None:
                rejected.extend(rejected_words)

            log.info(
                "[KSL] %d페이지: 수집 %d건 → 통과 %d건 (누적 %d건 / 전체 %d건)",
                page_no, len(page.items), len(accepted), len(rows), total_count,
            )
            if progress is not None:
                await progress(page_no, len(rows), total_count)

            # 전체 건수를 다 훑었으면 종료
            if total_count and page_no * rows_per_page >= total_count:
                break
            await asyncio.sleep(PAGE_DELAY)

    return rows
