"""Synthetic browser and actual CLI acceptance; screenshots contain no real data."""
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from playwright.sync_api import sync_playwright
from model_monitor import app
from model_monitor.design_preview.preview import make_preview, demo_snapshot
from model_monitor.relay_audit.core import CACHE, Manager, conditions
from model_monitor.test_audit_contract import Synthetic

EVIDENCE=Path(__file__).parents[1]/'evidence/aig-audit'

def main():
    rows=[];script_errors=[]
    with tempfile.TemporaryDirectory(dir=str(CACHE)) as tmp:
        root=Path(tmp);manager=Manager(root/'jobs');token=secrets.token_urlsafe(32)
        class Scanner:
            def scan(self): return demo_snapshot(root/'synthetic-data')
        for protocol in ('chat','responses','anthropic'):
            for model,n in [('A',7),('A-mini',233)]:
                manager.import_baseline({'schema_version':1,'source':'synthetic:browser-cli','model':model,'protocol':protocol,'date':'2026-11-02','conditions':conditions(protocol),'samples':[n]*400})
        servers=[]
        fixtures=[Synthetic('honest'),Synthetic('slow'),Synthetic('bad_stream'),Synthetic('disguised')]
        try:
            for name,factory in [('classic',app.make_server),('preview',make_preview)]:
                directory=root/name;directory.mkdir()
                server=factory(Scanner(),directory,token,0,audit=manager);servers.append(server)
                threading.Thread(target=server.serve_forever,daemon=True).start()
            base='http://127.0.0.1:'+str(servers[0].server_port)
            def cli(action,body=None,ident=None):
                cmd=['/usr/bin/python3','-m','model_monitor.relay_audit',action,'--url',base]
                if ident:cmd+=['--id',ident]
                request={'auth_token':token}
                if body is not None:request['body']=body
                r=subprocess.run(cmd,input=json.dumps(request),capture_output=True,text=True,cwd=str(Path(__file__).parents[2]))
                rows.append({'command':cmd,'stdin':'JSON credentials redacted; synthetic only','exit_code':r.returncode})
                assert r.returncode==0,(action,r.stderr)
                return json.loads(r.stdout)['data']
            capabilities=cli('capabilities');assert len(cli('coverage')['baselines'])==6
            for protocol in ('chat','responses','anthropic'):
                for fixture in (fixtures[0],fixtures[3]):
                    spec=dict(endpoint=fixture.url,model='A',protocol=protocol,samples=40,timeout=1,token_budget=8192,conditions=conditions(protocol))
                    job=cli('submit',{'spec':spec,'api_key':'SYNTHETIC-memory-cli'})
                    while True:
                        result=cli('status',ident=job['id'])
                        if result['state'] in ('completed','failed','cancelled'):break
                        time.sleep(.3)
                    assert result['verdict']==('未发现异常' if fixture is fixtures[0] else '发现偏离'),result
                    (EVIDENCE/('cli-'+protocol+'-'+fixture.scenario+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                try:
                    for name,server in zip(('classic','preview'),servers):
                        page=browser.new_page(viewport={'width':1280,'height':900})
                        page.on('pageerror',lambda error:script_errors.append(str(error)))
                        page.goto('http://127.0.0.1:%d/#%s'%(server.server_port,token));page.wait_for_load_state('networkidle')
                        page.locator('#audit-history article').first.wait_for()
                        assert page.locator('#audit-history article').count()>=6
                        labels=page.locator('#rows').inner_text()
                        assert '声明一致' in labels and '声明冲突' in labels and '声明缺失' in labels,labels
                        form=page.locator('#audit-form');form.locator('[name=endpoint]').fill(fixtures[1].url);form.locator('[name=key]').fill('SYNTHETIC-browser-key')
                        page.locator('#audit-start').click();page.get_by_role('button',name='取消抽样',exact=True).first.wait_for();page.get_by_role('button',name='取消抽样',exact=True).first.click()
                        page.wait_for_function("() => document.querySelector('#audit-history article h3')?.textContent.includes('cancelled')")
                        form.locator('[name=endpoint]').fill(fixtures[2].url);form.locator('[name=samples]').fill('2');form.locator('[name=key]').fill('SYNTHETIC-browser-key');page.locator('#audit-start').click()
                        page.wait_for_function("() => document.querySelector('#audit-history article h3')?.textContent.includes('completed') && document.querySelector('#audit-history article h3')?.textContent.includes('证据不足')")
                        assert form.locator('[name=key]').input_value()==''
                        # Refresh only reads job history; check it does not send new probes.
                        # 刷新只读取历史，核对没有新增探针。
                        before=sum(f.calls for f in fixtures);page.reload();page.wait_for_load_state('networkidle');page.locator('#audit-history article').first.wait_for();assert before==sum(f.calls for f in fixtures)
                        page.locator('#route-audit').scroll_into_view_if_needed();page.screenshot(path=str(EVIDENCE/(name+'-desktop.png')),full_page=True)
                        page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(EVIDENCE/(name+'-narrow.png')),full_page=True)
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                        rows.append({'browser':name,'start':True,'cancel':True,'malformed_failure':True,'history_after_reload':True,'refresh_no_probes':True,'password_cleared':True,'narrow':True})
                        page.close()
                finally:browser.close()
            assert not script_errors,script_errors
            (EVIDENCE/'browser-cli.json').write_text(json.dumps({'rows':rows,'page_errors':script_errors,'synthetic_only':True},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print('PASS: CLI three protocols x honest/disguised; both UIs start/cancel/failure/history/narrow; page_errors=0')
        finally:
            manager.close()
            for server in servers:server.shutdown();server.server_close()
            for fixture in fixtures:fixture.close()

if __name__=='__main__':main()
