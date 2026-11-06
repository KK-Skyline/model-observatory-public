"""Frozen incremental review counterexamples, before product modification."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from model_monitor.relay_audit.core import CACHE,Manager

BASE=Path(__file__).parent/'evidence/aig-audit'

def fixture(protocol='chat',kind='honest'):
    return json.loads((BASE/('cli-'+protocol+'-'+kind+'.json')).read_text(encoding='utf-8'))

class HistoryResultTests(unittest.TestCase):
    def reject_raw(self,raw):
        with tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
            p=Path(d)/'history.json';p.write_bytes(raw)
            with self.assertRaises(ValueError) as caught:Manager(Path(d))
            self.assertEqual(raw,p.read_bytes());self.assertEqual('Invalid audit history schema; original file preserved',str(caught.exception))
    def reject(self,j):self.reject_raw(json.dumps({j['id']:j}).encode())
    def test_green_without_result_rejected(self):
        for variant in ('empty','missing'):
            with self.subTest(variant=variant):
                j=fixture()
                if variant=='empty':j['engine_result']={}
                else:del j['engine_result']
                self.reject(j)
    def test_incomplete_result_support_rejected(self):
        for field in ('sample_size','best_model_name','top5','forgery'):
            with self.subTest(field=field):
                j=fixture();del j['engine_result'][field];self.reject(j)
        for field in ('status','g2','g2_threshold','known_alt_log_bf'):
            with self.subTest(forgery_field=field):
                j=fixture();del j['engine_result']['forgery'][field];self.reject(j)
        for key,value in [('sample_size',True),('top5',{}),('best_posterior','bad')]:
            with self.subTest(type=key):
                j=fixture();j['engine_result'][key]=value;self.reject(j)
    def test_duplicate_job_key_rejected_without_overwrite(self):
        j=fixture();key=json.dumps(j['id']);raw=('{'+key+':{"state":"completed"},'+key+':'+json.dumps(j)+'}').encode();self.reject_raw(raw)
    def test_duplicate_nested_key_rejected(self):
        j=fixture();raw=json.dumps({j['id']:j}).replace('"state": "completed"','"state": "failed", "state": "completed"').encode();self.reject_raw(raw)
    def test_three_protocol_results_and_legal_states_remain_compatible(self):
        for protocol in ('chat','responses','anthropic'):
            for kind in ('honest','disguised'):
                for state in ('completed','failed','cancelled','running'):
                    with self.subTest(protocol=protocol,kind=kind,state=state),tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
                        j=fixture(protocol,kind);j['state']=state;p=Path(d)/'history.json';p.write_text(json.dumps({j['id']:j}),encoding='utf-8');m=Manager(Path(d));r=m.get(j['id']);self.assertEqual('failed' if state=='running' else state,r['state']);m.close()
    def test_insufficient_failure_cancel_and_calibration_without_engine(self):
        jobs=[]
        for state in ('completed','failed','cancelled','running'):
            j=fixture();j.update(state=state,verdict='证据不足');del j['engine_result'];jobs.append(j)
        calibrated=json.loads((BASE/'agent-context-free.json').read_text(encoding='utf-8'))['calibration'];self.assertNotIn('engine_result',calibrated);jobs.append(calibrated)
        for j in jobs:
            with self.subTest(state=j['state'],mode=j['target'].get('mode')),tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
                p=Path(d)/'history.json';p.write_text(json.dumps({j['id']:j}),encoding='utf-8');m=Manager(Path(d));self.assertEqual(j['id'],m.list()[0]['id']);m.close()
