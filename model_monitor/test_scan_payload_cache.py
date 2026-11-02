import hashlib
import json
import os
from pathlib import Path
import threading
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from model_monitor import test_monitor as fixtures
from model_monitor.app import Scanner, make_server


class PayloadCache(unittest.TestCase):
    def setUp(self):
        temporary_directory=tempfile.TemporaryDirectory
        # Keep timing fixtures outside iCloud-managed Documents metadata updates.
        # 计时用例避开 iCloud 管理的 Documents 元数据更新。
        with patch('model_monitor.test_monitor.tempfile.TemporaryDirectory',side_effect=lambda **kwargs:temporary_directory()):
            fixtures.ModelAcceptance.setUp(self)
    turn=fixtures.ModelAcceptance.turn
    call=fixtures.ModelAcceptance.call

    def test_warm_api_request_does_not_repeat_slow_unchanged_payload_reads(self):
        self.turn()
        for i in range(20):self.call(call=str(i))
        scanner=Scanner(self.db,self.traces)
        server=make_server(scanner,self.root,'synthetic-token',0)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        original=Path.read_text
        def slow_payload(path,*args,**kwargs):
            if path.parent==self.bundle and path.name!='trace.jsonl':time.sleep(.01)
            return original(path,*args,**kwargs)
        def fetch(timeout):
            request=Request('http://127.0.0.1:%d/api/data'%server.server_port,headers={'Authorization':'Bearer synthetic-token'})
            with urlopen(request,timeout=timeout) as response:return json.load(response)
        with patch.object(Path,'read_text',slow_payload):
            first=fetch(3)
            second=fetch(.15)
        self.assertEqual(first['threads'],second['threads'])
        self.assertEqual(second['error_count'],0)

    def test_same_size_rewrite_with_restored_mtime_invalidates_cache(self):
        self.turn();self.call()
        scanner=Scanner(self.db,self.traces);scanner.scan()
        path=self.bundle/'resp1.json';before=path.stat()
        raw=path.read_text(encoding='utf-8');path.write_text(raw.replace('"A"','"B"'),encoding='utf-8')
        os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        data=scanner.scan();record=next(t for t in data['threads'] if t['id']=='t')['records'][0]
        self.assertEqual(record['upstream_model'],'B')
        self.assertEqual(record['upstream_status'],'不一致')

    def test_corrupt_changed_payload_never_returns_cached_green(self):
        self.turn();self.call();scanner=Scanner(self.db,self.traces);scanner.scan()
        (self.bundle/'resp1.json').write_text('{broken',encoding='utf-8')
        for _ in range(2):
            data=scanner.scan();record=next(t for t in data['threads'] if t['id']=='t')['records'][0]
            self.assertEqual(data['errors']['bad_json'],1)
            self.assertEqual(record['upstream_status'],'待验证')
            self.assertIsNone(record['upstream_model'])

    def test_deleted_payload_is_reported_instead_of_using_cache(self):
        self.turn();self.call();scanner=Scanner(self.db,self.traces);scanner.scan()
        (self.bundle/'resp1.json').unlink()
        data=scanner.scan();record=next(t for t in data['threads'] if t['id']=='t')['records'][0]
        self.assertEqual(data['errors']['read_failure'],1)
        self.assertEqual(record['upstream_status'],'待验证')

    def test_cache_keeps_metadata_only_and_does_not_write_source_files(self):
        self.turn();self.call();paths=list(self.bundle.glob('*.json'))
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        scanner=Scanner(self.db,self.traces);scanner.scan()
        cached=json.dumps(list(scanner.payload_cache.values()))
        self.assertNotIn('SECRET_BODY',cached)
        self.assertEqual({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},before)
