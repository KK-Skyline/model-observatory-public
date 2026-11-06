"""Additional round frozen before fixes; no original contracts changed."""
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from model_monitor.relay_audit.core import CACHE, Manager, conditions, canonical

class RepairContract(unittest.TestCase):
    def test_corrupt_baseline_startup_never_writes(self):
        with tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
            m=Manager(Path(d))
            for model,n in [('A',7),('B',233)]:
                m.import_baseline(dict(schema_version=1,source='synthetic:repair',model=model,protocol='chat',date='2026-11-05',conditions=conditions('chat'),samples=[n]*40))
            m.close();p=Path(d)/'baselines.json';b=json.loads(p.read_text());b[1]['samples'][0]=1;p.write_text(canonical(b),encoding='utf-8');before=p.read_bytes()
            with self.assertRaises(ValueError):Manager(Path(d))
            self.assertEqual(before,p.read_bytes())
    def test_remote_configuration_and_explicit_execution_gate(self):
        with tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
            m=Manager(Path(d));s=dict(endpoint='https://api.example.invalid/v1',model='A',protocol='chat',samples=40,timeout=1,token_budget=8192,conditions=conditions('chat'),mode='calibrate',source='operator-attested:official-direct')
            self.assertEqual(s,m._validate(s))
            with self.assertRaises(ValueError):m.submit(s,'synthetic-unused')
            m.close()
    def test_drip_wall_deadline_and_cancel(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):return
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));self.send_response(200);self.send_header('Content-Length','10000');self.end_headers()
                try:
                    for i in range(100):self.wfile.write(b' ');self.wfile.flush();time.sleep(.025)
                except (BrokenPipeError,ConnectionResetError):return
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
                m=Manager(Path(d));s=dict(endpoint='http://127.0.0.1:%d/v1'%server.server_port,model='A',protocol='chat',samples=1,timeout=.15,token_budget=8192,conditions=conditions('chat'))
                start=time.monotonic();j=m.submit(s,'synthetic-unused')
                while m.get(j['id'])['state'] not in ('completed','failed','cancelled') and time.monotonic()-start<3:time.sleep(.01)
                r=m.get(j['id']);self.assertLess(time.monotonic()-start,2);self.assertEqual(0,r['valid_samples']);self.assertTrue(any('timeout' in x for x in r['failures']));m.close()
        finally:server.shutdown();server.server_close()
