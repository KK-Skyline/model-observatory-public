import json
import unittest
from model_monitor import test_monitor as fixtures


class PendingEvidence(unittest.TestCase):
    setUp=fixtures.ModelAcceptance.setUp
    turn=fixtures.ModelAcceptance.turn
    call=fixtures.ModelAcceptance.call
    scan=fixtures.ModelAcceptance.scan

    def native_source(self,thread,source='vscode',kind='user',envelope=None):
        (self.bundle/'scope-native.json').write_text(json.dumps({'thread_id':thread,'session_source':source}),encoding='utf-8')
        (self.bundle/'configured.json').write_text(json.dumps({'type':'session_configured','thread_id':thread,'thread_source':kind}),encoding='utf-8')
        (self.bundle/'manifest.json').write_text(json.dumps({'root_thread_id':thread}),encoding='utf-8')
        events=[{'thread_id':envelope,'payload':{'type':'thread_started','thread_id':thread,'metadata_payload':{'path':'scope-native.json'}}},
                {'thread_id':None,'payload':{'type':'protocol_event_observed','event_type':'session_configured','event_payload':{'path':'configured.json'}}}]
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as stream:
            for event in events:stream.write(json.dumps(event)+'\n')

    def test_real_native_null_envelope_subagent_is_excluded(self):
        self.turn();self.call();self.call(thread='child-only',call='child')
        self.native_source('child-only',{'subagent':{}})
        data=self.scan()
        self.assertNotIn('child-only',{t['id'] for t in data['threads']})
        self.assertEqual(data['gaps']['excluded_subagent_threads'],1)

    def test_proven_title_worker_is_not_a_user_conversation(self):
        self.turn();self.call();self.call(thread='title-only',call='title')
        self.native_source('title-only',kind='thread_title')
        before=(self.bundle/'trace.jsonl').read_bytes();data=self.scan()
        self.assertNotIn('title-only',{t['id'] for t in data['threads']})
        self.assertEqual(data['gaps']['excluded_internal_task_threads'],1)
        self.assertEqual((self.bundle/'trace.jsonl').read_bytes(),before)

    def test_native_only_user_kept_and_never_uses_session_model_as_selection(self):
        self.call(thread='user-only');self.native_source('user-only')
        data=self.scan();record=next(t for t in data['threads'] if t['id']=='user-only')['records'][0]
        self.assertIsNone(record['selected_model'])
        self.assertEqual(record['upstream_status'],'待验证')
        self.assertIn('selection_missing',record['evidence_diagnosis']['blockers'])

    def test_conflicting_native_envelope_does_not_hide_thread(self):
        self.call(thread='user-only');self.native_source('user-only',{'subagent':{}},envelope='wrong-thread')
        data=self.scan()
        self.assertIn('user-only',{t['id'] for t in data['threads']})
        self.assertGreater(data['monitoring_error_count'],0)

    def test_mismatched_configured_id_does_not_hide_thread(self):
        self.call(thread='user-only');self.native_source('user-only',kind='thread_title')
        (self.bundle/'configured.json').write_text('{"type":"session_configured","thread_id":"other-id","thread_source":"thread_title"}',encoding='utf-8')
        data=self.scan()
        self.assertIn('user-only',{t['id'] for t in data['threads']})
        self.assertGreater(data['monitoring_error_count'],0)

    def test_completed_missing_model_has_a_distinct_reason_from_pending(self):
        self.turn();self.call(upstream=None)
        data=self.scan();record=next(t for t in data['threads'] if t['id']=='t')['records'][0]
        self.assertIn('response_model_missing_no_capture',record['evidence_diagnosis']['blockers'])
        self.assertNotIn('native_terminal_missing',record['evidence_diagnosis']['blockers'])
        self.assertEqual(data['pending_diagnostics']['latest_default']['pending'],1)

    def test_child_configuration_in_parent_trace_does_not_raise_false_diagnostic(self):
        self.turn();self.call();self.call(thread='child-only',call='child')
        self.native_source('child-only',{'subagent':{}},kind='subagent')
        (self.bundle/'manifest.json').write_text('{"root_thread_id":"t"}',encoding='utf-8')
        data=self.scan()
        self.assertEqual(data['monitoring_error_count'],0)
        self.assertNotIn('child-only',{t['id'] for t in data['threads']})

    def test_upstream_incomplete_is_not_misreported_as_capture_corruption(self):
        from model_monitor.app import Scanner
        self.turn();self.call(upstream=None)
        capture=self.root/'capture';capture.mkdir()
        (capture/'upstream.jsonl').write_text(json.dumps({'thread_id':'t','response_id':'rcall','capture_complete':True,'completed':False,'terminal_type':'response.incomplete','upstream_model':'A'})+'\n',encoding='utf-8')
        data=Scanner(self.db,self.traces,capture).scan();r=next(t for t in data['threads'] if t['id']=='t')['records'][0]
        self.assertEqual(r['upstream_status'],'待验证')
        self.assertIn('upstream_incomplete',r['evidence_diagnosis']['blockers'])
        self.assertNotIn('capture_corrupt',r['evidence_diagnosis']['blockers'])

    def test_diagnostics_never_change_stale_exact_comparison(self):
        import time
        self.turn();self.call()
        for event in self.events:event['wall_time_unix_ms']=int(time.time()*1000)-25*3600*1000
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in self.events),encoding='utf-8')
        r=next(t for t in self.scan()['threads'] if t['id']=='t')['records'][0]
        self.assertEqual(r['upstream_status'],'待验证')
        self.assertEqual(r['evidence_diagnosis']['blockers'],['stale'])
