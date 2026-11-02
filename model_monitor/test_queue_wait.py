import http.client
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import select
import socket
import threading
import time
import unittest
from unittest.mock import patch

from model_monitor.collector import make_server
from model_monitor import test_collector_followup as fixtures
from model_monitor import test_monitor as monitor_fixtures
from model_monitor.app import Scanner


class QueueWait(unittest.TestCase):
    setUp=fixtures.CollectorFollowup.setUp
    serve=fixtures.CollectorFollowup.serve

    def http_wait(self,before_headers):
        wire=b'data: {"type":"response.completed","response":{"id":"queue-r","model":"A"}}\n\n'
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                if before_headers:time.sleep(.24)
                self.send_response(200);self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(wire)));self.end_headers()
                if not before_headers:time.sleep(.24)
                try:self.wfile.write(wire)
                except BrokenPipeError:self.server.client_closed=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        real=http.client.HTTPConnection
        class DeadlineSocket:
            def __init__(self,sock):self.sock=sock
            def __getattr__(self,name):return getattr(self.sock,name)
            def settimeout(self,value):self.sock.settimeout(value/1000 if value>=100 else value)
        class ScaledConnection(real):
            def connect(self):
                super().connect();self.sock=DeadlineSocket(self.sock)
        with patch('model_monitor.collector.http.client.HTTPConnection',ScaledConnection):
            proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
            client=real('127.0.0.1',proxy.server_port,timeout=3);self.addCleanup(client.close)
            client.request('POST','/backend-api/codex/responses',body=b'{}',headers={'thread-id':'t'})
            response=client.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(response.read(),wire)
        self.assertEqual(self.store.counts['http_error'],0)

    def test_queue_before_http_headers_does_not_cut_provider_wait_short(self):self.http_wait(True)

    def test_queue_before_first_sse_event_does_not_cut_provider_wait_short(self):self.http_wait(False)

    def test_websocket_waits_for_delayed_first_response(self):
        payload=b'{"type":"response.completed","response":{"id":"queue-ws","model":"A"}}'
        frame=bytes([0x81,len(payload)])+payload
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
                time.sleep(.24)
                try:self.connection.sendall(frame)
                except BrokenPipeError:self.server.client_closed=True
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        real_select=select.select
        def scaled(read,write,error,timeout):return real_select(read,write,error,timeout/1000 if timeout>=100 else timeout)
        with patch('model_monitor.collector.select.select',scaled):
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=3) as client:
                client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n'%proxy.server_port).encode('ascii'))
                headers=b''
                while not headers.endswith(b'\r\n\r\n'):headers+=client.recv(1)
                self.assertEqual(client.recv(4096),frame)
        self.assertEqual(self.store.counts['websocket_idle_closed'],0)

    def websocket_first_response(self,send_request,complete=False,client_pings=False,server_pongs=False,invalid_prefix=False):
        payload=b'{"type":"response.create","input":"synthetic"}'
        mask=b'1234'
        request=bytes([0x81,0x80|len(payload)])+mask+bytes(c^mask[i%4] for i,c in enumerate(payload))
        if invalid_prefix:request=b'\x81\x07invalid'+request
        received=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
                if send_request:
                    data=b''
                    while len(data)<len(request):data+=self.connection.recv(len(request)-len(data))
                    received.append(data)
                if complete:
                    body=b'{"type":"response.completed","response":{"id":"first-r","model":"A"}}'
                    self.connection.sendall(bytes([0x81,len(body)])+body)
                if server_pongs:
                    deadline=real_clock()+.42
                    while real_clock()<deadline:
                        if real_select([self.connection],[],[],.02)[0]:
                            data=self.connection.recv(4096)
                            if not data:break
                            try:self.connection.sendall(b'\x8a\x00')
                            except (BrokenPipeError,ConnectionResetError):break
                else:time.sleep(.42)
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        real_select=select.select;real_clock=time.monotonic;stop=threading.Event()
        def scaled(read,write,error,timeout):return real_select(read,write,error,timeout/1000 if timeout>=1 else timeout)
        with patch('model_monitor.collector.select.select',scaled),patch('model_monitor.collector.time.monotonic',lambda:real_clock()*1000):
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=3) as client:
                client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nthread-id: t\r\n\r\n'%proxy.server_port).encode('ascii'))
                headers=b''
                while not headers.endswith(b'\r\n\r\n'):headers+=client.recv(1)
                if send_request:client.sendall(request)
                def ping():
                    while not stop.wait(.04):
                        try:client.sendall(b'\x89\x80abcd')
                        except (BrokenPipeError,ConnectionResetError):return
                sender=threading.Thread(target=ping,daemon=True)
                if client_pings:sender.start()
                try:
                    while client.recv(4096):pass
                except ConnectionResetError:
                    # Closing with unread ping bytes can produce RST instead of FIN.
                    # 关闭时若仍有未读 ping 字节，系统可能发送 RST 而非 FIN。
                    self.client_reset=True
                finally:
                    stop.set()
                    if client_pings:sender.join(1)
        if send_request:self.assertEqual(received,[request])

    def test_sent_request_first_response_timeout_ignores_client_pings(self):
        self.websocket_first_response(True,client_pings=True)
        self.assertEqual(self.store.counts['upstream_wait_timeout'],1)
        event=next(v for v in self.store.recent if v['kind']=='upstream_wait_timeout')
        self.assertEqual(event['stage'],'upstream_first_response')
        self.assertEqual(event['thread_id'],'t')
        self.assertEqual(event['wait_limit_seconds'],300)

    def test_unrequested_websocket_idle_is_not_model_wait_timeout(self):
        self.websocket_first_response(False)
        self.assertEqual(self.store.counts['upstream_wait_timeout'],0)
        self.assertEqual(self.store.counts['websocket_idle_closed'],1)

    def test_terminal_response_clears_sent_request_wait(self):
        self.websocket_first_response(True,complete=True)
        self.assertEqual(self.store.counts['upstream_wait_timeout'],0)
        self.assertEqual(self.store.counts['websocket_idle_closed'],1)

    def test_server_pongs_do_not_extend_model_response_wait(self):
        self.websocket_first_response(True,client_pings=True,server_pongs=True)
        self.assertEqual(self.store.counts['upstream_wait_timeout'],1)

    def test_bad_client_message_does_not_disable_next_request_detection(self):
        self.websocket_first_response(True,invalid_prefix=True)
        self.assertEqual(self.store.counts['websocket_request_parse_error'],1)
        self.assertEqual(self.store.counts['upstream_wait_timeout'],1)

    def websocket_stream_progress(self,fragmented):
        created=b'{"type":"response.created","response":{"id":"progress-r","model":"A"}}'
        delta=b'{"type":"response.output_text.delta","item_id":"item-r","delta":"synthetic"}'
        completed=b'{"type":"response.completed","response":{"id":"progress-r","model":"A"}}'
        frames=[bytes([0x81,len(created)])+created]
        pieces=[delta[i:i+15] for i in range(0,len(delta),15)] if fragmented else [delta]*5
        for i,piece in enumerate(pieces):
            opcode=1 if i==0 or not fragmented else 0
            final=not fragmented or i==len(pieces)-1
            frames.append(bytes([opcode|(0x80 if final else 0),len(piece)])+piece)
        frames.append(bytes([0x81,len(completed)])+completed)
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
                for i,frame in enumerate(frames):
                    if 0<i<len(frames)-1:time.sleep(.08)
                    try:self.connection.sendall(frame)
                    except (BrokenPipeError,ConnectionResetError):self.server.client_closed=True;return
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        real_select=select.select;real_clock=time.monotonic
        def scaled(read,write,error,timeout):return real_select(read,write,error,timeout/1000 if timeout>=1 else timeout)
        with patch('model_monitor.collector.select.select',scaled),patch('model_monitor.collector.time.monotonic',lambda:real_clock()*1000):
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=3) as client:
                client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n'%proxy.server_port).encode('ascii'))
                headers=b''
                while not headers.endswith(b'\r\n\r\n'):headers+=client.recv(1)
                received=b''
                while True:
                    chunk=client.recv(4096)
                    if not chunk:break
                    received+=chunk
        self.assertEqual(received,b''.join(frames))
        self.assertEqual(self.store.counts['upstream_wait_timeout'],0)
        records=[json.loads(line) for line in (self.path/'upstream.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(records),1)
        self.assertTrue(records[0]['completed'] and records[0]['capture_complete'])

    def test_model_deltas_without_response_id_keep_wait_budget_alive(self):
        self.websocket_stream_progress(False)

    def test_fragmented_model_message_keeps_wait_budget_alive(self):
        self.websocket_stream_progress(True)


    def test_http_budget_expiry_has_explicit_wait_diagnostic(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                time.sleep(.36)
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        real=http.client.HTTPConnection
        class ScaledConnection(real):
            def connect(self):
                super().connect();raw=self.sock
                class DeadlineSocket:
                    def __getattr__(self,name):return getattr(raw,name)
                    def settimeout(self,value):raw.settimeout(value/1000 if value>=100 else value)
                self.sock=DeadlineSocket()
        with patch('model_monitor.collector.http.client.HTTPConnection',ScaledConnection):
            proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
            client=real('127.0.0.1',proxy.server_port,timeout=3);self.addCleanup(client.close)
            client.request('POST','/backend-api/codex/responses',body=b'{}',headers={'thread-id':'t'})
            response=client.getresponse();self.assertEqual(response.status,502);response.read()
        self.assertEqual(self.store.counts['upstream_wait_timeout'],1)
        event=next(v for v in self.store.recent if v['kind']=='upstream_wait_timeout')
        self.assertEqual(event['wait_limit_seconds'],300)
        self.assertEqual(event['thread_id'],'t')
        self.assertEqual(event['stage'],'upstream_headers')

    def test_websocket_active_response_timeout_is_not_idle_housekeeping(self):
        payload=b'{"type":"response.created","response":{"id":"waiting-r","model":"A"}}'
        frame=bytes([0x81,len(payload)])+payload
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n'+frame)
                time.sleep(.36);self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        real_select=select.select
        def scaled(read,write,error,timeout):return real_select(read,write,error,timeout/1000 if timeout>=100 else timeout)
        with patch('model_monitor.collector.select.select',scaled):
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=3) as client:
                client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nthread-id: t\r\n\r\n'%proxy.server_port).encode('ascii'))
                while client.recv(4096):pass
        self.assertEqual(self.store.counts['upstream_wait_timeout'],1)
        self.assertEqual(self.store.counts['websocket_error'],1)
        self.assertEqual(self.store.counts['websocket_idle_closed'],0)


class WaitWarning(unittest.TestCase):
    setUp=monitor_fixtures.ModelAcceptance.setUp
    turn=monitor_fixtures.ModelAcceptance.turn
    call=monitor_fixtures.ModelAcceptance.call

    def test_wait_timeout_is_visible_without_changing_model_result(self):
        self.turn();self.call()
        capture=self.root/'capture';capture.mkdir()
        value={'pid':os.getpid(),'counts':{'http_error':1,'upstream_wait_timeout':1},
               'current_counts':{'http_error':1,'upstream_wait_timeout':1}}
        (capture/'collector-status.json').write_text(json.dumps(value),encoding='utf-8')
        data=Scanner(self.db,self.traces,capture).scan()
        self.assertTrue(any('300' in alert and '等待超时' in alert for alert in data['alerts']))
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'一致')

    def test_wait_timeout_history_remains_visible_after_restart(self):
        self.turn();self.call()
        capture=self.root/'capture';capture.mkdir()
        value={'pid':os.getpid(),'counts':{'http_error':1,'upstream_wait_timeout':2},'current_counts':{'started':1}}
        (capture/'collector-status.json').write_text(json.dumps(value),encoding='utf-8')
        data=Scanner(self.db,self.traces,capture).scan()
        self.assertTrue(any('等待超时' in alert and '历史累计 2' in alert and '本次启动 0' in alert for alert in data['alerts']))
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'一致')
