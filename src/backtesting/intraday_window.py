"""Explicit, preselected research windows; never selected from returns."""
import pandas as pd
from .data import utc
from .intraday_data import window

V092_START='2025-02-01T00:00:00Z'
V092_END='2026-10-28T00:00:00Z'
WINDOW_NOTICE=('전체 기간을 교체하지 않는 별도 연속 연구 창입니다. 알려진 자료 충돌/누락 날짜를 시작 전에 제외한 선택으로 '
               '기간 축소·선택 편향 및 기존 전체 기간과의 비교가능성 저하가 있습니다. 새 불일치를 자동 제외하거나 기간을 다시 줄이지 않습니다. '
               '자료는 이미 관찰한 exploratory/in-sample이며 2025/2026도 미관측 OOS가 아닙니다. '
               '기존 전체 기간 스윙 수익률과 직접 비교할 수 없고 동일 UTC 창·자본·비용으로 새 실행이 필요합니다. '
               '시작 이전 이력을 쓰지 않으므로 처음 200개 완료 1h SMA와 15m 지표 워밍업 후부터만 진입 가능합니다.')


def selected_window(symbol,v08_dir,start=None,end=None):
    if (start is None)!=(end is None):raise ValueError('--start와 --end는 반드시 함께 지정하세요.')
    if start is None:return window(symbol,v08_dir)
    a,b=utc(start),utc(end)
    if a>=b or a!=a.floor('h') or b!=b.floor('h'):raise ValueError('명시 기간은 시작<종료인 UTC 정시여야 합니다.')
    if b>pd.Timestamp.now(tz='UTC').floor('h'):raise ValueError('미완성 미래 기간 금지')
    return a,b


def context(start,end,explicit):
    return dict(start=start.isoformat(),end_exclusive=end.isoformat(),explicit=explicit,
        preset='Issue12_v0.9.2' if explicit and start==utc(V092_START) and end==utc(V092_END) else ('explicit_user_window' if explicit else 'legacy_v08_window'),
        notice=WINDOW_NOTICE if explicit else '기존 v0.8 기간 선택 유지')
