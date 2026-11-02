import json
import os
import unittest
from model_monitor import test_monitor as fixtures
from model_monitor.app import Scanner


class CaptureJoinAcceptance(unittest.TestCase):
    setUp=fixtures.ModelAcceptance.setUp
    turn=fixtures.ModelAcceptance.turn
    call=fixtures.ModelAcceptance.call
    def capture(self,thread='t',model='A',complete=True):
        d=self.root/'capture';d.mkdir(exist_ok=True)
        value={'thread_id':thread,'response_id':'rcall','upstream_model':model,'source_fields':['response.model'],'capture_complete':complete,'completed':True,'conflict':False}
        (d/'upstream.jsonl').write_text(json.dumps(value)+'\n',encoding='utf-8')
        return d
    def test_exact_response_id_join(self):
        self.turn();self.call(upstream=None);capture=self.capture()
        data=Scanner(self.db,self.traces,capture).scan();r=next(t for t in data['threads'] if t['id']=='t')['records'][0]
        self.assertEqual(r['upstream_model'],'A');self.assertEqual(r['upstream_status'],'一致');self.assertIn('upstream-metadata',r['upstream_source'])
    def test_cross_thread_capture_not_used(self):
        self.turn();self.call(upstream=None)
        d=Scanner(self.db,self.traces,self.capture('other')).scan();r=next(t for t in d['threads'] if t['id']=='t')['records'][0]
        self.assertIsNone(r['upstream_model']);self.assertEqual(r['upstream_status'],'待验证')
    def test_incomplete_capture_not_certified(self):
        self.turn();self.call(upstream=None)
        d=Scanner(self.db,self.traces,self.capture(complete=False)).scan();r=next(t for t in d['threads'] if t['id']=='t')['records'][0]
        self.assertEqual(r['upstream_status'],'待验证')
    def test_complete_but_not_declared(self):
        self.turn();self.call(upstream=None)
        d=Scanner(self.db,self.traces,self.capture(model=None)).scan();r=next(t for t in d['threads'] if t['id']=='t')['records'][0]
        self.assertEqual(r['upstream_status'],'待验证');self.assertIn('该回包未声明模型',r['upstream_reason'])
