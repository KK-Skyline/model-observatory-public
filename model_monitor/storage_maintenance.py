"""Lossless native filesystem compression of completed Codex payload snapshots."""
import argparse
import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import uuid


TERMINAL={'inference_completed','inference_failed','inference_cancelled'}
PREFLIGHT_ERRORS=('trace_read_failure','invalid_trace_rows','unsafe_trace','unsafe_payload_reference','invalid_payload_reference')


def fingerprint(path):
    value=path.stat()
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):result.update(chunk)
    return result.hexdigest()


def completed_payloads(root,counts):
    candidates=set();protected=set();closed_snapshots=set()
    starts=collections.defaultdict(list);ends=collections.defaultdict(list);references={}
    def reference(log,ref):
        if not isinstance(ref,dict) or not isinstance(ref.get('path'),str):
            counts['invalid_payload_reference']+=1;return None
        key=(log,ref['path'])
        if key in references:return references[key]
        references[key]=None
        path=log.parent/ref['path']
        if path.is_symlink():counts['symlink_skipped']+=1;return None
        path=path.resolve()
        if not path.is_relative_to(root):counts['unsafe_payload_reference']+=1;return None
        references[key]=path
        return path
    for log in sorted(root.glob('*/trace.jsonl')):
        if log.is_symlink() or not log.resolve().is_relative_to(root):
            counts['unsafe_trace']+=1;continue
        trace_refs=set();misc_refs=set();started_rollouts=set();last_kind=None;last_rollout=None
        try:
            with log.open(encoding='utf-8') as stream:
                for line in stream:
                    try:event=json.loads(line)
                    except (ValueError,UnicodeError):counts['invalid_trace_rows']+=1;continue
                    payload=event.get('payload') if isinstance(event,dict) else None
                    if not isinstance(payload,dict):counts['invalid_trace_rows']+=1;continue
                    kind=payload.get('type')
                    if not isinstance(kind,str):counts['invalid_trace_rows']+=1;continue
                    last_kind=kind;last_rollout=event.get('rollout_id')
                    if kind=='rollout_started':
                        identity=(last_rollout,payload.get('root_thread_id'),payload.get('trace_id'))
                        if not all(isinstance(v,str) and v for v in identity):counts['invalid_trace_rows']+=1;continue
                        started_rollouts.add(identity)
                    for field,value in payload.items():
                        if field.endswith('_payload') and isinstance(value,dict) and 'path' in value:
                            path=reference(log,value)
                            if path is not None:
                                trace_refs.add(path)
                                if kind not in TERMINAL and kind!='inference_started':misc_refs.add(path)
                    if kind not in TERMINAL and kind!='inference_started':
                        counts['non_inference_rows']+=1;continue
                    key=(event.get('thread_id'),event.get('codex_turn_id'),payload.get('inference_call_id'))
                    if not all(isinstance(v,str) and v for v in key) or payload.get('thread_id',key[0])!=key[0] or payload.get('codex_turn_id',key[1])!=key[1]:
                        counts['invalid_trace_rows']+=1;continue
                    (starts if kind=='inference_started' else ends)[key].append((log,payload))
        except (OSError,UnicodeError):counts['trace_read_failure']+=1
        if last_kind=='rollout_ended':
            manifest=log.parent/'manifest.json'
            try:
                if manifest.is_symlink():raise ValueError('unsafe manifest')
                value=json.loads(manifest.read_text(encoding='utf-8'))
                if not isinstance(value,dict):raise ValueError('invalid manifest')
                identity=(value.get('rollout_id'),value.get('root_thread_id'),value.get('trace_id'))
                if not all(isinstance(v,str) and v for v in identity):raise ValueError('missing rollout identity')
                if identity not in started_rollouts or last_rollout!=identity[0]:raise ValueError('rollout identity mismatch')
                closed_snapshots.update(trace_refs);counts['verified_closed_traces']+=1
            except (OSError,ValueError,UnicodeError):
                counts['unverified_closed_traces']+=1;protected.update(misc_refs)
        # Open producers retain miscellaneous payloads; completed inference refs have their own proof.
        # 未关闭生产者的杂项快照保留，唯一终态推理引用另有独立完成证明。
        else:protected.update(misc_refs)
    candidates.update(closed_snapshots)
    counts['closed_trace_payload_candidates']=len(closed_snapshots)
    counts['unmatched_terminal_calls']+=sum(key not in starts for key in ends)
    for key in starts.keys()|ends.keys():
        requests=starts.get(key,[]);done=ends.get(key,[])
        if len(requests)!=1 or len(done)!=1:
            counts['active_calls_skipped' if requests and not done else 'ambiguous_calls_skipped']+=1
            for log,payload in requests+done:
                for field in ('request_payload','response_payload','partial_response_payload'):
                    if field not in payload or payload[field] is None:continue
                    path=reference(log,payload[field])
                    if path is not None:protected.add(path)
            continue
        log,request=requests[0]
        refs=[(log,request.get('request_payload'))]
        log,terminal=done[0]
        refs.extend((log,terminal[field]) for field in ('response_payload','partial_response_payload') if field in terminal and terminal[field] is not None)
        for log,ref in refs:
            path=reference(log,ref)
            if path is not None:candidates.add(path)
    counts['active_shared_files_skipped']+=len(candidates&protected)
    # Malformed metadata cannot prove completion; abort before replacing any source.
    # 损坏元数据无法证明完成状态，任何原文件替换前即中止。
    if any(counts[k] for k in PREFLIGHT_ERRORS):return []
    return sorted(candidates-protected)


