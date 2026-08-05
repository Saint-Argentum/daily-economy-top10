"""
/api/keywords — 오늘의 경제 키워드 Top 10 + 감성 점수를 반환하는 서버리스 함수.

지금까지 구현된 단계 (PLAN.md 기준):
  2단계: 뼈대 + 더미 JSON
  3단계: RSS 6개 피드 수집 (feedparser)
  4단계: 기사 링크(link) 기준 중복 제거
  5단계: BeautifulSoup으로 HTML 엔티티 정리 + 대괄호 말머리 제거
  6단계: pubDate 기준 최근 24시간 이내(접속 시점 기준 롤링 윈도우) 기사만 필터링
  7단계: RSS 수집 결과에 캐시 적용
  8단계: kiwipiepy 형태소 분석 연결 (NNG·NNP·VV·VA·SL만 추출)
  9단계: 동사·형용사 원형(어간+"다") 변환
  10단계: 불용어·1글자 단어 제외 목록 적용
  11단계: 단어 빈도 집계 (제목당 1회 카운트, 상위 10개 선정)
  12단계: 감성사전 로드 (SentiWord_info.json + econNLPdic.txt 병합)
  13단계: 제목별 감성 점수 계산 (형태소 단위 매칭, 검출 감성어 평균)
  14단계: 키워드별 감성 점수 계산 및 긍정·중립·부정 판정
  15단계: API 응답 스키마 확정 — 지금 여기까지

11단계 참고: "등장 기사 5건 미만 제외" 기준은 사용자 요청으로 두지 않기로 했고,
PRD.md·PLAN.md·CLAUDE.md·DESIGN.md에서도 이 규칙을 함께 제거했다.

13단계 참고: 형태소 단위 매칭으로도 완전히 못 막는 동형이의어 오탐이 있었다 — "지지력"은
kiwipiepy가 "지지"(NNG)+"력"(접미사)으로 쪼개는데, KnuSentiLex에 "지지"가 -1(부정,
아기말 "지지"=더러운 것)로 등록돼 있어 금융 용어 "지지력"(support level)이 부정으로
잘못 잡혔다. "지지하다"도 지지(NNG)+하(XSV)+다(EF)로 분석돼 명사와 태그가 똑같아서
품사로 구분할 방법이 없었다. 사용자 확인 후 SENTIMENT_MATCH_EXCLUDE로 "지지"만
감성 매칭에서 제외했다 (키워드 집계에는 영향 없음).

15단계 참고: 응답 스키마를 최종 확정하면서, 그동안 검증용으로 붙여뒀던 디버그 필드
(stage·cache·feedStatus.perFeed·sampleEntries·sentimentDict 등)를 모두 걷어냈다.
최종 스키마는 build_response()의 docstring 참고. 프론트 렌더링(16~19단계)은 이제
이 스키마를 그대로 쓰면 된다.

6단계 추가 참고: 원래 "오늘(KST 00:00 이후)" 캘린더 날짜 기준이었는데, 사용자 요청으로
"접속 시점 기준 최근 24시간" 롤링 윈도우로 바꿨다. PRD.md·PLAN.md·CLAUDE.md·DESIGN.md도
같이 고쳤다. 그래서 API 응답도 asOfDate(날짜 하나) 대신 windowStart/windowEnd(구간)로
바뀌었다 — 캘린더 날짜라는 개념 자체가 더 이상 안 맞기 때문이다.

주의: Vercel Python 런타임은 /api/keywords.py 하나만 함수로 번들링하고
같은 폴더의 다른 .py 파일(예: _lib/*)은 자동으로 같이 담아주지 않는다.
그래서 PRD가 정한 대로 이 파일 하나에 모든 로직을 둔다.

확인됨: 코드가 아닌 일반 데이터 파일(stopwords.txt처럼 프로젝트 루트에 있는 .txt 등)은
Vercel Python 런타임에서도 정상적으로 읽힌다 (실제 배포 환경에서 확인). 그래서
econNLPdic.txt·SentiWord_info.json(12~13단계)도 같은 방식으로 루트에서 읽으면 된다.
"""

from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re

import feedparser
from bs4 import BeautifulSoup
from kiwipiepy import Kiwi

# 콜드 스타트당 한 번만 로드되도록 모듈 전역에 둔다 (모델 로딩이 느림).
_kiwi = Kiwi()

