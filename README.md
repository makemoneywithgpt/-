# 바이낸스 USDT-M 선물 시장 데이터 수집기

Python 3.12 이상. BTCUSDT·ETHUSDT의 **USDT-M 선물** 1시간봉을 공식 무료 공개 데이터에서 수집합니다. 기본 범위는 2023-01-01 00:00 UTC부터 실행 시점의 마지막 완료 봉까지입니다. 주문 기능·계좌 접근·API 키는 없습니다.

## 1. 설치 (Windows PowerShell)

Python 3.12 이상을 설치할 때 `Add Python to PATH`를 선택하세요. 이 README가 있는 폴더에서 PowerShell을 열고 아래 명령을 차례로 실행합니다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

가상환경 활성화 없이 실행하므로 PowerShell 실행 정책 변경이 필요 없습니다.
Linux/macOS에서는 `python3 -m venv .venv`를 실행한 뒤, 아래 명령들의 `.\.venv\Scripts\python.exe`를 `.venv/bin/python`으로 바꾸세요.

## 2. 수집

```powershell
.\.venv\Scripts\python.exe -m market_data.cli collect
```

처음에는 전체 기간을 수집하므로 시간이 걸립니다. 다시 실행하면 기존 파일의 누락된 시간만 보충합니다. 아카이브는 월/일 단위 파일이라 일부 시간만 필요해도 해당 파일을 내려받지만, 저장 시에는 누락된 시간만 추가합니다.

먼저 하루만 확인하려면:

```powershell
.\.venv\Scripts\python.exe -m market_data.cli collect --symbols BTCUSDT --start 2023-01-01 --end 2023-01-02
```

`--end`는 **포함하지 않는** UTC 경계입니다. 시간 단위 경계만 허용하며 미래 시간과 아직 진행 중인 봉은 제외합니다. 시간대 없는 입력은 UTC로 해석합니다. `--data-dir D:\market-data`로 저장 위치를 바꿀 수 있습니다.

## 3. 검증·4시간봉·자동 테스트

```powershell
.\.venv\Scripts\python.exe -m market_data.cli validate
.\.venv\Scripts\python.exe -m market_data.cli resample
.\.venv\Scripts\python.exe -m pytest -q
```

하루만 수집했다면 `validate`에도 같은 `--symbols`, `--start`, `--end`를 지정하세요. 그렇지 않으면 기본 전체 기간을 검사하므로 누락으로 보고됩니다.

수집 완료 시에도 자동 검증과 4시간봉 생성을 수행합니다. 4시간봉은 UTC 00·04·08·12·16·20시를 시작점으로 하고, 1시간봉 4개가 모두 있는 구간만 만듭니다. 가격은 first/max/min/last, 거래량·거래대금·체결 수는 합산합니다. 검증 또는 수집 실패 시 종료 코드는 1입니다.

## 저장 데이터

```text
data/klines/BTCUSDT/1h.parquet
 data/klines/BTCUSDT/4h.parquet
 data/klines/ETHUSDT/1h.parquet
 data/klines/ETHUSDT/4h.parquet
logs/market-data.log
```

위 경로의 앞 공백은 표시용입니다. Parquet의 `open_time`은 UTC 시간대가 있는 봉 시작 시각입니다. 가격·수량은 연구용 float64 정밀도이며 회계·주문 계산 용도가 아닙니다.

| 필드 | 의미 |
|---|---|
| open_time | UTC 봉 시작 시각 |
| open, high, low, close | USDT 기준 가격 |
| volume | 기초자산 거래량 (BTC 또는 ETH) |
| quote_volume | USDT 거래대금 |
| trades | 체결 수 |
| taker_buy_volume | 테이커 매수 기초자산 거래량 |
| taker_buy_quote_volume | 테이커 매수 USDT 거래대금 |

Pandas로 읽기: `pd.read_parquet("data/klines/BTCUSDT/1h.parquet")`.

## 데이터 품질과 장애 처리

