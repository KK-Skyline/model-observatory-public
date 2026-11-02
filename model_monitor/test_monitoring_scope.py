import json
import sqlite3
import unittest

from model_monitor import test_monitor as fixtures


class MonitoringScope(unittest.TestCase):
    setUp=fixtures.ModelAcceptance.setUp
    turn=fixtures.ModelAcceptance.turn
    call=fixtures.ModelAcceptance.call
    scan=fixtures.ModelAcceptance.scan

    def db_source(self,thread,source,kind=None):
        with sqlite3.connect(str(self.db)) as connection:
            connection.executescript('ALTER TABLE threads ADD COLUMN source TEXT; ALTER TABLE threads ADD COLUMN thread_source TEXT;')
            connection.execute('UPDATE threads SET source=?,thread_source=? WHERE id=?',(source,kind,thread))

    def native_source(self,thread,source,metadata_thread=None):
        (self.bundle/'scope.json').write_text(json.dumps({'thread_id':metadata_thread or thread,'session_source':source}),encoding='utf-8')
        event={'thread_id':thread,'payload':{'type':'thread_started','thread_id':thread,'metadata_payload':{'path':'scope.json'}}}
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(event)+'\n')

    def test_database_subagents_are_excluded_without_deleting_history(self):
        self.turn();self.call();self.call(call='child',thread='other')
        self.db_source('other',json.dumps({'subagent':{'thread_spawn':{'parent_thread_id':'t'}}}),'subagent')
        before=(self.bundle/'trace.jsonl').read_bytes();data=self.scan()
        self.assertNotIn('other',{t['id'] for t in data['threads']})
        self.assertEqual(data['gaps']['excluded_subagent_threads'],1)
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'一致')
        self.assertEqual((self.bundle/'trace.jsonl').read_bytes(),before)

    def test_unrecorded_native_subagent_is_excluded_by_stable_metadata_id(self):
        self.turn();self.call();self.call(call='child',thread='child-only')
        self.native_source('child-only',{'subagent':{'other':'guardian'}})
        data=self.scan()
        self.assertNotIn('child-only',{t['id'] for t in data['threads']})
        self.assertEqual(data['gaps']['excluded_subagent_threads'],1)

    def test_unknown_source_or_model_name_does_not_hide_user_thread(self):
        self.turn(model='gpt-6-luna');self.call(model='gpt-6-luna',upstream='gpt-6-luna')
        self.db_source('t',json.dumps({'custom':'thread_sample'}),'user')
        data=self.scan()
        self.assertIn('t',{t['id'] for t in data['threads']})
        self.assertEqual(data['gaps']['excluded_subagent_threads'],0)

    def test_mismatched_source_thread_id_does_not_exclude_unproven_child(self):
        self.turn();self.call();self.call(call='child',thread='child-only')
        self.native_source('child-only',{'subagent':{}},metadata_thread='wrong-thread')
        data=self.scan()
        self.assertIn('child-only',{t['id'] for t in data['threads']})
        self.assertEqual(data['errors']['scope_metadata_invalid'],1)
        self.assertEqual(data['blocking_error_count'],0)

    def test_invalid_source_is_counted_and_kept_without_model_downgrade(self):
        self.turn();self.call();self.db_source('t','{broken')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(data['errors']['scope_metadata_invalid'],1)
        self.assertEqual(data['monitoring_error_count'],1)
        self.assertEqual(thread['records'][0]['upstream_status'],'一致')

    def test_explicit_subagent_trace_is_not_parsed_for_model_failures(self):
        self.turn();self.call();self.db_source('other','{"subagent":{}}','subagent')
        child=self.traces/'child';child.mkdir()
        (child/'manifest.json').write_text('{"root_thread_id":"other"}',encoding='utf-8')
        (child/'trace.jsonl').write_text('{broken',encoding='utf-8')
        data=self.scan()
        self.assertEqual(data['error_count'],0)
        self.assertEqual(data['gaps']['excluded_subagent_trace_files'],1)

    def test_late_native_subagent_classification_precedes_bad_rollout_read(self):
        self.turn();self.call();self.native_source('other',{'subagent':{}})
        (self.root/'other.jsonl').write_text('{broken',encoding='utf-8')
        data=self.scan()
        self.assertNotIn('other',{t['id'] for t in data['threads']})
        self.assertEqual(data['blocking_error_count'],0)
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'一致')

    def test_bad_trace_row_before_proven_child_marker_does_not_block_parent(self):
        self.turn();self.call()
        child=self.traces/'child';child.mkdir()
        (child/'manifest.json').write_text('{"root_thread_id":"child-only"}',encoding='utf-8')
        (child/'scope.json').write_text('{"thread_id":"child-only","session_source":{"subagent":{}}}',encoding='utf-8')
        event={'thread_id':'child-only','payload':{'type':'thread_started','thread_id':'child-only','metadata_payload':{'path':'scope.json'}}}
        (child/'trace.jsonl').write_text('{broken\n'+json.dumps(event)+'\n',encoding='utf-8')
        data=self.scan()
        self.assertEqual(data['blocking_error_count'],0)
        self.assertEqual(data['gaps']['excluded_subagent_threads'],1)
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'一致')

    def test_invalid_source_field_is_reported_even_if_other_field_proves_child(self):
        self.turn();self.call();self.db_source('other','{broken','subagent')
        data=self.scan()
        self.assertNotIn('other',{t['id'] for t in data['threads']})
        self.assertEqual(data['errors']['scope_metadata_invalid'],1)
        self.assertEqual(data['blocking_error_count'],0)

    def test_child_marker_in_later_trace_excludes_earlier_child_errors(self):
        self.turn();self.call()
        early=self.traces/'a-child';early.mkdir()
        (early/'manifest.json').write_text('{"root_thread_id":"child-only"}',encoding='utf-8')
        (early/'trace.jsonl').write_text('{broken',encoding='utf-8')
        later=self.traces/'z-child';later.mkdir()
        (later/'manifest.json').write_text('{"root_thread_id":"child-only"}',encoding='utf-8')
        (later/'scope.json').write_text('{"thread_id":"child-only","session_source":{"subagent":{}}}',encoding='utf-8')
        event={'thread_id':'child-only','payload':{'type':'thread_started','thread_id':'child-only','metadata_payload':{'path':'scope.json'}}}
        (later/'trace.jsonl').write_text(json.dumps(event)+'\n',encoding='utf-8')
        data=self.scan()
        self.assertEqual(data['blocking_error_count'],0)
        self.assertEqual(data['gaps']['excluded_subagent_trace_errors'],1)
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'一致')