# PRD.md §5 M1/§9-8: 추출 대상 품사 — 명사(NNG·NNP)·동사(VV)·형용사(VA)·외국어(SL)
TARGET_POS = {"NNG", "NNP", "VV", "VA", "SL"}


def load_stopwords() -> set[str]:
    """stopwords.txt(프로젝트 루트)를 읽어 불용어 집합으로 만든다.

    keywords.py 기준 부모의 부모 폴더(my-app/)에 있다. Vercel Python 런타임이
    api/ 바깥 파일도 실제로 읽어주는지 이번에 처음 확인하는 것이라, 실패하면
    바로 알 수 있도록 예외를 감추지 않는다.
    """
    path = Path(__file__).resolve().parent.parent / "stopwords.txt"
    words: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            words.add(line)
    return words


STOPWORDS = load_stopwords()


def load_noise_topics() -> set[str]:
    """noise_topics.txt(프로젝트 루트)를 읽는다.

    stopwords.txt(개별 단어 제외)와 달리, 여기 있는 단어가 제목에 있으면 기사
    자체를 통째로 뺀다 — "정책/금융" 피드에 섞여 들어오는 순수 정치·사법 기사를
    거르기 위함 (사용자 확인 후 추가, PRD.md §5 M0 참고).
    """
    path = Path(__file__).resolve().parent.parent / "noise_topics.txt"
    words: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            words.add(line)
    return words


NOISE_TOPICS = load_noise_topics()


def is_noise_article(title: str) -> bool:
    """제목에 정치·사법 절차 용어가 하나라도 있으면 기사를 통째로 노이즈로 본다."""
    return any(term in title for term in NOISE_TOPICS)


def load_econ_dict() -> dict[str, int]:
    """econNLPdic.txt(재무 특화 60개)를 읽는다. POS -> +2, NEG -> -2로 환산한다."""
    path = Path(__file__).resolve().parent.parent / "econNLPdic.txt"
    result: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            word, polarity = parts[0].strip(), parts[1].strip()
            if polarity == "POS":
                result[word] = 2
            elif polarity == "NEG":
                result[word] = -2
    return result


def load_knu_dict() -> dict[str, int]:
    """SentiWord_info.json(KnuSentiLex, 약 14,843개)을 읽는다.

    각 항목은 {"word", "word_root", "polarity"} 형태다. "word"를 매칭 키로 쓴다 —
    "word_root"는 내부 분류용 축약형이라, 실제 사전 표제어에 가까운 "word"가
    13단계에서 kiwipiepy 원형(lemma)과 비교하기에 더 적합하다. polarity는 이미
    -2~2 정수 문자열이라 econNLPdic과 같은 척도라서 별도 환산이 필요 없다.
    """
    path = Path(__file__).resolve().parent.parent / "SentiWord_info.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    result: dict[str, int] = {}
    for item in data:
        word = (item.get("word") or "").strip()
        if not word:
            continue
        try:
            result[word] = int(item.get("polarity", "0"))
        except (TypeError, ValueError):
            continue
    return result


KNU_DICT = load_knu_dict()
ECON_DICT = load_econ_dict()

# 두 사전을 병합한다. 충돌하는 단어는 econNLPdic이 우선한다 (PRD.md §5 M2) —
# KnuSentiLex로 먼저 채우고 econNLPdic으로 덮어써서 나중에 쓴 값이 이기게 한다.
SENTIMENT_DICT: dict[str, int] = {**KNU_DICT, **ECON_DICT}


def is_noise_token(lemma: str) -> bool:
    """불용어 목록에 있거나 1글자면 노이즈로 본다 (PRD.md §5 M1/§9-10)."""
    return lemma in STOPWORDS or len(lemma) < 2

# 제목 맨 앞의 대괄호 말머리 — 예: "[증시-마감]", "[도쿄환시]"
LEADING_BRACKET_RE = re.compile(r"^\[[^\]]*\]\s*")

# 한국 시간대는 DST가 없는 고정 UTC+9라, zoneinfo(IANA tzdata)에 기대지 않고
# 직접 고정 오프셋을 쓴다 — 서버리스 실행 환경에 tzdata가 없을 수도 있어서다.
KST = timezone(timedelta(hours=9))

