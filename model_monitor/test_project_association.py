import sqlite3
import hashlib
import json
import unittest

from model_monitor import test_monitor as fixtures
from model_monitor.app import Scanner


class ProjectAssociation(unittest.TestCase):
    setUp=fixtures.ModelAcceptance.setUp
    turn=fixtures.ModelAcceptance.turn
    call=fixtures.ModelAcceptance.call
    scan=fixtures.ModelAcceptance.scan

    def duplicate_roots(self,ambiguous_thread='other'):
        root=str(self.root/'project')
        with sqlite3.connect(str(self.db)) as connection:
            connection.executescript('ALTER TABLE threads ADD COLUMN cwd TEXT; CREATE TABLE project_roots(project_id TEXT,path TEXT);')
            connection.execute('UPDATE threads SET cwd=?',(str(self.root/'unrelated'),))
            connection.execute('INSERT INTO projects VALUES (?,?)',('duplicate','同路径的第二个项目'))
            connection.executemany('INSERT INTO project_roots VALUES (?,?)',[('p',root),('duplicate',root)])
            connection.execute('UPDATE threads SET project_id=NULL,cwd=? WHERE id=?',(root,ambiguous_thread))

    def test_project_ambiguity_does_not_downgrade_unrelated_model_evidence(self):
        self.turn();self.call();self.duplicate_roots()
        data=self.scan()
        self.assertEqual(data['errors']['association_failure'],1)
        thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(thread['records'][0]['upstream_status'],'一致')
        self.assertEqual(data['error_count'],1)
        self.assertEqual(data['blocking_error_count'],0)
        ambiguous=next(t for t in data['threads'] if t['id']=='other')
        self.assertIsNone(ambiguous['project_id'])
        self.assertEqual(ambiguous['project_status'],'ambiguous')
        self.assertEqual({p['id'] for p in ambiguous['project_candidates']},{'p','duplicate'})
        self.assertTrue(any('项目归属' in alert for alert in data['alerts']))

    def test_ambiguous_project_keeps_its_own_independent_model_evidence(self):
        self.turn();self.call();self.duplicate_roots('t')
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(thread['records'][0]['upstream_status'],'一致')
        self.assertEqual(thread['project'],'未分配项目')
        self.assertIn('项目归属待确认',thread['project_source'])
        self.assertIsNone(thread['project_id'])
        self.assertIn('duplicate',thread['project_source'])
        self.assertIsNotNone(data['last_success'])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),before)

    def test_project_warning_does_not_fill_missing_upstream_evidence(self):
        self.turn();self.call(upstream=None);self.duplicate_roots('t')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertIsNone(thread['records'][0]['upstream_model'])
        self.assertEqual(thread['records'][0]['upstream_status'],'待验证')
        self.assertEqual(data['blocking_error_count'],0)

    def test_project_warning_preserves_cancelled_without_response_filter(self):
        self.turn();self.call();self.duplicate_roots()
        self.events[-1]['payload']={'type':'inference_cancelled','inference_call_id':'call'}
        (self.bundle/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in self.events),encoding='utf-8')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertTrue(thread['ignore_cancelled_without_response'])
        self.assertEqual(len(thread['records']),1)

    def test_true_read_failure_still_blocks_model_results(self):
        self.turn();self.call();self.duplicate_roots()
        with (self.bundle/'trace.jsonl').open('a',encoding='utf-8') as stream:stream.write('{broken\n')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(data['error_count'],2)
        self.assertEqual(data['blocking_error_count'],1)
        self.assertEqual(thread['records'][0]['upstream_status'],'待验证')

    def test_response_association_failure_is_not_exempted_by_its_kind(self):
        self.turn();self.call();self.duplicate_roots()
        response=self.bundle/self.events[-1]['payload']['response_payload']['path']
        response.write_text('{"response_id":"wrong-response","model":"A"}',encoding='utf-8')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(data['errors']['association_failure'],2)
        self.assertEqual(data['blocking_error_count'],1)
        self.assertEqual(thread['records'][0]['upstream_status'],'待验证')

    def test_explicit_binding_wins_over_shared_roots(self):
        self.turn();self.call();self.duplicate_roots('t')
        with sqlite3.connect(str(self.db)) as connection:connection.execute('UPDATE threads SET project_id=? WHERE id=?',('p','t'))
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(thread['project_id'],'p')
        self.assertEqual(thread['project_source'],'threads.project_id')
        self.assertEqual(data['error_count'],0)

    def test_unique_longest_root_beats_ambiguous_ancestor(self):
        self.turn();self.call();self.duplicate_roots('t')
        nested=self.root/'project/nested'
        with sqlite3.connect(str(self.db)) as connection:
            connection.execute('INSERT INTO project_roots VALUES (?,?)',('duplicate',str(nested)))
            connection.execute('UPDATE threads SET cwd=? WHERE id=?',(str(nested/'work'),'t'))
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(thread['project_id'],'duplicate')
        self.assertEqual(data['error_count'],0)

    def test_duplicate_rows_of_same_owner_are_unambiguous(self):
        self.turn();self.call();self.duplicate_roots('t')
        with sqlite3.connect(str(self.db)) as connection:
            connection.execute('DELETE FROM project_roots WHERE project_id=?',('duplicate',))
            connection.execute('INSERT INTO project_roots VALUES (?,?)',('p',str(self.root/'project/../project')))
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(thread['project_id'],'p')
        self.assertEqual(data['error_count'],0)

    def test_all_59_project_warnings_keep_root_provenance(self):
        self.turn();self.call();self.duplicate_roots()
        with sqlite3.connect(str(self.db)) as connection:
            connection.executemany('INSERT INTO threads(id,rollout_path,name,title,project_id,archived,updated_at,model,cwd) VALUES (?,?,?,?,?,?,?,?,?)',
                [('ambiguous-'+str(i),str(self.root/'other.jsonl'),'合成归属歧义','',None,0,1,'A',str(self.root/'project')) for i in range(58)])
        data=self.scan();threads=[t for t in data['threads'] if t.get('project_status')=='ambiguous']
        self.assertEqual(data['error_count'],59)
        self.assertEqual(data['project_error_count'],59)
        self.assertEqual(data['blocking_error_count'],0)
        self.assertEqual(len(threads),59)
        for thread in threads:
            self.assertIn(str(self.root/'project'),thread['project_source'])
            self.assertEqual({path for p in thread['project_candidates'] for path in p['root_paths']},{str(self.root/'project')})

    def desktop_state(self,projectless=False,target='duplicate',host='local'):
        value={'thread-project-assignments':{} if projectless else {'t':{'projectKind':'local','projectId':'legacy-project'}},
            'thread-project-membership-host-ids':{'t':host},'projectless-thread-ids':['t'] if projectless else [],
            'app-server-project-id-by-legacy-project-id-by-host':{'local:'+str(self.root):{'legacy-project':target}}}
        (self.root/'.codex-global-state.json').write_text(json.dumps(value),encoding='utf-8')

    def test_desktop_assignment_resolves_shared_root_by_stable_migration_ids(self):
        self.turn();self.call();self.duplicate_roots('t');self.desktop_state()
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(thread['project_id'],'duplicate')
        self.assertIn('thread-project-assignments/t',thread['project_source'])
        self.assertIn('legacy-project -> duplicate',thread['project_source'])
        self.assertEqual(thread['records'][0]['upstream_status'],'一致')
        self.assertEqual(data['error_count'],0)

    def test_canonical_database_binding_retains_precedence(self):
        self.turn();self.call();self.duplicate_roots('t');self.desktop_state()
        with sqlite3.connect(str(self.db)) as connection:connection.execute('UPDATE threads SET project_id=? WHERE id=?',('p','t'))
        thread=next(t for t in self.scan()['threads'] if t['id']=='t')
        self.assertEqual(thread['project_id'],'p')
        self.assertEqual(thread['project_source'],'threads.project_id')

    def test_explicit_projectless_assignment_does_not_guess_from_shared_cwd(self):
        self.turn();self.call();self.duplicate_roots('t');self.desktop_state(projectless=True)
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertIsNone(thread['project_id'])
        self.assertIn('明确无项目',thread['project_source'])
        self.assertEqual(data['error_count'],0)

    def test_invalid_desktop_mapping_is_reported_without_model_downgrade(self):
        self.turn();self.call();self.duplicate_roots('t');self.desktop_state(target='missing-project')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(data['errors']['project_binding_invalid'],1)
        self.assertIsNone(thread['project_id'])
        self.assertEqual(thread['records'][0]['upstream_status'],'一致')
        self.assertEqual(data['blocking_error_count'],0)

    def test_remote_membership_is_not_applied_as_local_assignment(self):
        self.turn();self.call();self.duplicate_roots('t');self.desktop_state(host='remote-other')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertIsNone(thread['project_id'])
        self.assertEqual(data['errors']['project_binding_invalid'],1)

    def test_partial_desktop_state_is_counted_as_project_warning(self):
        self.turn();self.call();self.duplicate_roots('t')
        (self.root/'.codex-global-state.json').write_text('{broken',encoding='utf-8')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(data['errors']['project_state_invalid'],1)
        self.assertEqual(data['blocking_error_count'],0)
        self.assertEqual(thread['records'][0]['upstream_status'],'一致')

    def test_repaired_desktop_assignment_is_seen_on_next_refresh(self):
        self.turn();self.call();self.duplicate_roots('t');self.desktop_state()
        scanner=Scanner(self.db,self.traces)
        first=next(t for t in scanner.scan()['threads'] if t['id']=='t')
        self.assertEqual(first['project_id'],'duplicate')
        self.desktop_state(target='p')
        second=next(t for t in scanner.scan()['threads'] if t['id']=='t')
        self.assertEqual(second['project_id'],'p')
        self.assertEqual(second['records'][0]['upstream_status'],'一致')

    def test_legacy_local_assignment_without_membership_uses_exact_host_migration(self):
        self.turn();self.call();self.duplicate_roots('t');self.desktop_state()
        path=self.root/'.codex-global-state.json';value=json.loads(path.read_text(encoding='utf-8'))
        value['thread-project-membership-host-ids']={};path.write_text(json.dumps(value),encoding='utf-8')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertEqual(thread['project_id'],'duplicate')
        self.assertEqual(data['error_count'],0)

    def test_mapping_from_other_codex_home_is_not_used(self):
        self.turn();self.call();self.duplicate_roots('t');self.desktop_state()
        path=self.root/'.codex-global-state.json';value=json.loads(path.read_text(encoding='utf-8'))
        value['app-server-project-id-by-legacy-project-id-by-host']={'local:/other-codex-home':{'legacy-project':'duplicate'}}
        path.write_text(json.dumps(value),encoding='utf-8')
        data=self.scan();thread=next(t for t in data['threads'] if t['id']=='t')
        self.assertIsNone(thread['project_id'])
        self.assertEqual(data['errors']['project_binding_invalid'],1)