- UTC 시간 정렬, 중복 시각, 시간 누락, 비정상 OHLC 관계, 음수·NaN·무한대, 비정수 체결 수, 테이커 수량 초과를 검사합니다.
- 기존 중복은 로그를 남기고 시각별 마지막 행을 유지합니다. 값이 비정상인 행은 조용히 수정하지 않고 오류로 중단합니다. 원본 파일을 백업·조사한 뒤 해당 데이터를 제거하고 재수집하세요.
- 월별 체크포인트를 임시 파일에 쓴 후 원자적으로 교체합니다. 실패해도 이전 완료 월의 데이터는 남습니다. 같은 데이터 폴더/심볼에는 **한 프로세스만** 실행하세요 (동시 쓰기 잠금은 미구현).
- 네트워크 오류·타임아웃·HTTP 418/429/5xx는 최대 5회 재시도합니다. 지수 지연과 Retry-After를 사용하며 단일 대기는 최대 300초입니다. 401/403/451 등 권한·지역 차단은 재시도하지 않습니다.
- 공식 ZIP의 SHA-256 `.CHECKSUM`을 확인합니다. 체크섬/파싱 오류는 중단하고 로그를 남깁니다. 검증을 끄지 않습니다.
- 아카이브에 없는 최신 구간과 남은 누락은 REST로 보충합니다. 거래소 자체의 누락은 임의 보간하지 않고 실패로 보고합니다.
- 로그는 `logs/market-data.log`에 누적됩니다. 장기 운영에서는 별도 보관/삭제 정책이 필요합니다.

## 공식 출처와 네트워크

- 아카이브: https://data.binance.vision/?prefix=data/futures/um/
- 공식 데이터 설명: https://github.com/binance/binance-public-data
- 공개 API: `https://fapi.binance.com/fapi/v1/klines`
- API 문서: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data

클라우드에서는 `data.binance.vision`, `fapi.binance.com`의 HTTPS 접근이 필요합니다. API 키나 비밀번호는 설정하지 마세요. 지역에 따라 바이낸스 접근이 제한될 수 있으며 우회 기능은 제공하지 않습니다.

## 구조와 확장

```text
src/market_data/
  binance.py    # 공식 아카이브·REST, 체크섬, 재시도
  sources.py    # KlineSource 인터페이스
  core.py       # UTC, 검증, Parquet 원자 저장, 집계
  collector.py  # 누락 보충 및 체크포인트
  cli.py        # 실행 명령
 tests/test_market_data.py
```

OI·펀딩비는 각기 다른 주기·스키마가 필요하므로 향후 별도 소스/검증 모듈과 `data/open_interest/`, `data/funding/`에 추가할 수 있습니다. ML은 저장된 데이터만 읽는 별도 모듈로 확장하세요. 현재 단계에는 전략·백테스트·ML·주문 기능이 없습니다.

## 현재 검증 범위

자동 테스트는 UTC, 품질 검사, 누락 보충·재실행, Parquet, 4시간봉, 재시도, REST 페이지 순회, ZIP 체크섬을 모의 응답으로 검증합니다. 현재 클라우드에서는 바이낸스 도메인이 프록시에서 차단되어 실데이터와 전체 기간 수집을 검증하지 못했습니다. 환경 설정의 허용 도메인을 적용한 후 위 하루 수집 명령부터 실행하세요. Windows 설치 명령은 제공하지만 실제 Windows 실행은 검증하지 않았습니다.

## 로컬 백테스팅 MVP (Issue #1)

`src/backtesting/`는 수집기와 별도인 **오프라인 계산 전용** 모듈입니다. 인터넷,
API 키, 계좌 연결이나 실제 주문을 사용하지 않습니다. 전략은 검증용 SMA(20/50)
교차 예제이며, 돈을 벌 수 있다고 검증한 전략이 아닙니다.
**펀딩비 데이터가 없으므로 0으로 가정합니다. 출력 수익률은 실제 거래 수익이 아닙니다.**

### Windows에서 실행

저장소의 최신 코드를 받은 뒤, README가 있는 폴더에서 PowerShell을 여세요.
기존 `data/klines/BTCUSDT/1h.parquet`, `data/klines/ETHUSDT/1h.parquet`은 그대로 보관하세요.
가상환경이 없다면 `py -3 -m venv .venv`로 만듭니다. 설치/갱신은 다음 한 줄입니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