# 연합인포맥스 RSS의 pubDate는 오프셋 없이 "YYYY-MM-DD HH:MM:SS" 형태로 오고,
# 실제로는 이미 한국 시간(KST) 기준 값이다 (UTC가 아님, 직접 확인함).
PUB_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 연합인포맥스 RSS 6개 피드 목록 (PRD.md §5 M0 표와 동일하게 유지할 것)
FEEDS = [
    {"name": "전체기사", "category": "전체", "url": "https://news.einfomax.co.kr/rss/allArticle.xml"},
    {"name": "정책/금융", "category": "정책·금융", "url": "https://news.einfomax.co.kr/rss/S1N15.xml"},
    {"name": "채권/외환", "category": "채권·외환", "url": "https://news.einfomax.co.kr/rss/S1N16.xml"},
    {"name": "해외주식", "category": "해외주식", "url": "https://news.einfomax.co.kr/rss/S1N21.xml"},
    {"name": "국제뉴스", "category": "국제", "url": "https://news.einfomax.co.kr/rss/S1N23.xml"},
    {"name": "증권", "category": "증권", "url": "https://news.einfomax.co.kr/rss/S1N2.xml"},
]

FETCH_TIMEOUT_SECONDS = 6

# PRD.md는 "최소 5분"이라 30분도 규칙에 맞음. RSS 서버 부담을 더 줄이기 위해 30분으로 설정.
CACHE_TTL_SECONDS = 30 * 60

# 서버리스 함수가 재사용(warm)되는 동안 이 모듈 전역 값이 그대로 남는 점을 이용한
# 메모리 캐시. DB나 파일에 쓰지 않는다 (PRD.md: 영구 저장 없음).
_feed_cache: dict = {"results": None, "fetchedAt": None}

# 순위 변동(↑↓) 계산용 — 직전 RSS 수집 시점의 키워드 순위만 메모리에 들고 있는다
# (DB 아님, 서버리스 프로세스가 재시작되면 사라짐. PRD.md: 영구 저장 없음).
_previous_ranking_cache: dict = {"ranks": None}


def _fetch_one(feed: dict) -> dict:
    """피드 하나를 가져와 (name, ok, entries, error) 형태로 반환한다."""
    try:
        with urlopen(feed["url"], timeout=FETCH_TIMEOUT_SECONDS) as response:
            raw = response.read()
        parsed = feedparser.parse(raw)
        entries = [
            {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "pubDate": entry.get("published", ""),
                "source": feed["name"],
            }
            for entry in parsed.entries
        ]
        return {"name": feed["name"], "ok": True, "entries": entries, "error": None}
    except (URLError, HTTPError, TimeoutError, OSError) as exc:
        return {"name": feed["name"], "ok": False, "entries": [], "error": str(exc)}


def fetch_all_feeds() -> list[dict]:
    """6개 피드를 동시에 가져온다. 피드별 성공 여부를 함께 반환해 M3 표시에 쓴다."""
    results = []
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        futures = [pool.submit(_fetch_one, feed) for feed in FEEDS]
        for future in as_completed(futures):
            results.append(future.result())
    order = {feed["name"]: i for i, feed in enumerate(FEEDS)}
    results.sort(key=lambda r: order[r["name"]])
    return results


def fetch_all_feeds_cached() -> tuple[list[dict], datetime, bool]:
    """fetch_all_feeds()를 30분 캐시로 감싼다. (results, fetchedAt, cacheHit) 반환."""
    now = datetime.now(timezone.utc)
    cached = _feed_cache["results"]
    fetched_at = _feed_cache["fetchedAt"]

    if cached is not None and fetched_at is not None:
        age_seconds = (now - fetched_at).total_seconds()
        if age_seconds < CACHE_TTL_SECONDS:
            return cached, fetched_at, True

    results = fetch_all_feeds()
    _feed_cache["results"] = results
    _feed_cache["fetchedAt"] = now
    return results, now, False


def dedupe_by_link(entries: list[dict]) -> list[dict]:
    """기사 link 기준으로 중복을 제거한다 (전체기사 피드와 카테고리 피드가 겹치기 때문, PRD.md §5 M0).

    같은 링크가 여러 피드에 나오면 먼저 나온 것만 남긴다. FEEDS 순서상 "전체기사"가
    가장 먼저 오므로, 특별한 사정이 없으면 "전체기사" 쪽 항목이 남는다.
    """
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        link = entry["link"]
        if link in seen:
            continue
        seen.add(link)
        unique.append(entry)
    return unique


