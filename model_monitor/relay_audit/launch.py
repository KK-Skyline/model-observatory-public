"""Launch both isolated candidate frontends without persisting any credentials."""
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import webbrowser

def main():
    token=secrets.token_urlsafe(32)
    child=subprocess.Popen([sys.executable,'-m','model_monitor.relay_audit','serve'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,cwd=str(Path(__file__).parents[2]))
    try:
        child.stdin.write(json.dumps({'auth_token':token})+'\n');child.stdin.close()
        line=child.stdout.readline()
        if not line: raise RuntimeError('Candidate failed before startup')
        data=json.loads(line)
        for url in data['urls']:webbrowser.open(url+'/#'+token)
        print('隔离候选已打开：'+', '.join(data['urls'])+'；Ctrl-C 停止。授权仅在内存中传递。',flush=True)
        child.wait()
        if child.returncode:raise RuntimeError('Candidate exited nonzero')
    except KeyboardInterrupt:
        child.send_signal(signal.SIGINT);child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT);child.wait(timeout=30)

if __name__=='__main__':main()
