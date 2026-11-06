"""Read-only Codex metadata monitor; Python 3.9+, no dependencies."""
import argparse
import collections
import hashlib
import hmac
import imghdr
import re
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import webbrowser
from .connectivity import failure_code, annotate as annotate_connectivity
from .project_sources import desktop_bindings
from .monitoring_scope import is_subagent
from .pending_diagnostics import diagnose_records
from .rollout_sources import session_sources
from .timeout_state import annotate_timeouts

BASE = Path(__file__).resolve().parent
WALLPAPERS = {'default', 'aurora', 'dunes'}
STALE_MS = 24 * 60 * 60 * 1000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMPORTED = re.compile(r'import-[0-9a-f]{64}\.(png|jpeg|webp)\Z')


def compare(selected, actual, aliases=None):
    aliases = aliases or {}
    for rule in aliases.values():
        if not isinstance(rule, dict) or not rule.get('source') or not rule.get('canonical'):
            raise ValueError('别名规则必须记录 canonical 和确认来源 source')
    def canonical(value):
        return aliases[value]['canonical'] if value in aliases else value
    if not selected or not actual:
        return '待验证'
    return '一致' if canonical(selected) == canonical(actual) else '不一致'


def valid_wallpaper(directory, name):
    return isinstance(name,str) and (name in WALLPAPERS or
        bool(IMPORTED.fullmatch(name) and (Path(directory)/'wallpapers'/name).is_file()))


def import_wallpaper(directory, image):
    if not image or len(image)>MAX_IMAGE_BYTES:
        raise ValueError('图片必须小于 8 MiB')
    kind=imghdr.what(None,h=image)
    if kind not in ('png','jpeg','webp'):
        raise ValueError('仅支持 PNG / JPEG / WebP 静态图片')
    name='import-'+hashlib.sha256(image).hexdigest()+'.'+kind
    folder=Path(directory)/'wallpapers';folder.mkdir(exist_ok=True)
    path=folder/name
    temp=folder/(name+'.tmp')
    with temp.open('wb') as f:
        os.chmod(str(temp),0o600)
        f.write(image);f.flush();os.fsync(f.fileno())
    os.replace(str(temp),str(path))
    return {'wallpaper':name}


def read_settings(directory):
    p = Path(directory) / 'settings.json'
    if not p.exists():
        return {'wallpaper': 'default'}
    data = json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'wallpaper'} or not valid_wallpaper(directory,data['wallpaper']):
        raise ValueError('壁纸设置无效')
    return data


def save_settings(directory, data):
    if not isinstance(data, dict) or set(data) != {'wallpaper'} or not valid_wallpaper(directory,data['wallpaper']):
        raise ValueError('壁纸必须为示例、默认或已导入的有效图片')
    p = Path(directory) / 'settings.json'
    temp = p.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        os.chmod(str(temp), 0o600)
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(temp), str(p))
    return data


