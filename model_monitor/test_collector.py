import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from model_monitor.collector import Store,Observer,Frames,make_server


class CaptureAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent/'data');self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.store=Store(self.path)
    def read(self):return [json.loads(l) for l in (self.path/'upstream.jsonl').read_text().splitlines()]
    def test_header_body_and_privacy(self):
        o=Observer(self.store,'thread-1','http',{'openai-model':'A','Authorization':'SECRET'})
        o.sse(b'data: {"type":"response.created","response":{"id":"resp-1","model":"A","output":"SECRET_BODY"}}\n\n')
        o.sse(b'data: {"type":"response.completed","response":{"id":"resp-1"}}\n\n');o.finish()
        r=self.read()[0];self.assertEqual(r['upstream_model'],'A');self.assertTrue(r['capture_complete'])
        self.assertNotIn('SECRET',(self.path/'upstream.jsonl').read_text())
    def test_conflicting_and_missing_declaration(self):
        o=Observer(self.store,'t','http',{'openai-model':'A'})
        o.event({'type':'response.completed','response':{'id':'r','model':'B'}})
        self.assertTrue(self.read()[0]['conflict']);self.assertIsNone(self.read()[0]['upstream_model'])
        Observer(self.store,'t','websocket',{'openai-model':'DONT-REUSE-HANDSHAKE'}).event({'type':'response.completed','response':{'id':'r2'}})
        self.assertIsNone(self.read()[1]['upstream_model'])
    def test_fragmented_websocket_no_cross_response_reuse(self):
        o=Observer(self.store,'t','websocket');f=Frames(o)
        for rid,model in [('r1','A'),('r2',None)]:
            data={'id':rid}
            if model:data['model']=model
            msg=json.dumps({'type':'response.completed','response':data}).encode()
            part1=msg[:12];part2=msg[12:]
            frames=bytes([1,len(part1)])+part1+bytes([128,len(part2)])+part2
            for i in range(0,len(frames),3):f.feed(frames[i:i+3])
        self.assertEqual([r['upstream_model'] for r in self.read()],['A',None])
    def test_partial_and_invalid_event_counted(self):
        o=Observer(self.store,'t','http');o.sse(b'data: {broken}\n\n');o.sse(b'data: {');o.finish()
        self.assertEqual(self.store.counts['bad_json'],1);self.assertEqual(self.store.counts['incomplete_capture'],1)
    def test_real_http_forward(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*a):return
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                data=b'data: {"type":"response.completed","response":{"id":"real-local-response","model":"A","output":"PRIVATE"}}\n\n'
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        u=ThreadingHTTPServer(('127.0.0.1',0),Upstream);self.addCleanup(u.server_close);threading.Thread(target=u.serve_forever,daemon=True).start();self.addCleanup(u.shutdown)
        proxy=make_server(self.store,0,'127.0.0.1',u.server_port,False);self.addCleanup(proxy.server_close);threading.Thread(target=proxy.serve_forever,daemon=True).start();self.addCleanup(proxy.shutdown)
        c=http.client.HTTPConnection('127.0.0.1',proxy.server_port);c.request('POST','/backend-api/codex/responses',body='PRIVATE_REQUEST',headers={'thread-id':'t','Authorization':'PRIVATE_TOKEN'});r=c.getresponse();self.assertEqual(r.status,200);self.assertIn(b'PRIVATE',r.read());c.close()
        records=self.read();self.assertEqual(records[0]['response_id'],'real-local-response');self.assertEqual(records[0]['thread_id'],'t');self.assertNotIn('PRIVATE',(self.path/'upstream.jsonl').read_text())
