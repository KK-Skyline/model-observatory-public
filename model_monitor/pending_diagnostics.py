"""Explain evidence gaps without changing model comparisons or linking requests."""
import collections

LABELS={
    'execution_timeout':'推理请求超时',
    'monitor_read_error':'监控证据读取异常',
    'stale':'超过24小时，仅供历史追查',
    'selection_conflict':'同回合选择冲突',
    'response_id_mismatch':'原生回包response_id不匹配',
    'selection_missing':'缺少同回合选择快照',
    'native_terminal_missing':'尚无原生推理终态',
    'native_terminal_unsuccessful':'原生推理失败或取消',
    'native_call_ambiguous':'原生调用ID不唯一',
    'capture_ambiguous':'上游采集记录不唯一',
    'capture_corrupt':'响应采集不完整',
    'upstream_incomplete':'上游返回未完成响应',
    'upstream_failed':'上游返回失败响应',
    'upstream_not_completed':'上游未返回完成终态',
    'model_conflict':'独立模型声明冲突',
    'capture_model_missing':'真实回包没有声明模型',
    'response_model_missing_no_capture':'原生响应无model，且无可关联上游采集',
}


def diagnose_records(threads,starts,completed,captures,matched,now,blocking_count,selection_timings):
    threads=list(threads)
    start_counts=collections.Counter(key for key,_ in starts)
    source_counts=collections.Counter();capture_coverage=collections.Counter();delays=[]
    native_ids={}
    for thread in threads:
        for record in thread['records']:
            key=(record['thread_id'],record['codex_turn_id'],record['inference_call_id'])
            done=completed.get(key,[]);rid=record['response_id'];items=captures.get((key[0],rid),[]) if rid else []
            if rid:native_ids.setdefault(rid,set()).add(key[0])
            blockers=[]
            if blocking_count:blockers.append('monitor_read_error')
            if record.get('execution_state')=='timeout':blockers.append('execution_timeout')
            if now-record['updated_at']>24*60*60*1000:blockers.append('stale')
            if not record['selected_model']:blockers.append('selection_conflict' if '选择冲突' in record['upstream_reason'] else 'selection_missing')
            if 'response_id 关联失败' in record['upstream_reason']:blockers.append('response_id_mismatch')
            if start_counts[key]!=1 or len(done)>1:blockers.append('native_call_ambiguous')
            elif not done:blockers.append('native_terminal_missing')
            elif done[0][1]['type']!='inference_completed':blockers.append('native_terminal_unsuccessful')
            capture_coverage['matched' if items else 'no_matching_capture']+=1
            if len(items)>1:blockers.append('capture_ambiguous')
            elif items:
                captured=items[0][1]
                if not captured.get('capture_complete'):blockers.append('capture_corrupt')
                if not captured.get('completed'):
                    blockers.append({'response.incomplete':'upstream_incomplete','response.failed':'upstream_failed'}.get(captured.get('terminal_type'),'upstream_not_completed'))
                if captured.get('conflict'):blockers.append('model_conflict')
                elif captured.get('capture_complete') and captured.get('completed') and not captured.get('upstream_model'):blockers.append('capture_model_missing')
            if not record['upstream_model'] and not blockers and not items:blockers.append('response_model_missing_no_capture')
            # Missing selection/staleness can coexist with a genuinely absent response model.
            # 缺少选择或记录陈旧可能与真实回包缺字段同时存在。
            if not record['upstream_model'] and len(done)==1 and done[0][1]['type']=='inference_completed' and not items and 'response_model_missing_no_capture' not in blockers:
                blockers.append('response_model_missing_no_capture')
            if '冲突' in record['upstream_reason'] and 'model_conflict' not in blockers:blockers.append('model_conflict')
            ended=done[0][0]['wall_time_unix_ms'] if len(done)==1 else None
            if ended is not None and record['upstream_model'] and not items:
                source_counts['native_response_model']+=1
            elif record['upstream_model'] and items:source_counts['collector_response_model']+=1
            else:source_counts['no_usable_model']+=1
            if ended is not None and ended>=record['updated_at']:delays.append(ended-record['updated_at'])
            record['evidence_diagnosis']={'blockers':blockers,'labels':[LABELS[k] for k in blockers],
                'native_terminal_at':ended,'matching_capture_records':len(items),
                'capture_recorded_at':None,'capture_timestamp_available':False}
    unmatched=collections.Counter()
    for (tid,rid),items in captures.items():
        if (tid,rid) in matched:continue
        category='thread_id_mismatch' if rid in native_ids and tid not in native_ids[rid] else 'native_join_ambiguous' if tid in native_ids.get(rid,set()) else 'no_matching_native_response_id'
        unmatched[category]+=len(items)
    def summarize(records,unrecorded=0):
        pending=[r for r in records if r['upstream_status']=='待验证']
        return {'records':len(records),'unrecorded_threads':unrecorded,'pending':len(pending),
            'primary_reasons':dict(collections.Counter(r['evidence_diagnosis']['blockers'][0] if r['evidence_diagnosis']['blockers'] else 'unclassified' for r in pending)),
            'blockers':dict(collections.Counter(k for r in pending for k in r['evidence_diagnosis']['blockers']))}
    latest=[t for t in threads if not t.get('ignore_default',t.get('ignore_connection_failure')) and not t['archived']]
    projects={}
    for thread in latest:
        key=(thread['project_id'],thread['project'])
        group=projects.setdefault(key,{'project_id':key[0],'project':key[1],'threads':0,'pending_threads':0,'unrecorded_threads':0,'selection_read_ms':0,'selection_files':0,'blockers':collections.Counter()})
        group['threads']+=1
        timing=selection_timings.get(thread['id'])
        if timing:
            group['selection_read_ms']+=timing['read_ms'];group['selection_files']+=timing['files']
        if not thread['records']:group['unrecorded_threads']+=1;group['pending_threads']+=1
        elif thread['records'][0]['upstream_status']=='待验证':
            group['pending_threads']+=1;group['blockers'].update(thread['records'][0]['evidence_diagnosis']['blockers'])
    for group in projects.values():
        group['blockers']=dict(group['blockers']);group['selection_read_ms']=round(group['selection_read_ms'],3)
    def percentile(values,fraction):return sorted(values)[round((len(values)-1)*fraction)] if values else None
    return {'project_readings':list(projects.values()),'labels':LABELS,'latest_default':summarize([t['records'][0] for t in latest if t['records']],sum(not t['records'] for t in latest)),
        'history':summarize([r for t in threads for r in t['records']]),'evidence_sources':dict(source_counts),
        'capture_coverage':dict(capture_coverage),'unmatched_captures':dict(unmatched),
        'latency':{'native_inference_duration_ms':{'samples':len(delays),'p50':percentile(delays,.5),'p95':percentile(delays,.95)},
            'completion_to_capture_ms':None,'capture_to_scan_ms':None,
            'unavailable_reason':'现有上游记录未保存采集时间；不能用文件mtime或推理耗时冒充采集延迟'}}
