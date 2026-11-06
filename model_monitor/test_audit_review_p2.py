"""Frozen regression contracts for the two management-review P2 findings."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from model_monitor.relay_audit.core import CACHE, Manager

EVIDENCE=Path(__file__).parent/'evidence/aig-audit'

def valid_job():
    return json.loads((EVIDENCE/'cli-chat-honest.json').read_text(encoding='utf-8'))

class HistoryBoundaryTests(unittest.TestCase):
    def reject(self,history):
        with tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
            path=Path(d)/'history.json';raw=json.dumps(history,ensure_ascii=False).encode();path.write_bytes(raw)
            with self.assertRaises(ValueError) as caught:Manager(Path(d))
            self.assertNotIn('SYNTHETIC_PRIVATE',str(caught.exception));self.assertEqual(raw,path.read_bytes())
    def test_incomplete_terminal_rejected(self):
        self.reject({'x':{'state':'completed'}})
    def test_mixed_valid_corrupt_rejected_without_partial_write(self):
        j=valid_job();self.reject({j['id']:j,'bad':{'state':'completed','secret':'SYNTHETIC_PRIVATE'}})
    def test_types_and_missing_fields_are_rejected(self):
        original=valid_job()
        for key,value in [('target',{}),('errors',True),('failures','SYNTHETIC_PRIVATE'),('valid_samples',-1),('state','unknown'),('created_at','bad'),('context',{'saw_start':'true'}),('baseline_hashes',[1]),('notice',None),('engine_result',[]),('schema_version',True)]:
            with self.subTest(key=key):
                j=copy.deepcopy(original);j[key]=value;self.reject({j['id']:j})
        for key in original:
            with self.subTest(missing=key):
                if key in ('context','engine_result'):continue
                j=copy.deepcopy(original);del j[key];self.reject({original['id']:j})
    def test_all_records_validated_before_running_recovery(self):
        j=valid_job();j['state']='running';self.reject({j['id']:j,'bad':{'state':'completed'}})
    def test_valid_terminal_history_retained(self):
        for state in ('completed','failed','cancelled'):
            with self.subTest(state=state),tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
                j=valid_job();j['state']=state;path=Path(d)/'history.json';path.write_text(json.dumps({j['id']:j}),encoding='utf-8')
                m=Manager(Path(d));self.assertEqual([j],m.list());m.close()
    def test_valid_running_history_recovered_explicitly(self):
        with tempfile.TemporaryDirectory(dir=str(CACHE)) as d:
            j=valid_job();j['state']='running';j['verdict']='证据不足';path=Path(d)/'history.json';path.write_text(json.dumps({j['id']:j}),encoding='utf-8')
            m=Manager(Path(d));r=m.get(j['id']);self.assertEqual('failed',r['state']);self.assertEqual(j['errors']+1,r['errors']);self.assertEqual(j['failures']+['interrupted_by_restart'],r['failures']);self.assertEqual(r,json.loads(path.read_text())[j['id']]);m.close()
