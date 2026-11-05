"""v0.9.3 ETH-only read-only post-hoc stop path diagnostics, no policy changes."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .intraday_lab import load_verified
from .intraday_engine import simulate,IntradayConfig
from .intraday_signals import features
from .intraday_data import STEP,digest
from .intraday_window import V092_START,V092_END
from .data import utc
from .diagnostics import table

EXPECTED={'total_trades':1031,'stop':502,'take_profit':243,'max_holding':286}
EXPECTED_NET=-4512.645144131257
NOTICE=('후향적 사후 경로 설명이며 실제 반사실 전략 재시뮬레이션 수익이 아닙니다. '
        '손절 후 회복은 손절을 넓히거나 없애면 벌었다는 뜻이 아닙니다. 자본 재사용·다음 신호·포지션 중복·수량·기회비용이 달라집니다. '
        '관찰된 연구 자료와 선택 편향이 있으며 투자 수익성이나 전략 채택의 근거가 아닙니다. '
        '펀딩비 0, 실제 마진·강제청산·호가 미구현. 원래 -45.126% 성과를 수정하지 않습니다.')


def verify_reproduction(trades,equity,summary,root,expected=None,expected_net=EXPECTED_NET):
    expected=EXPECTED if expected is None else expected;root=Path(root)
    old_t=pd.read_csv(root/'trades.csv');old_e=pd.read_csv(root/'equity.csv')
    old_s=json.loads((root/'summary.json').read_text())['summary']
    for c in ['entry_time','exit_time']:old_t[c]=pd.to_datetime(old_t[c],utc=True)
    old_e['time']=pd.to_datetime(old_e.time,utc=True)
    for name,a,b in [('trades.csv',trades,old_t),('equity.csv',equity,old_e)]:
        try:pd.testing.assert_frame_equal(a.reset_index(drop=True),b.reset_index(drop=True),check_dtype=False,check_exact=False,atol=1e-6,rtol=1e-9)
        except AssertionError as exc:raise ValueError(f'기준 재현 실패 {name}: {exc}') from exc
    if summary['config']!=old_s['config']:raise ValueError('기준 Config 불일치')
    for key,value in summary.items():
        if key=='config':continue
        if value is None:
            if old_s.get(key) is not None:raise ValueError(f'기준 summary {key} 불일치')
        elif key not in old_s or not np.isclose(value,old_s[key],atol=1e-6,rtol=1e-9):raise ValueError(f'기준 summary {key} 불일치')
    counts={str(k):int(v) for k,v in trades.reason.value_counts().items()}
    if len(trades)!=expected['total_trades'] or any(counts.get(k,0)!=v for k,v in expected.items() if k!='total_trades'):
        raise ValueError(f'고정 기준 1031 거래/502 손절 재현 실패: {len(trades)} / {counts}')
    if sum(counts.values())!=sum(v for k,v in expected.items() if k!='total_trades'):raise ValueError('예상하지 않은 청산 사유')
    if expected_net is not None and not np.isclose(summary['net_pnl'],expected_net,atol=1e-6,rtol=1e-9):raise ValueError('Issue #13 기준 순손익 불일치')
    if not np.allclose(equity.cash+equity.unrealized_pnl,equity.equity,atol=1e-6,rtol=1e-9):raise ValueError('자산 회계 불일치')
    if not np.isclose(trades.net_pnl.sum(),summary['net_pnl'],atol=1e-6,rtol=1e-9):raise ValueError('누적 거래 손익 불일치')
    return dict(verified=True,total_trades=len(trades),reasons=counts,net_pnl=summary['net_pnl'],full_trade_and_equity_path_matched=True)


def prior_atr(frame):
    previous=frame.close.shift(1)
    tr=pd.concat([frame.high-frame.low,(frame.high-previous).abs(),(frame.low-previous).abs()],axis=1).max(axis=1)
    tr.iloc[0]=np.nan
    return tr.rolling(14,min_periods=14).mean().shift(1)


def excursion(window,entry,side):
    if window.empty:return None,None
    best=float(window.high.max()) if side==1 else float(window.low.min())
    worst=float(window.low.min()) if side==1 else float(window.high.max())
    return max(0.,side*(best/entry-1)*100),max(0.,-side*(worst/entry-1)*100)


def touch(window,level,side,favorable):
    if window.empty:return None
    condition=(window.high>=level if side==1 else window.low<=level) if favorable else (window.low<=level if side==1 else window.high>=level)
    found=window.loc[condition,'open_time']
    return found.iloc[0].isoformat() if len(found) else None


def analyze_trade(t,frame,index,atr,trend=None):
    entry=utc(t.entry_time);exit=utc(t.exit_time);i=index.get_indexer([entry])[0]
    if i<0 or entry!=entry.floor('15min'):raise ValueError('진입 시각/15m 경계 불일치')
    side=1 if t.side=='long' else -1;fill=float(t.entry_price);raw=float(frame.open.iloc[i]);qty=float(t.quantity)
    horizon=entry+pd.Timedelta(hours=6);w=frame.iloc[i:min(i+24,len(frame))]
    censored=len(w)<24
    mfe,mae=excursion(w,fill,side);raw_mfe,raw_mae=excursion(w,raw,side)
    result=dict(entry_time=entry.isoformat(),exit_time=exit.isoformat(),side=t.side,reason=t.reason,entry_fill=fill,entry_raw=raw,
        quantity=qty,actual_net_pnl=float(t.net_pnl),year=entry.year,horizon_time=horizon.isoformat(),observed_bars=len(w),
        censored=censored,mfe_fill_pct=mfe,mae_fill_pct=mae,mfe_raw_pct=raw_mfe,mae_raw_pct=raw_mae,
        prior_atr14=float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else None,
        prior_atr14_pct=float(atr.iloc[i]/raw*100) if pd.notna(atr.iloc[i]) else None,posthoc_only=True)
    best=float(w.high.max()) if side==1 else float(w.low.min())
    worst=float(w.low.min()) if side==1 else float(w.high.max())
    def liquidation_pct(price):
        adjusted=price*(1-side*.0002)
        return (side*(adjusted-fill)-.0004*(fill+adjusted))/fill*100
    result['best_hypothetical_liquidation_net_pct']=liquidation_pct(best)
    result['worst_hypothetical_liquidation_net_pct']=liquidation_pct(worst)
    result['hourly_trend']=int(trend.trend.iloc[i-1]) if trend is not None and i>0 else None
    result['hourly_trend_pass']=result['hourly_trend']==side if result['hourly_trend'] is not None else None
    for threshold,name in [(.005,'05'),(.01,'10'),(.02,'20')]:
        a=touch(w,fill*(1+side*threshold),side,True);b=touch(w,fill*(1-side*threshold),side,False)
        result[f'first_favorable_{name}_bar_open']=a;result[f'first_adverse_{name}_bar_open']=b
        result[f'first_favorable_{name}_bar_close']=(utc(a)+STEP).isoformat() if a else None
        result[f'first_adverse_{name}_bar_close']=(utc(b)+STEP).isoformat() if b else None
        order='not_both_observed'
        if a is not None and b is not None:
            order='favorable_first_bar' if utc(a)<utc(b) else 'adverse_first_bar'
            if a==b:
                opening=float(w.loc[w.open_time==utc(a),'open'].iloc[0])
                fav_open=side*(opening/fill-1)>=threshold
                adv_open=-side*(opening/fill-1)>=threshold
                order='favorable_at_open' if fav_open else 'adverse_at_open' if adv_open else 'unknown_same_bar'
        result[f'first_{name}_pair_order']=order
        result[f'first_{name}_same_bar_order_unknown']=order=='unknown_same_bar'
    for key in ['h6_fill_return_pct','h6_raw_return_pct','h6_fixed_qty_net_usdt','h6_fixed_qty_net_return_pct']:
        result[key]=None
    if not censored:
        price=float(w.close.iloc[-1]);exit_fill=price*(1-side*.0002)
        net=side*qty*(exit_fill-fill)-qty*(fill+exit_fill)*.0004
        result.update(h6_fill_return_pct=side*(price/fill-1)*100,h6_raw_return_pct=side*(price/raw-1)*100,
                      h6_fixed_qty_net_usdt=net,h6_fixed_qty_net_return_pct=net/(qty*fill)*100)
    for key in ['stop_bar_open','stop_same_bar_recovery_possible','stop_same_bar_tp_possible','stop_take_same_bar_ambiguous',
        'confirmed_post_stop_recovery','recovery_bar_open','recovery_status','post_stop_tp_touch','post_stop_tp_bar_open',
        'post_stop_stop_level_touch','post_stop_stop_level_bar_open','post_stop_recovery_stop_same_bar_unknown',
        'stop_close_confirms_recovery','stop_close_confirms_tp','stop_same_bar_recovery_order_unknown','stop_same_bar_tp_order_unknown','h6_after_stop_observable',
        'immediate_after_stop_fill_return_pct','pre_stop_mfe_lower_pct','pre_stop_mfe_upper_pct','pre_stop_mae_known_pct',
        'pre_stop_mae_upper_pct','post_stop_further_adverse_touch','h6_worse_than_actual_stop_net','h6_difference_vs_actual_stop_net']:
        result[key]=None
    if t.reason not in ('stop','stop_gap'):return result
    stop=fill*(1-side*.01);target=fill*(1+side*.02)
    stop_index=index.get_indexer([exit if t.reason=='stop_gap' else exit-STEP])[0]
    if stop_index<i or stop_index>=len(frame):raise ValueError('손절 시각이 실제 봉과 불일치')
    bar=frame.iloc[stop_index]
    if t.reason=='stop_gap':
        hit=bar.open<=stop if side==1 else bar.open>=stop
    else:hit=bar.low<=stop if side==1 else bar.high>=stop
    if not hit:raise ValueError('기록된 손절이 실제 가격 범위에 없음')
    stop_time=bar.open_time
    post_start=stop_index if t.reason=='stop_gap' else stop_index+1
    post=frame.iloc[post_start:min(i+24,len(frame))]
    recovery=touch(post,fill,side,True);tp=touch(post,target,side,True);sl=touch(post,stop,side,False)
    close_observed=stop_index<i+24
    close_recovery=bool(close_observed and t.reason=='stop' and (bar.close>=fill if side==1 else bar.close<=fill))
    close_tp=bool(close_observed and t.reason=='stop' and (bar.close>=target if side==1 else bar.close<=target))
    if close_recovery:recovery=stop_time.isoformat()
    if close_tp:tp=stop_time.isoformat()
    if close_observed and (bar.close<=stop if side==1 else bar.close>=stop):sl=stop_time.isoformat()
    same_recovery=(bar.high>=fill if side==1 else bar.low<=fill) and t.reason=='stop'
    same_tp=(bar.high>=target if side==1 else bar.low<=target) and t.reason=='stop'
    pre=frame.iloc[i:stop_index]
    lower_mfe,lower_mae=excursion(pre,fill,side)
    upper_window=frame.iloc[i:stop_index+1] if t.reason=='stop' else pre
    upper_mfe,upper_mae=excursion(upper_window,fill,side)
    raw_exit=float(t.exit_price)/(1-side*.0002)
    at_stop_adverse=max(0.,-side*(raw_exit/fill-1)*100)
    result.update(stop_bar_open=stop_time.isoformat(),stop_same_bar_recovery_possible=bool(same_recovery),
        stop_same_bar_tp_possible=bool(same_tp),stop_take_same_bar_ambiguous=bool(same_tp),stop_close_confirms_recovery=close_recovery,stop_close_confirms_tp=close_tp,
        stop_same_bar_recovery_order_unknown=bool(same_recovery and not close_recovery),stop_same_bar_tp_order_unknown=bool(same_tp and not close_tp),
        h6_after_stop_observable=bool(close_observed),
        confirmed_post_stop_recovery=bool(recovery),recovery_bar_open=recovery,
        recovery_status='observed_after_stop' if recovery else 'same_bar_order_unknown' if same_recovery else 'not_observed_in_available_window',
        post_stop_tp_touch=bool(tp),post_stop_tp_bar_open=tp,post_stop_stop_level_touch=bool(sl),post_stop_stop_level_bar_open=sl,
        post_stop_recovery_stop_same_bar_unknown=bool(recovery and recovery==sl and not (t.reason=='stop_gap' and sl==stop_time.isoformat())),
        immediate_after_stop_fill_return_pct=side*(float(bar.close)/fill-1)*100 if close_observed else None,
        pre_stop_mfe_lower_pct=lower_mfe if lower_mfe is not None else 0.,pre_stop_mfe_upper_pct=upper_mfe if upper_mfe is not None else 0.,
        pre_stop_mae_known_pct=max(lower_mae or 0.,at_stop_adverse),pre_stop_mae_upper_pct=max(upper_mae or 0.,at_stop_adverse),
        post_stop_further_adverse_touch=bool(((post.low<raw_exit).any() if side==1 else (post.high>raw_exit).any()) or (close_observed and side*(bar.close-raw_exit)<0)))
    if not censored and close_observed:
        result['h6_worse_than_actual_stop_net']=result['h6_fixed_qty_net_usdt']<float(t.net_pnl)
        result['h6_difference_vs_actual_stop_net']=result['h6_fixed_qty_net_usdt']-float(t.net_pnl)
    return result


def analyze_paths(trades,frame,trend=None):
    index=pd.Index(frame.open_time);atr=prior_atr(frame)
    rows=[dict(trade_index=i,**analyze_trade(t,frame,index,atr,trend)) for i,t in enumerate(trades.itertuples(index=False))]
    data=pd.DataFrame(rows)
    if not rows:return data,[]
    values=data.prior_atr14_pct.dropna();cuts=[float(x) for x in values.quantile([.25,.5,.75])] if len(values) else []
    data['entry_atr_group']=['unknown' if pd.isna(x) else f'Q{int(np.searchsorted(cuts,x,side="left"))+1}' for x in data.prior_atr14_pct]
    return data,cuts


def distribution(values):
    values=pd.to_numeric(values,errors='coerce').dropna()
    return dict(n=len(values),mean=float(values.mean()) if len(values) else None,median=float(values.median()) if len(values) else None,
        p10=float(values.quantile(.1)) if len(values) else None,p25=float(values.quantile(.25)) if len(values) else None,
        p75=float(values.quantile(.75)) if len(values) else None,p90=float(values.quantile(.9)) if len(values) else None)


def rate(series):
    series=series.dropna();n=len(series);count=int(series.astype(bool).sum())
    return dict(numerator=count,denominator=n,rate_pct=100*count/n if n else None)


def summarize(rows):
    if rows.empty:return dict(total=0,full_horizon=0,censored=0)
    full=rows.loc[~rows.censored];stops=full.loc[full.reason.isin(['stop','stop_gap'])]
    metrics=dict(total=len(rows),full_horizon=len(full),censored=int(rows.censored.sum()),full_horizon_stops=len(stops),
        all=dict(h6_raw=distribution(full.h6_raw_return_pct),h6_fill=distribution(full.h6_fill_return_pct),mfe=distribution(full.mfe_fill_pct),mae=distribution(full.mae_fill_pct)),
        stops=dict(confirmed_recovery=rate(stops.confirmed_post_stop_recovery),same_stop_bar_recovery_possible=rate(stops.stop_same_bar_recovery_possible),
        same_stop_bar_recovery_order_unknown=rate(stops.stop_same_bar_recovery_order_unknown),stop_close_confirms_recovery=rate(stops.stop_close_confirms_recovery),
        h6_fill_positive=rate(stops.h6_fill_return_pct>0),h6_fill_above_012=rate(stops.h6_fill_return_pct>.12),
        h6_raw_positive=rate(stops.h6_raw_return_pct>0),h6_raw_above_012=rate(stops.h6_raw_return_pct>.12),
        h6_fixed_quantity_net_positive=rate(stops.h6_fixed_qty_net_usdt>0),post_stop_tp_touch=rate(stops.post_stop_tp_touch),
        post_stop_original_stop_level_touch=rate(stops.post_stop_stop_level_touch),post_stop_recovery_stop_same_bar_unknown=rate(stops.post_stop_recovery_stop_same_bar_unknown),
        same_stop_bar_tp_possible=rate(stops.stop_same_bar_tp_possible),same_stop_bar_tp_order_unknown=rate(stops.stop_same_bar_tp_order_unknown),stop_worse_move_after_exit=rate(stops.post_stop_further_adverse_touch),
        stop_limited_worse_h6_fixed_qty_loss=rate(stops.h6_worse_than_actual_stop_net),
        h6_fill=distribution(stops.h6_fill_return_pct),h6_raw=distribution(stops.h6_raw_return_pct),
        immediate_after_stop=distribution(stops.immediate_after_stop_fill_return_pct),pre_stop_mfe_lower=distribution(stops.pre_stop_mfe_lower_pct),
        pre_stop_mfe_upper=distribution(stops.pre_stop_mfe_upper_pct),pre_stop_mae_known=distribution(stops.pre_stop_mae_known_pct),pre_stop_mae_upper=distribution(stops.pre_stop_mae_upper_pct)),
        same_bar_stop_tp_count=int(rows.stop_take_same_bar_ambiguous.fillna(False).astype(bool).sum()))
    metrics['first_touch_counts']={name:dict(denominator=len(full),favorable_observed=int(full[f'first_favorable_{name}_bar_open'].notna().sum()),adverse_observed=int(full[f'first_adverse_{name}_bar_open'].notna().sum()),same_bar_order_unknown=int(full[f'first_{name}_same_bar_order_unknown'].sum()),pair_orders={str(k):int(v) for k,v in full[f'first_{name}_pair_order'].value_counts().items()}) for name in ['05','10','20']}
    eligible=stops.loc[stops.h6_worse_than_actual_stop_net.notna()]
    recovered=eligible.confirmed_post_stop_recovery.astype(bool);worse=eligible.h6_worse_than_actual_stop_net.astype(bool)
    metrics['stop_endpoint_cross_counts']=dict(denominator=len(eligible),full_horizon_stops=len(stops),h6_stop_comparison_unavailable=int(stops.h6_worse_than_actual_stop_net.isna().sum()),recovered_and_worse=int((recovered&worse).sum()),
        recovered_not_worse=int((recovered&~worse).sum()),not_confirmed_recovered_and_worse=int((~recovered&worse).sum()),neither_confirmed=int((~recovered&~worse).sum()))
    return metrics


def subgroup_summaries(data):
    if data.empty:return {}
    out={}
    groups={'population':{'all_filled':data,'stop':data.loc[data.reason=='stop'],'stop_gap':data.loc[data.reason=='stop_gap'],
        'take_profit':data.loc[data.reason=='take_profit'],'max_holding':data.loc[data.reason=='max_holding']}}
    for column in ['side','year','entry_atr_group','hourly_trend_pass']:
        groups[column]={str(k):g for k,g in data.groupby(column,dropna=False)}
    for name,g in groups.items():out[name]={key:summarize(value) for key,value in g.items()}
    return out


def write_report(report,data,output):
    data.to_csv(output/'per_trade.csv',index=False,encoding='utf-8-sig')
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    s=report['overall'];text='# ETHUSDT v0.9.3 손절 경로 진단\n\n'+NOTICE+'\n\n'
    text+=f'기준 재현: {json.dumps(report["reproduction"],ensure_ascii=False)}\n\n기간 UTC: {report["start"]} ~ {report["end_exclusive"]} (종료 제외).\n\n'
    text+=f'전체 {s["total"]}개, 완전 6h {s["full_horizon"]}개, 말단 관측 부족 {s["censored"]}개. 말단 부족은 주 통계에서 제외하고 per_trade에는 보존합니다.\n\n'
    if 'stops' in s:
        text+='## 손절 후 관측 (분자/분모)\n\n'+table([dict(metric=k,**v) for k,v in s['stops'].items() if isinstance(v,dict) and 'numerator' in v])
        text+='\n## 손절 표본 분포\n\n'+table([dict(metric=k,**v) for k,v in s['stops'].items() if isinstance(v,dict) and 'mean' in v])
        text+='\n회복과 6h 추가 손실은 동시에 생길 수 있습니다: '+json.dumps(s['stop_endpoint_cross_counts'],ensure_ascii=False)+'\n'
    text+='\n## 정의와 모호성\n\nMFE/MAE는 원래 진입 후 24개 15m 봉 전체의 사후 범위이며 실제 청산 후 가격도 포함합니다. entry fill 기준과 원시 시가 기준을 따로 기록합니다. 첫 접촉 시각은 해당 15m 봉 시작이며 정확한 접촉 시각은 open~open+15m 사이입니다. 같은 봉에서 두 레벨이 닿으면 일반적으로 선후 unknown이며, 시가가 이미 한 레벨을 넘어선 경우만 at_open으로 별도 표시합니다.\n'
    text+='일반 손절 봉의 high/low는 손절 이전인지 이후인지 몰라 고가에 의한 확인된 후속 회복에는 다음 봉부터만 포함합니다. 단 손절 봉 종가가 진입가를 회복하면 손절 후 닫힌 시점의 회복은 확인되므로 별도 플래그와 함께 포함합니다. 그 봉에서의 회복/TP 가능성은 모호 표본으로 별도 기록합니다. 갭 손절은 시가에서 발생하므로 해당 봉의 이후 범위를 관측하되 내부 순서는 미상입니다.\n'
    text+='pre-stop MFE 하한은 손절 봉 이전 완료 봉만, 상한은 손절 봉 범위까지 포함합니다. 갭 손절은 그 봉 이후 범위를 pre-stop에 포함하지 않습니다. MAE known은 이전 봉과 실제 손절 가격까지, upper는 모호한 손절 봉 전체를 허용한 경계입니다. 원래 stop-first 규칙은 바꾸지 않습니다.\n'
    text+='즉후 수익률은 손절이 발생한 15m 봉 종가의 첫 종료 관측값이며 tick 직후가 아닙니다. best/worst hypothetical liquidation net은 원시 극값을 청산할 경우의 비용포함 고정 단위 설명 경계이며 실제 체결이 아닙니다. 6h는 진입 봉부터 24번째 봉의 종가이며 다음 봉의 시가와 다를 수 있습니다. 6h 시점 fill/raw 가격수익률 및 +0.12% 근사 허들을 각각 구분합니다. fill 기준 +0.12%를 정확한 왕복 비용이라고 해석하지 마세요. fixed_qty_net은 기존 진입 수량·진입 fill을 고정한 뒤 6h 종가에 청산 슬리피지와 양쪽 수수료를 한 번씩 적용한 설명값입니다. 이 값을 합쳐 가상 전략 수익률로 쓰면 안 됩니다.\n'
    text+='손절이 더 큰 손실을 막았다는 지표는 동일 수량 6h 사후 손익이 실제 손절 순손익보다 나쁜 표본 비율일 뿐 인과 효과가 아닙니다. 도중 반등/추가 하락은 겹칠 수 있고, 불완전 6h 표본은 이 비교에서 제외합니다.\n'
    text+='ATR14는 진입 이전 완료 15m로만 계산하며 분위기 경계는 전체 진입 표본의 사후 분위수입니다. 같은 값은 낮은 bin에 배정하며 동일 크기 4집단을 보장하지 않습니다. 사후 subgroup을 승자 필터로 선택하지 않습니다.\n'
    text+='\n## 모집단/하위집단\n\n'+table([dict(group=k,total=v['total'],full_horizon=v['full_horizon'],censored=v['censored']) for k,v in report['groups'].get('population',{}).items()])
    if 'first_touch_counts' in s:
        text+='\n최초 레벨 접촉 (fill 기준 05=0.5%, 10=1%, 20=2%):\n\n'+table([dict(level=k,**{a:b for a,b in v.items() if a!='pair_orders'}) for k,v in s['first_touch_counts'].items()])
    for kind,groups in report['groups'].items():
        compact=[]
        for key,value in groups.items():
            stop=value.get('stops',{});all_rows=value.get('all',{})
            compact.append(dict(group=key,total=value['total'],full_horizon=value['full_horizon'],censored=value['censored'],
                h6_fill_mean=all_rows.get('h6_fill',{}).get('mean'),mfe_mean=all_rows.get('mfe',{}).get('mean'),mae_mean=all_rows.get('mae',{}).get('mean'),
                stop_recovery_n=stop.get('confirmed_recovery',{}).get('numerator'),stop_recovery_den=stop.get('confirmed_recovery',{}).get('denominator'),
                stop_worse_h6_n=stop.get('stop_limited_worse_h6_fixed_qty_loss',{}).get('numerator'),stop_worse_h6_den=stop.get('stop_limited_worse_h6_fixed_qty_loss',{}).get('denominator')))
        text+=f'\n## {kind} 하위집단\n\n'+table(compact)+'\n상세 분포/분모/분자는 summary.json에도 기록합니다.\n'

    text+='\n## 다음 결정 기준 (이번에는 변경하지 않음)\n\n확인 가능한 후속 회복과 비용후 6h 설명값이 반복되면 별도 버전에서 사전 고정 stop 대안 하나를 전면 재실행해 검증할 수 있습니다. 회복 후에도 불리하면 exit 조정보다 진입/국면 품질 진단을 우선합니다. 어느 경우든 비용 2배·연도 안정성·관찰하지 않은 미래 검증이 필요하며 본 진단만으로 실거래/수익성 채택을 금지합니다.\n'
    text+='\n표본은 거래/6h 관측이 서로 겹치므로 독립 표본 통계적 유의성이나 인과 효과를 주장하지 않습니다. 원본 SHA256/Config/ATR 분위 경계와 전체 하위집단은 summary.json에 포함됩니다. Windows 실제 검증은 별도입니다.\n'
    (output/'report.md').write_text(text,encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description='v0.9.3 ETH 1031거래/502손절 고정 읽기 전용 경로 진단')
    for flag,default in [('data-dir','data/intraday_v092_verified_input'),('research-dir','data/intraday_v092_window_research'),('v08-dir','data/timeframe_v08'),('output-dir','data/intraday_v093_stop_diagnostics')]:
        parser.add_argument('--'+flag,type=Path,default=Path(default))
    args=parser.parse_args(argv);out=args.output_dir.resolve();protected=[args.data_dir.resolve(),args.research_dir.resolve(),args.v08_dir.resolve(),Path('.venv').resolve()]
    protected += [(Path('data')/k).resolve() for k in ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06','exit_policy_v07','timeframe_v08','intraday_v09','intraday_v091_research','intraday_integrity_v091','intraday_integrity_v092_window','intraday_v09_verified_input','intraday_v092_verified_input','intraday_v092_window_research','intraday_downloads_v09','intraday_integrity_v091_cache']]
    if any(out==p or p in out.parents or out in p.parents for p in protected):
        print('원본 데이터/연구/감사와 겹치는 출력 금지',file=sys.stderr);return 1
    try:out.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 출력 경로 필요: {exc}',file=sys.stderr);return 1
    target=out/'ETHUSDT';target.mkdir();pins={}
    try:
        start,end=utc(V092_START),utc(V092_END)
        source=args.research_dir/'ETHUSDT'/'breakout_20_cost1'
        paths=[source/name for name in ['trades.csv','equity.csv','summary.json']]
        paths += [args.data_dir/'klines/ETHUSDT'/name for name in ['15m.parquet','1h.parquet','integrity.provenance.json']]
        pins={str(p):digest(p) for p in paths}
        f,provenance,a,b=load_verified(args.data_dir,args.v08_dir,'ETHUSDT',start,end)
        if (a,b)!=(start,end) or len(f)!=60864:raise ValueError('고정 v0.9.2 연구 기간/60864봉 불일치')
        causal=features(f);t,e,_,summary=simulate(f,causal.breakout_20,IntradayConfig())
        reproduced=verify_reproduction(t,e,summary,source)
        data,cuts=analyze_paths(t,f,causal)
        report=dict(symbol='ETHUSDT',start=start.isoformat(),end_exclusive=end.isoformat(),notice=NOTICE,reproduction=reproduced,
            baseline_summary=summary,source_rows=len(f),input_sha256=pins,source_parity=provenance['parity'],atr_quartile_cuts_pct=cuts,
            overall=summarize(data),groups=subgroup_summaries(data))
        if any(digest(Path(p))!=sha for p,sha in pins.items()):raise ValueError('분석 중 원본 파일 변동 감지')
        write_report(report,data,target)
        (out/'status.json').write_text(json.dumps({'status':'success','analysis':'posthoc_only','reproduction_verified':True},indent=2)+'\n')
        print(f'ETHUSDT: 1031거래/502손절 재현 및 사후 진단 완료. {target}/report.md')
        return 0
    except (OSError,ValueError,KeyError,TypeError) as exc:
        (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n')
        (out/'status.json').write_text(json.dumps({'status':'failed','reproduction_verified':False},indent=2)+'\n')
        print(f'ETHUSDT 진단 중단 — {exc}',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
