"""Add execution timeout evidence without changing model comparison states."""
import collections
import re

TIMEOUT=re.compile(r'\b(?:timeout|timed out)\b',re.I)


def annotate_timeouts(threads,completed,events):
    diagnostics=[];by_thread=collections.Counter()
    for source,event in events:
        if event['kind']!='upstream_wait_timeout':continue
        entry={key:event.get(key) for key in ('at','thread_id','connection_id','transport','stage','wait_limit_seconds')}
        entry.update(state='timeout',label='超时',source=source)
        diagnostics.append(entry)
        if isinstance(entry['thread_id'],str):by_thread[entry['thread_id']]+=1
    for thread in threads:
        for record in thread['records']:
            key=(record['thread_id'],record['codex_turn_id'],record['inference_call_id'])
            done=completed.get(key,[]);timed_out=False
            if len(done)==1 and done[0][1]['type']=='inference_failed':
                message=done[0][1].get('error')
                timed_out=isinstance(message,str) and bool(TIMEOUT.search(message))
            record['execution_state']='timeout' if timed_out else None
            record['timeout_source']=done[0][3] if timed_out else None
        thread['execution_state']=thread['records'][0]['execution_state'] if thread['records'] else None
        # A thread/connection event alone cannot identify its latest inference call.
        # 仅线程/连接事件无法证明属于该线程最新推理调用。
        thread['timeout_history_count']=by_thread[thread['id']]
    return diagnostics
