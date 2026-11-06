"""Executable negative/positive evidence; only starts loopback synthetic fixtures."""
import argparse
import json
from pathlib import Path
import tempfile
import time
from .core import CACHE, Manager, conditions
from model_monitor.test_audit_contract import Synthetic

def main():
    p=argparse.ArgumentParser();p.add_argument('--break-fixture',action='store_true');args=p.parse_args()
    with tempfile.TemporaryDirectory(dir=str(CACHE)) as directory:
        m=Manager(Path(directory));f=Synthetic('bad_stream' if args.break_fixture else 'honest')
        try:
            for model,n in [('A',7),('A-mini',233)]:
                m.import_baseline({'schema_version':1,'source':'synthetic:acceptance','model':model,'protocol':'chat','date':'2026-11-02','conditions':conditions('chat'),'samples':[n]*400})
            j=m.submit({'endpoint':f.url,'model':'A','protocol':'chat','samples':40,'timeout':1,'token_budget':8192,'conditions':conditions('chat')},'synthetic-memory-only')
            while m.get(j['id'])['state'] not in ('completed','cancelled','failed'): time.sleep(.05)
            result=m.get(j['id']);print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result['verdict']=='未发现异常' else 1
        finally: m.close();f.close()

if __name__=='__main__': raise SystemExit(main())