다음 **한 줄**을 복사해서 실행하면 두 종목을 각각 계산합니다. 종료 시각은 포함하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m backtesting.cli --data-dir data --symbols BTCUSDT ETHUSDT --start 2023-01-01 --end 2026-11-04T13:00:00Z --output-dir data/backtests
```

데이터가 다른 폴더에 있으면 `--data-dir "D:\market-data"`처럼 바꾸세요.
그 폴더 아래에 `klines/BTCUSDT/1h.parquet` 구조가 있어야 합니다.
누락이 있으면 **누락 개수와 UTC 구간을 출력하고 해당 종목 계산을 중단**합니다.
오류 구간을 임의의 가격으로 채우거나 삭제해서 통과시키지 마세요.
두 종목 중 하나가 실패해도 다른 종목은 별도로 검사/계산합니다.
종료 코드 0은 전체 성공, 1은 설정 또는 적어도 한 종목 오류입니다.
분석 시작 전의 봉은 읽어 지표를 준비하지 않습니다. 분석 구간 내 처음 50개 봉은
지표 준비에 쓰이며, 이전/현재 이동평균을 비교할 수 있는 51번째 봉부터 신호가 가능합니다.

자동 테스트 실행:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

현재 저장소의 Python 3.12 이상 요구사항을 따르며 클라우드 **Python 3.12에서 합성 데이터로 검증**했습니다.
사용자 Windows 파일을 이 환경에서 읽은 것은 아니며 전체 실제 데이터 수익률을 측정하지 않았습니다.
**Windows 및 Python 3.15 실행은 미검증**입니다. 최신 main에 있는 pandas/NumPy/PyArrow의 버전 제한은 그대로 유지했으며,
이 구현으로 Python 3.15 호환성을 실제 검증했다고 주장하지 않습니다.
3.15에서 설치가 실패하면 Python 3.12를 별도로 설치하고
`py -3.12 -m venv .venv-backtest`로 새 가상환경을 만든 뒤, 위 명령의 `.venv`를
`.venv-backtest`로 바꾸세요. 기존 Python이나 데이터 파일을 삭제할 필요는 없습니다.

### 계산과 가상 체결 규칙

- UTC 1시간봉을 정렬합니다. 요청 구간 내 중복, 빈 시간, 누락, 미완성 봉,
  NaN/무한대/음수, 0 이하 가격, 모순되는 OHLC를 거부합니다.
  수집기 열이 있으면 체결 수 정수 여부와 테이커 수량 상한도 검사합니다.
  시간대 없는 입력은 UTC로 해석합니다. `--end` 생략 시 실행 시각의 완료 봉까지 검사합니다.
- 완료된 t봉 종가의 SMA가 교차하면 신호를 만들고 **t+1봉 시가**에 가상 체결합니다.
  상승 교차는 롱, 하락 교차는 숏입니다. 교차가 없으면 새 신호가 없습니다.
  마지막 봉 신호는 다음 시가가 없으므로 실행하지 않습니다.
- 전략 인터페이스는 `Strategy.target(completed)`이며 지금까지 완료된 봉만 전달합니다.
  반환값은 `1`(롱 교차), `-1`(숏 교차), `0`(새 신호 없음)입니다.
  과거 프레임은 읽기 전용으로 취급해야 합니다. 전략은 포지션/외부 계좌에 접근하지 않습니다.
- 한 종목에 최대 1포지션입니다. 반대 신호는 다음 시가에서 기존 포지션을 청산하고 반대로
  진입합니다. 손절/시간 제한 청산 시 그 시가에 예약된 신호를 취소하며, 이후 새로운 교차가
  있어야 재진입합니다. 한 봉 안에서 청산 후 즉시 재진입하지 않습니다.
- 예제 기본 가정: **종목당 독립 초기 자본 10,000 USDT**, 수수료 편도 0.04%,
  슬리피지 편도 0.02%, 명목금액 최대 1배, 거래당 정상 손절 위험 예산 1%,
  손절 거리 2%, 익절 거리 4%, 최대 보유 72시간입니다. 이 값은 거래소의 보장 조건이 아닙니다.
- 모든 매수는 가격에 슬리피지를 더하고 모든 매도는 뺍니다. 진입과 청산 각각 체결 명목금액에
  수수료를 부과합니다. 반전에는 청산/새 진입 각각 비용이 발생합니다.
- 수량은 `min(자산×위험비율÷1단위 정상 손절손실, 자산×레버리지상한÷(진입가격×(1+수수료율)))`입니다.
  정상 손절손실에는 진입/청산 수수료와 청산 슬리피지가 포함됩니다.
  위험 비율은 최대 1, 레버리지는 **1 이하**만 허용합니다. 손절/익절은 항상 활성화됩니다.
  **갭·유동성 부족 등으로 실제 손실이 위험 예산을 넘을 수 있습니다.** 손실 보장 장치가 아닙니다.
- 기존 포지션에서 시가가 손절가보다 불리하면 **시가에 추가 슬리피지**를 적용해 청산합니다.
  이후 72시간 한도와 신호 반전을 처리합니다. 진입 후 봉의 고가/저가가 손절·익절에 모두 닿으면
  순서를 알 수 없으므로 **손절을 먼저 적용**합니다. 익절 갭의 유리한 가격 개선은 가정하지 않습니다.
  손절·익절 체결 시각은 시간봉 내부의 정확한 체결시각을 알 수 없어 해당 봉 종료로 기록합니다.
- 보유 72시간에 도달한 시가에서 강제 종료합니다 (`--max-holding-hours`로 1~72 설정 가능).
  기간 끝까지 남은 포지션은 마지막 완료 봉 종가에 비용을 적용해 종료합니다.
- 시간별 평가자산 = 현금 + 미실현 손익입니다. MDD는 초기 자본을 포함한 시간별 평가자산의
  최고점 대비 하락률입니다. 봉 내부 최대 낙폭과 같지 않습니다. 비용으로 자산이 0 이하가 되면
  새 진입을 막습니다. 선물 증거금·강제청산·호가 단위·유동성·공유 포트폴리오는 모델링하지 않습니다.
- 펀딩 비용은 기본 `ZeroFunding` 모듈로 분리했습니다. 향후 `FundingModel` 구현을 주입할 수 있으나
  실제 펀딩 데이터/정산은 미구현입니다. 사용자 모델은 포지션을 보유한 봉 종료 시 비용을 계산하며
  그 봉 내부에 이미 청산된 포지션은 호출하지 않는 인터페이스입니다.

다른 가정 예시 (수익 권장 설정이 아닙니다):

```powershell
.\.venv\Scripts\python.exe -m backtesting.cli --symbols BTCUSDT --start 2023-01-01 --end 2026-11-04T13:00:00Z --fee-rate 0.0004 --slippage 0.0002 --initial-capital 10000 --max-leverage 0.5 --risk-fraction 0.005 --stop-pct 0.02 --take-profit-pct 0.04 --max-holding-hours 72
```

### 결과 파일

성공한 종목별 `data/backtests/BTCUSDT/`, `data/backtests/ETHUSDT/`에 저장됩니다.
같은 출력 폴더로 재실행하면 그 종목의 기존 결과를 덮어쓰므로 보관하려면 `--output-dir`을 바꾸세요.

| 파일 | 내용 |
|---|---|
| `summary.json` | 기간·종목·가정·전략 설정, 거래 수, 승률(%), 순손익(USDT), 순수익률(%), MDD(%), profit factor, 평균 손익, 비용 합계, 경고 |
| `trades.csv` | 진입/청산 시간·가격, 롱/숏, 수량, 총손익, 양쪽 수수료, 펀딩비, 순손익, 보유 시간, 청산 사유 |
| `equity.csv` | 매시간 종료의 현금·미실현 손익·평가자산·포지션 방향 |
| `주의사항.txt` | 펀딩비 누락 등 결과 해석의 한계 |
| `error.json` | 실패한 종목의 오류와 요청 기간. 이전 성공 결과는 제거해 오래된 결과와 혼동을 방지 |

승률·profit factor는 **비용 차감 후 거래 손익**으로 계산합니다. 손실 거래가 0개이면
profit factor는 JSON `null`입니다. 거래가 없으면 승률/평균손익은 0, profit factor는 `null`입니다.
CSV는 Windows 엑셀용 UTF-8 BOM으로 저장하며 시간은 UTC입니다.
원시 데이터 및 결과는 `.gitignore`의 `data/` 아래에 생성되어 GitHub에 자동 업로드되지 않습니다.

## 백테스트 진단 보고서 (Issue #3)

먼저 기존 백테스트를 성공시켜 종목별 `trades.csv`, `equity.csv`, `summary.json`을 준비한 뒤,
Windows PowerShell에서 아래 **한 줄**을 실행하세요. 실제 전략 실행이나 다운로드는 하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m backtesting.diagnostics --symbols BTCUSDT ETHUSDT
```