class Scanner:
    def __init__(self, db, traces, capture_dir=None):
        self.db = Path(db)
        self.traces = Path(traces).resolve()
        self.capture_dir = Path(capture_dir) if capture_dir else None
        self.last_success = None
        self.lock = threading.Lock()
        self.payload_cache = {}

    def scan(self):
        with self.lock:
            return self._scan()

    def _scan(self):
        scan_started = int(time.time()*1000)
        errors = collections.Counter()
        blocking_errors = collections.Counter()
        scope_errors = collections.Counter()
        gaps = collections.Counter()
        issues = []
        def error(kind, source, scope='evidence'):
            errors[kind] += 1
            scope_errors[scope] += 1
            if scope=='evidence':blocking_errors[kind] += 1
            if len(issues) < 30:
                issues.append({'kind': kind, 'source': source, 'scope':scope})

        def load(path, source):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    error('missing_fields', source)
                    return None
                return data
            except (ValueError, UnicodeError):
                error('bad_json', source)
            except OSError:
                error('read_failure', source)
            return None

        def lines(path, source, report_error=error):
            try:
                with path.open(encoding='utf-8') as f:
                    for n, line in enumerate(f, 1):
                        try:
                            obj = json.loads(line)
                            if not isinstance(obj, dict):
                                report_error('missing_fields', source+':'+str(n))
                            else:
                                yield n, obj
                        except (ValueError, UnicodeError):
                            report_error('bad_json', source+':'+str(n))
            except (OSError, UnicodeError):
                report_error('read_failure', source)

        threads = {}
        excluded_subagents = set()
        excluded_internal = set()
        excluded_threads = set()
        try:
            with sqlite3.connect(self.db.resolve().as_uri()+'?mode=ro', uri=True) as c:
                c.row_factory = sqlite3.Row
                projects = {r['id']:r['name'] for r in c.execute('SELECT id,name FROM projects')}
                desktop_projects,project_issues=desktop_bindings(self.db.parent/'.codex-global-state.json',projects)
                for kind,source in project_issues:error(kind,source,scope='project')
                columns = {r['name'] for r in c.execute('PRAGMA table_info(threads)')}
                tables = {r['name'] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                roots = list(c.execute('SELECT project_id,path FROM project_roots')) if 'project_roots' in tables else []
                query = 'SELECT id,rollout_path,name,title,project_id,archived,updated_at'+''.join(','+name for name in ('cwd','source','thread_source') if name in columns)+' FROM threads'
                for r in c.execute(query):
                    signals=[];invalid_source=False
                    for name in ('source','thread_source'):
                        if name not in columns:continue
                        try:signals.append(is_subagent(r[name]))
                        except (ValueError,UnicodeError):invalid_source=True
                    child=any(signals)
                    if invalid_source:error('scope_metadata_invalid','database-source/'+r['id'],scope='monitoring')
                    if child:excluded_subagents.add(r['id']);excluded_threads.add(r['id']);continue
                    project_id = r['project_id']
                    project_candidates = []
                    project_source = 'threads.project_id' if project_id in projects else '无明确项目归属'
                    desktop=desktop_projects.get(r['id']) if project_id is None else None
                    if desktop:
                        project_id=desktop['project_id'];project_source=desktop['source']
                    if project_id is None and desktop is None and 'cwd' in columns:
                        cwd = Path(r['cwd']).resolve()
                        matches = [(len(Path(root['path']).resolve().parts),root['project_id'],str(Path(root['path']).resolve())) for root in roots
                                   if cwd.is_relative_to(Path(root['path']).resolve()) and root['project_id'] in projects]
                        if matches:
                            depth = max(m[0] for m in matches)
                            owners = {m[1] for m in matches if m[0]==depth}
                            if len(owners)==1:
                                project_id = next(iter(owners))
                                project_source = 'threads.cwd 匹配 project_roots（最长唯一根目录，非显式项目绑定）'
                            else:
                                # A shared project root cannot invalidate independent model evidence.
                                # 多项目共用根目录不能使独立模型证据失效。
                                error('association_failure','project_roots/'+r['id'],scope='project')
                                project_candidates=[{'id':owner,'name':projects[owner],
                                    'root_paths':sorted({m[2] for m in matches if m[0]==depth and m[1]==owner})} for owner in sorted(owners)]
                                project_source='threads.cwd='+str(cwd)+' 匹配多个同深度 project_roots；项目归属待确认：'+', '.join(p['name']+' ['+p['id']+'] @ '+', '.join(p['root_paths']) for p in project_candidates)
                    threads[r['id']] = {'id':r['id'],'name':r['name'] or r['title'] or r['id'],
                        'project':projects.get(project_id,'未分配项目'),
                        'project_id':project_id,'project_source':project_source,
                        'project_status':'ambiguous' if project_candidates else 'resolved' if project_id in projects else 'unassigned',
                        'project_candidates':project_candidates,
                        'archived':bool(r['archived']),'updated_at':r['updated_at']*1000,
                        'records':[],'rollout':r['rollout_path']}
        except (sqlite3.Error, OSError):
            error('database_failure', 'state_5.sqlite (read-only)')

        native_started=time.perf_counter()
        starts = []
        completed = collections.defaultdict(list)
        deferred_trace_errors = []
        if not self.traces.is_dir():
            error('read_failure', 'native-traces/')
        for log in sorted(self.traces.glob('*/trace.jsonl')):
            if not log.resolve().is_relative_to(self.traces):
                error('unsafe_path', 'trace.jsonl');continue
            source = str(log.relative_to(self.traces))
            manifest=log.parent/'manifest.json'
            root_thread=None
            if manifest.exists():
                try:
                    if not manifest.resolve().is_relative_to(self.traces):raise ValueError('scope manifest outside traces')
                    metadata=json.loads(manifest.read_text(encoding='utf-8'))
                    root_thread=metadata.get('root_thread_id') if isinstance(metadata,dict) else None
                    if root_thread is not None and not isinstance(root_thread,str):raise ValueError('invalid scope root thread ID')
                except (OSError,ValueError,UnicodeError):error('scope_metadata_invalid',source+' manifest',scope='monitoring')
            if isinstance(root_thread,str) and root_thread in excluded_threads:
                gaps['excluded_subagent_trace_files' if root_thread in excluded_subagents else 'excluded_internal_task_trace_files']+=1;continue
            trace_errors=[]
            trace_threads=set()
            def trace_error(kind,origin):trace_errors.append((kind,origin))
            for n,e in lines(log, source,trace_error):
                p = e.get('payload')
                if not isinstance(p, dict) or not isinstance(p.get('type'),str):
                    trace_error('missing_fields',source+':'+str(n));continue
                typ = p['type']
                if typ=='thread_started' or typ=='protocol_event_observed' and p.get('event_type')=='session_configured':
                    field='metadata_payload' if typ=='thread_started' else 'event_payload'
                    ref=p.get(field)
                    if not isinstance(ref,dict) or not isinstance(ref.get('path'),str):
                        error('scope_metadata_invalid',source+':'+str(n),scope='monitoring');continue
                    metadata_path=(log.parent/ref['path']).resolve()
                    try:
                        if not metadata_path.is_relative_to(self.traces):raise ValueError('scope metadata outside traces')
                        metadata=json.loads(metadata_path.read_text(encoding='utf-8'))
                        if not isinstance(metadata,dict):raise ValueError('invalid scope metadata')
                        tid=p.get('thread_id') if typ=='thread_started' else metadata.get('thread_id')
                        if not isinstance(tid,str) or not tid or metadata.get('thread_id')!=tid:raise ValueError('scope thread ID mismatch')
                        if e.get('thread_id') is not None and e['thread_id']!=tid:raise ValueError('scope envelope thread ID mismatch')
                        parent_thread=metadata.get('parent_thread_id')
                        if parent_thread is not None and not isinstance(parent_thread,str):raise ValueError('invalid parent scope identity')
                        if typ!='thread_started' and (tid not in trace_threads and root_thread!=tid and parent_thread not in ({root_thread} | trace_threads)-{None} or metadata.get('type')!='session_configured'):raise ValueError('configured scope root mismatch')
                        if typ=='thread_started':trace_threads.add(tid)
                        signal=metadata.get('session_source') if typ=='thread_started' else metadata.get('thread_source')
                        if is_subagent(signal):excluded_subagents.add(tid);excluded_threads.add(tid)
                        elif typ!='thread_started' and signal=='thread_title':excluded_internal.add(tid);excluded_threads.add(tid)
                        if root_thread==tid and tid in excluded_threads:
                            gaps['excluded_subagent_trace_files' if tid in excluded_subagents else 'excluded_internal_task_trace_files']+=1;break
                    except (OSError,ValueError,UnicodeError):error('scope_metadata_invalid',source+':'+str(n),scope='monitoring')
                    continue
                if typ not in ('inference_started','inference_completed','inference_failed','inference_cancelled'):
                    continue
                tid = e.get('thread_id')
                turn = e.get('codex_turn_id')
                call = p.get('inference_call_id')
                if not all(isinstance(v,str) and v for v in (tid,turn,call)) or not isinstance(e.get('wall_time_unix_ms'),(int,float)) or 'seq' not in e:
                    trace_error('missing_fields',source+':'+str(n));continue
                if p.get('thread_id',tid)!=tid or p.get('codex_turn_id',turn)!=turn:
                    trace_error('association_failure',source+':'+str(n));continue
                key = (tid, turn, call)
                entry = (e,p,log,source+':'+str(n))
                if typ == 'inference_started':
                    starts.append((key,entry))
                else:
                    completed[key].append(entry)
            deferred_trace_errors.append((root_thread,trace_errors))
        for root_thread,trace_errors in deferred_trace_errors:
            if isinstance(root_thread,str) and root_thread in excluded_threads:
                gaps['excluded_subagent_trace_errors' if root_thread in excluded_subagents else 'excluded_internal_task_trace_errors']+=len(trace_errors)
            else:
                for kind,origin in trace_errors:error(kind,origin)
        gaps['excluded_subagent_inference_starts']=sum(key[0] in excluded_subagents for key,_ in starts)
        gaps['excluded_internal_task_inference_starts']=sum(key[0] in excluded_internal for key,_ in starts)
        starts=[entry for entry in starts if entry[0][0] not in excluded_threads]
        completed={key:entries for key,entries in completed.items() if key[0] not in excluded_threads}
        for tid in excluded_threads:threads.pop(tid,None)
        native_read_ms=round((time.perf_counter()-native_started)*1000,3)
        selection_started=time.perf_counter()
        selection_timings={}
        selections = {}
        aborted_turns = {}
        wanted=set(threads)|{key[0] for key,_ in starts}
        additional_sources=session_sources(self.db.parent,wanted,error,gaps)
        for tid in sorted(wanted):
            thread_read_started=time.perf_counter()
            paths=[]
            if tid in threads:paths.append(Path(threads[tid].pop('rollout')).resolve())
            for path in additional_sources.get(tid,[]):
                if path not in paths:
                    paths.append(path);gaps['additional_selection_sources']+=1
            contexts = collections.defaultdict(list)
            for path in paths:
                origin='rollout/'+tid+' @ '+str(path)
                source_valid=path not in additional_sources.get(tid,[])
                for n,e in lines(path, origin):
                    if n==1 and e.get('type')=='session_meta':
                        identity=e.get('payload')
                        if not isinstance(identity,dict) or identity.get('id')!=tid:
                            source_valid=False;error('association_failure',origin+' database session identity mismatch');break
                    if not source_valid:
                        identity=e.get('payload')
                        if n!=1 or e.get('type')!='session_meta' or not isinstance(identity,dict) or identity.get('id')!=tid:
                            error('association_failure',origin+' session identity changed');break
                        source_valid=True
                    lifecycle=e.get('payload')
                    if e.get('type')=='event_msg' and isinstance(lifecycle,dict) and lifecycle.get('type')=='turn_aborted':
                        abort_id=lifecycle.get('turn_id')
                        if isinstance(abort_id,str) and abort_id:
                            aborted_turns[(tid,abort_id)]=origin+':'+str(n)+' turn_aborted'
                        else:gaps['unattributed_turn_aborts']+=1
                        continue
                    if e.get('type') != 'turn_context':
                        continue
                    p = e.get('payload')
                    if not isinstance(p, dict) or not isinstance(p.get('turn_id'), str) or not isinstance(p.get('model'), str):
                        error('missing_fields', origin+':'+str(n)); continue
                    contexts[p['turn_id']].append((p['model'],origin+':'+str(n)))
                if not source_valid:error('association_failure',origin+' session identity unavailable')
            selection_timings[tid]={'files':len(paths),'read_ms':round((time.perf_counter()-thread_read_started)*1000,3),'turns':len(contexts)}
            for turn, values in contexts.items():
                models = {v[0] for v in values}
                selections[(tid,turn)] = (next(iter(models)) if len(models)==1 else None,
                    ', '.join(v[1] for v in values)+' turn_context.model',
                    '同回合选择冲突' if len(models)>1 else '')

        selection_read_ms=round((time.perf_counter()-selection_started)*1000,3)
        gaps['excluded_subagent_threads']=len(excluded_subagents)
        gaps['excluded_internal_task_threads']=len(excluded_internal)
        start_counts = collections.Counter(key for key,_ in starts)

        live_payloads = set()
        def payload(p, field, log, source):
            ref = p.get(field)
            if not isinstance(ref,dict) or not isinstance(ref.get('path'),str):
                error('missing_fields',source+' '+field);return None,source
            path = (log.parent/ref['path']).resolve()
            if not path.is_relative_to(self.traces):
                error('unsafe_path',source+' '+field);return None,source
            rel = str(path.relative_to(self.traces))
            key=(path,field);live_payloads.add(key)
            def fingerprint():
                stat=path.stat()
                return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            try:signature=fingerprint()
            except OSError:
                self.payload_cache.pop(key,None);gaps['payload_cache_stat_failure']+=1
                return load(path,rel),rel
            cached=self.payload_cache.get(key)
            if cached and cached[0]==signature:return cached[1],rel
            self.payload_cache.pop(key,None)
            data=load(path,rel)
            if data is None:return None,rel
            try:after=fingerprint()
            except OSError:
                gaps['payload_cache_stat_failure']+=1
                return data,rel
            if signature!=after:
                error('read_failure',rel+' changed while reading');return None,rel
            # Cache only validated file identity and needed fields, never prompt/output bodies.
            # 只缓存文件身份与必要字段，绝不缓存提示词或输出正文。
            keys=('model',) if field=='request_payload' else ('response_id','id','model')
            metadata={k:data[k] for k in keys if k in data}
            if any(v is not None and not isinstance(v,str) for v in metadata.values()):return data,rel
            if field=='response_payload' and 'response' in data:
                body=data['response']
                if not isinstance(body,dict) or body.get('model') is not None and not isinstance(body['model'],str):return data,rel
                metadata['response']={k:body[k] for k in ('model',) if k in body}
            self.payload_cache[key]=(signature,metadata)
            return metadata,rel

        capture_index = collections.defaultdict(list)
        collector_status = None
        connection_events = []
        capture_alerts = []
        matched_captures = set()
        if self.capture_dir:
            journal=self.capture_dir/'collector-diagnostics.jsonl'
            if journal.exists():
                for line,event in lines(journal,'collector-diagnostics'):
                    if not isinstance(event.get('kind'),str) or type(event.get('at')) is not int:
                        error('connection_metadata_invalid','collector-diagnostics:'+str(line));continue
                    connection_events.append(('collector-diagnostics:'+str(line),event))
            capture_file = self.capture_dir/'upstream.jsonl'
            if capture_file.exists():
                for n,record in lines(capture_file,'upstream-metadata'):
                    if not isinstance(record.get('response_id'),str) or not isinstance(record.get('thread_id'),str):
                        error('capture_missing_ids','upstream-metadata:'+str(n));continue
                    if record['thread_id'] in excluded_threads:
                        gaps['excluded_subagent_captures' if record['thread_id'] in excluded_subagents else 'excluded_internal_task_captures']+=1;continue
                    capture_index[(record['thread_id'],record['response_id'])].append((n,record))
            status_file=self.capture_dir/'collector-status.json'
            if status_file.exists():
                collector_status=load(status_file,'collector-status')
                if collector_status:
                    try:os.kill(collector_status['pid'],0)
                    except (OSError,KeyError,TypeError):error('collector_not_running','collector-status')
                    failures={k:v for k,v in collector_status.get('counts',{}).items() if any(w in k for w in ['error','bad_','invalid','incomplete','missing_','too_large','rejected']) and v}
                    if failures:
                        issues.append({'kind':'collector_diagnostics','counts':failures})
                        current=collector_status.get('current_counts')
                        if isinstance(current,dict):
                            current_failures={k:v for k,v in current.items() if any(w in k for w in ['error','bad_','invalid','incomplete','missing_','too_large','rejected']) and v}
                            disconnects=sum(collector_status['counts'].get(k,0) for k in ('http_client_disconnected','websocket_client_disconnected'))
                            retries=collector_status['counts'].get('http_connect_retry',0)
                            recovered=collector_status['counts'].get('http_connect_recovered',0)
                            capture_alerts.append('采集器历史累计异常（保留）：'+json.dumps(failures,ensure_ascii=False)+'；本次启动新增异常：'+(json.dumps(current_failures,ensure_ascii=False) if current_failures else '0')+('；客户端接收端断连累计：'+str(disconnects) if disconnects else '')+('；发送前建连重试/恢复累计：'+str(retries)+'/'+str(recovered) if retries else ''))
                        else:
                            capture_alerts.append('采集器诊断存在异常：'+json.dumps(failures,ensure_ascii=False))
                    total_waits=collector_status.get('counts',{}).get('upstream_wait_timeout',0)
                    if total_waits:
                        current_waits=collector_status.get('current_counts')
                        current_text=str(current_waits.get('upstream_wait_timeout',0)) if isinstance(current_waits,dict) else '未知'
                        capture_alerts.append('上游等待超时：连续 300 秒未收到数据（历史累计 '+str(total_waits)+' 次；本次启动 '+current_text+' 次）；详见采集器诊断。')
        now = int(time.time()*1000)
        for key,(e,p,log,source) in starts:
            tid,turn,call = key
            if tid not in threads:
                threads[tid] = {'id':tid,'name':'未入库对话 '+tid,'project':'未分配项目','project_id':None,
                    'archived':False,'updated_at':e['wall_time_unix_ms'],'records':[],'project_source':'未入库线程，无项目绑定'}
            selected,selection_source,selection_reason = selections.get((tid,turn),(None,'无此回合 turn_context','该轮选择缺失'))
            req,req_source = payload(p,'request_payload',log,source)
            request_model = req.get('model') if req else None
            if req is not None and not isinstance(request_model,str):
                error('missing_fields',req_source+' model');request_model=None
            upstream_model = None
            upstream_source = '无可关联完成回包'
            reason = '请求尚未完成、失败或已取消'
            done = completed.get(key,[])
            if len(done)==1 and start_counts[key]==1 and done[0][1]['type']=='inference_completed':
                de,dp,dl,ds = done[0]
                response,upstream_source = payload(dp,'response_payload',dl,ds)
                reason = '响应缺少独立 model 字段'
                if response is not None:
                    body = response.get('response',response)
                    if isinstance(body,dict):
                        upstream_model = body.get('model')
                    if not isinstance(upstream_model,str) or not upstream_model:
                        upstream_model=None
                    rid = response.get('response_id',response.get('id'))
                    if rid and dp.get('response_id') and rid != dp['response_id']:
                        error('association_failure',ds);upstream_model=None;reason='response_id 关联失败'
                else:
                    reason='响应文件读取失败或更新到一半'
            elif len(done)>1 or start_counts[key]>1:
                reason='调用 ID 重复，无法唯一关联回包'
                error('association_failure',source)
            response_id=done[0][1].get('response_id') if len(done)==1 else None
            captures=capture_index.get((tid,response_id),[]) if response_id else []
            if captures and start_counts[key]==1:
                matched_captures.add((tid,response_id))
                if len(captures)!=1:
                    upstream_model=None;reason='上游采集记录重复，无法唯一核对';error('capture_ambiguity',source)
                else:
                    line,captured=captures[0]
                    upstream_source='upstream-metadata:'+str(line)+' '+','.join(captured.get('source_fields',[]))
                    if not captured.get('capture_complete') or not captured.get('completed'):
                        upstream_model=None;reason='真实响应采集未完整完成'
                    elif captured.get('conflict'):
                        upstream_model=None;reason='上游多个独立模型声明冲突'
                    elif isinstance(captured.get('upstream_model'),str):
                        if upstream_model and upstream_model!=captured['upstream_model']:
                            upstream_model=None;reason='原生响应与采集响应声明冲突';error('capture_conflict',source)
                        else:
                            upstream_model=captured['upstream_model'];reason='真实响应模型，按 thread_id + response_id 精确关联'
                    else:
                        upstream_model=None;reason='已检查真实回包：该回包未声明模型'
            stale = now-e['wall_time_unix_ms'] > STALE_MS
            if stale:
                reason='记录陈旧（超过 24 小时），仅供历史追查'
            upstream_status = compare(selected,upstream_model)
            if stale:
                upstream_status='待验证'
            if not selected:
                reason=selection_reason+'；'+reason
                gaps['selection_missing']+=1
            if not upstream_model:
                gaps['upstream_model_missing']+=1
            if not request_model:
                gaps['request_model_missing']+=1
            if not done:
                gaps['completion_missing']+=1
            if stale:
                gaps['stale_records']+=1
            if selected and upstream_model and not stale:
                reason='独立响应 model 精确比较；仅代表上游声明'
            cancelled_by_turn=(tid,turn) in aborted_turns and (not done or len(done)==1 and done[0][1]['type']=='inference_cancelled')
            r = {'id':hashlib.sha256((source+str(e['seq'])).encode()).hexdigest(),
                 'thread_id':tid,'codex_turn_id':turn,'inference_call_id':call,'sequence':e['seq'],'response_id':response_id,
                 'upstream_request_id':done[0][1].get('upstream_request_id') if len(done)==1 else None,
                 'selected_model':selected,'request_model':request_model,'upstream_model':upstream_model,
                 'selection_source':selection_source,'request_source':req_source,'upstream_source':upstream_source,
                 'request_status':compare(selected,request_model) if not stale else '待验证',
                 'upstream_status':upstream_status,'request_reason':('记录陈旧（超过 24 小时）' if stale else selection_reason or ('请求 model 缺失' if not request_model else '每轮上下文选择与请求体 model 精确比较')),
                 'upstream_reason':reason,'updated_at':e['wall_time_unix_ms'],
                 'outcome':done[0][1]['type'] if len(done)==1 else 'inference_pending',
                 'cancelled_by_turn':cancelled_by_turn,'cancellation_source':aborted_turns.get((tid,turn)) if cancelled_by_turn else None,
                 'has_partial_response':bool(len(done)==1 and done[0][1].get('partial_response_payload') is not None),
                 'connection_failure_code':failure_code(done[0][1]) if len(done)==1 else None,
                 'connection_failure_source':done[0][3] if len(done)==1 else None}
            threads[tid]['records'].append(r)
        self.payload_cache={key:value for key,value in self.payload_cache.items() if key in live_payloads}
        for t in threads.values():
            t['records'].sort(key=lambda r:(r['updated_at'],r['sequence']),reverse=True)
        if capture_index:
            gaps['capture_unmatched_records']=sum(len(v) for k,v in capture_index.items() if k not in matched_captures)
        count = sum(errors.values())
        blocking_count = sum(blocking_errors.values())
        project_count = scope_errors['project']
        monitoring_count = scope_errors['monitoring']
        if blocking_count:
            for t in threads.values():
                for r in t['records']:
                    r['upstream_status']='待验证'
                    r['upstream_reason']='监控读取存在异常；'+r['upstream_reason']
        if not blocking_count:
            self.last_success=now
        connection_visibility=annotate_connectivity(threads.values(),connection_events,now,
            collector_status.get('pid') if collector_status else None,allow_ignore=not blocking_count)
        timeout_diagnostics=annotate_timeouts(threads.values(),completed,connection_events)
        pending_diagnostics=diagnose_records(threads.values(),starts,completed,capture_index,matched_captures,now,blocking_count,selection_timings)
        scan_finished=int(time.time()*1000)
        return {'threads':sorted(threads.values(),key=lambda t:max([t['updated_at']]+[r['updated_at'] for r in t['records']]),reverse=True),
                'errors':dict(errors),'error_count':count,'blocking_error_count':blocking_count,'project_error_count':project_count,'monitoring_error_count':monitoring_count,'issues':issues,'gaps':dict(gaps),
                'unrecorded_threads':sum(not t['records'] for t in threads.values()),
                'alerts':(['读取异常：%d 条；查看诊断，结果可能不完整'%blocking_count] if blocking_count else [])+(['项目归属诊断：%d 项；未能唯一归属的项目来源标为待确认，候选保留；独立模型证据继续核对'%project_count] if project_count else [])+(['监测范围来源异常：%d 项；来源不明的普通条目继续保留'%monitoring_count] if monitoring_count else [])+capture_alerts,
                'unmatched_captures':[{k:r.get(k) for k in ['thread_id','response_id','upstream_request_id','upstream_model','source_fields','transport','capture_complete','completed','conflict']} for key,items in capture_index.items() if key not in matched_captures for _,r in items],
                'timeout_diagnostics':timeout_diagnostics,'pending_diagnostics':pending_diagnostics,'scan_timing':{'started_at':scan_started,'finished_at':scan_finished,'duration_ms':scan_finished-scan_started,'native_event_read_ms':native_read_ms,'selection_read_ms':selection_read_ms,'thread_selection_reads':selection_timings},
                'collector_status':collector_status,'connection_visibility':connection_visibility,'last_success':self.last_success,'scanned_at':now,'stale_after_hours':24,'demo':False}


def make_server(scanner, directory, token, port, audit=None):
    settings_lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def respond(self, status, data, content_type='application/json; charset=utf-8'):
            body = data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            for k,v in {'Content-Type':content_type,'Content-Length':str(len(body)), 'Cache-Control':'no-store',
                        'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                        'Content-Security-Policy':"default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"}.items():
                self.send_header(k,v)
            self.end_headers();self.wfile.write(body)

        def allowed(self, auth=True):
            host='127.0.0.1:'+str(self.server.server_port)
            if self.headers.get('Host')!=host or self.headers.get('Origin', 'http://'+host)!='http://'+host:
                self.respond(403,{'error':'不允许的 Host / Origin'});return False
            if auth and not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.respond(401,{'error':'需要本地私有授权；请用启动器打开'});return False
            return True

        def do_GET(self):
            from model_monitor.relay_audit.http import dispatch
            if dispatch(self,audit):return
            path=urlsplit(self.path).path
            if not self.allowed(auth=path.startswith('/api/')):
                return
            if path=='/api/data':
                try:self.respond(200,scanner.scan())
                except Exception:
                    self.respond(500,{'error':'监控读取失败；保留页面上次数据，检查终端错误'})
                    raise
            elif path=='/api/wallpaper':
                name=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
                if not IMPORTED.fullmatch(name) or not valid_wallpaper(directory,name):
                    self.respond(404,{'error':'导入图片不存在'});return
                mime='image/'+name.rsplit('.',1)[1]
                self.respond(200,(Path(directory)/'wallpapers'/name).read_bytes(),mime)
            elif path=='/api/demo':
                from model_monitor.demo import build
                with settings_lock:self.respond(200,build())
            elif path=='/api/settings':
                try:
                    with settings_lock:self.respond(200,read_settings(directory))
                except (OSError,ValueError):self.respond(500,{'error':'设置文件读取失败；未修改壁纸'})
            else:
                assets={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),'/aurora.svg':('aurora.svg','image/svg+xml'),'/dunes.svg':('dunes.svg','image/svg+xml')}
                if path not in assets:self.respond(404,{'error':'不存在'});return
                filename,mime=assets[path]
                self.respond(200,(BASE/'static'/filename).read_bytes(),mime)

        def do_POST(self):
            from model_monitor.relay_audit.http import dispatch
            if dispatch(self,audit):return
            if not self.allowed():return
            if self.path=='/api/wallpaper':
                try:
                    n=int(self.headers.get('Content-Length','0'))
                    if not 0<n<=MAX_IMAGE_BYTES:raise ValueError('图片必须小于 8 MiB')
                    image=self.rfile.read(n)
                    if len(image)!=n:raise ValueError('图片上传不完整')
                    with settings_lock:result=import_wallpaper(directory,image)
                    self.respond(200,result)
                except (ValueError,TypeError):self.respond(400,{'error':'导入失败：仅支持小于 8 MiB 的 PNG / JPEG / WebP；保留上一张'})
                except OSError:self.respond(500,{'error':'导入图片保存失败；保留上一张'})
                return
            if self.path!='/api/settings':self.respond(404,{'error':'不存在'});return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=1024:raise ValueError('设置体长度无效')
                data=json.loads(self.rfile.read(n))
                with settings_lock:result=save_settings(directory,data)
                self.respond(200,result)
            except (ValueError,TypeError):self.respond(400,{'error':'壁纸设置无效；保留上一张'})
            except OSError:self.respond(500,{'error':'壁纸保存失败；保留上一张'})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18083)
    parser.add_argument('--no-open',action='store_true')
    args=parser.parse_args()
    if args.port==18082:parser.error('18082 为既有服务保留')
    scanner=Scanner(Path.home()/'.codex/state_5.sqlite',Path.home()/'.local/share/codex-request-inspector/native-traces',Path.home()/'.local/share/codex-request-inspector/model-monitor-capture/metadata')
    token=secrets.token_urlsafe(32)
    server=make_server(scanner,BASE/'data',token,args.port)
    state=BASE/'data/session.json'
    with state.open('w',encoding='utf-8') as f:
        os.chmod(str(state),0o600)
        json.dump({'port':server.server_port,'token':token,'pid':os.getpid()},f)
    print('本机面板已启动：127.0.0.1:%d；私有授权不打印。用 python3 -m model_monitor.open_panel 打开。'%server.server_port,flush=True)
    if not args.no_open:webbrowser.open('http://127.0.0.1:%d/?open=%s#%s'%(server.server_port,secrets.token_hex(4),token))
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':
    main()