def compress_file(path,now,min_age,min_bytes):
    if path.is_symlink():return {'status':'symlink_skipped'}
    value=path.stat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink!=1:return {'status':'non_regular_or_linked_skipped'}
    if now-max(value.st_mtime,value.st_ctime)<min_age:return {'status':'recent_file_skipped'}
    if value.st_size<min_bytes:return {'status':'small_file_skipped'}
    if value.st_flags&stat.UF_COMPRESSED:return {'status':'already_compressed'}
    identity=fingerprint(path);original_hash=digest(path)
    descriptor,name=tempfile.mkstemp(prefix='.codex-compress-',suffix='.tmp',dir=str(path.parent))
    os.close(descriptor);temporary=Path(name)
    try:
        # Native compression preserves the original JSON path and ordinary read behavior.
        # 系统透明压缩保留原 JSON 路径及普通文件读取方式。
        subprocess.run(['/usr/bin/ditto','--hfsCompression','--noclone',str(path),str(temporary)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if digest(temporary)!=original_hash:raise ValueError('compressed content hash mismatch')
        if fingerprint(path)!=identity:return {'status':'changed_during_compression_skipped'}
        compressed=temporary.stat()
        before=value.st_blocks*512;after=compressed.st_blocks*512
        if not compressed.st_flags&stat.UF_COMPRESSED or after>=before:return {'status':'no_savings_skipped'}
        if stat.S_IMODE(compressed.st_mode)!=stat.S_IMODE(value.st_mode):raise ValueError('compressed file permissions changed')
        with temporary.open('rb') as stream:os.fsync(stream.fileno())
        if fingerprint(path)!=identity:return {'status':'changed_during_compression_skipped'}
        os.replace(str(temporary),str(path))
        return {'status':'compressed','logical_bytes':value.st_size,'allocated_before':before,'allocated_after':after,'reclaimed_bytes':before-after,'sha256':original_hash}
    finally:
        if temporary.exists():temporary.unlink()


def save_report(path,report):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=path.with_suffix('.tmp')
    fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream:
        os.fchmod(stream.fileno(),0o600);json.dump(report,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(str(target),str(path))
    if report['state']!='running':
        history=path.parent/'storage-maintenance-history';history.mkdir(exist_ok=True,mode=0o700)
        record=history/(report['run_id']+'.json')
        fd=os.open(str(record),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(report,stream,indent=2);stream.flush();os.fsync(stream.fileno())


def maintain(root,report_path,apply=False,min_age=120,min_bytes=128*1024):
    if sys.platform!='darwin':raise RuntimeError('Native transparent compression requires macOS')
    root=Path(root).resolve();report_path=Path(report_path)
    if not root.is_dir() or min_age<0 or min_bytes<0:raise ValueError('Invalid storage maintenance inputs')
    report_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock_path=root/'snapshot-compression.lock'
    descriptor=os.open(str(lock_path),os.O_WRONLY|os.O_CREAT,0o600)
    with os.fdopen(descriptor,'w') as lock:
        os.fchmod(lock.fileno(),0o600)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        counts=collections.Counter();candidates=completed_payloads(root,counts)
        report={'run_id':uuid.uuid4().hex,'started_at_ms':int(time.time()*1000),'mode':'apply' if apply else 'dry-run','state':'running','candidate_files':len(candidates),'counts':dict(counts),'reclaimed_bytes':0,'failures':[],'verified_files':[]}
        if any(counts[k] for k in PREFLIGHT_ERRORS):
            report.update(state='blocked_invalid_metadata',finished_at_ms=int(time.time()*1000))
            save_report(report_path,report);return report
        save_report(report_path,report)
        for index,path in enumerate(candidates):
            try:
                result=compress_file(path,time.time(),min_age,min_bytes) if apply else {'status':'dry_run_candidate'}
                counts[result['status']]+=1
                report['reclaimed_bytes']+=result.get('reclaimed_bytes',0)
                if result['status']=='compressed':report['verified_files'].append({'path':str(path.relative_to(root)),**result})
            except (OSError,ValueError,subprocess.CalledProcessError) as error:
                counts['compression_failure']+=1
                report['failures'].append({'path':str(path.relative_to(root)),'exception':type(error).__name__})
            if index%25==0:
                report['processed_files']=index+1;report['counts']=dict(counts);save_report(report_path,report)
        report.update(state='complete',processed_files=len(candidates),counts=dict(counts),finished_at_ms=int(time.time()*1000))
        save_report(report_path,report)
        return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.home()/'.local/share/codex-request-inspector/native-traces')
    parser.add_argument('--report',type=Path,default=Path.home()/'.local/share/codex-request-inspector/model-monitor-capture/metadata/storage-maintenance.json')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();os.nice(10)
    report=maintain(args.root,args.report,args.apply)
    print(json.dumps({'state':report['state'],'counts':report['counts'],'reclaimed_bytes':report['reclaimed_bytes']}))
    if report['failures'] or any(report['counts'].get(k) for k in PREFLIGHT_ERRORS):raise SystemExit(1)


if __name__=='__main__':main()