기본 입력은 `data/backtests/{symbol}/`, 결과는 `data/diagnostics/{symbol}/report.md`입니다.
다른 위치는 `--input-dir "D:\market-data\backtests" --output-dir "D:\market-data\diagnostics"`로 지정합니다.
Markdown은 메모장이나 Markdown 뷰어로 읽을 수 있습니다. CSV는 엑셀용 UTF-8 BOM입니다.
`diagnostics.json`은 전체 분석, `daily.csv`, `monthly.csv`, `yearly.csv`는 날짜/월/연도별 결과,
`by_side.csv`, `by_exit_reason.csv`는 롱·숏/청산 사유별 결과,
`worst_months.csv`는 자산이 감소한 상위 5개월입니다. 화면에도 보고서 표를 출력합니다.

- 원본 UTC 시간과 **원본 초기 자본**을 사용합니다. BTC와 ETH가 각각 10,000 USDT이면
  독립 계정 2개를 비교합니다. `comparison.json`에 각 계정 자본/기간을 따로 기록하며,
  20,000 USDT 공동 포트폴리오나 공유 증거금·리밸런싱 수익률은 계산하지 않습니다.
- **평가손익**은 자산곡선(미실현손익 포함)의 기간 말/직전 말 차이입니다. 첫 기간 분모는 초기 자본입니다.
  **실현손익**은 그 기간에 청산된 거래의 순손익 합입니다. 진입 비용·미실현손익의 시점 때문에 서로 다릅니다.
  모든 시각은 UTC이며 자산곡선의 시각은 봉 종료 경계입니다. 예를 들어 1월 1일 00시 종료 값과
  그 경계의 청산 거래는 직전 12월에 배정합니다. 자정 시가 청산도 같은 경계 규칙을 일관 적용합니다.