def clean_title(raw_title: str) -> str:
    """HTML 엔티티(&quot; 등)를 일반 문자로 바꾸고, 앞머리 대괄호 태그를 제거한다.

    PRD.md §5 M0 순서대로: BeautifulSoup으로 엔티티 정리 → 대괄호 말머리 제거.
    "&"가 없는 제목은 애초에 풀어야 할 엔티티가 없으므로 BeautifulSoup을 건너뛴다
    (그래야 "파일 경로 같다"는 MarkupResemblesLocatorWarning도 안 뜨고 더 빠르다).
    """
    text = BeautifulSoup(raw_title, "html.parser").get_text() if "&" in raw_title else raw_title
    text = LEADING_BRACKET_RE.sub("", text)
    return text.strip()


def parse_pub_date_kst(raw: str) -> datetime | None:
    """pubDate 문자열을 KST가 붙은 datetime으로 바꾼다. 형식이 안 맞으면 None."""
    try:
        naive = datetime.strptime(raw, PUB_DATE_FORMAT)
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=KST)


def is_within_last_24h(pub_date_kst: datetime | None, window_start_kst: datetime) -> bool:
    """캘린더 날짜(자정) 기준이 아니라, 접속 시점에서 24시간 거슬러 올라간 롤링 윈도우 기준."""
    if pub_date_kst is None:
        return False
    return pub_date_kst >= window_start_kst


def extract_pos_tokens(text: str) -> list[dict]:
    """제목에서 명사(NNG·NNP)·동사(VV)·형용사(VA)·외국어(SL) 토큰만 뽑는다.

    동사·형용사(VV·VA)는 kiwipiepy가 이미 활용 어미를 떼어낸 어간만 주므로,
    여기서 "다"만 붙이면 원형이 된다 (예: "먹었다" -> 어간 "먹" -> "먹다").
    불용어·1글자 제외(10단계)는 다음 단계에서 추가한다.
    """
    tokens = []
    for tok in _kiwi.tokenize(text):
        if tok.tag not in TARGET_POS:
            continue
        lemma = tok.form + "다" if tok.tag in ("VV", "VA") else tok.form
        tokens.append({"form": tok.form, "tag": tok.tag, "lemma": lemma})
    return tokens


def aggregate_keywords(entries: list[dict]) -> list[dict]:
    """제목당 1회 카운트로 키워드별 등장 기사 수를 세고 상위 10개를 고른다.

    "5건 미만 제외" 같은 최소 등장 기준은 두지 않는다 (PRD.md에서 삭제됨, 위 안내 참고).
    감성 점수 계산(12~15단계)에서 다시 쓸 수 있도록 각 키워드가 등장한 제목·링크도 같이 들고 있는다.
    """
    counts: dict[str, dict] = {}
    for entry in entries:
        all_tokens = extract_pos_tokens(entry["title"])
        lemmas_in_title = {t["lemma"] for t in all_tokens if not is_noise_token(t["lemma"])}
        for lemma in lemmas_in_title:
            bucket = counts.setdefault(
                lemma, {"keyword": lemma, "articleCount": 0, "entries": []}
            )
            bucket["articleCount"] += 1
            bucket["entries"].append({"title": entry["title"], "link": entry["link"]})

    ranked = sorted(counts.values(), key=lambda b: b["articleCount"], reverse=True)
    top10 = ranked[:10]
    for i, bucket in enumerate(top10, start=1):
        bucket["rank"] = i
    return top10


def compute_rank_changes(top_keywords: list[dict], is_new_fetch: bool) -> dict[str, str]:
    """직전 RSS 수집 시점의 순위와 비교해 "up"/"down"/"same"/"new"를 매긴다.

    비교 기준점은 "요청 시각"이 아니라 "직전 실제 RSS 수집 시각"이다 — 캐시가 살아있는
    30분 동안은 같은 스냅숏이라 매번 바뀌면 의미가 없기 때문. is_new_fetch=True(캐시 미스,
    즉 방금 새로 수집)일 때만 스냅숏을 다음 비교 기준으로 갱신한다.
    """
    previous_ranks: dict[str, int] | None = _previous_ranking_cache["ranks"]
    changes: dict[str, str] = {}

    for kw in top_keywords:
        if previous_ranks is None or kw["keyword"] not in previous_ranks:
            changes[kw["keyword"]] = "new"
        else:
            old_rank = previous_ranks[kw["keyword"]]
            if kw["rank"] < old_rank:
                changes[kw["keyword"]] = "up"
            elif kw["rank"] > old_rank:
                changes[kw["keyword"]] = "down"
            else:
                changes[kw["keyword"]] = "same"

    if is_new_fetch:
        _previous_ranking_cache["ranks"] = {kw["keyword"]: kw["rank"] for kw in top_keywords}

    return changes


