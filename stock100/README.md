# 주식 공부 100일 챌린지 자동화 (stock100)

『주식 공부 100일 챌린지』의 매일 루틴을 자동으로 수행합니다.
100일을 마친 상태(`mode: full`)에서는 아래 과정을 **매일 전부** 돌립니다.

| 책의 단계 | 자동으로 하는 일 | 언제 |
|---|---|---|
| Day 4~40 복습 | 상한가·거래량 천만 주 종목을 찾고, 기사로 **사유**를 추정하고, 키워드·연관 종목·공시·최대주주·재무·차트를 **종목 노트**(에버노트 대신 Markdown)에 정리 | 평일 18:30 |
| Day 41~50 예습 | 새벽 뉴스 헤드라인 수집 | 평일 04:00 |
| Day 51~60 추정 | 새벽 뉴스 ↔ 복습 지식 연결: 종목명 직접 언급(3점), **연관 종목** 뉴스(2~3점, 예: 에스엠랩 → DSC인베스트먼트), 과거 상승 사유 키워드(1점), 주력 분야(+0.5) → 시그널과 추정 기록 | 04:00 |
| Day 61~70 관찰·기록 | 시그널 후 D+1/3/5 수익률과 최고 수익률을 추적해 적중 여부와 누적 적중률 기록 | 18:30 |
| Day 71~99 소액 매매 | 시그널 1위 종목을 **모의** 매매: 시가 매수 → +3% 익절(욕심 = 매도 시그널) / −3% 손절 / 종가 청산. 수량은 1주 → 10주 → 100만 원 → 500만 원 | 18:30 |
| Day 100 | `status` 명령으로 누적 성과 확인 | 수시 |
| **장중 5분 감시** | 두 그룹을 동시에 감시해 비교: ① 08:55/08:59 예상체결 등락률 상위 100, ② 최근 10거래일 거래대금 100위 이내 + 15%↑. 5분 거래대금 +5억 & 등락률 +1.5%p면 🔴 주목, 그룹 안 거래대금 순위까지 오르면 **보유금 20% 매수 제안**(모의). 신호 후 30분·1시간·종가 수익률을 기록해 '성과비교' 시트로 두 방식 비교 | 08:50~15:30 (`watch`) |
| **테마 분석 엑셀** | 거래대금 100억↑ & 등락률 10%↑ 종목의 뉴스를 모아 테마로 묶고(신규 형성·확산·지속·축소·재부각), 대장주와 20일 테마 흐름을 **엑셀**로 저장 | 18:30 (또는 `theme` 명령) |

> 실제 주문은 내지 않습니다. 모의매매 결과로 충분히 검증한 뒤 증권사 API(키움 등) 연동을 검토하세요.

## 설치

```bash
cd stock100
pip install -r requirements.txt
copy config.example.yaml config.yaml      # (mac/linux: cp)
```

`config.yaml`에서 `focus_sectors`(Day 3 주력 분야)와 API 키를 설정합니다. 키는 환경변수로 넣어도 됩니다.

| 키 | 용도 | 없으면 |
|---|---|---|
| `KIWOOM_APPKEY` / `KIWOOM_SECRETKEY` | 키움 REST API — 오늘의 거래대금·등락률·거래량 순위와 일봉을 정확하게 수집 (조회만, 주문 없음) | KRX/네이버 사용 |
| `KRX_ID` / `KRX_PW` | KRX 정보데이터시스템 로그인(pykrx) — 과거 날짜 전종목 시세 | 오늘 데이터만 네이버 금융에서 수집, `backfill` 불가 |
| `DART_API_KEY` | OpenDART — 최대주주 지분, 최근 공시, 재무 추이 | 노트에서 해당 항목 생략 |
| `NAVER_CLIENT_ID` / `SECRET` | 네이버 검색 API 뉴스 | 구글 뉴스 RSS만 사용 |

- 키움 REST API: https://openapi.kiwoom.com (키움 계좌 필요 → API 사용 신청 → 앱키/시크릿키, 접속 IP 등록). 연결 점검: `python -m stock100 kiwoom`
- KRX 회원가입: https://data.krx.co.kr
- OpenDART 키 발급: https://opendart.fss.or.kr
- 네이버 API: https://developers.naver.com (검색 API)

## 사용법

