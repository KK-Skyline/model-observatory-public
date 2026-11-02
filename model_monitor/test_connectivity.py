import json
import os
import time
import unittest
from model_monitor import test_monitor as fixtures
from model_monitor.app import Scanner


class ConnectionVisibility(unittest.TestCase):
    setUp=fixtures.ModelAcceptance.setUp
    turn=fixtures.ModelAcceptance.turn
    call=fixtures.ModelAcceptance.call

    def network_failure(self,error='unexpected status 502 Bad Gateway',partial=None):
        self.events[-1]['payload']={'type':'inference_failed','inference_call_id':self.events[-1]['payload']['inference_call_id'],
            'error':error,'partial_response_payload':partial}
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in self.events),encoding='utf-8')

    def thread(self,capture=None):
        data=Scanner(self.db,self.traces,capture).scan()
        return next(thread for thread in data['threads'] if thread['id']=='t')

    def test_network_failure_is_ignorable_but_records_remain(self):
        self.turn();self.call();self.network_failure()
        thread=self.thread()
        self.assertTrue(thread.get('ignore_connection_failure',False))
        self.assertEqual(thread['records'][0]['upstream_status'],'待验证')
        self.assertEqual(thread['connection']['failed_attempts'],1)
        self.assertEqual(len(thread['records']),1)
        self.assertNotIn('Bad Gateway',json.dumps(thread))

    def test_unknown_rate_limit_partial_and_valid_results_stay_visible(self):
        self.turn();self.call(upstream=None)
        self.assertFalse(self.thread().get('ignore_connection_failure',False))
        self.network_failure('unexpected status 429 Too Many Requests')
        self.assertFalse(self.thread().get('ignore_connection_failure',False))
        self.network_failure(partial={'path':'synthetic-partial.json'})
        self.assertFalse(self.thread().get('ignore_connection_failure',False))
        self.call(call='success',upstream='B')
        thread=self.thread()
        self.assertFalse(thread.get('ignore_connection_failure',False))
        self.assertEqual(thread['records'][0]['upstream_status'],'不一致')

    def test_reconnect_state_uses_exact_thread_and_connection_ids(self):
        self.turn();self.call(completed=False)
        capture=self.root/'capture';capture.mkdir()
        now=int(time.time()*1000)
        (capture/'collector-status.json').write_text(json.dumps({'pid':os.getpid(),'counts':{}}),encoding='utf-8')
        journal=capture/'collector-diagnostics.jsonl'
        event={'at':now,'pid':os.getpid(),'kind':'websocket_connect_retry','thread_id':'other','connection_id':'c','attempt':1,'stage':'tls_handshake'}
        journal.write_text(json.dumps(event)+'\n',encoding='utf-8')
        self.assertFalse(self.thread(capture).get('ignore_connection_failure',False))

        event['thread_id']='t';journal.write_text(json.dumps(event)+'\n',encoding='utf-8')
        thread=self.thread(capture)
        self.assertTrue(thread.get('ignore_connection_failure',False))
        self.assertEqual(thread['connection']['state'],'reconnecting')
        self.assertEqual(thread['connection']['reconnect_count'],1)
        event['kind']='websocket_connect_recovered'
        with journal.open('a',encoding='utf-8') as stream:stream.write(json.dumps(event)+'\n')
        self.assertFalse(self.thread(capture).get('ignore_connection_failure',False))

    def test_latest_failed_connection_can_be_ignored_without_erasing_success_history(self):
        self.turn();self.call(call='old-success')
        self.call(call='new-failure');self.network_failure()
        thread=self.thread()
        self.assertTrue(thread.get('ignore_connection_failure',False))
        self.assertEqual(len(thread['records']),2)
        self.assertEqual(thread['records'][1]['upstream_status'],'一致')

    def cancel(self,partial=None):
        self.events[-1]['payload']={'type':'inference_cancelled','inference_call_id':self.events[-1]['payload']['inference_call_id'],
            'reason':'synthetic cancellation','partial_response_payload':partial}
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in self.events),encoding='utf-8')

    def test_cancelled_without_response_is_ignored_as_cancellation(self):
        self.turn();self.call();self.cancel()
        thread=self.thread()
        self.assertTrue(thread.get('ignore_default',False))
        self.assertTrue(thread.get('ignore_cancelled_without_response',False))
        self.assertFalse(thread['ignore_connection_failure'])
        self.assertEqual(thread['ignore_reason'],'cancelled_without_response')
        self.assertEqual(thread['connection']['failed_attempts'],0)
        self.assertEqual(thread['records'][0]['upstream_status'],'待验证')
        self.assertEqual(len(thread['records']),1)

    def test_cancelled_with_partial_response_is_visible(self):
        self.turn();self.call();self.cancel(partial={'path':'partial.json'})
        self.assertFalse(self.thread().get('ignore_default',False))

    def test_success_after_cancelled_request_reappears_with_history(self):
        self.turn();self.call(call='cancelled');self.cancel()
        self.assertTrue(self.thread().get('ignore_default',False))
        self.call(call='next',upstream='B')
        thread=self.thread()
        self.assertFalse(thread['ignore_default'])
        self.assertEqual(thread['records'][0]['upstream_status'],'不一致')
        self.assertEqual(len(thread['records']),2)

    def abort_turn(self,turn):
        with self.rollout.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'type':'event_msg','payload':{'type':'turn_aborted','turn_id':turn,'reason':'interrupted'}})+'\n')

    def test_rollout_abort_proves_cancellation_when_native_end_is_missing(self):
        self.turn();self.call(completed=False);self.abort_turn('turn')
        thread=self.thread()
        self.assertTrue(thread.get('ignore_cancelled_without_response',False))
        self.assertFalse(thread['ignore_connection_failure'])
        self.assertEqual(thread['records'][0]['outcome'],'inference_pending')
        self.assertTrue(thread['records'][0]['cancelled_by_turn'])
        self.assertIn('rollout/t',thread['records'][0]['cancellation_source'])

    def test_abort_from_another_turn_does_not_hide_current_request(self):
        self.turn(turn='old');self.abort_turn('old')
        self.turn(turn='new');self.call(turn='new',completed=False)
        self.assertFalse(self.thread().get('ignore_default',False))

    def test_unattributed_abort_is_counted_without_guessing(self):
        self.turn();self.call(completed=False);self.abort_turn(None)
        data=Scanner(self.db,self.traces).scan()
        self.assertEqual(data['gaps'].get('unattributed_turn_aborts'),1)
        self.assertFalse(next(t for t in data['threads'] if t['id']=='t')['ignore_default'])