# 감성사전에는 있지만 동형이의어 때문에 경제 뉴스에서 오탐을 일으키는 단어들 —
# 감성 매칭에서만 제외한다 (키워드 집계용 stopwords.txt와는 별개).
# "지지": KnuSentiLex에 부정(-1, 아기말 "더러운 것" 의미)으로 등록돼 있지만, kiwipiepy가
# "지지력"/"지지선"/"지지율" 등에서도 항상 "지지"를 명사(NNG)로 떼어내기 때문에
# ("동사로 쓰일 때만 나눈다" 같은 구분은 kiwipiepy 태깅상 존재하지 않음 — "지지하다"도
# 지지(NNG)+하(XSV)+다(EF)로 분석돼 똑같이 NNG로 나옴), 금융 용어로 쓰인 "지지"까지
# 매번 부정으로 오탐된다. 사용자 확인 후 감성 매칭에서만 제외하기로 함.
SENTIMENT_MATCH_EXCLUDE = {"지지"}


def sentiment_lemma(tok) -> str:
    """감성사전 매칭용 원형. 동사·형용사는 어간+"다", 나머지는 형태소 그대로.

    extract_pos_tokens()와 달리 품사를 NNG/NNP/VV/VA/SL로 제한하지 않는다 —
    "매우"(MAG), "충분히"(MAG) 같은 부사도 감성사전에 있어서, 품사를 제한하면
    이런 단어가 영영 매칭되지 않는다.
    """
    if tok.tag in ("VV", "VA"):
        return tok.form + "다"
    return tok.form


def score_title(text: str) -> tuple[float | None, list[dict]]:
    """제목의 감성 점수 = 검출된 감성어 점수들의 평균 (합이 아님, PRD.md §5 M2).

    형태소 단위로만 사전과 비교한다 (문자열 부분 일치 금지) — 즉 title 전체 문자열에서
    "지지" 같은 부분 문자열을 찾는 게 아니라, kiwipiepy가 나눈 형태소 토큰이 사전에
    정확히 있는 경우만 감성어로 센다.
    감성어가 하나도 검출되지 않으면 None을 반환한다 (0점으로 넣지 않음).
    """
    matched = []
    for tok in _kiwi.tokenize(text):
        lemma = sentiment_lemma(tok)
        if lemma in SENTIMENT_MATCH_EXCLUDE:
            continue
        if lemma in SENTIMENT_DICT:
            matched.append({"lemma": lemma, "tag": tok.tag, "score": SENTIMENT_DICT[lemma]})

    if not matched:
        return None, matched

    avg = sum(m["score"] for m in matched) / len(matched)
    return avg, matched


def score_keyword(entries: list[dict]) -> tuple[float | None, int]:
    """키워드 감성 점수 = 그 키워드가 등장한 제목들의 점수 평균 (PRD.md §5 M2).

    제목 점수가 None(감성어 미검출)인 제목은 평균에서 제외한다. 모든 제목이 None이면
    키워드 점수도 None — 화면에는 0.00이 아니라 "-"로 표시해야 한다(PRD.md §5 M2).
    (검출된 제목 수, 전체 등장 제목 수)도 같이 반환해 검증에 쓴다.
    """
    title_scores = [score_title(e["title"])[0] for e in entries]
    valid_scores = [s for s in title_scores if s is not None]
    if not valid_scores:
        return None, 0
    return sum(valid_scores) / len(valid_scores), len(valid_scores)


def sentiment_label(score: float | None) -> str:
    """+0.5 이상 긍정 / -0.5 이하 부정 / 그 사이 중립 / 미검출은 별도 표시 (PRD.md §5 M2)."""
    if score is None:
        return "미검출"
    if score >= 0.5:
        return "긍정"
    if score <= -0.5:
        return "부정"
    return "중립"


