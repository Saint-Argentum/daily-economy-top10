"use client";

import { Fragment, useEffect, useState } from "react";

// PLAN.md 16~19단계 + 키워드 클릭 시 기사 목록 펼치기 + 순위 변동 + 주요 시장 지표.
//   16단계: Top 10 표 렌더링
//   17단계: 감성 점수 색상 (긍정=빨강 / 부정=파랑 / 중립=회색, 미검출은 "-")
//   18단계: 표 오른쪽 위 전체 평균값, 표 아래 판정 기준 범례
//   19단계: 화면 상단 집계 기준 표시
//   순위 변동(▲▼): api/keywords.py의 rankChange 그대로 표시
//   주요 시장 지표: api/markets.py(yfinance)에서 지수·금리·환율 8종 + 1개월 그래프

type Article = {
  title: string;
  link: string;
};

type RankChange = "up" | "down" | "same" | "new";

type Keyword = {
  rank: number;
  keyword: string;
  articleCount: number;
  sentimentScore: number | null;
  rankChange: RankChange;
  articles: Article[];
};

type KeywordsResponse = {
  windowStart: string;
  windowEnd: string;
  updatedAt: string;
  articleCount: number;
  feedStatus: { total: number; ok: number };
  averageScore: number | null;
  keywords: Keyword[];
};

type MarketPoint = { date: string; value: number };

type MarketSeries = {
  key: string;
  name: string;
  unit: string;
  ok: boolean;
  error: string | null;
  latest: number | null;
  change: number | null;
  changeUnit: string | null;
  yahooUrl: string;
  history: MarketPoint[];
};

type MarketsResponse = {
  updatedAt: string;
  series: MarketSeries[];
};

// 감성어가 하나도 검출되지 않은 키워드는 0.00이 아니라 "-"로 표시한다 (PRD.md §5 M2).
function formatScore(score: number | null): string {
  if (score === null) return "-";
  return score >= 0 ? `+${score.toFixed(2)}` : score.toFixed(2);
}

// 판정 기준은 +0.5 이상 긍정 / -0.5 이하 부정 / 그 사이 중립.
// 라벨 컬럼을 따로 만들지 않고 점수 숫자에 색만 입힌다 (PRD.md §5 M2).
// 색상은 국내 증시 관례를 따라 긍정=빨강, 부정=파랑.
function scoreColor(score: number | null): string {
  if (score === null) return "text-zinc-400";
  if (score >= 0.5) return "text-[#D32F2F]";
  if (score <= -0.5) return "text-[#1565C0]";
  return "text-[#757575]";
}

// 직전 RSS 수집 시점 대비 순위 변동을 화살표로 보여준다.
function RankChangeIcon({ change }: { change: RankChange }) {
  if (change === "up") return <span className="text-[#D32F2F]">▲</span>;
  if (change === "down") return <span className="text-[#1565C0]">▼</span>;
  if (change === "new") return <span className="text-xs text-zinc-400">NEW</span>;
  return <span className="text-zinc-300">–</span>;
}

// 최근 1개월 추이를 보여주는 간단한 SVG 선 그래프 (별도 차트 라이브러리 없이 직접 그림).
function Sparkline({ history }: { history: MarketPoint[] }) {
  if (history.length < 2) return <p className="text-xs text-zinc-400">데이터 부족</p>;

  const values = history.map((h) => h.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const width = 200;
  const height = 48;

  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * width;
      const y = height - ((v - min) / range) * height;
      return `${x},${y}`;
    })
    .join(" ");

  // 국내 증시 관례: 최근값이 한 달 전보다 오르면 빨강, 내리면 파랑.
  const rising = values[values.length - 1] >= values[0];
  const color = rising ? "#D32F2F" : "#1565C0";

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-12 w-full" preserveAspectRatio="none">
      <polyline points={points} fill="none" stroke={color} strokeWidth="2" />
    </svg>
  );
}

// 전일 대비 변동 — 금리는 bp, 그 외는 pt/원 그대로. 국내 증시 관례대로 상승=빨강/하락=파랑.
function formatChange(change: number | null, unit: string | null): { text: string; color: string } {
  if (change === null || unit === null) return { text: "", color: "text-zinc-400" };
  if (change === 0) return { text: `0${unit}`, color: "text-zinc-500" };
  const sign = change > 0 ? "+" : "";
  return {
    text: `${sign}${change}${unit}`,
    color: change > 0 ? "text-[#D32F2F]" : "text-[#1565C0]",
  };
}

// Top10 키워드와 시장 지표를 느슨하게 연결하는 표. 완전 자동 매칭이 아니라 자주 쓰이는
// 별칭을 미리 정해둔 것이라, 실제 키워드가 이 목록에 없으면 하이라이트되지 않는다.
const MARKET_KEYWORD_ALIASES: Record<string, string[]> = {
  kospi: ["코스피", "증시", "코스피지수"],
  kosdaq: ["코스닥"],
  nasdaq: ["나스닥"],
  eurostoxx50: ["유럽", "유로존", "유로증시"],
  sox: ["반도체", "필라델피아", "SOX"],
  vix: ["변동성", "VIX"],
  ust10y: ["금리", "국채", "채권", "미국채", "국채금리"],
  dxy: ["달러인덱스", "DXY"],
  wti: ["유가", "원유", "WTI"],
  brent: ["브렌트유", "브렌트"],
  usdkrw: ["환율", "달러", "원달러", "원화"],
  usdjpy: ["엔화", "엔달러", "엔저", "엔고"],
  eurusd: ["유로", "유로달러"],
  usdcny: ["위안화", "위안"],
};

