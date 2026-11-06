# v0.9.4 진행 중 확인한 기존 감사 테스트/예외 처리 문제

새 확인 정책의 17개 synthetic 테스트와 실제 1031/502 재현은 통과했지만,
전체 회귀 실행 중 기존 `test_trustworthy_correction_separate_reference_only`가 간헐적으로 실패했다.

## 재현 및 원인

`tests/test_integrity_audit.py`의 합성 ZIP 생성은 ZIP 내부 시각을 기본 현재 시각으로 기록한다.
동일한 CSV를 같은 경로에 재생성해도 ZIP 시간 경계를 넘으면 바이트와 SHA256이 바뀐다.
가짜 원천 fixture가 이전 checksum 기록 이후 그 경로를 새 바이트로 덮어쓰므로,
실제 감사기의 `발행 증거 hash 불일치` 거부는 올바른 동작이다.
테스트 결과가 실행 시각에 좌우되지 않으려면 synthetic ZIP metadata의 시각을 고정해야 한다.
내용/assertion/검증 강도/실제 ZIP·시세·전략을 바꾸는 수정이 아니다.

별도 임시 synthetic 재현에서 ZIP 시각을 매번 다르게 만들었고,
원래 `ValueError: 발행 증거 hash 불일치`와 바깥 `NameError: zipfile is not defined`를 확인했다.
원본 시세나 기존 연구 결과를 사용하거나 수정하지 않았다.

## 별도 기존 코드 패치 제안 (이번에는 미적용)

`src/backtesting/integrity_audit.py`는 except tuple에서 `zipfile.BadZipFile`을 참조하지만
`zipfile`을 import하지 않아 예외 처리 중 원래 오류를 가린다.
Issue #14는 기존 감사 모듈 변경을 금지하므로 현재 작업에서는 이를 고치지 않는다.
후속 허가/작업에서 import 누락을 수정하고, 강제로 checksum/출판 실패를 주입해
`error.json`, `status=failed`, 원본 불변을 검증하는 별도 회귀 테스트를 추가하는 것이 필요하다.
전략·신호·체결·비용 계산과는 별개의 오류 처리 문제다.

v0.9.4 연구기는 검증 완료된 bundle을 읽으며 기존 감사 `investigate`를 실행하지 않는다.
새 연구의 데이터 검증/기준 재현 실패는 자체 오류 처리로 차단한다.

## 사용자 예외 허용 후 적용 및 최종 검증 (2026-11-06)

사용자가 기존 **합성 테스트 ZIP의 시각 설정만** 고정하도록 명시적으로 허용했다.
`tests/test_integrity_audit.py`의 `zip_bytes`에서 `data.csv`에
`ZipInfo(..., date_time=(2025, 1, 1, 0, 0, 0))`를 사용하도록 한 줄만 변경했다.
CSV 내용, 기대값, assertion, 체크섬/출처 검증 로직, 전략·엔진·실데이터·기존 성과는 변경하지 않았다.

- 수정 전 전체 회귀: **289 passed, 1 failed** (기존 ZIP fixture 간헐 실패).
- 수정 전 통제 시계 00초/02초: CSV는 동일하지만 ZIP 시각·SHA256은 서로 달랐다.
- 수정 후 같은 두 시계: ZIP 시각이 동일하게 고정되고 바이트·SHA256이 일치했다.
  고정 시각에서의 ZIP 바이트는 수정 전 00초 사례와도 동일했다.
- 기존 실패 테스트를 별도 프로세스로 **10회 연속 실행: 10/10 통과**.
- 전체 `pytest -q` 1차: **290 passed in 40.22s**.
- 전체 `pytest -q` 2차: **290 passed in 39.85s**.

이 변경은 잘못된 checksum을 통과시키는 것이 아니라, 원천 fixture가 반복 호출 도중
시간 metadata 때문에 바뀌던 문제만 제거한다. 기존 부정합 거부 테스트는 유지했다.
위에 기록한 기존 감사 모듈의 `zipfile` import 누락은 별도 코드 문제로 **수정하지 않았다**.
현재 허용 범위 밖이며, v0.9.4의 전략/체결이나 검증 bundle 로더 계산에는 변경이 없다.
