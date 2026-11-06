"""v0.9.1 evidence-led recovery. No strategy/backtest execution."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .data import utc
from .intraday_data import STEP, aggregate_hourly, parity, validate15, digest, window
from .integrity_sources import OfficialSources, quality
from .intraday_window import selected_window,context

COLS=['open','high','low','close','volume']


def plain(f):
    g=f.copy();g.attrs={};return g


def concatenate(frames):
    frames=[plain(f) for f in frames if f is not None and len(f)]
    if not frames:return None
    result=pd.concat(frames,ignore_index=True).sort_values('open_time').reset_index(drop=True)
    if result.open_time.duplicated().any():raise ValueError('복구 결합 중 중복 시각: 삭제하여 숨기지 않음')
    return result


def select(f,start,end):
    return f.loc[(f.open_time>=start)&(f.open_time<end)].copy() if f is not None else None


def missing(f,start,end,step):
    present=pd.DatetimeIndex(f.open_time) if f is not None else pd.DatetimeIndex([],tz='UTC')
    return pd.date_range(start,end,freq=step,inclusive='left').difference(present)


def ranges(times,step):
    result=[]
    for t in times:
        if result and t==utc(result[-1]['end_exclusive']):result[-1]['end_exclusive']=(t+step).isoformat()
        else:result.append(dict(start=t.isoformat(),end_exclusive=(t+step).isoformat()))
    return result


def hourly(f):
    if f is None:return None
    counts=f.groupby(f.open_time.dt.floor('h')).size()
    good=counts.index[counts.eq(4)]
    return aggregate_hourly(f.loc[f.open_time.dt.floor('h').isin(good)])


def compare(a,b):
    if a is None or b is None:return dict(compared_hours=0,mismatches=[],counts={c:0 for c in COLS})
    joint=a[['open_time',*COLS]].merge(b[['open_time',*COLS]],on='open_time',suffixes=('_a','_b'),validate='one_to_one')
    details=[];counts={}
    for c in COLS:
        atol,rtol=(1e-6,1e-8) if c=='volume' else (1e-8,1e-10)
        bad=~np.isclose(joint[c+'_a'],joint[c+'_b'],atol=atol,rtol=rtol);counts[c]=int(bad.sum())
        for row in joint.loc[bad].itertuples(index=False):
            x=float(getattr(row,c+'_a'));y=float(getattr(row,c+'_b'))
            details.append(dict(time=row.open_time.isoformat(),column=c,a=x,b=y,absolute_difference=abs(x-y),
                relative_difference=abs(x-y)/abs(y) if y else None,
                decimal_difference_of_serialized_values=str(abs(Decimal(str(x))-Decimal(str(y)))),atol=atol,rtol=rtol))
    return dict(compared_hours=len(joint),mismatches=details,counts=counts)


def equal(a,b):
    if a is None or b is None or len(a)!=len(b):return False
    if not pd.DatetimeIndex(a.open_time).equals(pd.DatetimeIndex(b.open_time)):return False
    return not compare(a,b)['mismatches']


def decide_day(rest15,rest1,daily15,hour_archives,start,end):
    """No voting/majority heuristics: independent intervals must match exactly.

    REST may fill an archive-absent day. Contradictory available official sources
    are never silently overruled; correction requires REST15 + REST1 agreement
    and no contradictory available daily15/hour archive.
    """
    if rest15 is None or rest1 is None:return False,'REST 15m/1h 독립 교차 검증 불가'
    try:
        r=validate15(rest15,start,end)
        if len(rest1)!=int((end-start)/pd.Timedelta(hours=1)):return False,'REST 1h 개수 부족'
        if not equal(hourly(r),rest1):return False,'REST 15m 집계와 REST 1h 불일치'
        if daily15 is not None and compare(r,daily15)['mismatches']:return False,'일별15m와 REST15m 불일치'
        if any(a is not None and compare(rest1,a)['mismatches'] for a in hour_archives):return False,'공식1h 아카이브와 REST1h 불일치'
    except ValueError as exc:return False,str(exc)
    return True,'REST15m/1h 일치, 조회 가능한 일별15m/공식1h와 모순 없음'


def exact_archive_lexemes(frame,start,end):
    """Retain original CSV decimals for disputed source windows, not a float excuse."""
    if frame is None:return None
    raw=frame.attrs.get('raw_decimal')
    if not raw:return None
    selected=[r for r in raw if start<=utc(r['open_time'])<end]
    if not selected:return None
    return dict(rows=len(selected),open=selected[0]['open'],high=str(max(Decimal(r['high']) for r in selected)),
                low=str(min(Decimal(r['low']) for r in selected)),close=selected[-1]['close'],
                volume=str(sum((Decimal(r['volume']) for r in selected),Decimal(0))))


def publish_verified(candidate,reference,report,target):
    start,end=utc(report['start']),utc(report['end_exclusive'])
    candidate=validate15(candidate,start,end);checked=parity(candidate,reference,start,end)
    target=Path(target)
    if target.exists():raise ValueError(f'검증 입력 기존 경로 덮어쓰기 금지: {target}')
    target.parent.mkdir(parents=True,exist_ok=True)
    # Whole directory publication prevents half-published parquet/metadata pairs.
    import tempfile,os,shutil
    stage=Path(tempfile.mkdtemp(dir=target.parent,prefix='.verified-staging-'))
    try:
        candidate.to_parquet(stage/'15m.parquet',index=False);reference.to_parquet(stage/'1h.parquet',index=False)
        import shutil
        from .integrity_loader import SOURCE,validate_bundle
        original=Path(report['local_1h']['path'])
        if digest(original)!=report['local_1h']['sha256']:raise ValueError('출판 전 원본 hash 불일치')
        shutil.copyfile(original,stage/'original_1h.parquet')
        (stage/'verification_record.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        artifacts=[];seen=set();(stage/'evidence').mkdir()
        for item in report.get('evidence',[]):
            source_path=item.get('cache_path') or item.get('response_path')
            if not source_path:continue
            source_path=Path(source_path);sha=digest(source_path)
            if (str(source_path.resolve()),sha) in seen:continue
            seen.add((str(source_path.resolve()),sha))
            is_zip=source_path.suffix=='.zip'
            if is_zip and not item.get('checksum_verified'):raise ValueError('공식 checksum 미검증 ZIP으로 복구 발행 불가')
            expected=item.get('sha256') if is_zip else item.get('response_sha256')
            if sha!=expected:raise ValueError('발행 증거 hash 불일치')
            destination=f'evidence/{len(artifacts):04d}'+('.zip' if is_zip else '.json')
            shutil.copyfile(source_path,stage/destination)
            artifacts.append(dict(file=destination,sha256=sha,url=item['url'],params=item.get('params'),
                                  official_checksum=sha if is_zip else None))
        info=dict(schema_version=1,status='verified',symbol=report['symbol'],start=report['start'],end_exclusive=report['end_exclusive'],rows=len(candidate),
            source=SOURCE,parity=checked,parquet_sha256=digest(stage/'15m.parquet'),hourly_sha256=digest(stage/'1h.parquet'),
            original_hourly_sha256=digest(stage/'original_1h.parquet'),audit_record='verification_record.json',
            audit_record_sha256=digest(stage/'verification_record.json'),artifacts=artifacts,provenance=report['provenance'])
        (stage/'integrity.provenance.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
        validate_bundle(stage,report['symbol'],start,end)
        os.rename(stage,target)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return info


def investigate(symbol,start,end,data_dir,sources,output,verified_dir=None,window_spec=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    local_path=Path(data_dir)/'klines'/symbol/'1h.parquet'
    before=digest(local_path);local=pd.read_parquet(local_path);local['open_time']=pd.to_datetime(local.open_time,utc=True)
    quality(local,'1h');local=select(local,start,end)
    report=dict(symbol=symbol,start=start.isoformat(),end_exclusive=end.isoformat(),status='failed',provenance=[],
        window_selection=window_spec or context(start,end,False),local_1h=dict(path=str(local_path),sha256=before,mtime_utc=pd.Timestamp(local_path.stat().st_mtime,unit='s',tz='UTC').isoformat(),
          collection_time='行별 수집시각/원천 원본 metadata 없음. mtime은 수집시각 증명이 아님.',
          source_claim='기존 코드가 futures/um 및 fapi/klines를 허용한다는 것은 원래 각 행 출처의 증명이 아니다. 현물 혼용으로 단정하지 않음.'),
        source_days={},unresolved=[])
    try:
        parts=[];suspects=set();month=start.normalize().replace(day=1)
        while month<end:
            nxt=month+pd.offsets.MonthBegin(1);left=max(start,month);right=min(end,nxt)
            monthly=sources.archive(symbol,month,'15m',True);piece=select(monthly,left,right)
            parts.append(piece);gap=missing(piece,left,right,STEP)
            for day in sorted(set(t.normalize() for t in gap)):
                a,b=max(left,day),min(right,day+pd.Timedelta(days=1))
                daily=select(sources.archive(symbol,day,'15m',False),a,b)
                if daily is not None:
                    if piece is not None and compare(hourly(piece),hourly(daily))['mismatches']:suspects.add(day)
                    added=daily.loc[daily.open_time.isin(gap)];parts.append(added)
            report['provenance'].append(dict(interval='15m',period_start=left.isoformat(),period_end=right.isoformat(),source='official monthly/daily archives'))
            month=nxt
        candidate=concatenate(parts)
        gaps=missing(candidate,start,end,STEP)
        report['initial_missing_15m']=dict(count=len(gaps),timestamps=[t.isoformat() for t in gaps],ranges=ranges(gaps,STEP))
        first=compare(hourly(candidate),local);report['initial_local_parity']=first
        suspects.update(utc(d['time']).normalize() for d in first['mismatches'])
        suspects.update(t.normalize() for t in gaps)
        suspects.update(t.normalize() for t in missing(local,start,end,pd.Timedelta(hours=1)))
        reference=plain(local)
        if hasattr(sources,'revalidate_cache'):sources.revalidate_cache=True
        initial_hour_missing=missing(local,start,end,pd.Timedelta(hours=1))
        report['initial_missing_reference_1h']=dict(count=len(initial_hour_missing),timestamps=[t.isoformat() for t in initial_hour_missing])
        for day in sorted(suspects):
            a,b=max(start,day),min(end,day+pd.Timedelta(days=1));month=day.replace(day=1)
            m15=select(sources.archive(symbol,month,'15m',True),a,b)
            d15=select(sources.archive(symbol,day,'15m',False),a,b)
            m1=select(sources.archive(symbol,month,'1h',True),a,b)
            d1=select(sources.archive(symbol,day,'1h',False),a,b)
            r15=sources.rest(symbol,a,b,'15m');r1=sources.rest(symbol,a,b,'1h')
            values={'local_1h':select(local,a,b),'monthly_15m_aggregated':hourly(m15),'daily_15m_aggregated':hourly(d15),
                    'REST_15m_aggregated':hourly(r15),'monthly_1h':m1,'daily_1h':d1,'REST_1h':r1}
            cross={f'{name}_vs_local':compare(value,values['local_1h']) for name,value in values.items() if name!='local_1h'}
            day_report=dict(start=a.isoformat(),end_exclusive=b.isoformat(),source_row_counts={k:len(v) if v is not None else None for k,v in values.items()},cross_comparisons=cross,
                            pairwise={f'{x}_vs_{y}':compare(values[x],values[y]) for i,x in enumerate(values) for y in list(values)[i+1:]},
                            source_coverage={k:dict(missing_count=len(missing(v,a,b,pd.Timedelta(hours=1)))) for k,v in values.items()},
                            original_csv_decimals={},recovery_verified=False)
            # Only disputed hours require detailed original precision evidence.
            bad_hours=sorted({utc(d['time']) for d in first['mismatches'] if a<=utc(d['time'])<b})
            for hour in bad_hours:
                # Source frames retain lexical CSV fields where available.
                raw_m15=sources.archive(symbol,month,'15m',True)
                raw_d15=sources.archive(symbol,day,'15m',False)
                raw_m1=sources.archive(symbol,month,'1h',True)
                raw_d1=sources.archive(symbol,day,'1h',False)
                day_report['original_csv_decimals'][hour.isoformat()]={k:exact_archive_lexemes(v,hour,hour+pd.Timedelta(hours=1)) for k,v in
                    [('monthly15',raw_m15),('daily15',raw_d15),('monthly1h',raw_m1),('daily1h',raw_d1)]}
            okay,reason=decide_day(r15,r1,d15,[m1,d1],a,b)
            day_report['decision']=reason
            observations=[]
            if m15 is not None and len(m15)==0:observations.append('월별15m ZIP은 존재하지만 이 날짜의 행은 0개 (ZIP 내부 누락).')
            if d15 is None:observations.append('일별15m 조회 불가. HTTP 404/네트워크/권한은 evidence의 실제 응답으로 구분.')
            if m15 is not None and d15 is not None and len(m15) and len(d15):
                if not equal(m15,d15):observations.append('월별15m와 일별15m의 값 자체가 다름. 허용오차 확대/다수결 정정 금지.')
                elif compare(hourly(m15),m1)['mismatches']:observations.append('월별/일별15m는 서로 같지만 공식1h와 불일치: interval 간 공식 원천차이, REST 판정 필요.')
            if d15 is not None and m1 is not None and d1 is not None and equal(hourly(d15),m1) and equal(m1,d1):
                observations.append('일별15m 합성과 공식 월별/일별1h가 서로 일치.')
            if values['local_1h'] is not None and m1 is not None and len(values['local_1h']) and equal(values['local_1h'],m1):
                observations.append('이 날짜 기존 로컬1h는 새 조회한 공식 월별1h와 일치. 원래 수집 경로까지 증명하는 것은 아님.')
            if r15 is None or r1 is None:observations.append('REST 독립 판정 불가: 복구/참값 확정 보류.')
            day_report['observations']=observations
            day_report['http_summary']=[{k:e.get(k) for k in ['url','params','http_status','cached','checksum_verified','error_body']} for e in sources.evidence
                if day.strftime('%Y-%m-%d') in e['url'] or ((e.get('params') or {}).get('startTime')==int(a.timestamp()*1000))]
            if okay:
                old_c=select(candidate,a,b);old_r=select(reference,a,b)
                candidate=concatenate([candidate.loc[~((candidate.open_time>=a)&(candidate.open_time<b))] if candidate is not None else None,r15])
                reference=concatenate([reference.loc[~((reference.open_time>=a)&(reference.open_time<b))],r1])
                day_report['recovery_verified']=True
                day_report['changed_15m_vs_previous']=compare(hourly(r15),hourly(old_c))
                day_report['new_1h_vs_immutable_original']=compare(r1,old_r)
                report['provenance'].append(dict(start=a.isoformat(),end_exclusive=b.isoformat(),source='independently queried REST15m/REST1h, archives cross-checked'))
            else:report['unresolved'].append(dict(day=day.isoformat(),reason=reason))
            report['source_days'][day.isoformat()]=day_report
        final_gap=missing(candidate,start,end,STEP);final_hour_gap=missing(reference,start,end,pd.Timedelta(hours=1))
        report['final_missing_15m']=dict(count=len(final_gap),timestamps=[t.isoformat() for t in final_gap],ranges=ranges(final_gap,STEP))
        report['final_missing_reference_1h']=dict(count=len(final_hour_gap),ranges=ranges(final_hour_gap,pd.Timedelta(hours=1)))
        report['final_parity']=compare(hourly(candidate),reference)
        if not final_gap.empty or not final_hour_gap.empty or report['final_parity']['mismatches'] or report['unresolved']:
            report['error']='공식 자료 누락/불일치 또는 REST 교차검증 미완료. 백테스트 금지.'
        else:
            validate15(candidate,start,end);parity(candidate,reference,start,end)
            report['status']='verified'
            report['evidence']=list(sources.evidence)
            if verified_dir is not None:
                report['verified_input']=publish_verified(candidate,reference,report,Path(verified_dir)/'klines'/symbol)
            report['loader_notice']='명시적 허용된 검증 로더가 공식 증거/체크섬/복구 일자 교차검증을 다시 수행한다. 출처 검사를 제거하지 않는다.'
    except (OSError,ValueError,KeyError,TypeError,zipfile.BadZipFile) as exc:
        report['status']='failed';report['error']=str(exc)
    finally:
        if digest(local_path)!=before:raise RuntimeError('원본 1h hash 변동 감지!')
        report['local_1h']['unchanged']=True
        report['evidence']=sources.evidence
        (output/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        (output/'report.md').write_text(markdown(report),encoding='utf-8')
        details=report.get('initial_local_parity',{}).get('mismatches',[])
        pd.DataFrame(details).to_csv(output/'mismatches.csv',index=False,encoding='utf-8-sig')
        if report['status']=='failed':(output/'error.json').write_text(json.dumps({'error':report.get('error')},ensure_ascii=False)+'\n')
    return report


def markdown(report):
    version='v0.9.2 명시 연구 창' if report.get('window_selection',{}).get('explicit') else 'v0.9.1'
    text=f'# {report["symbol"]} {version} 데이터 정합성 조사\n\n상태: {report["status"]}\n\nUTC {report["start"]} ~ {report["end_exclusive"]} (종료 제외)\n\n'
    text+=report.get('window_selection',{}).get('notice','')+'\n\n'
    text+='원본 1h 및 v0.1~v0.8 결과를 수정하지 않았습니다. 수익률 계산/전략 실행은 하지 않습니다.\n\n'
    text+='## 확인된 증거\n\n원본 1h SHA256: '+report['local_1h']['sha256']+'\n\n'
    text+=report['local_1h']['collection_time']+'\n\n'+report['local_1h']['source_claim']+'\n\n'
    for key in ['initial_missing_15m','initial_local_parity','final_missing_15m','final_missing_reference_1h','final_parity']:
        text+=f'{key}:\n\n```json\n'+json.dumps(report.get(key),ensure_ascii=False,indent=2)+'\n```\n\n'
    for day,value in report['source_days'].items():
        text+=f'## {day}\n\n결론: {value["decision"]}\n\n'+json.dumps(value['source_row_counts'],ensure_ascii=False)+'\n\n'+'\n\n'.join(value.get('observations',[]))+'\n\nHTTP/캐시 증거:\n\n```json\n'+json.dumps(value.get('http_summary',[]),ensure_ascii=False,indent=2)+'\n```\n\n'
    text+='\n## 원인과 불확실성\n\nHTTP 404(파일 없음), ZIP 내부 누락, REST 접근 제한/누락, 실제 출처 수치 차이를 audit.json의 evidence/coverage/pairwise에서 구분합니다. 캐시의 HTTP 상태 null은 새 요청이 아니라 과거 검증 파일 재사용이며 현재 200을 뜻하지 않습니다. 체크섬 통과만으로 내용 완전성을 인증하지 않습니다.\n\n'
    text+='원본 CSV decimal과 고정 공차를 기록합니다. serialized float의 decimal 차이와 원본 lexical decimal은 구별합니다. 실제 가격/거래량 차이를 float 오차로 덮거나 공차를 확대하지 않습니다. 월/일/REST가 모순되면 다수결로 정정하지 않습니다.\n\n'
    text+='미해결: '+json.dumps(report['unresolved'],ensure_ascii=False)+'\n\n'+report.get('error',report.get('loader_notice',''))+'\n\n'
    text+='백테스트는 모든 15m와 신뢰 가능한 동기간 1h 검증 전까지 금지합니다. 복구 데이터는 별도 디렉터리만 사용하며 출처 검사를 우회하지 않습니다. 역사적 데이터는 모두 관찰한 in-sample이며 투자 성과 인증이 아닙니다.\n'
    return text


def main(argv=None):
    parser=argparse.ArgumentParser(description='v0.9.1 공식 아카이브/REST 교차검증 및 격리 복구')
    parser.add_argument('--symbols',nargs='+',choices=['BTCUSDT','ETHUSDT'],default=['BTCUSDT','ETHUSDT'])
    for flag,default in [('data-dir','data'),('cache-dir','data/intraday_integrity_v091_cache'),('legacy-cache','data/intraday_downloads_v09'),
                         ('v08-dir','data/timeframe_v08'),('output-dir',None),('verified-dir',None)]:
        parser.add_argument('--'+flag,type=Path,default=Path(default) if default else None)
    parser.add_argument('--start');parser.add_argument('--end')
    args=parser.parse_args(argv);explicit=args.start is not None or args.end is not None
    try:
        chosen=selected_window(None,args.v08_dir,args.start,args.end) if explicit else None
    except ValueError as exc:print(f'기간 오류: {exc}',file=sys.stderr);return 1
    args.output_dir=args.output_dir or Path('data/intraday_integrity_v092_window' if explicit else 'data/intraday_integrity_v091')
    args.verified_dir=args.verified_dir or Path('data/intraday_v092_verified_input' if explicit else 'data/intraday_v09_verified_input')
    out=args.output_dir.resolve();verified=args.verified_dir.resolve();cache=args.cache_dir.resolve()
    protected=[Path('.venv').resolve(),args.v08_dir.resolve(),args.legacy_cache.resolve()]+[(args.data_dir/k).resolve() for k in
        ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06','exit_policy_v07','timeframe_v08','intraday_v09']]
    protected += [(Path('data')/k).resolve() for k in ['klines','backtests','diagnostics','regimes','experiments','strategy_lab_v05','root_cause_v06','exit_policy_v07','timeframe_v08','intraday_v09','intraday_downloads_v09']]
    if explicit:protected += [Path(k).resolve() for k in ['data/intraday_integrity_v091','data/intraday_v09_verified_input','data/intraday_v091_research']]
    targets=[out,verified,cache]
    if any(x==p or p in x.parents or x in p.parents for x in targets for p in protected) or any(a==b or a in b.parents or b in a.parents for i,a in enumerate(targets) for b in targets[i+1:]):
        print('감사/복구/캐시 출력이 보호 경로 또는 서로와 겹침',file=sys.stderr);return 1
    try:out.mkdir(parents=True,exist_ok=False)
    except OSError as exc:print(f'새 감사 출력 필요: {exc}',file=sys.stderr);return 1
    if chosen is not None:(out/'window_spec.json').write_text(json.dumps(context(*chosen,True),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    status={};failed=False
    for symbol in dict.fromkeys(args.symbols):
        try:
            start,end=selected_window(symbol,args.v08_dir,args.start,args.end)
            sources=OfficialSources(cache,args.legacy_cache)
            result=investigate(symbol,start,end,args.data_dir,sources,out/symbol,verified,context(start,end,explicit))
            status[symbol]=result['status'];failed|=result['status']!='verified'
            print(f'{symbol}: {result["status"]}. {out/symbol}/report.md')
        except (OSError,ValueError,KeyError,TypeError) as exc:
            failed=True;target=out/symbol;target.mkdir(exist_ok=True)
            (target/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n');status[symbol]='failed'
            print(f'{symbol}: 조사 실패 — {exc}',file=sys.stderr)
    (out/'status.json').write_text(json.dumps(dict(status='blocked' if failed else 'verified',symbols=status,backtest_allowed=not failed),ensure_ascii=False,indent=2)+'\n')
    return int(failed)


if __name__=='__main__':raise SystemExit(main())
