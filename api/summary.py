"""
/api/summary — 오늘의 Top10 키워드 + 주요 시장 지표를 바탕으로 LLM이 매크로 이슈를
한국어로 요약하는 서버리스 함수.

경고: 이 프로젝트는 원래 "LLM 기반 요약·감성 판정 없음, 감성은 사전 규칙만 사용"이
원칙이었다 (PRD.md §6, CLAUDE.md). 이 함수는 사용자가 명시적으로 요청해서 추가한
예외이며, 키워드 감성 점수 계산(§5 M2, api/keywords.py)에는 전혀 관여하지 않는다 —
이미 사전 매칭으로 계산된 점수와 시장 지표 숫자를 문장으로 풀어 설명해줄 뿐이다.

프론트가 /api/keywords, /api/markets에서 이미 받아온 데이터를 그대로 POST로 넘기면,
이 함수는 뉴스나 시세를 다시 수집하지 않고 요약만 만든다 (keywords.py/markets.py와
독립적으로 유지하기 위함).

OPENAI_API_KEY는 .env(로컬)나 Vercel 프로젝트 환경변수(배포 후)에서 읽는다. 이
프로젝트에서 실제 앱 런타임이 LLM API를 호출하는 유일한 지점이다.
"""

from http.server import BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os

from openai import OpenAI

KST = timezone(timedelta(hours=9))
MODEL = "gpt-4o-mini"

CACHE_TTL_SECONDS = 30 * 60
_summary_cache: dict = {"text": None, "fetchedAt": None}

SYSTEM_PROMPT = """당신은 한국 경제 뉴스 대시보드의 요약 작성자입니다.
아래 매크로 지표 해석 원칙을 참고해, 오늘의 Top10 경제 키워드와 시장 지표 변동을
자연스러운 한국어 문장으로 요약하세요.

해석 원칙 (국내 증권사 리서치센터가 아침 시황에서 보는 순서):
- 미국 국채 금리 상승 = 긴축·할인율 상승 → 증시에 부담, 특히 코스닥 등 성장주에 더 크게 영향
- 달러인덱스(DXY)·원달러 상승 = 원화 약세 → 외국인 자금 유출 우려. 다만 반도체·자동차·조선 등
  수출기업엔 유리하고, 음식료·항공 등 내수·수입 의존 기업엔 불리
- 나스닥·필라델피아반도체(SOX) 상승 = AI·기술주 심리 개선 → 코스피·코스닥 반도체주에 우호적
- 엔화 강세(엔/원 상승) = 한일 수출 경쟁에서 한국 기업(자동차·철강·조선)에 유리. 다만 안전자산
  수요發 엔화 강세라면 위험회피 신호로 반대 해석
- 유로 강세는 통상 달러 약세·글로벌 위험선호와 동반돼 한국 증시에 우호적인 경우가 많음
- 유가(WTI·브렌트) 상승 = 인플레이션 우려, 에너지 수입국인 한국엔 대체로 부담
- VIX 상승 = 변동성·위험회피 심리 확대

작성 규칙:
- 3~5문장, 실제로 제공된 오늘의 키워드·수치만 근거로 사용한다 (지어내지 않는다)
- 토스증권 앱처럼 쉽고 간결한 문체 (전문용어 최소화, 존댓말, 과장된 확신 표현 지양)
- 특정 종목의 매수·매도를 권유하지 않는다 (투자자문이 아니다)
- 모든 수치를 나열하지 말고, 오늘 두드러진 흐름 위주로 서술한다
"""


def load_openai_client() -> OpenAI:
    """OPENAI_API_KEY는 우선 Vercel 환경변수(os.environ)에서 찾는다 — 배포 후 정식 경로다.
    로컬 vercel dev에서 아직 못 읽어오는 경우를 대비해 .env를 직접 읽는 예비 경로도 둔다.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("OPENAI_API_KEY"):
                        _, _, value = line.partition("=")
                        api_key = value.strip()
                        break
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY를 찾을 수 없습니다 (.env 또는 Vercel 환경변수 확인 필요)")
    return OpenAI(api_key=api_key)


def build_user_prompt(keywords: list, markets: list) -> str:
    kw_lines = [
        f"{k.get('rank')}위 {k.get('keyword')} "
        f"(기사 {k.get('articleCount')}건, 감성점수 {k.get('sentimentScore')})"
        for k in keywords
    ]
    mk_lines = [
        f"{m.get('name')}: {m.get('latest')}{m.get('unit')} "
        f"({'+' if (m.get('change') or 0) >= 0 else ''}{m.get('change')}{m.get('changeUnit')})"
        for m in markets
        if m.get("ok")
    ]
    return (
        "오늘의 경제 키워드 Top10:\n"
        + "\n".join(kw_lines)
        + "\n\n오늘의 주요 시장 지표 변동(전일 대비):\n"
        + "\n".join(mk_lines)
    )


def call_llm(keywords: list, markets: list) -> str:
    client = load_openai_client()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(keywords, markets)},
        ],
        temperature=0.4,
        max_tokens=500,
    )
    return response.choices[0].message.content.strip()


def get_summary_cached(keywords: list, markets: list) -> tuple[str, bool]:
    now = datetime.now(timezone.utc)
    cached_text = _summary_cache["text"]
    fetched_at = _summary_cache["fetchedAt"]

    if cached_text is not None and fetched_at is not None:
        if (now - fetched_at).total_seconds() < CACHE_TTL_SECONDS:
            return cached_text, True

    text = call_llm(keywords, markets)
    _summary_cache["text"] = text
    _summary_cache["fetchedAt"] = now
    return text, False


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            keywords = payload.get("keywords", [])
            markets = payload.get("markets", [])
            text, cached = get_summary_cached(keywords, markets)
            body = json.dumps(
                {"summary": text, "cached": cached}, ensure_ascii=False
            ).encode("utf-8")
            status = 200
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            status = 500

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
