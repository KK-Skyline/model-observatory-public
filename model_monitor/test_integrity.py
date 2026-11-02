import json
import sqlite3
import unittest
from model_monitor import test_monitor as fixtures


class AdditionalAcceptance(unittest.TestCase):
    setUp = fixtures.ModelAcceptance.setUp
    turn = fixtures.ModelAcceptance.turn
    call = fixtures.ModelAcceptance.call
    scan = fixtures.ModelAcceptance.scan
    record = fixtures.ModelAcceptance.record
    def test_18_project_root_source(self):
        with sqlite3.connect(str(self.db)) as c:
            c.executescript('ALTER TABLE threads ADD COLUMN cwd TEXT; CREATE TABLE project_roots(project_id TEXT,path TEXT);')
            c.execute('UPDATE threads SET cwd=?,project_id=NULL', (str(self.root/'project/sub'),))
            c.execute('INSERT INTO project_roots VALUES (?,?)',('p',str(self.root/'project')))
        d=self.scan()
        self.assertEqual(d['threads'][0]['project'],'真实项目')
        self.assertIn('project_roots',d['threads'][0]['project_source'])

    def test_19_root_collision_unknown(self):
        with sqlite3.connect(str(self.db)) as c:
            c.executescript('ALTER TABLE threads ADD COLUMN cwd TEXT; CREATE TABLE project_roots(project_id TEXT,path TEXT);')
            c.execute('INSERT INTO projects VALUES (?,?)',('p2','第二项目'))
            c.execute('UPDATE threads SET cwd=?,project_id=NULL',(str(self.root),))
            c.executemany('INSERT INTO project_roots VALUES (?,?)',[('p',str(self.root)),('p2',str(self.root))])
        d=self.scan()
        self.assertTrue(all(t['project']=='未分配项目' for t in d['threads']))
        self.assertGreater(d['errors']['association_failure'],0)

    def test_20_read_error_invalidates_green(self):
        self.turn();self.call()
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as f:f.write('{')
        r=self.record()
        self.assertEqual(r['upstream_status'],'待验证')
        self.assertIn('异常',r['upstream_reason'])

    def test_21_same_timestamp_retry_order(self):
        self.turn();self.call(call='old');self.call(call='new',completed=False)
        for e in self.events:e['wall_time_unix_ms']=self.events[0]['wall_time_unix_ms']
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in self.events),encoding='utf-8')
        self.assertEqual(self.record()['inference_call_id'],'new')
