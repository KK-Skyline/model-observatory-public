"""Connection visibility metadata; model comparison results are never changed here."""
import collections
import re


def failure_code(payload):
    if payload.get('type')!='inference_failed' or payload.get('partial_response_payload') is not None:
        return None
    message=payload.get('error')
    if not isinstance(message,str):return None
    status=re.search(r'(?:unexpected status|HTTP(?:/[0-9.]+)?(?: error)?|status code)\s*:?\s*(\d{3})\b',message,re.I)
    if status:return 'gateway_'+status[1] if status[1] in ('502','503','504') else None
    if re.search(r'failed to connect|error trying to connect|connection refused|network is unreachable|dns (?:error|lookup)|connection timed out',message,re.I):
        return 'connect_failed'
    if re.search(r'stream disconnected|websocket closed|failed to send websocket request',message,re.I):
        return 'stream_disconnected'
    return None


def annotate(threads,events,now,collector_pid,allow_ignore=True):
    by_thread=collections.defaultdict(list)
    for source,event in events:
        if isinstance(event.get('thread_id'),str):by_thread[event['thread_id']].append((source,event))
    ignored=0;ignored_connections=0;ignored_cancellations=0
    for thread in threads:
        rows=thread['records'];entries=by_thread[thread['id']]
        retries=set();latest={};sources=[]
        for source,event in entries:
            kind=event['kind'];connection=event.get('connection_id')
            if not isinstance(connection,str) or not connection:continue
            if kind in ('http_connect_retry','websocket_connect_retry'):
                retries.add((connection,event.get('attempt')));state='reconnecting'
            elif kind in ('http_connect_recovered','websocket_connect_recovered','http_connect_succeeded','websocket_connect_succeeded'):
                state='connected'
            elif kind in ('http_error','websocket_error') and event.get('stage') in ('tcp_connect','tls_handshake'):
                state='failed'
            else:continue
            latest[connection]=(state,event['at'],event.get('pid'))
            sources.append(source)
        failures=[row for row in rows if row.get('connection_failure_code')]
        current=rows[0] if rows else None
        native_failed=bool(current and current.get('connection_failure_code'))
        has_response=bool(current and (current.get('outcome')=='inference_completed' or current.get('has_partial_response') or current.get('upstream_model')))
        live=[value[0] for value in latest.values() if value[2]==collector_pid and 0<=now-value[1]<=120000]
        connecting=bool(live) and all(state in ('failed','reconnecting') for state in live)
        state='failed' if native_failed else 'reconnecting' if connecting and 'reconnecting' in live else 'failed' if connecting else 'connected' if 'connected' in live else 'unknown'
        cancelled=bool(allow_ignore and current and (current.get('outcome')=='inference_cancelled' or current.get('cancelled_by_turn')) and not has_response)
        eligible=allow_ignore and not has_response and not cancelled and (native_failed or connecting)
        sources.extend(row['connection_failure_source'] for row in failures)
        thread['ignore_connection_failure']=bool(eligible)
        thread['ignore_cancelled_without_response']=cancelled
        thread['ignore_default']=bool(eligible or cancelled)
        thread['ignore_reason']='cancelled_without_response' if cancelled else 'connection_failure' if eligible else None
        thread['connection']={'state':state,'reconnect_count':len(retries),'failed_attempts':len(failures),
            'failure_codes':sorted({row['connection_failure_code'] for row in failures}),
            'sources':sources[-8:],'reason':'最近一次请求有连接失败或重连证据，且无完成或部分响应；历史保留' if eligible else None}
        ignored+=bool(eligible or cancelled);ignored_connections+=bool(eligible);ignored_cancellations+=cancelled
    return {'default_ignored_threads':ignored,'ignored_connection_threads':ignored_connections,
        'ignored_cancelled_threads':ignored_cancellations,'evidence':'native outcomes, partial-response flags and collector thread/connection IDs'}