- `partial=True`는 해당 달/연도 전체를 포함하지 않은 **부분 기간**입니다. 2026년 도중에 끝나는
  분석은 연간 확정 성과가 아니며, 11월 도중이면 그 달도 부분 기간입니다.
- MDD는 초기 자본도 포함한 시간별 평가자산 고점 대비 최대 하락률입니다. 고점/저점/회복 시각,
  고점에서 회복까지 또는 분석 종료까지의 시간, 가장 오래 미회복 상태였던 구간을 기록합니다.
  회복되지 않았으면 `recovered=False`입니다. 기간 표 낙폭은 각 기간 시작 자산에서 새로 계산합니다.
- 최악/최선 30일은 **정확히 720시간** 자산 증감(USDT)으로 정합니다. 30일 미만이면 `null`입니다.
  연속 손실은 청산 순서의 비용 차감 순손익이 음수인 거래이며, 0이나 이익 거래에서 끊깁니다.
  거래당 기대손익은 표본 평균일 뿐 미래 수익 예측이 아닙니다.
- 필수 파일/열, 숫자, UTC 시간 정렬/중복/누락/범위, 종목/봉·거래 개수,
  `cash + unrealized_pnl == equity`, 거래별 비용 차감과 총손익 회계를 확인합니다.
  숫자 비교 허용오차는 절대 `1e-6` USDT + 상대 `1e-9`입니다.
  실패한 종목은 파일과 가능한 행 번호를 출력하고 `error.json`을 남깁니다.
  동일 출력 폴더의 이전 진단 결과는 제거해 오래된 보고서와 혼동하지 않게 합니다.
  다른 종목은 계속 처리하며 하나라도 실패하면 종료 코드 1, 모두 통과하면 0입니다.
  재실행 시 같은 출력 폴더의 보고서는 덮어쓰므로 보관하려면 출력 폴더를 바꾸세요.
- **펀딩비 0 가정, 시간별 종가 낙폭, 실제 마진/강제청산 미포함** 한계는 그대로입니다.
  손실 구간이 특정 시장 추세/횡보 때문에 발생했다고 단정하지 않습니다.
  가격 데이터 결합 및 미래정보 누출 없는 시장 국면 분석은 이번 기능에 포함하지 않았습니다.

진단 검증에는 합성 CSV만 사용했습니다. 사용자 Windows PC의 전체 결과와 Python 3.15 실행은
클라우드에서 검증하지 않았으며, 실제 성과 수치를 임의로 만들지 않았습니다.
수집기와 SMA 전략 및 가상 체결 엔진은 변경하지 않았습니다.
