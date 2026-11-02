import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from model_monitor.app import Scanner, compare, make_server, save_settings, read_settings


class ModelAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'data')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.traces = self.root / 'traces'
        self.bundle = self.traces / 'bundle'
        self.bundle.mkdir(parents=True)
        self.db = self.root / 'state.sqlite'
        self.rollout = self.root / 'rollout.jsonl'
        self.rollout.write_text('', encoding='utf-8')
        with sqlite3.connect(str(self.db)) as c:
            c.executescript('CREATE TABLE threads(id TEXT, rollout_path TEXT, name TEXT, title TEXT, project_id TEXT, archived INTEGER, updated_at INTEGER, model TEXT); CREATE TABLE projects(id TEXT, name TEXT);')
            c.execute('INSERT INTO projects VALUES (?,?)', ('p', '真实项目'))
            c.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)', ('t', str(self.rollout), '对话 A', '旧标题', 'p', 0, 1, 'DO-NOT-USE-CURRENT'))
            c.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)', ('other', str(self.root/'other.jsonl'), '无记录', '', None, 1, 1, 'x'))
        (self.root/'other.jsonl').write_text('', encoding='utf-8')
        self.events = []

    def turn(self, turn='turn', model='A'):
        with self.rollout.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'turn_id': turn, 'model': model}})+'\n')

    def call(self, call='call', turn='turn', model='A', upstream='A', thread='t', completed=True):
        seq = len(self.events)+1
        req = 'req%d.json' % seq
        (self.bundle/req).write_text(json.dumps({'model': model, 'input': 'SECRET_BODY'}), encoding='utf-8')
        base = {'thread_id': thread, 'codex_turn_id': turn, 'wall_time_unix_ms': int(time.time()*1000)+seq}
        self.events.append(dict(base, seq=seq, payload={'type':'inference_started', 'thread_id':thread,'codex_turn_id':turn,'inference_call_id':call,'model':model,'request_payload':{'path':req}}))
        if completed:
            resp = 'resp%d.json' % seq
            body = {'response_id': 'r'+call, 'output_items':['SECRET_BODY']}
            if upstream is not None:
                body['model'] = upstream
            (self.bundle/resp).write_text(json.dumps(body), encoding='utf-8')
            self.events.append(dict(base, seq=seq+1,payload={'type':'inference_completed','inference_call_id':call,'response_id':'r'+call,'response_payload':{'path':resp}}))
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in self.events), encoding='utf-8')

    def scan(self):
        return Scanner(self.db, self.traces).scan()

    def record(self):
        return next(t for t in self.scan()['threads'] if t['id']=='t')['records'][0]

    def test_01_match(self):
        self.turn(); self.call()
        r=self.record()
        self.assertEqual((r['request_status'],r['upstream_status']),('一致','一致'))
        self.assertIn('turn_context',r['selection_source'])
        self.assertEqual(r['selected_model'],'A')

    def test_02_mismatch(self):
        self.turn();self.call(model='B',upstream='C')
        r=self.record()
        self.assertEqual((r['request_status'],r['upstream_status']),('不一致','不一致'))

    def test_03_missing_upstream(self):
        self.turn();self.call(upstream=None)
        r=self.record()
        self.assertEqual(r['upstream_status'],'待验证')
        self.assertIn('model',r['upstream_reason'])

    def test_04_missing_selection(self):
        self.call()
        r=self.record()
        self.assertIsNone(r['selected_model'])
        self.assertEqual(r['upstream_status'],'待验证')
        self.assertEqual(r['request_status'],'待验证')

    def test_05_aliases(self):
        self.assertEqual(compare('A','a'),'不一致')
        rules={'A':{'canonical':'a','source':'synthetic confirmed rule'}}
        self.assertEqual(compare('A','a',rules),'一致')
        with self.assertRaises(ValueError):compare('A','a',{'A':{'canonical':'a','source':''}})

    def test_06_retries(self):
        self.turn();self.call(call='first',completed=False);self.call(call='retry')
        rs=next(t for t in self.scan()['threads'] if t['id']=='t')['records']
        self.assertEqual(len(rs),2)
        self.assertEqual({r['inference_call_id'] for r in rs},{'first','retry'})
        self.assertEqual(rs[1]['upstream_status'],'待验证')

    def test_07_cross_thread(self):
        self.turn();self.call(completed=False)
        e=dict(self.events[0]);e['thread_id']='other';e['payload']={'type':'inference_completed','inference_call_id':'call','response_payload':{'path':'foreign.json'}}
        (self.bundle/'foreign.json').write_text('{"model":"A"}',encoding='utf-8')
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(e)+'\n')
        self.assertEqual(self.record()['upstream_status'],'待验证')

    def test_08_model_switch(self):
        self.turn();self.call();self.turn('new','B');self.call('newcall','new','B',completed=False)
        rs=next(t for t in self.scan()['threads'] if t['id']=='t')['records']
        self.assertEqual(rs[0]['selected_model'],'B')
        self.assertEqual(rs[0]['upstream_status'],'待验证')
        self.assertEqual(rs[1]['upstream_status'],'一致')

    def test_09_bad_records_counted(self):
        self.turn();self.call()
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as f:f.write('{broken\n{}\n{"payload":')
        data=self.scan()
        self.assertEqual(data['errors']['bad_json'],2)
        self.assertEqual(data['errors']['missing_fields'],1)
        self.assertEqual(data['error_count'],3)
        self.assertTrue(data['alerts'])

    def test_10_wallpaper_persistence(self):
        save_settings(self.root,{'wallpaper':'aurora'})
        self.assertEqual(read_settings(self.root)['wallpaper'],'aurora')
        with self.assertRaises(ValueError):save_settings(self.root,{'wallpaper':'../../secret'})
        self.assertEqual(read_settings(self.root)['wallpaper'],'aurora')
        save_settings(self.root,{'wallpaper':'default'})
        self.assertEqual(read_settings(self.root)['wallpaper'],'default')

    def test_11_http_and_privacy(self):
        self.turn();self.call()
        s=make_server(Scanner(self.db,self.traces),self.root,'test-session',0)
        self.addCleanup(s.server_close)
        thread=threading.Thread(target=s.serve_forever,daemon=True);thread.start()
        self.addCleanup(s.shutdown)
        base='http://127.0.0.1:'+str(s.server_port)
        with self.assertRaises(HTTPError) as err:urlopen(base+'/api/data')
        self.assertEqual(err.exception.code,401)
        def req(path, data=None):
            return urlopen(Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Authorization':'Bearer test-session','Content-Type':'application/json','Origin':base}))
        with req('/api/data') as r:
            body=r.read().decode();d=json.loads(body)
        self.assertEqual(len(d['threads']),2)
        self.assertNotIn('SECRET_BODY',body)
        self.assertNotIn('DO-NOT-USE-CURRENT',body)
        self.assertNotIn('Authorization',body)
        self.assertEqual(d['threads'][0]['project'],'真实项目')
        with req('/api/settings',{'wallpaper':'dunes'}) as r:self.assertEqual(r.status,200)
        with req('/api/settings') as r:self.assertEqual(json.load(r)['wallpaper'],'dunes')
        with self.assertRaises(HTTPError):req('/api/settings',{'wallpaper':'bad'})
        with req('/api/settings') as r:self.assertEqual(json.load(r)['wallpaper'],'dunes')

    def test_12_unrecorded_and_archived(self):
        d=self.scan(); t=next(t for t in d['threads'] if t['id']=='other')
        self.assertEqual(t['project'],'未分配项目');self.assertTrue(t['archived']);self.assertEqual(t['records'],[])

    def test_13_stale_and_incomplete(self):
        self.turn();self.call()
        for e in self.events:e['wall_time_unix_ms']=1
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in self.events),encoding='utf-8')
        self.assertEqual(self.record()['upstream_status'],'待验证')
        self.assertIn('陈旧',self.record()['upstream_reason'])

    def test_14_read_failure_alert(self):
        scanner=Scanner(self.db,self.traces);self.turn();self.call();d=scanner.scan()
        good=d['last_success'];self.db.rename(self.root/'moved.sqlite')
        d=scanner.scan()
        self.assertTrue(d['alerts']);self.assertEqual(d['last_success'],good)
        self.assertGreater(d['error_count'],0)

    def test_15_partial_payload(self):
        self.turn();self.call()
        (self.bundle/'resp1.json').write_text('{',encoding='utf-8')
        d=self.scan()
        self.assertEqual(d['errors']['bad_json'],1)
        self.assertEqual(next(t for t in d['threads'] if t['id']=='t')['records'][0]['upstream_status'],'待验证')

    def test_16_request_model_is_payload(self):
        self.turn();self.call()
        (self.bundle/'req1.json').write_text('{"model":"B"}',encoding='utf-8')
        self.assertEqual(self.record()['request_model'],'B')
        self.assertEqual(self.record()['request_status'],'不一致')

    def test_17_ambiguous_completion(self):
        self.turn();self.call();self.call()
        self.assertTrue(all(r['upstream_status']=='待验证' for r in next(t for t in self.scan()['threads'] if t['id']=='t')['records']))
