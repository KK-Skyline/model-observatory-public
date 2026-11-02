import json,time,unittest
from model_monitor import test_monitor as fixtures
from model_monitor.app import Scanner


class TimeoutState(unittest.TestCase):
    setUp=fixtures.ModelAcceptance.setUp
    turn=fixtures.ModelAcceptance.turn
    call=fixtures.ModelAcceptance.call
    scan=fixtures.ModelAcceptance.scan

    def native_timeout(self):
        self.events[-1]['payload']={'type':'inference_failed','inference_call_id':'call','error':'stream idle timeout while waiting for response'}
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in self.events),encoding='utf-8')

    def journal(self,tid='t'):
        directory=self.root/'capture';directory.mkdir(exist_ok=True)
        e={'kind':'upstream_wait_timeout','at':int(time.time()*1000),'thread_id':tid,'connection_id':'exact-connection','transport':'websocket','stage':'upstream_read','wait_limit_seconds':300}
        (directory/'collector-diagnostics.jsonl').write_text(json.dumps(e)+'\n',encoding='utf-8')
        return directory

    def test_native_timeout_is_separate_from_model_mismatch(self):
        self.turn();self.call();self.native_timeout()
        t=next(t for t in self.scan()['threads'] if t['id']=='t');r=t['records'][0]
        self.assertEqual(r['execution_state'],'timeout');self.assertEqual(t['execution_state'],'timeout')
        self.assertEqual(r['upstream_status'],'待验证');self.assertEqual(r['request_status'],'一致')
        self.assertIn('execution_timeout',r['evidence_diagnosis']['blockers'])

    def test_collector_thread_history_does_not_falsely_mark_current_request(self):
        self.turn();self.call(upstream='B');directory=self.journal()
        d=Scanner(self.db,self.traces,directory).scan();t=next(t for t in d['threads'] if t['id']=='t')
        self.assertIsNone(t['execution_state']);self.assertEqual(t['timeout_history_count'],1)
        self.assertEqual(t['records'][0]['upstream_status'],'不一致')
        self.assertEqual(d['timeout_diagnostics'][0]['state'],'timeout')
        self.assertEqual(d['timeout_diagnostics'][0]['connection_id'],'exact-connection')

    def test_unattributed_timeout_stays_in_details_without_attaching_to_thread(self):
        self.turn();self.call();d=Scanner(self.db,self.traces,self.journal(None)).scan()
        self.assertEqual(len(d['timeout_diagnostics']),1)
        self.assertTrue(all(t['timeout_history_count']==0 for t in d['threads']))

    def test_recovery_does_not_inherit_old_timeout_state(self):
        self.turn();self.call();self.native_timeout();self.call(call='recovered')
        t=next(t for t in self.scan()['threads'] if t['id']=='t')
        self.assertIsNone(t['execution_state']);self.assertEqual(t['records'][0]['upstream_status'],'一致')
        self.assertEqual(t['records'][1]['execution_state'],'timeout')
