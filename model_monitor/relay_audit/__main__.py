"""Run with --help. Secrets are accepted exclusively through stdin JSON."""
import argparse
import json
from pathlib import Path
import secrets
import sys
import threading
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlsplit
from .core import CACHE, Manager, canonical, NoRedirect

def main():
    parser=argparse.ArgumentParser(description='Isolated AIG audit, version 1. Secrets: stdin JSON only.')
    parser.add_argument('action',choices=['serve','diagnose','capabilities','coverage','submit','status','cancel','history','import-baseline'])
    parser.add_argument('--url',help='Candidate instance URL, no credentials')
    parser.add_argument('--id',help='Job ID for status/cancel')
    args=parser.parse_args()
    if args.action=='diagnose':
        m=Manager(CACHE/'candidate');print(canonical({'schema_version':1,'data':m.diagnose()}));m.close();return
    config=json.load(sys.stdin)
    if args.action=='serve':
        from model_monitor import app
        from model_monitor.design_preview.preview import make_preview, demo_snapshot
        token=config['auth_token']
        if not isinstance(token,str) or len(token)<20: raise ValueError('Use an ephemeral auth token of at least 20 characters')
        manager=Manager(CACHE/'candidate',allow_remote=config.get('allow_remote',False))
        manager.diagnose()
        for name in ('candidate-classic','candidate-preview'):
            (CACHE/name).mkdir(exist_ok=True,mode=0o700)
        class SyntheticScanner:
            def scan(self): return demo_snapshot(CACHE/'candidate-ui-fixture')
        scanner=SyntheticScanner()
        # Both UI shells share one backend; no existing scanner/database is opened.
        # 两套界面共用后端，不打开现役采集或数据库。
        servers=[app.make_server(scanner,CACHE/'candidate-classic',token,0,audit=manager),make_preview(scanner,CACHE/'candidate-preview',token,0,audit=manager)]
        for server in servers:
            threading.Thread(target=server.serve_forever,daemon=True).start()
        print(canonical({'schema_version':1,'urls':['http://127.0.0.1:'+str(s.server_port) for s in servers],'notice':'Synthetic candidate only; auth token is not printed.'}),flush=True)
        try: threading.Event().wait()
        except KeyboardInterrupt: pass
        finally:
            manager.close()
            for server in servers: server.shutdown();server.server_close()
        return
    u=urlsplit(args.url or '')
    if u.scheme!='http' or u.hostname!='127.0.0.1' or not u.port or u.port in range(18082,18086) or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
        raise ValueError('Explicit isolated loopback instance URL required')
    paths={'capabilities':'capabilities','coverage':'coverage','submit':'jobs','status':'jobs/'+str(args.id),'cancel':'jobs/'+str(args.id)+'/cancel','history':'jobs','import-baseline':'baselines'}
    payload={'submit':config.get('body'),'import-baseline':config.get('body'),'cancel':{}}.get(args.action)
    request=Request(args.url.rstrip('/')+'/api/audit/v1/'+paths[args.action],data=None if payload is None else canonical(payload).encode(),headers={'Authorization':'Bearer '+config['auth_token'],'Content-Type':'application/json'})
    with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=30) as response:
        print(response.read().decode())

if __name__=='__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'schema_version':1,'error':type(error).__name__}),file=sys.stderr)
        sys.exit(1)
