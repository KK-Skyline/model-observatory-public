"""Frozen end-to-end contract: real local HTTP and unmodified AIG scoring."""
import json
import random
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

CACHE = Path.home()/'Library/Caches/model-observatory-aig'

class Synthetic:
    def __init__(self, scenario='honest', seed=917):
        self.scenario, self.calls, self.bad = scenario, 0, 0
        self.rng = random.Random(seed)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return
            def do_POST(self):
                d = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls += 1
                scenario = owner.scenario
                if scenario == 'slow':
                    time.sleep(.2)
                if scenario == 'timeout':
                    time.sleep(.5)
                status = 429 if scenario == '429' else 400 if scenario == 'incompatible' else 200
                prompt = str(d.get('input', d.get('messages')))
                bad = scenario in ('disguised', 'downgrade') or (scenario == 'mixed' and owner.rng.random() < .5)
                owner.bad += int(bad)
                text = '233' if bad else '7'
                if 'CANARY_' in prompt:
                    import re
                    tokens = re.findall(r'CANARY_[A-Za-z0-9]+', prompt)
                    text = '\n'.join(tokens[-1:] if scenario == 'truncated' else tokens)
                usage = {'input_tokens':1, 'output_tokens':1}
                if self.path.endswith('/responses'):
                    payload = {'model':d['model'], 'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':text}]}], 'usage':usage}
                elif self.path.endswith('/messages'):
                    payload = {'model':d['model'],'content':[{'type':'text','text':text}], 'stop_reason':'end_turn','usage':usage}
                else:
                    payload = {'model':d['model'], 'choices':[{'message':{'content':text},'finish_reason':'stop'}], 'usage':{'prompt_tokens':1,'completion_tokens':1}}
                body = b'data: broken-stream-secret-SYNTHETIC\n\n' if scenario == 'bad_stream' else json.dumps(payload).encode()
                self.send_response(status); self.send_header('Content-Length',str(len(body))); self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    owner.disconnects = getattr(owner, 'disconnects', 0) + 1
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.url = 'http://127.0.0.1:%s/v1' % self.server.server_port
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def close(self):
        self.server.shutdown(); self.server.server_close()

