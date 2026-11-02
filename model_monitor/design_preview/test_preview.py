import json
import re
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from model_monitor import app
from model_monitor.design_preview.preview import BASE, demo_snapshot, make_preview


class PreviewAcceptance(unittest.TestCase):
    def test_original_controller_and_dom_contract(self):
        html = (BASE/'static/index.html').read_text(encoding='utf-8')
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        controller = (app.BASE/'static/app.js').read_text(encoding='utf-8')
        self.assertTrue(set(re.findall(r"\$\('([^']+)'\)", controller)).issubset(ids))

    def test_http_api_and_settings_remain_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = demo_snapshot(temp)
            self.assertTrue(snapshot['demo'])
            self.assertEqual(snapshot['error_count'], 0)
            self.assertEqual({t['records'][0]['upstream_status'] for t in snapshot['threads']},
                             {'一致', '不一致', '待验证'})
            from pathlib import Path
            scanner = app.Scanner(Path(temp)/'demo/state.sqlite', Path(temp)/'demo/traces')
            server = make_preview(scanner, temp, 'synthetic-test-only', 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = 'http://127.0.0.1:'+str(server.server_port)
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(url+'/api/data')
                self.assertEqual(rejected.exception.code, 401)
                headers = {'Authorization':'Bearer synthetic-test-only', 'Content-Type':'application/json'}
                with urlopen(Request(url+'/api/data', headers=headers)) as response:
                    result = json.load(response)
                self.assertEqual(len(result['threads']), 3)
                with urlopen(Request(url+'/api/settings', data=b'{"wallpaper":"aurora"}', headers=headers)) as response:
                    self.assertEqual(json.load(response), {'wallpaper':'aurora'})
                self.assertEqual(app.read_settings(temp), {'wallpaper':'aurora'})
                with urlopen(url+'/app.js') as response:
                    code = response.read()
                self.assertTrue(code.startswith((app.BASE/'static/app.js').read_bytes()))
                with urlopen(url+'/icons/search.svg') as response:
                    self.assertIn(b'<svg', response.read())
                with self.assertRaises(HTTPError):
                    urlopen(url+'/icons/../../data/session.json')
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
