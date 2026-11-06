"""Exercise documented API from a process with no checkout on its import path."""
import json
from pathlib import Path
import secrets
import signal
import subprocess
from .core import CACHE
from model_monitor.test_audit_contract import Synthetic

# This client only knows the documented endpoint contract and stdlib.
# 客户端仅知道公开接口合同与标准库，不读取本地项目。
CLIENT=r'''
import json,sys,time
from urllib.request import Request,build_opener,ProxyHandler
c=json.load(sys.stdin)
def call(path,body=None):
 r=Request(c['url']+'/api/audit/v1/'+path,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+c['token'],'Content-Type':'application/json'})
 with build_opener(ProxyHandler({})).open(r,timeout=20) as response:return json.load(response)['data']
def finish(job):
 deadline=time.monotonic()+30
 while time.monotonic()<deadline:
  j=call('jobs/'+job['id'])
  if j['state'] in ('completed','failed','cancelled'):return j
  time.sleep(.1)
 raise RuntimeError('job deadline')
cap=call('capabilities');condition=cap['conditions']['chat']
coverage=call('coverage')
b=call('baselines',{'schema_version':1,'source':'synthetic:context-free-agent','model':'A-mini','protocol':'chat','date':'2026-11-02','conditions':condition,'samples':[233]*400})
spec={'endpoint':c['target'],'model':'A','protocol':'chat','samples':40,'timeout':1,'token_budget':8192,'conditions':condition,'mode':'calibrate','source':'synthetic:context-free-agent'}
cal=finish(call('jobs',{'spec':spec,'api_key':c['key']}));assert cal['state']=='completed' and cal['calibrated_baseline']['sample_count']==40,cal
spec.pop('mode');spec.pop('source');result=finish(call('jobs',{'spec':spec,'api_key':c['key']}));assert result['verdict']=='未发现异常',result
cancelled=call('jobs',{'spec':spec,'api_key':c['key']});call('jobs/'+cancelled['id']+'/cancel',{});cancelled=finish(cancelled);assert cancelled['state']=='cancelled',cancelled
print(json.dumps({'schema_version':1,'context_free':True,'capabilities_commit':cap['commit'],'baseline_import_hash':b['hash'],'calibration':cal,'audit':result,'cancel':{'id':cancelled['id'],'state':cancelled['state']},'history_count':len(call('jobs')['jobs'])},ensure_ascii=False,indent=2))
'''

def main():
    token=secrets.token_urlsafe(32);fixture=Synthetic('honest')
    server=subprocess.Popen(['/usr/bin/python3','-m','model_monitor.relay_audit','serve'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=str(Path(__file__).parents[2]))
    try:
        server.stdin.write(json.dumps({'auth_token':token})+'\n');server.stdin.close()
        state=json.loads(server.stdout.readline())
        r=subprocess.run(['/usr/bin/python3','-I','-c',CLIENT],input=json.dumps({'url':state['urls'][0],'token':token,'target':fixture.url,'key':'synthetic-agent-memory-only'}),capture_output=True,text=True,cwd=str(CACHE),timeout=90)
        output=Path(__file__).parents[1]/'evidence/aig-audit/agent-context-free.json'
        if r.returncode:
            raise RuntimeError('Context-free client failed: '+r.stderr)
        output.write_text(r.stdout,encoding='utf-8');print('Context-free API discovery/import/calibrate/audit/cancel/history: exit 0')
    finally:
        fixture.close()
        if server.poll() is None:server.send_signal(signal.SIGINT)
        server.wait(timeout=30)
        if server.returncode:raise RuntimeError('Candidate shutdown failed')

if __name__=='__main__':main()
