# CLAUDE.md

이 파일은 이 저장소에서 작업할 때 Claude Code(claude.ai/code)에게 제공하는 가이드입니다.

## 프로젝트 현재 상태

**배포 완료.** Next.js + Python 서버리스 함수 3개(키워드/감성, 시장 지표, AI 요약)로 구현이 끝났고, Vercel 프로덕션에 올라가 있습니다.
- 라이브: https://my-app-wheat-kappa-35.vercel.app
- 저장소: https://github.com/Saint-Argentum/daily-economy-top10

## 이 프로젝트는 무엇인가

"Daily Economy Top 10 Topics" — 연합인포맥스(Yonhap Infomax) RSS 피드에서 경제 뉴스 제목을 수집하고, 한국어 형태소 분석으로 최근 24시간 동안 가장 많이 등장한 키워드 Top 10을 추출한 뒤, 사전 기반 매칭으로 각 키워드의 감성 점수를 보여주는 단일 페이지 Next.js 앱입니다. 로그인 없음, 데이터 영구 저장 없음. **키워드 감성 점수 계산에는 LLM을 쓰지 않습니다** (사전 매칭만 사용) — 다만 이미 계산된 결과를 설명하는 AI 이슈 요약(M8)은 예외로 LLM을 씁니다. 자세한 배경은 아래 "범위 밖인 것들" 참고.

## 진실의 원천(Source of truth)

- [`PRD.md`](PRD.md)가 공식 명세이며, 다른 문서와 내용이 충돌하면 항상 PRD.md를 따릅니다.
- [`prd_lite.md`](prd_lite.md)는 PRD.md의 요약본입니다. PRD.md와 내용이 어긋나면 요약본을 신뢰하지 않습니다.
- 수집·키워드 추출·감성 점수 계산을 구현하기 전에 PRD.md의 5장("주요 기능")과 9장("개발 단위")을 먼저 읽으세요. 아래 규칙들은 "당연해 보이는" 구현으로는 조용히 어겨지기 쉬운 부분들입니다.

## 기술 스택

- **프론트엔드**: Next.js (App Router), TypeScript, Tailwind CSS
- **백엔드**: Python 서버리스 함수 3개, Vercel Python 런타임에 배포 — Node/Next.js API 라우트가 아닙니다
  - `/api/keywords.py` — 뉴스 키워드·감성 분석
  - `/api/markets.py` — 시장 지표(지수·금리·환율·원자재 16종), keywords.py와 무관한 별도 함수
  - `/api/summary.py` — AI 이슈 요약(M8). keywords.py/markets.py가 이미 수집한 데이터를 POST로 받아 요약만 생성 (뉴스·시세를 다시 수집하지 않음)
- **자연어 처리**: 형태소 분석은 `kiwipiepy` 사용 (`konlpy`/Okt는 Java 의존성 때문에 서버리스 환경에서 동작하지 않아 사용하지 않음)
- **수집**: `feedparser`(RSS) + `beautifulsoup4`(HTML 엔티티·태그 정리) + `yfinance`(시장 지표, API 키 불필요)
- **AI 요약**: `openai` 패키지, `gpt-4o-mini` 모델. `OPENAI_API_KEY`를 실제로 쓰는 유일한 지점 (아래 API 키 항목 참고)
- **DB 없음**: PostgreSQL·Supabase 등 외부 DB 없음, 파일 누적 저장도 없음. 요청마다 RSS·시장 지표를 다시 가져와 메모리에서 처리하며, 각각 30분짜리 캐시를 둡니다 (PRD의 "최소 5분" 기준 충족, 서버 부담을 더 줄이기 위해 30분으로 설정).
- **API 키**: RSS·감성사전·`yfinance`는 키가 필요 없습니다. `OPENAI_API_KEY`만 예외로 실제 앱 런타임에 쓰입니다 — 로컬은 `.env`, 배포본은 Vercel 프로젝트 환경변수에서 읽습니다 (`.env`는 배포 번들에 포함되지 않으므로 반드시 둘 다 설정 필요). `.env`에는 이 외에도 도구용 자격증명(GitHub/Supabase/Vercel)이 있으며, 이 파일은 계속 gitignore 상태로 유지되어야 합니다.

## 이 저장소의 데이터 소스

- [`econNLPdic.txt`](econNLPdic.txt) — 재무 특화 감성사전 60개 항목(KOSELF, 조수지·김흥규·양철원 2021). 79줄뿐이라 직접 읽어도 무방합니다.
- [`SentiWord_info.json`](SentiWord_info.json) (KnuSentiLex, 14,852개 항목, 약 74,000줄) — 저장소에 포함돼 있습니다. 직접 열어 읽지 말고 코드로만 로드하세요 — 불필요한 토큰 낭비를 유발합니다.
- [`stopwords.txt`](stopwords.txt) — 키워드 집계에서 제외할 불용어 52개 (지명·기사형식어·"대통령"/"전환" 같은 포괄적 명사). 개별 단어만 제외하며 기사 자체는 살아있습니다.
- [`noise_topics.txt`](noise_topics.txt) — 제목에 포함되면 **기사 전체**를 집계에서 제외하는 정치·사법 절차 용어 목록(수사권/탄핵/국회 등). "정책/금융" RSS 피드는 그대로 유지하되(PRD §5 M0), 그 안에 섞인 순수 정치 기사만 걸러내기 위함. stopwords.txt와 다른 층위이니 혼동하지 말 것.
- `기업 재무분석을 위한 한국어 감성사전 구축.pdf` — `econNLPdic.txt`의 근거가 된 KOSELF 논문. 참고용입니다.

