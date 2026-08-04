"""
/api/markets — 주요 시장 지표(지수·금리·환율)를 최근 1개월 추이와 함께 반환하는
서버리스 함수. keywords.py(뉴스 키워드/감성)와는 완전히 별개 기능이라 파일을 분리했다.

야후 파이낸스(yfinance)에서 직접 확인한 결과, 요청받은 종목 중 아래는 확보하지 못했다:
  - 미국채 1년물: 현물·선물 어디에도 없음
  - 미국채 2년물: 현물 지수 없음 (선물 2YY=F로 대체 가능하지만, 사용자 요청으로 제외)
  - 한국 국채 1년/2년/10년물: 야후는 한국 국채 "수익률(%)" 자체를 취급하지 않음
    (국채 ETF 가격은 있지만 수익률이 아니라서 다른 지표이므로 제외)
그래서 실제로 안정적인 히스토리가 확인된 것만 담는다 (지수 5·원자재 2·달러 관련 3·환율 4).

사용자가 공유한 "아침 시황 체크리스트"(미국 금리 → DXY → USD/KRW → S&P500/Nasdaq → SOX →
원자재 순으로 해석) 기준으로 나스닥·SOX·DXY·WTI·브렌트유·USD/JPY·EUR/USD·KOSDAQ을 추가했다.
이 지표들은 summary.py가 "오늘의 매크로 이슈"를 요약할 때도 함께 참고한다.

주의: yfinance는 야후 파이낸스 비공식 라이브러리라 API 키는 필요 없지만, 야후 쪽
사정으로 언제든 응답 형식이 바뀌거나 막힐 수 있다. 배포 후에도 계속 확인이 필요하다.
"""

from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import json

import yfinance as yf

KST = timezone(timedelta(hours=9))

YAHOO_QUOTE_URL = "https://finance.yahoo.com/quote/{}"

# key, 표시 이름, 야후 티커, 단위, 값 변환 함수(없으면 그대로)
SERIES = [
    {"key": "kospi", "name": "KOSPI", "ticker": "^KS11", "unit": "pt"},
    {"key": "kosdaq", "name": "KOSDAQ", "ticker": "^KQ11", "unit": "pt"},
    {"key": "nasdaq", "name": "나스닥", "ticker": "^IXIC", "unit": "pt"},
    {"key": "eurostoxx50", "name": "유로스탁스50", "ticker": "^STOXX50E", "unit": "pt"},
    {"key": "sox", "name": "필라델피아반도체(SOX)", "ticker": "^SOX", "unit": "pt"},
    {"key": "vix", "name": "VIX", "ticker": "^VIX", "unit": "pt"},
    {"key": "ust10y", "name": "미국채 10년물", "ticker": "^TNX", "unit": "%"},
    {"key": "dxy", "name": "달러인덱스(DXY)", "ticker": "DX-Y.NYB", "unit": "pt"},
    {"key": "wti", "name": "WTI유", "ticker": "CL=F", "unit": "달러"},
    {"key": "brent", "name": "브렌트유", "ticker": "BZ=F", "unit": "달러"},
    {"key": "usdkrw", "name": "원/달러", "ticker": "KRW=X", "unit": "원"},
    {"key": "usdjpy", "name": "달러/엔", "ticker": "JPY=X", "unit": "엔"},
    {"key": "eurusd", "name": "유로/달러", "ticker": "EURUSD=X", "unit": "달러"},
    {"key": "jpykrw", "name": "엔/원 (100엔)", "ticker": "JPYKRW=X", "unit": "원", "multiply": 100},
    {"key": "eurkrw", "name": "유로/원", "ticker": "EURKRW=X", "unit": "원"},
    # 위안/원 직접 크로스(CNYKRW=X)는 야후에 하루치만 있어서, 데이터가 꽉 찬
    # KRWCNY=X(원 기준 위안화)를 가져와 역수를 취해 위안/원으로 바꾼다. 다만 클릭했을 때
    # 이동할 야후 페이지는 우리가 표시하는 방향과 같은 CNYKRW=X로 보낸다 — KRWCNY=X 페이지를
    # 보여주면 0.0047처럼 우리 화면 숫자(212원)와 안 맞아 보여서 헷갈린다.
    {
        "key": "cnykrw",
        "name": "위안/원",
        "ticker": "KRWCNY=X",
        "linkTicker": "CNYKRW=X",
        "unit": "원",
        "invert": True,
    },
]

CACHE_TTL_SECONDS = 30 * 60
_market_cache: dict = {"data": None, "fetchedAt": None}


def _fetch_one_series(series: dict) -> dict:
    yahoo_url = YAHOO_QUOTE_URL.format(quote(series.get("linkTicker", series["ticker"])))
    try:
        hist = yf.Ticker(series["ticker"]).history(period="1mo")
        closes = hist["Close"].dropna()
        if closes.empty:
            return {
                **series,
                "ok": False,
                "error": "데이터 없음",
                "history": [],
                "latest": None,
                "change": None,
                "changeUnit": None,
                "yahooUrl": yahoo_url,
            }

        values = closes
        if series.get("invert"):
            values = 1 / values
        if series.get("multiply"):
            values = values * series["multiply"]

        history = [
            {"date": idx.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
            for idx, v in values.items()
        ]

        # 전일 대비 변동 — 금리(%)는 bp(1bp=0.01%p)로, 그 외(지수·환율)는 표시 단위 그대로.
        change = None
        change_unit = series["unit"]
        if len(history) >= 2:
            diff = history[-1]["value"] - history[-2]["value"]
            if series["unit"] == "%":
                change = round(diff * 100, 1)
                change_unit = "bp"
            else:
                change = round(diff, 2)

        return {
            "key": series["key"],
            "name": series["name"],
            "unit": series["unit"],
            "ok": True,
            "error": None,
            "latest": history[-1]["value"],
            "change": change,
            "changeUnit": change_unit,
            "yahooUrl": yahoo_url,
            "history": history,
        }
    except Exception as exc:  # noqa: BLE001 - 야후 쪽 장애도 화면에 그대로 보여준다
        return {
            "key": series["key"],
            "name": series["name"],
            "unit": series["unit"],
            "ok": False,
            "error": str(exc),
            "latest": None,
            "change": None,
            "changeUnit": None,
            "yahooUrl": yahoo_url,
            "history": [],
        }


def fetch_all_series() -> list[dict]:
    results = []
    with ThreadPoolExecutor(max_workers=len(SERIES)) as pool:
        futures = [pool.submit(_fetch_one_series, s) for s in SERIES]
        for future in as_completed(futures):
            results.append(future.result())
    order = {s["key"]: i for i, s in enumerate(SERIES)}
    results.sort(key=lambda r: order[r["key"]])
    return results


def fetch_all_series_cached() -> tuple[list[dict], datetime]:
    now = datetime.now(timezone.utc)
    cached = _market_cache["data"]
    fetched_at = _market_cache["fetchedAt"]

    if cached is not None and fetched_at is not None:
        if (now - fetched_at).total_seconds() < CACHE_TTL_SECONDS:
            return cached, fetched_at

    results = fetch_all_series()
    _market_cache["data"] = results
    _market_cache["fetchedAt"] = now
    return results, now


def build_response() -> dict:
    series, fetched_at = fetch_all_series_cached()
    return {
        "updatedAt": fetched_at.astimezone(KST).isoformat(),
        "series": series,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = json.dumps(build_response(), ensure_ascii=False).encode("utf-8")
            status = 200
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            status = 500

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
