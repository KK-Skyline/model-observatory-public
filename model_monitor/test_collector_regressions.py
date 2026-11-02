import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import gzip
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from model_monitor.collector import Store, Observer, HTTPObservation, make_server
from model_monitor.app import Scanner
from model_monitor import test_monitor as fixtures


class CollectorRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent/'data')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)

    def serve(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def proxy(self, upstream):
        server = make_server(self.store, 0, '127.0.0.1', upstream.server_port, False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_restart_keeps_all_four_warning_counts(self):
        original = {'http_error':16, 'websocket_error':28, 'bad_json':46, 'websocket_upgrade_rejected':42}
        (self.directory/'collector-status.json').write_text(json.dumps({'pid':123, 'updated_at':1, 'counts':original}), encoding='utf-8')
        restarted = Store(self.directory)
        restarted.status('started')
        status = json.loads((self.directory/'collector-status.json').read_text(encoding='utf-8'))
        for name, count in original.items():
            self.assertEqual(status['counts'].get(name), count, name)

    def test_rejected_websocket_preserves_error_body(self):
        body = b'{"error":{"code":"synthetic_upgrade_denied"}}'
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(403)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        proxy = self.proxy(self.serve(Upstream))
        with socket.create_connection(('127.0.0.1', proxy.server_port), timeout=2) as client:
            request = ('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: synthetic\r\nSec-WebSocket-Version: 13\r\n\r\n' % proxy.server_port)
            client.sendall(request.encode('ascii'))
            received = b''
            while True:
                chunk = client.recv(4096)
                if not chunk: break
                received += chunk
        self.assertEqual(received.split(b'\r\n\r\n', 1)[1], body)
        self.assertEqual(self.store.counts['websocket_upgrade_rejected'], 1)

    def test_connect_failure_returns_explicit_http_502(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
        upstream = self.serve(Upstream)
        proxy = self.proxy(upstream)
        upstream.shutdown()
        upstream.server_close()
        connection = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=2)
        self.addCleanup(connection.close)
        connection.request('POST', '/backend-api/codex/responses', body=b'{}')
        response = connection.getresponse()
        self.assertEqual(response.status, 502)
        self.assertEqual(self.store.counts['http_error'], 1)

    def test_gzip_json_observation_keeps_wire_bytes_and_model(self):
        raw = b'{"type":"response.completed","response":{"id":"compressed-r","model":"A"}}'
        wire = gzip.compress(raw)
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Content-Length', str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
        proxy = self.proxy(self.serve(Upstream))
        connection = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=2)
        self.addCleanup(connection.close)
        connection.request('POST', '/backend-api/codex/responses', body=b'{}', headers={'thread-id':'t'})
        response = connection.getresponse()
        self.assertEqual(response.read(), wire)
        self.assertEqual(self.store.counts['bad_json'], 0)
        records = [json.loads(line) for line in (self.directory/'upstream.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[0]['upstream_model'], 'A')

    def test_tls_buffered_frame_arrives_without_another_network_packet(self):
        config = self.directory/'openssl.cnf'
        config.write_text('[req]\ndistinguished_name=dn\n[dn]\n[v3]\nsubjectAltName=IP:127.0.0.1\n', encoding='utf-8')
        cert = self.directory/'cert.pem'
        key = self.directory/'key.pem'
        subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-config', str(config), '-extensions', 'v3',
                        '-keyout', str(key), '-out', str(cert)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert), str(key))
        client_context = ssl.create_default_context(cafile=str(cert))
        release = threading.Event()
        self.addCleanup(release.set)
        message = b'{"type":"response.completed","response":{"id":"buffered-r","model":"A"}}'
        frame = bytes([0x81, len(message)]) + message
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                # One TLS record contains both HTTP headers and the first WebSocket frame.
                # 同一个 TLS record 内同时包含 HTTP 响应头与第一个 WebSocket 帧。
                self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n' + frame)
                release.wait(2)
                self.close_connection = True
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        upstream.socket = server_context.wrap_socket(upstream.socket, server_side=True)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        with patch('model_monitor.collector.ssl.create_default_context', return_value=client_context):
            proxy = make_server(self.store, 0, '127.0.0.1', upstream.server_port, True)
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            self.addCleanup(proxy.server_close)
            self.addCleanup(proxy.shutdown)
            with socket.create_connection(('127.0.0.1', proxy.server_port), timeout=.75) as client:
                client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n' % proxy.server_port).encode('ascii'))
                handshake = b''
                while not handshake.endswith(b'\r\n\r\n'): handshake += client.recv(1)
                self.assertEqual(client.recv(len(frame)), frame)
            release.set()

    def test_real_bad_json_remains_visible_without_body_or_credentials(self):
        observer = Observer(self.store, 'thread-test', 'http')
        observer.json_event(b'{"PRIVATE_BODY":"PRIVATE_CREDENTIAL",broken}')
        status = json.loads((self.directory/'collector-status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['counts']['bad_json'], 1)
        self.assertEqual(status['recent_diagnostics'][-1]['stage'], 'event_decode')
        self.assertEqual(status['recent_diagnostics'][-1]['exception'], 'JSONDecodeError')
        self.assertNotIn('PRIVATE', json.dumps(status))

    def test_invalid_status_is_not_silently_replaced(self):
        path = self.directory/'collector-status.json'
        content = '{"counts":{"bad_json":"invalid"}}'
        path.write_text(content, encoding='utf-8')
        with self.assertRaises(ValueError): Store(self.directory)
        self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_non_json_and_unsupported_encoding_have_explicit_diagnostics(self):
        observation = HTTPObservation(Observer(self.store, 't', 'http'), 'text/html', 'identity')
        observation.feed(b'<html>PRIVATE_CHALLENGE</html>')
        observation.finish()
        self.assertEqual(self.store.counts['http_content_type_error'], 1)
        self.assertEqual(self.store.counts['bad_json'], 0)
        observation = HTTPObservation(Observer(self.store, 't', 'http'), 'application/json', 'br')
        observation.feed(b'PRIVATE_COMPRESSED_BYTES')
        observation.finish()
        self.assertEqual(self.store.counts['http_encoding_error'], 1)
        self.assertFalse((self.directory/'upstream.jsonl').exists())
        self.assertNotIn('PRIVATE', (self.directory/'collector-status.json').read_text(encoding='utf-8'))

    def test_deflate_sse_is_observed_and_truncated_compression_is_reported(self):
        import zlib
        raw = b'data: {"type":"response.completed","response":{"id":"deflate-r","model":"A"}}\n\n'
        observation = HTTPObservation(Observer(self.store, 't', 'http'), 'text/event-stream', 'deflate')
        data = zlib.compress(raw)
        for index in range(0, len(data), 3): observation.feed(data[index:index+3])
        observation.finish()
        record = json.loads((self.directory/'upstream.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(record['upstream_model'], 'A')
        observation = HTTPObservation(Observer(self.store, 't', 'http'), 'application/json', 'gzip')
        observation.feed(gzip.compress(b'{}')[:-4])
        observation.finish()
        self.assertEqual(self.store.counts['http_encoding_error'], 1)

    def test_incomplete_compressed_sse_cannot_certify_terminal_event(self):
        raw = b'data: {"type":"response.completed","response":{"id":"truncated-sse","model":"A"}}\n\n'
        observation = HTTPObservation(Observer(self.store, 't', 'http'), 'text/event-stream', 'gzip')
        observation.feed(gzip.compress(raw)[:-4])
        observation.finish()
        record = json.loads((self.directory/'upstream.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertFalse(record['capture_complete'])
        self.assertEqual(self.store.counts['http_encoding_error'], 1)

    def test_closed_client_is_counted_separately_from_upstream_failure(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body = b'synthetic response'
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
        proxy = self.proxy(self.serve(Upstream))
        handler = proxy.RequestHandlerClass
        setup = handler.setup
        class ClosedBodyWriter:
            def __init__(self,stream): self.stream=stream
            def __getattr__(self,name): return getattr(self.stream,name)
            def write(self,data):
                if data.startswith(b'HTTP/'): return self.stream.write(data)
                raise BrokenPipeError(32,'synthetic closed client')
        def setup_closed(instance):
            setup(instance);instance.wfile=ClosedBodyWriter(instance.wfile)
        handler.setup=setup_closed
        connection = http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=2)
        self.addCleanup(connection.close)
        connection.request('POST','/backend-api/status',body=b'{}')
        response=connection.getresponse();response.read()
        self.assertEqual(self.store.counts['http_client_disconnected'],1)
        self.assertEqual(self.store.counts['http_error'],0)
        status=json.loads((self.directory/'collector-status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['recent_diagnostics'][-1]['stage'],'client_write')

    def test_connection_retry_happens_before_exactly_one_request_send(self):
        real_connection=http.client.HTTPConnection
        attempts=[];sent=[];received=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
        proxy=self.proxy(self.serve(Upstream))
        class FlakyConnection(real_connection):
            def connect(self):
                attempts.append(True)
                if len(attempts)==1: raise ssl.SSLEOFError(8,'synthetic TLS handshake EOF')
                super().connect()
            def request(self,*args,**kwargs):
                sent.append(True);return super().request(*args,**kwargs)
        with patch('model_monitor.collector.http.client.HTTPConnection',FlakyConnection):
            connection=real_connection('127.0.0.1',proxy.server_port,timeout=2)
            self.addCleanup(connection.close)
            connection.request('POST','/backend-api/status',body=b'unique synthetic request')
            response=connection.getresponse();response.read()
        self.assertEqual(response.status,200)
        self.assertEqual(len(attempts),2)
        self.assertEqual(len(sent),1)
        self.assertEqual(received,[b'unique synthetic request'])
        self.assertEqual(self.store.counts['http_connect_retry'],1)
        self.assertEqual(self.store.counts['http_connect_recovered'],1)

    def test_response_failure_after_send_is_never_replayed(self):
        real_connection=http.client.HTTPConnection
        sent=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
        proxy=self.proxy(self.serve(Upstream))
        class FailingResponse(real_connection):
            def request(self,*args,**kwargs):
                sent.append(True);return super().request(*args,**kwargs)
            def getresponse(self): raise ssl.SSLEOFError(8,'synthetic EOF after request send')
        with patch('model_monitor.collector.http.client.HTTPConnection',FailingResponse):
            connection=real_connection('127.0.0.1',proxy.server_port,timeout=2)
            self.addCleanup(connection.close)
            connection.request('POST','/backend-api/status',body=b'unique synthetic request')
            response=connection.getresponse();response.read()
        self.assertEqual(response.status,502)
        self.assertEqual(len(sent),1)
        self.assertEqual(self.store.counts['http_connect_retry'],0)
        status=json.loads((self.directory/'collector-status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['recent_diagnostics'][-1]['stage'],'upstream_headers')


class CollectorDiagnosticPresentation(unittest.TestCase):
    setUp = fixtures.ModelAcceptance.setUp
    turn = fixtures.ModelAcceptance.turn
    call = fixtures.ModelAcceptance.call

    def test_history_stays_visible_and_current_failures_are_separate(self):
        import os
        self.turn();self.call()
        capture = self.root/'capture';capture.mkdir()
        status = {'pid':os.getpid(), 'counts':{'bad_json':46,'http_error':17,'http_client_disconnected':2}, 'current_counts':{'started':1,'http_client_disconnected':2}}
        (capture/'collector-status.json').write_text(json.dumps(status),encoding='utf-8')
        data = Scanner(self.db,self.traces,capture).scan()
        alert = ' '.join(data['alerts'])
        self.assertIn('历史累计',alert)
        self.assertIn('46',alert)
        self.assertIn('17',alert)
        self.assertIn('本次启动新增异常：0',alert)
        self.assertIn('客户端接收端断连累计：2',alert)
        self.assertEqual(next(t for t in data['threads'] if t['id']=='t')['records'][0]['upstream_status'],'一致')

