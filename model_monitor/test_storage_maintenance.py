import hashlib
import fcntl
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from model_monitor.storage_maintenance import maintain


class StorageMaintenance(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name);self.root=self.path/'traces';self.bundle=self.root/'bundle';self.bundle.mkdir(parents=True)
        self.source=self.bundle/'request.json';self.source.write_bytes(b'{"synthetic":"'+b'x'*2000000+b'"}');self.source.chmod(0o600)
        self.report=self.path/'metadata/report.json'
    def trace(self,completed=True,terminal_thread='t',ref='request.json'):
        events=[{'thread_id':'t','codex_turn_id':'turn','payload':{'type':'inference_started','inference_call_id':'call','request_payload':{'path':ref}}}]
        if completed:events.append({'thread_id':terminal_thread,'codex_turn_id':'turn','payload':{'type':'inference_completed','inference_call_id':'call'}})
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    def test_completed_snapshot_is_lossless_and_still_readable_at_original_path(self):
        self.trace();before=hashlib.sha256(self.source.read_bytes()).hexdigest()
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(result['counts']['compressed'],1)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),before)
        self.assertEqual(json.loads(self.source.read_text(encoding='utf-8'))['synthetic'],'x'*2000000)
        self.assertLess(self.source.stat().st_blocks*512,100000)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode),0o600)
        self.assertGreater(result['reclaimed_bytes'],1000000)
        self.assertEqual(result['verified_files'][0]['sha256'],before)
        self.assertEqual(stat.S_IMODE(self.report.stat().st_mode),0o600)
    def test_active_or_wrong_thread_terminal_does_not_make_payload_eligible(self):
        self.trace(completed=False);first=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(first['candidate_files'],0)
        self.trace(terminal_thread='different');second=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(second['candidate_files'],0)
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)
    def test_dry_run_and_recent_files_are_not_modified(self):
        self.trace();before=hashlib.sha256(self.source.read_bytes()).hexdigest()
        dry=maintain(self.root,self.report,False,min_age=0)
        self.assertEqual(dry['counts']['dry_run_candidate'],1)
        recent=maintain(self.root,self.report,True,min_age=3600)
        self.assertEqual(recent['counts']['recent_file_skipped'],1)
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),before)
    def test_checksum_mismatch_preserves_original_and_reports_failure(self):
        self.trace();before=self.source.read_bytes()
        with patch('model_monitor.storage_maintenance.digest',side_effect=['original-hash','different-hash']):
            result=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(result['counts']['compression_failure'],1)
        self.assertEqual(self.source.read_bytes(),before)
        self.assertFalse(list(self.bundle.glob('.codex-compress-*')))
    def test_unsafe_reference_is_counted_and_not_compressed(self):
        outside=self.path/'outside.json';outside.write_bytes(self.source.read_bytes())
        self.trace(ref='../../outside.json');result=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(result['counts']['unsafe_payload_reference'],1)
        self.assertEqual(result['candidate_files'],0)
        self.assertFalse(outside.stat().st_flags&stat.UF_COMPRESSED)
    def test_repeated_runs_skip_already_compressed_snapshots(self):
        self.trace();maintain(self.root,self.report,True,min_age=0)
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(result['counts']['already_compressed'],1)
        self.assertEqual(result['reclaimed_bytes'],0)
    def test_source_change_during_copy_is_not_overwritten(self):
        self.trace()
        from model_monitor import storage_maintenance as storage
        real=storage.subprocess.run
        def changed(*args,**kwargs):
            result=real(*args,**kwargs);self.source.write_bytes(b'new-synthetic-content');return result
        with patch('model_monitor.storage_maintenance.subprocess.run',changed):
            result=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(result['counts']['changed_during_compression_skipped'],1)
        self.assertEqual(self.source.read_bytes(),b'new-synthetic-content')
        self.assertFalse(list(self.bundle.glob('.codex-compress-*')))

    def test_payload_shared_with_active_call_is_not_compressed(self):
        self.trace()
        event={'thread_id':'t','codex_turn_id':'turn','payload':{'type':'inference_started','inference_call_id':'still-active','request_payload':{'path':'request.json'}}}
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(event)+'\n')
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(result['counts']['active_shared_files_skipped'],1)

    def test_invalid_trace_preflight_never_mutates_any_payload(self):
        self.trace()
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as stream:stream.write('{broken\n')
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(result['state'],'blocked_invalid_metadata')
        self.assertEqual(result['counts']['invalid_trace_rows'],1)
    def test_duplicate_stable_call_in_another_trace_is_not_unique(self):
        self.trace();other=self.root/'second';other.mkdir()
        (other/'trace.jsonl').write_text((self.bundle/'trace.jsonl').read_text(encoding='utf-8').replace('request.json','../bundle/request.json'),encoding='utf-8')
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(result['counts']['ambiguous_calls_skipped'],1)
    def test_response_shared_with_ambiguous_call_is_protected(self):
        self.trace()
        extra=[{'thread_id':'t','codex_turn_id':'turn','payload':{'type':'inference_started','inference_call_id':'ambiguous','request_payload':{'path':'private-absent.json'}}},
               {'thread_id':'t','codex_turn_id':'turn','payload':{'type':'inference_completed','inference_call_id':'ambiguous','response_payload':{'path':'request.json'}}}]
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(''.join(json.dumps(v)+'\n' for v in extra+[extra[-1]]))
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(result['counts']['active_shared_files_skipped'],1)
    def test_partial_response_from_unmatched_terminal_is_protected(self):
        self.trace()
        event={'thread_id':'t','codex_turn_id':'turn','payload':{'type':'inference_cancelled','inference_call_id':'no-start','partial_response_payload':{'path':'request.json'}}}
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(event)+'\n')
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(result['counts']['unmatched_terminal_calls'],1)

    def closed_trace(self,ended=True,wrong_identity=False):
        self.trace()
        extra=self.bundle/'misc.json';extra.write_bytes(self.source.read_bytes())
        manifest={'rollout_id':'rollout-1','root_thread_id':'t','trace_id':'trace-1'}
        (self.bundle/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        start={'rollout_id':'rollout-1','payload':{'type':'rollout_started','root_thread_id':'t','trace_id':'trace-1'}}
        invocation={'rollout_id':'rollout-1','payload':{'type':'tool_call_started','invocation_payload':{'path':'misc.json'}}}
        events=[start]+[json.loads(v) for v in (self.bundle/'trace.jsonl').read_text(encoding='utf-8').splitlines()]+[invocation]
        if ended:events.append({'rollout_id':'wrong' if wrong_identity else 'rollout-1','payload':{'type':'rollout_ended','status':'complete'}})
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(v)+'\n' for v in events),encoding='utf-8')
        return extra
    def test_closed_rollout_snapshots_are_compressed_with_matching_identity(self):
        extra=self.closed_trace();before=hashlib.sha256(extra.read_bytes()).hexdigest()
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(result['counts']['verified_closed_traces'],1)
        self.assertTrue(extra.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(hashlib.sha256(extra.read_bytes()).hexdigest(),before)
    def test_open_rollout_misc_snapshots_are_left_in_place(self):
        extra=self.closed_trace(ended=False)
        maintain(self.root,self.report,True,min_age=0)
        self.assertFalse(extra.stat().st_flags&stat.UF_COMPRESSED)
    def test_mismatched_closed_rollout_identity_does_not_authorize_misc_compression(self):
        extra=self.closed_trace(wrong_identity=True)
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(result['counts']['unverified_closed_traces'],1)
        self.assertFalse(extra.stat().st_flags&stat.UF_COMPRESSED)
    def test_closed_rollout_does_not_override_active_shared_payload_protection(self):
        extra=self.closed_trace()
        event={'thread_id':'t','codex_turn_id':'turn','payload':{'type':'inference_started','inference_call_id':'active','request_payload':{'path':'misc.json'}}}
        path=self.bundle/'trace.jsonl';events=[json.loads(v) for v in path.read_text(encoding='utf-8').splitlines()]
        events.insert(-1,event)
        path.write_text(''.join(json.dumps(v)+'\n' for v in events),encoding='utf-8')
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertFalse(extra.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(result['counts']['active_shared_files_skipped'],1)

    def test_malformed_rollout_identity_blocks_before_copy(self):
        self.trace()
        event={'rollout_id':{'invalid':'shape'},'payload':{'type':'rollout_started','root_thread_id':'t','trace_id':'x'}}
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(event)+'\n')
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertEqual(result['state'],'blocked_invalid_metadata')
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)

    def test_same_root_uses_one_lock_even_with_different_report_destinations(self):
        self.trace()
        with (self.root/'snapshot-compression.lock').open('w') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):maintain(self.root,self.path/'other/report.json',True,min_age=0)
        self.assertFalse(self.source.stat().st_flags&stat.UF_COMPRESSED)
    def test_maintenance_audit_history_is_preserved_across_runs(self):
        self.trace();maintain(self.root,self.report,False,min_age=0);maintain(self.root,self.report,False,min_age=0)
        history=list((self.report.parent/'storage-maintenance-history').glob('*.json'))
        self.assertEqual(len(history),2)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in history))

    def test_closed_misc_payload_shared_by_open_trace_is_protected(self):
        extra=self.closed_trace()
        other=self.root/'open';other.mkdir()
        event={'payload':{'type':'tool_call_started','invocation_payload':{'path':'../bundle/misc.json'}}}
        (other/'trace.jsonl').write_text(json.dumps(event)+'\n',encoding='utf-8')
        result=maintain(self.root,self.report,True,min_age=0)
        self.assertFalse(extra.stat().st_flags&stat.UF_COMPRESSED)
        self.assertEqual(result['counts']['active_shared_files_skipped'],1)