## 놓치기 쉬운 도메인 규칙 (수집 → 분석 → 표시 전 단계에 걸침)

각 항목이 파이프라인 여러 단계에 걸쳐 있어 틀리기 쉽습니다. 재해석하지 말고 PRD.md 5장을 그대로 따르세요.

- **중복 제거**: RSS 항목은 제목이 아니라 기사 `link` 기준으로 중복 제거합니다 ("전체기사" 피드와 카테고리별 피드가 겹치기 때문).
- **날짜 필터**: `pubDate`가 **접속 시점 기준 최근 24시간 이내**인 기사만 집계합니다 (캘린더 날짜 자정 기준이 아니라 요청마다 다시 계산되는 롤링 윈도우입니다).
- **텍스트 정제 순서**: BeautifulSoup으로 HTML 엔티티를 제거하고, 앞머리 대괄호 태그(예: `[증시-마감]`)를 제거한 뒤에 형태소 분석을 돌립니다.
- **품사 필터**: NNG, NNP, VV, VA, SL만 추출합니다. 동사·형용사는 원형(어간+"다")으로 통일하고, 한 제목 안에서 같은 단어가 반복돼도 1회만 카운트합니다.
- **사전 병합**: KnuSentiLex + `econNLPdic.txt`를 병합하며, econNLPdic의 POS/NEG는 +2/−2로 환산합니다. 두 사전의 극성이 충돌하면 **econNLPdic이 우선**합니다.
- **감성어 매칭은 형태소 단위로만** 하고, 절대 문자열 부분 일치를 쓰지 않습니다 (부분 일치를 쓰면 "지지력"의 "지지"가 부정어로 오탐될 수 있습니다).
- **점수 계산**: 제목 1건의 점수 = 그 제목에서 검출된 감성어 점수들의 평균(합이 아님). 감성어가 하나도 검출되지 않은 제목은 평균 계산에서 아예 제외합니다(0점으로 넣지 않음). 키워드 점수 = 해당 키워드가 등장한 제목들의 점수 평균입니다.
- **라벨 표시**: +0.5 이상 긍정, −0.5 이하 부정, 그 사이는 중립이지만, 이는 항상 색깔 있는 숫자(빨강/파랑/회색)로만 표시하고 별도의 라벨 컬럼을 만들지 않습니다. 감성어가 전혀 검출되지 않은 키워드는 `0.00`이 아니라 `-`로 표시합니다.
- econNLPdic의 일부 단어(이상/매우/진행/수치/결국/제외/매각/처리/공격/바닥)는 여전히 PRD.md 5장에서 실제 순위를 확인한 뒤 제외 여부를 결정하기로 한 후보입니다. **"전환"은 이미 결론 남** — 실제 Top10에서 정치·사법 기사("보완수사권 폐지" 등)에 붙어 노이즈를 일으키는 게 확인돼 `stopwords.txt`에서 제외했습니다.
- 키워드 노이즈 제거는 두 층위입니다 — `stopwords.txt`(개별 단어 제외)와 `noise_topics.txt`(기사 전체 제외). 새로운 노이즈 단어를 발견하면 어느 층위 문제인지부터 판단하세요: 그 단어만 무의미하면 stopwords.txt, 기사 자체가 경제 이슈가 아니면 noise_topics.txt.

## 명시적으로 범위 밖인 것들

로그인/회원가입 없음, 기사 본문 열람 화면 없음(키워드별 기사 **목록**은 있음 — 키워드를 누르면 제목 + 원문 링크가 펼쳐짐), 경제 외 카테고리 없음, 과거 날짜·추이 조회 없음(키워드 기준. 시장 지표는 최근 1개월 그래프를 보여줌 — PRD.md M7 참고), 외부 DB나 영구 저장 없음, 기사 본문 크롤링 없음(제목만 사용). **LLM 기반 감성 판정은 여전히 하지 않습니다**(키워드 점수는 오직 사전 매칭) — **다만 LLM 기반 이슈 요약(M8)은 사용자 요청으로 추가한 명시적 예외**이며, 이미 계산된 결과를 문장으로 설명할 뿐 감성 계산 자체에는 관여하지 않습니다.

## 작업 규칙

- 모든 설명과 주석은 한국어로 작성한다.
- 새 파일은 `my-app` 폴더 안에만 만든다.
- 기술 스택은 PRD에 정한 대로 **Next.js로 고정**한다. 다른 프레임워크로 바꾸거나 마이그레이션을 제안하지 않는다. 배포는 **Vercel**을 사용한다.
- 코드를 바꾸면 반드시 무엇을 왜 바꿨는지 한 줄로 알려준다.
- `.env` 등 비밀 정보 파일과 `node_modules` 폴더는 `.gitignore`에 등록해 두고, 절대 커밋하지 않는다.
- 외부 서비스 인증이 필요하면 토큰 값을 사용자에게 묻거나 채팅에 출력하지 말고, `.env`에 있는 값을 읽어서 사용한다.
  - 예: Supabase를 쓸 상황이 생기면 Supabase CLI를 설치해 `.env`의 `SUPABASE_ACCESS_TOKEN`으로 작업한다.
  - 예: Vercel 작업(배포 등)이 필요하면 Vercel CLI를 설치해 `.env`의 `VERCEL_TOKEN`으로 인증해 작업한다.
- 파일을 지워야 할 때는 바로 삭제하지 말고, `trash-can` 폴더를 만들어 그 안으로 옮겨만 둔다. 작업이 끝난 뒤 사용자가 직접 확인하고 삭제한다.
- 이미 설치된 서브에이전트는 필요할 때마다 적극 활용한다.

<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