```bash
python -m stock100 demo                 # 네트워크 없이 가짜 데이터로 전체 흐름 시연 → data_demo/
python -m stock100 backfill --days 100  # 과거 100 거래일 복습을 한 번에 (지식 DB 구축, KRX 로그인 필요)
python -m stock100 morning              # 새벽 예습 → data/reports/YYYY-MM-DD_morning.md
python -m stock100 evening              # 복습 + 추적 + 모의매매 → data/reports/YYYY-MM-DD_evening.md
python -m stock100 theme                # 오늘 거래대금 상위 + 고등락률 종목 테마 분석 → 엑셀
python -m stock100 theme --value 300 --pct 15 --date 20261030   # 기준과 날짜 지정
python -m stock100 watch                # 장중 5분 감시 (키움 필요). --list 대상만, --once 1회, --demo 가짜 장
python -m stock100 status               # 단계, 누적 적중률, 모의 손익
python -m stock100 update               # GitHub 최신 코드로 업데이트 (config.yaml·data 유지, scripts\update.bat 더블클릭도 가능)
```

처음에는 `backfill --days 100`으로 "100일치 복습"을 먼저 채우는 것을 권장합니다. 그래야 다음 날 새벽부터 예습 시그널이 나옵니다.

### 업데이트

`python -m stock100 update` 한 번이면 GitHub `main`의 최신 코드로 바뀝니다. 바뀐 파일만 덮어쓰고 이전 파일은 `backup/날짜시각/`에 남깁니다. `config.yaml`과 `data` 폴더는 건드리지 않고, `requirements.txt`가 바뀌면 패키지도 설치합니다. `--check`는 새 버전이 있는지만 봅니다.

### 매일 자동 실행

- Windows: (Python이 여러 개면 먼저 `setx PY_PYTHON 3.14`처럼 기본 버전 지정) `scripts\install_schedule_windows.bat` 실행 → 작업 스케줄러에 평일 04:00 / 08:50 / 18:30 등록. PC가 켜져 있어야 합니다.
- Linux/mac: `scripts/crontab.example` 참고

### 챌린지 모드

책처럼 단계별로 기능을 하나씩 켜려면 `mode: challenge`와 `challenge_start`를 설정하세요. Day 1~3은 안내만 하고, Day 4부터 복습, 41부터 예습, 51부터 추적, 71부터 모의매매를 켭니다.

## 결과물 (`data/`)

- `notes/코드_종목명.md` — 종목 노트: 요약, 이벤트(왜 올랐나), 차트, 재무 추이, 공시, 시그널 기록, 관련 기사. **`## 내 메모` 아래에 쓴 내용은 자동 갱신 때도 보존됩니다.**
- `charts/코드.png` — 1년 일봉, 이동평균선(5/20/60/120), 거래량. 상한가·천만주 날짜는 노란 세로선으로 표시
- `reports/` — 새벽(morning)·저녁(evening) 리포트
- `watch/날짜_실시간감시.xlsx` — 시트: 성과비교(오늘·누적) / 실시간_①예상체결 / 실시간_②거래대금이력(빨간 음영=주목, 진한 빨강=매수) / 신호성과 / 신호기록 / 5분대금흐름 / 감시대상 / 기준. Excel에서 열어 둔 상태면 `_최신.xlsx`로 저장
- `excel/날짜_테마분석.xlsx` — 시트: 테마요약(상태·연속일·대장주·대표 뉴스) / 종목상세 / 뉴스(링크) / 테마흐름(20일) / 기준
- `kb.sqlite` — 지식 DB (stocks, events, news, keywords, relations, signals, paper_trades)

Markdown 노트는 Obsidian이나 VS Code에서 열면 차트 이미지까지 함께 보입니다.

## 한계

- 사유는 기사 제목에서 추정합니다. 기사가 없거나 엉뚱한 경우가 있으니 노트를 직접 확인하세요. 책에서도 매매의 '감'은 직접 쌓아야 한다고 강조합니다.
- 네이버 금융 페이지 구조가 바뀌면 오늘 데이터 수집(`NaverSource`)이 깨질 수 있습니다. KRX 로그인을 설정하면 이 경로는 쓰지 않습니다.
- 모의매매는 일봉만 사용합니다. 같은 날 고가와 저가 중 무엇이 먼저였는지 모르므로 손절을 먼저 가정합니다.
