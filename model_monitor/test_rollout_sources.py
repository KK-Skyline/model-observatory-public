import json
import unittest
from model_monitor import test_monitor as fixtures


class RolloutSources(unittest.TestCase):
    setUp=fixtures.ModelAcceptance.setUp
    turn=fixtures.ModelAcceptance.turn
    call=fixtures.ModelAcceptance.call
    scan=fixtures.ModelAcceptance.scan

    def old_rollout(self,tid='t',model='A',turn='turn',folder='sessions'):
        directory=self.root/folder;directory.mkdir(exist_ok=True)
        path=directory/'untrusted-name.jsonl'
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':tid}})+'\n'+json.dumps({'type':'turn_context','payload':{'turn_id':turn,'model':model}})+'\n',encoding='utf-8')
        return path

    def test_older_same_thread_session_recovers_exact_turn_selection(self):
        self.call();self.turn('new-turn','B');self.old_rollout()
        data=self.scan();r=next(t for t in data['threads'] if t['id']=='t')['records'][0]
        self.assertEqual(r['selected_model'],'A');self.assertEqual(r['upstream_status'],'一致')
        self.assertIn('untrusted-name.jsonl',r['selection_source'])
        self.assertEqual(data['gaps']['additional_selection_sources'],1)

    def test_foreign_thread_filename_cannot_prove_selection(self):
        self.call();path=self.old_rollout(tid='foreign');path.rename(path.with_name('rollout-t.jsonl'))
        r=next(t for t in self.scan()['threads'] if t['id']=='t')['records'][0]
        self.assertIsNone(r['selected_model']);self.assertEqual(r['upstream_status'],'待验证')

    def test_same_turn_conflicting_models_remain_pending(self):
        self.turn();self.call();self.old_rollout(model='B')
        r=next(t for t in self.scan()['threads'] if t['id']=='t')['records'][0]
        self.assertIsNone(r['selected_model']);self.assertEqual(r['upstream_status'],'待验证')
        self.assertIn('选择冲突',r['upstream_reason'])

    def test_archived_source_and_duplicate_current_path_do_not_duplicate_records(self):
        self.turn();self.call();path=self.old_rollout(folder='archived_sessions')
        data=self.scan();r=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(len(r['records']),1);self.assertEqual(r['records'][0]['upstream_status'],'一致')

    def test_broken_additional_source_counts_error_and_never_leaves_old_green(self):
        self.turn();self.call();path=self.old_rollout()
        with path.open('a',encoding='utf-8') as f:f.write('{broken\n')
        data=self.scan();self.assertEqual(data['errors']['bad_json'],1)
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'待验证')

    def test_changed_old_source_is_detected_on_next_scan(self):
        from model_monitor.app import Scanner
        self.call();path=self.old_rollout();scanner=Scanner(self.db,self.traces)
        self.assertEqual(next(t for t in scanner.scan()['threads'] if t['id']=='t')['records'][0]['selected_model'],'A')
        self.old_rollout(model='B')
        self.assertEqual(next(t for t in scanner.scan()['threads'] if t['id']=='t')['records'][0]['upstream_status'],'不一致')

    def test_identity_change_between_discovery_and_read_is_never_joined(self):
        from unittest.mock import patch
        from model_monitor.rollout_sources import session_sources
        self.call();path=self.old_rollout()
        def race(*args):
            result=session_sources(*args)
            self.old_rollout(tid='foreign',model='A')
            return result
        with patch('model_monitor.app.session_sources',race):data=self.scan()
        r=next(t for t in data['threads'] if t['id']=='t')['records'][0]
        self.assertIsNone(r['selected_model']);self.assertEqual(r['upstream_status'],'待验证')
        self.assertGreater(data['errors']['association_failure'],0)

    def test_database_rollout_explicit_foreign_identity_blocks_selection(self):
        self.call()
        self.rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':'foreign'}})+'\n'+json.dumps({'type':'turn_context','payload':{'turn_id':'turn','model':'A'}})+'\n',encoding='utf-8')
        data=self.scan();r=next(t for t in data['threads'] if t['id']=='t')['records'][0]
        self.assertIsNone(r['selected_model']);self.assertEqual(r['upstream_status'],'待验证')
        self.assertGreater(data['errors']['association_failure'],0)

    def test_unreadable_session_identity_does_not_leave_existing_green(self):
        self.turn();self.call();path=self.old_rollout()
        path.write_text('{broken-header\n',encoding='utf-8')
        data=self.scan()
        self.assertGreater(data['blocking_error_count'],0)
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'待验证')
