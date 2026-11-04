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
