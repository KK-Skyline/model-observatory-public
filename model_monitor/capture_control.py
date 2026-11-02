"""Back up, activate, and roll back the single inference routing setting."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import plistlib
import signal
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parent
CONFIG=Path.home()/'.codex/config.toml'
STATE=Path.home()/'.local/share/codex-request-inspector/model-monitor-capture'
AGENT=Path.home()/'Library/LaunchAgents/com.codex.model-monitor-capture.plist'
LABEL='com.codex.model-monitor-capture'
DOMAIN='gui/'+str(os.getuid())
ROUTE='http://127.0.0.1:18084/backend-api/codex'
MARKER='# model-monitor metadata-only upstream observer\n'
PREFIX=MARKER+'openai_base_url = "'+ROUTE+'"\n'

def digest(data):return hashlib.sha256(data).hexdigest()
def atomic(path,data,mode):
    temp=path.with_name(path.name+'.model-monitor-tmp')
    with temp.open('wb') as f:os.chmod(str(temp),mode);f.write(data);f.flush();os.fsync(f.fileno())
    os.replace(str(temp),str(path))
def stop_owned(pid):
    command=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],stdout=subprocess.PIPE,text=True).stdout
    if not command.strip():return
    if ' -m model_monitor.collector' not in command and str(STATE/'collector.py') not in command:raise RuntimeError('PID 已不属于本任务采集器，拒绝终止')
    try:os.kill(pid,signal.SIGTERM)
    except ProcessLookupError:return

def unload_agent():
    probe=subprocess.run(['/bin/launchctl','print',DOMAIN+'/'+LABEL],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if probe.returncode==0:
        result=subprocess.run(['/bin/launchctl','bootout',DOMAIN,str(AGENT)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode:raise RuntimeError('采集登录任务卸载失败，拒绝继续清理')

def enable():
    original=CONFIG.read_bytes()
    if AGENT.exists():raise RuntimeError('目标登录任务文件已存在，拒绝覆盖')
    if PREFIX.encode() in original:raise RuntimeError('采集路由已启用；不要重复覆盖备份')
    if re.search(rb'^openai_base_url\s*=',original,re.M):raise RuntimeError('已有自定义上游入口，停止以免覆盖')
    if re.search(rb'(?i)(bearer\s+|sk-[a-z0-9_-]{15,}|(?:api_key|password|authorization|access_token|refresh_token)\s*=)',original):raise RuntimeError('配置含疑似凭据，不复制；需先单独规划无凭据备份')
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(str(STATE),0o700)
    backup=STATE/('config.before-'+digest(original)[:16]+'.toml')
    if backup.exists() and backup.read_bytes()!=original:raise RuntimeError('备份内容冲突')
    if not backup.exists():atomic(backup,original,0o600)
    metadata=STATE/'metadata';metadata.mkdir(exist_ok=True,mode=0o700)
    previous=BASE/'data/capture/upstream.jsonl'
    if previous.exists() and not (metadata/'upstream.jsonl').exists():atomic(metadata/'upstream.jsonl',previous.read_bytes(),0o600)
    installed=STATE/'collector.py';atomic(installed,(BASE/'collector.py').read_bytes(),0o600)
    status=metadata/'collector-status.json'
    if status.exists():
        pid=json.loads(status.read_text())['pid']
        # Only stop a collector whose authenticated-free metadata listener is ours.
        # 仅处理本任务记录的采集进程；启动阶段禁止占用其他端口进程。
        try:os.kill(pid,0)
        except ProcessLookupError:pass
        else:raise RuntimeError('已有采集进程运行，请先停止本任务临时采集器后启用')
    payload=plistlib.dumps({'Label':LABEL,'ProgramArguments':[sys.executable,str(installed),'--directory',str(metadata)],
        'WorkingDirectory':str(STATE),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':5,
        'StandardOutPath':'/dev/null','StandardErrorPath':'/dev/null','ProcessType':'Background'})
    atomic(AGENT,payload,0o600)
    boot=subprocess.run(['/bin/launchctl','bootstrap',DOMAIN,str(AGENT)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if boot.returncode:AGENT.unlink();raise RuntimeError('登录任务启动失败；配置未改')
    ready=False;pid=None
    for _ in range(50):
        try:
            current=json.loads(status.read_text())
            candidate=current['pid']
            if current['counts'].get('started'):
                os.kill(candidate,0);pid=candidate;ready=True;break
        except (FileNotFoundError,ValueError,ProcessLookupError):pass
        time.sleep(.1)
    if not ready:unload_agent();AGENT.unlink();raise RuntimeError('采集器启动超时，配置未改')
    if CONFIG.read_bytes()!=original:unload_agent();raise RuntimeError('配置发生并发改动，未覆盖')
    updated=PREFIX.encode()+original
    activation={'backup_path':str(backup),'before_sha256':digest(original),'active_sha256':digest(updated),'collector_pid':pid,'launchagent_sha256':digest(payload),'launchagent_path':str(AGENT),'route':ROUTE,'installed_source_sha256':digest(installed.read_bytes()),'config_mode':CONFIG.stat().st_mode&0o777,'state':'active'}
    atomic(STATE/'activation.json',json.dumps(activation,indent=2).encode(),0o600)
    try:atomic(CONFIG,updated,activation['config_mode'])
    except OSError:unload_agent();raise
    print(json.dumps({'state':'active','backup_sha256':activation['before_sha256'],'config_sha256':activation['active_sha256'],'collector_pid':pid,'launchagent_sha256':digest(payload),'launchagent_path':str(AGENT)}))

def rollback():
    path=STATE/'activation.json';state=json.loads(path.read_text());original=Path(state['backup_path']).read_bytes()
    if digest(original)!=state['before_sha256']:raise RuntimeError('备份哈希不符，拒绝回退')
    current=CONFIG.read_bytes()
    if digest(current)==state['active_sha256']:restored=original
    elif current.count(PREFIX.encode())==1:restored=current.replace(PREFIX.encode(),b'',1)
    elif current==original:restored=original
    else:raise RuntimeError('找不到唯一受控配置片段，拒绝覆盖外部修改')
    atomic(CONFIG,restored,state['config_mode'])
    if AGENT.exists():
        if digest(AGENT.read_bytes())!=state['launchagent_sha256']:raise RuntimeError('登录任务有外部改动，未覆盖')
        unload_agent();AGENT.unlink()
    else:stop_owned(state['collector_pid'])
    state['state']='rolled_back';atomic(path,json.dumps(state,indent=2).encode(),0o600)
    print(json.dumps({'state':'rolled_back','restored_sha256':digest(restored),'exact_original':restored==original}))

if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('action',choices=['enable','rollback']);a=p.parse_args()
    if a.action=='enable':enable()
    else:rollback()