class AuditContract(unittest.TestCase):
    def setUp(self):
        from model_monitor.relay_audit.core import Manager, conditions
        self.tmp = tempfile.TemporaryDirectory(dir=str(CACHE))
        self.manager = Manager(Path(self.tmp.name))
        self.conditions = conditions
        self.fixtures = []
        for protocol in ('responses','chat','anthropic'):
            for model, number in (('A',7),('A-mini',233)):
                self.manager.import_baseline({'schema_version':1,'source':'synthetic fixture, not official','model':model,'protocol':protocol,'date':'2026-11-02','conditions':conditions(protocol),'samples':[number]*400})
    def tearDown(self):
        self.manager.close()
        for fixture in self.fixtures:
            fixture.close()
        self.tmp.cleanup()
    def submit(self, scenario='honest', protocol='chat', **extra):
        fixture = Synthetic(scenario); self.fixtures.append(fixture)
        spec = dict(endpoint=fixture.url, model='A', protocol=protocol, samples=40, timeout=1, token_budget=8192, conditions=self.conditions(protocol))
        spec.update(extra)
        return fixture, self.manager.submit(spec, 'SYNTHETIC-secret-never-report')
    def finish(self, job):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            r = self.manager.get(job['id'])
            if r['state'] in ('completed','failed','cancelled'):
                return r
            time.sleep(.02)
        self.fail('job deadline exceeded')
    def test_three_protocols_honest_and_same_name_disguised(self):
        for protocol in ('responses','chat','anthropic'):
            with self.subTest(protocol=protocol):
                _, job = self.submit(protocol=protocol)
                r = self.finish(job)
                self.assertEqual('未发现异常',r['verdict']); self.assertEqual(40,r['valid_samples'])
                _, job = self.submit('disguised',protocol)
                r = self.finish(job)
                self.assertEqual('发现偏离',r['verdict']); self.assertEqual('声明一致',r['declaration'])
                self.assertIn('forgery',r['engine_result'])
    def test_same_family_downgrade(self):
        _, job = self.submit('downgrade'); self.assertEqual('发现偏离',self.finish(job)['verdict'])
    def test_mixed_seed_budget_detection_and_misses(self):
        detected = 0
        for _ in range(3):
            f, job = self.submit('mixed'); r = self.finish(job)
            self.assertEqual(41,f.calls); self.assertGreater(f.bad,0)
            detected += r['verdict']=='发现偏离'
        self.assertEqual(3,detected)
    def test_truncated_context(self):
        _, job = self.submit('truncated'); r = self.finish(job)
        self.assertEqual('发现偏离',r['verdict']); self.assertFalse(r['context']['saw_start'])
    def test_missing_baseline(self):
        _, job = self.submit(model='unknown'); self.assertEqual('证据不足',self.finish(job)['verdict'])
    def test_condition_mismatch(self):
        c = self.conditions('chat'); c['temperature']=.2
        _,job=self.submit(conditions=c); self.assertEqual('证据不足',self.finish(job)['verdict'])
    def test_small_sample(self):
        _,job=self.submit(samples=5); self.assertEqual('证据不足',self.finish(job)['verdict'])
    def test_incompatible_429_timeout_bad_stream_counted(self):
        for scenario in ('incompatible','429','timeout','bad_stream'):
            with self.subTest(scenario=scenario):
                _,job=self.submit(scenario,samples=2,timeout=.1)
                r=self.finish(job); self.assertEqual('证据不足',r['verdict']); self.assertGreater(r['errors'],0)
                self.assertTrue(r['failures'])
    def test_cancel_no_new_probes(self):
        f,job=self.submit('slow'); time.sleep(.05); self.manager.cancel(job['id']); r=self.finish(job)
        calls=f.calls; time.sleep(.3); self.assertEqual(calls,f.calls); self.assertEqual('cancelled',r['state'])
    def test_restart_retains_history(self):
        from model_monitor.relay_audit.core import Manager
        _,job=self.submit(); r=self.finish(job); self.manager.close()
        self.manager=Manager(Path(self.tmp.name)); self.assertEqual(r,self.manager.get(job['id']))
    def test_engine_disconnected_never_inherits_green(self):
        _,job=self.submit(); self.assertEqual('未发现异常',self.finish(job)['verdict'])
        self.manager.engine_python=Path(self.tmp.name)/'absent-python'
        _,job=self.submit(); r=self.finish(job)
        self.assertEqual('failed',r['state']); self.assertEqual('证据不足',r['verdict']); self.assertGreater(r['errors'],0)
    def test_no_secrets_in_history(self):
        _,job=self.submit('bad_stream',samples=2); self.finish(job)
        for p in Path(self.tmp.name).rglob('*'):
            if p.is_file():
                s=p.read_text(encoding='utf-8'); self.assertNotIn('SYNTHETIC-secret-never-report',s); self.assertNotIn('broken-stream-secret',s)
    def test_auth_host_origin_and_unknown_job(self):
        from model_monitor.relay_audit.http import make_server
        server=make_server(self.manager,'synthetic-local-auth',0)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base='http://127.0.0.1:%s' % server.server_port
        try:
            for headers,code in (({},401),({'Authorization':'Bearer wrong'},401),({'Authorization':'Bearer synthetic-local-auth','Origin':'http://evil.test'},403)):
                with self.assertRaises(HTTPError) as cm: urlopen(Request(base+'/api/audit/v1/capabilities',headers=headers))
                self.assertEqual(code,cm.exception.code)
            with self.assertRaises(HTTPError) as cm: urlopen(Request(base+'/api/audit/v1/jobs/nonexistent',headers={'Authorization':'Bearer synthetic-local-auth'}))
            self.assertEqual(404,cm.exception.code)
        finally: server.shutdown();server.server_close()
    def test_input_limits_and_file_command_rejected(self):
        for extra in ({'endpoint':'file:///etc/passwd'},{'samples':10001},{'command':'id'},{'token_budget':-1},{'protocol':'guess'}):
            with self.subTest(extra=extra),self.assertRaises(ValueError): self.submit(**extra)
    def test_coverage_has_exact_conditions_hash(self):
        coverage=self.manager.coverage(); self.assertEqual(6,len(coverage))
        for b in coverage: self.assertEqual(64,len(b['hash']));self.assertIn('conditions',b)