def build_response() -> dict:
    """15단계: 최종 API 응답 스키마.

    필드: windowStart/windowEnd(접속 시점 기준 최근 24시간 롤링 윈도우) / updatedAt(RSS
    수집 시각, KST) / articleCount(집계 기사 수) / feedStatus(정상 응답 피드 수) /
    averageScore(Top10 전체 평균, 점수 있는 것만) / keywords(Top10 배열:
    rank·keyword·articleCount·sentimentScore·rankChange·articles).

    rankChange는 직전 RSS 수집 시점 대비 순위 변동이다 ("up"/"down"/"same"/"new").
    DB 없이 메모리 캐시로만 유지하므로 서버리스 프로세스가 재시작되면 초기화된다.

    articles는 그 키워드가 등장한 기사들의 제목·링크다 (화면에서 키워드를 누르면
    펼쳐 보여준다). 제목과 링크만 담고 본문은 담지 않는다.

    날짜 필터는 캘린더 자정 기준이 아니라 롤링 윈도우다 (사용자 확인 후 변경) — 그래서
    "기준 날짜" 하나가 아니라 windowStart~windowEnd 구간으로 표시한다.

    sentimentScore는 감성어가 하나도 검출되지 않으면 null이다 (PRD.md §5 M2: 0.00이 아니라
    "-"로 표시해야 하므로, 프론트가 null을 "-"로 렌더링한다). 긍정/부정 라벨은 별도
    필드로 만들지 않는다 — PRD가 "라벨 컬럼을 따로 만들지 않고 점수 색으로만 표현한다"고
    정했으므로, +0.5 이상/−0.5 이하 판정은 프론트에서 점수를 보고 색만 입힌다(18단계).
    """
    now_kst = datetime.now(KST)
    window_start_kst = now_kst - timedelta(hours=24)

    feed_results, fetched_at, cache_hit = fetch_all_feeds_cached()

    ok_count = sum(1 for r in feed_results if r["ok"])
    all_entries = [entry for r in feed_results for entry in r["entries"]]
    unique_entries = dedupe_by_link(all_entries)

    cleaned_entries = [
        {
            "title": clean_title(entry["title"]),
            "link": entry["link"],
            "pubDate": entry["pubDate"],
        }
        for entry in unique_entries
    ]

    recent_entries = [
        entry
        for entry in cleaned_entries
        if is_within_last_24h(parse_pub_date_kst(entry["pubDate"]), window_start_kst)
        and not is_noise_article(entry["title"])
    ]

    top_keywords = aggregate_keywords(recent_entries)
    rank_changes = compute_rank_changes(top_keywords, is_new_fetch=not cache_hit)

    keywords_view = []
    scores_for_average = []
    for kw in top_keywords:
        raw_score, _scored_title_count = score_keyword(kw["entries"])
        score = round(raw_score, 2) if raw_score is not None else None
        if score is not None:
            scores_for_average.append(score)
        keywords_view.append(
            {
                "rank": kw["rank"],
                "keyword": kw["keyword"],
                "articleCount": kw["articleCount"],
                "sentimentScore": score,
                "rankChange": rank_changes[kw["keyword"]],  # "up" | "down" | "same" | "new"
                # 화면에서 키워드를 누르면 펼쳐 보여줄 기사 목록 (제목 + 링크).
                # 본문은 담지 않는다 — PRD는 기사 본문 크롤링을 계속 비범위로 둔다.
                "articles": kw["entries"],
            }
        )

    average_score = (
        round(sum(scores_for_average) / len(scores_for_average), 2)
        if scores_for_average
        else None
    )

    return {
        "windowStart": window_start_kst.isoformat(),
        "windowEnd": now_kst.isoformat(),
        "updatedAt": fetched_at.astimezone(KST).isoformat(),
        "articleCount": len(recent_entries),
        "feedStatus": {"total": len(feed_results), "ok": ok_count},
        "averageScore": average_score,
        "keywords": keywords_view,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = json.dumps(build_response(), ensure_ascii=False).encode("utf-8")
            status = 200
        except Exception as exc:  # noqa: BLE001 - 수집 단계 확인용 임시 처리
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            status = 500

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