function findMatchedKeyword(seriesKey: string, keywords: Keyword[]): string | null {
  const aliases = MARKET_KEYWORD_ALIASES[seriesKey] ?? [];
  const hit = keywords.find((kw) => aliases.includes(kw.keyword));
  return hit ? hit.keyword : null;
}

function MarketCard({ series, matchedKeyword }: { series: MarketSeries; matchedKeyword: string | null }) {
  const change = formatChange(series.change, series.changeUnit);
  return (
    <a
      href={series.yahooUrl}
      target="_blank"
      rel="noopener noreferrer"
      className={`block rounded border bg-white p-3 hover:shadow-sm ${
        matchedKeyword
          ? "border-[#D32F2F] ring-1 ring-[#D32F2F]/30"
          : "border-zinc-200 hover:border-[#1a2b4c]"
      }`}
    >
      <div className="flex items-center justify-between">
        <p className="text-xs text-zinc-500">{series.name}</p>
        {matchedKeyword && (
          <span className="rounded-full bg-[#D32F2F]/10 px-1.5 py-0.5 text-[10px] font-medium text-[#D32F2F]">
            Top10 &ldquo;{matchedKeyword}&rdquo;
          </span>
        )}
      </div>
      {series.ok ? (
        <>
          <p className="mt-1 text-lg font-semibold tabular-nums text-zinc-900">
            {series.latest?.toLocaleString(undefined, { maximumFractionDigits: 2 })}
            <span className="ml-1 text-xs font-normal text-zinc-400">{series.unit}</span>
          </p>
          <p className={`text-xs font-medium tabular-nums ${change.color}`}>{change.text}</p>
          <div className="mt-2">
            <Sparkline history={series.history} />
          </div>
        </>
      ) : (
        <p className="mt-2 text-xs text-zinc-400">불러오기 실패</p>
      )}
    </a>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function Home() {
  const [data, setData] = useState<KeywordsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [markets, setMarkets] = useState<MarketsResponse | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);

  useEffect(() => {
    fetch("/api/keywords")
      .then((res) => res.json())
      .then(setData)
      .catch((err) => setError(String(err)));
    fetch("/api/markets")
      .then((res) => res.json())
      .then(setMarkets)
      .catch(() => {}); // 시장 데이터는 부가 정보라 실패해도 키워드 화면은 그대로 보여준다
  }, []);

  // 키워드·시장 데이터가 둘 다 준비되면 그걸 그대로 넘겨서 AI 요약을 요청한다.
  // (요약 함수가 뉴스/시세를 다시 수집하지 않도록 프론트가 받은 데이터를 재사용)
  useEffect(() => {
    if (!data || !markets) return;
    setSummaryLoading(true);
    fetch("/api/summary", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keywords: data.keywords, markets: markets.series }),
    })
      .then((res) => res.json())
      .then((json) => setSummary(json.summary ?? null))
      .catch(() => setSummary(null))
      .finally(() => setSummaryLoading(false));
  }, [data, markets]);

  return (
    <div className="min-h-screen bg-zinc-50 p-8 font-sans text-zinc-900">
      <main className="mx-auto w-full max-w-[1200px]">
        <h1 className="text-2xl font-bold text-[#1a2b4c]">오늘의 경제 키워드 Top 10</h1>

        {error && <p className="mt-6 text-red-600">에러: {error}</p>}
        {!data && !error && <p className="mt-6 text-zinc-500">불러오는 중...</p>}

        {data && (
          <>
            {/* 19단계: 집계 기준 — 숫자를 믿을 수 있게 근거를 같이 보여준다 */}
            <p className="mt-2 text-sm text-zinc-500">
              최근 24시간 ({formatTime(data.windowStart)} ~ {formatTime(data.windowEnd)}) · 집계
              기사 {data.articleCount}건 · {data.feedStatus.total}개 중 {data.feedStatus.ok}개 피드
              수집 · 갱신 {formatTime(data.updatedAt)}
            </p>

            {/* 오늘의 경제 이슈 AI 요약 — Top10 키워드 + 시장 지표를 근거로 LLM이 작성.
                감성 점수 계산에는 관여하지 않고, 이미 계산된 결과를 설명만 해준다. */}
            <section className="mt-4 rounded-lg border border-[#1a2b4c]/10 bg-[#1a2b4c]/5 p-4">
              <h2 className="text-sm font-bold text-[#1a2b4c]">오늘의 경제 이슈 요약</h2>
              {summaryLoading && <p className="mt-1 text-sm text-zinc-500">요약 작성 중...</p>}
              {!summaryLoading && summary && (
                <p className="mt-1 text-sm leading-relaxed text-zinc-700">{summary}</p>
              )}
              {!summaryLoading && !summary && (
                <p className="mt-1 text-xs text-zinc-400">요약을 불러오지 못했습니다.</p>
              )}
              <p className="mt-2 text-[10px] text-zinc-400">
                AI가 오늘의 키워드·시장 지표를 바탕으로 자동 생성한 요약이며, 투자 조언이 아닙니다.
              </p>
            </section>

            {/* 18단계: 표 오른쪽 위 전체 평균값 */}
            <div className="mt-6 flex items-end justify-end">
              <span className="text-sm text-zinc-600">
                평균값:{" "}
                <span className={`font-semibold ${scoreColor(data.averageScore)}`}>
                  {formatScore(data.averageScore)}
                </span>
              </span>
            </div>

            <table className="mt-2 w-full border-collapse text-left">
              <thead>
                <tr className="border-b-2 border-[#1a2b4c] text-sm text-[#1a2b4c]">
                  <th className="px-4 py-3 font-semibold">순위</th>
                  <th className="px-4 py-3 font-semibold">키워드</th>
                  <th className="px-4 py-3 text-right font-semibold">등장 기사 수</th>
                  <th className="px-4 py-3 text-right font-semibold">감성 점수</th>
                </tr>
              </thead>
              <tbody>
                {data.keywords.map((kw) => {
                  const isOpen = expanded === kw.keyword;
                  return (
                    <Fragment key={kw.keyword}>
                      <tr
                        onClick={() => setExpanded(isOpen ? null : kw.keyword)}
                        className="cursor-pointer border-b border-zinc-200 hover:bg-zinc-100"
                      >
                        <td className="px-4 py-3 tabular-nums text-zinc-500">
                          <span className="mr-1.5 inline-block w-4 text-center text-xs">
                            <RankChangeIcon change={kw.rankChange} />
                          </span>
                          {kw.rank}
                        </td>
                        <td className="px-4 py-3 font-medium">
                          <span className="mr-2 inline-block w-3 text-zinc-400">
                            {isOpen ? "▾" : "▸"}
                          </span>
                          {kw.keyword}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums">{kw.articleCount}</td>
                        <td
                          className={`px-4 py-3 text-right font-semibold tabular-nums ${scoreColor(kw.sentimentScore)}`}
                        >
                          {formatScore(kw.sentimentScore)}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="border-b border-zinc-200">
                          <td colSpan={4} className="bg-zinc-100 px-4 py-3">
                            <ul className="space-y-2">
                              {kw.articles.map((a) => (
                                <li key={a.link} className="text-sm">
                                  <a
                                    href={a.link}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-[#1a2b4c] hover:underline"
                                  >
                                    {a.title}
                                  </a>
                                </li>
                              ))}
                            </ul>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>

            {/* 18단계: 표 아래 판정 기준 범례 */}
            <p className="mt-3 text-xs text-zinc-500">
              +0.5 이상 긍정 · -0.5 이하 부정 · 그 사이 중립 · <code>-</code>는 감성어 미검출 ·
              ▲▼는 직전 수집 대비 순위 변동, NEW는 새로 진입 · 키워드를 누르면 해당 기사 목록이
              펼쳐집니다
            </p>

            {/* 주요 시장 지표 — 지수·금리·환율 8종, 최근 1개월 추이 (yfinance) */}
            {markets && (
              <section className="mt-10">
                <h2 className="text-lg font-bold text-[#1a2b4c]">주요 시장 지표</h2>
                <p className="mt-1 text-xs text-zinc-400">
                  최근 1개월 · 갱신 {formatTime(markets.updatedAt)} · 출처 Yahoo Finance
                </p>
                <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {markets.series.map((s) => (
                    <MarketCard
                      key={s.key}
                      series={s}
                      matchedKeyword={findMatchedKeyword(s.key, data.keywords)}
                    />
                  ))}
                </div>
                <p className="mt-2 text-xs text-zinc-400">
                  빨간 테두리 = 오늘 Top10 키워드와 관련된 지표
                </p>
              </section>
            )}

            {/* 22단계: 출처 표기 — 두 감성사전 모두 라이선스상 출처 명시가 필수다 (PRD.md §7) */}
            <footer className="mt-10 border-t border-zinc-200 pt-4 text-xs leading-relaxed text-zinc-400">
              <p>
                기사 출처:{" "}
                <a
                  href="https://news.einfomax.co.kr"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:underline"
                >
                  연합인포맥스
                </a>{" "}
                (제목·링크만 사용)
              </p>
              <p>
                감성사전: KnuSentiLex (KNU 한국어 감성사전,{" "}
                <a
                  href="https://github.com/park1200656/KnuSentiLex"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:underline"
                >
                  github.com/park1200656/KnuSentiLex
                </a>
                ) · KOSELF (조수지·김흥규·양철원, 2021, &ldquo;기업 재무분석을 위한 한국어 감성사전
                구축&rdquo;, 한국증권학회지 50(2), 135-170)
              </p>
            </footer>
          </>
        )}
      </main>
    </div>
  );
}
