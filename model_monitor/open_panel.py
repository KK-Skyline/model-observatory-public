"""Start/reuse only this panel and open its private URL without printing it."""
import json
import os
import secrets
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request,urlopen
import webbrowser

base=Path(__file__).resolve().parent
state=base/'data/session.json'
def connect():
    data=json.loads(state.read_text(encoding='utf-8'))
    url='http://127.0.0.1:'+str(data['port'])
    with urlopen(Request(url+'/api/settings',headers={'Authorization':'Bearer '+data['token']}),timeout=2) as r:
        if r.status!=200:raise RuntimeError('已有面板鉴权失败')
    return url+'/?open='+secrets.token_hex(4)+'#'+data['token']
try:
    url=connect()
except (OSError,ValueError):
    with (base/'data/service.log').open('ab') as log:
        process=subprocess.Popen([sys.executable,'-m','model_monitor.app','--no-open'],cwd=str(base.parent),stdout=log,stderr=log,start_new_session=True)
    for i in range(50):
        time.sleep(.1)
        if process.poll() is not None:raise RuntimeError('面板启动失败；查看 model_monitor/data/service.log')
        try:
            url=connect();break
        except (OSError,ValueError):
            if i==49:raise RuntimeError('面板启动超时；查看 model_monitor/data/service.log')
if not webbrowser.open(url):raise RuntimeError('浏览器未能打开；本机服务仍可运行，请检查默认浏览器')
print('已打开本机私有模型核对面板。')
