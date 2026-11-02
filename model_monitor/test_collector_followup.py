import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import select
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from model_monitor.collector import Store, Observer, make_server


class CollectorFollowup(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent/'data')
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.store = Store(self.path)

    def serve(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def tls_upstream(self, fail_connections, websocket, close_after_request=False):
        config = self.path/'openssl.cnf'
        config.write_text('[req]\ndistinguished_name=dn\n[dn]\n[v3]\nsubjectAltName=IP:127.0.0.1\n', encoding='utf-8')
        cert, key = self.path/'cert.pem', self.path/'key.pem'
        subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-config', str(config), '-extensions', 'v3',
                        '-keyout', str(key), '-out', str(cert)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        trusted = ssl.create_default_context(cafile=str(cert))
        self.assertTrue(trusted.check_hostname)
        self.assertEqual(trusted.verify_mode, ssl.CERT_REQUIRED)
        attempts, requests = [], []
        release = threading.Event()
        self.addCleanup(release.set)
        payload = b'{"type":"response.completed","response":{"id":"followup-r","model":"A"}}'

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def handle_response(self):
                requests.append(self.command)
                if close_after_request:
                    self.close_connection = True
                    return
                if websocket:
                    self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n' + bytes([0x81, len(payload)]) + payload)
                    release.wait(3)
                    self.close_connection = True
                else:
                    self.rfile.read(int(self.headers['Content-Length']))
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            do_GET = handle_response
            do_POST = handle_response

        class FlakyTLS(ThreadingHTTPServer):
            def get_request(self):
                raw, address = super().get_request()
                attempts.append(True)
                if len(attempts) <= fail_connections:
                    raw.close()
                    raise OSError('synthetic pre-handshake close')
                try:
                    return context.wrap_socket(raw, server_side=True), address
                except OSError:
                    raw.close()
                    raise

        upstream = self.serve(FlakyTLS(('127.0.0.1', 0), Upstream))
        return upstream, trusted, attempts, requests, release, payload

    def test_http_two_handshake_closes_then_one_request(self):
        upstream, trusted, attempts, requests, release, payload = self.tls_upstream(2, False)
        with patch('model_monitor.collector.ssl.create_default_context', return_value=trusted):
            proxy = self.serve(make_server(self.store, 0, '127.0.0.1', upstream.server_port, True))
            client = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
            self.addCleanup(client.close)
            client.request('POST', '/backend-api/codex/responses', body=b'unique', headers={'thread-id':'t'})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), payload)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(requests, ['POST'])
        self.assertEqual(self.store.counts['http_connect_retry'], 2)
        self.assertEqual(self.store.counts['http_connect_recovered'], 1)
        self.assertEqual(self.store.counts['http_error'], 0)

    def test_websocket_handshake_retry_sends_one_upgrade(self):
        upstream, trusted, attempts, requests, release, payload = self.tls_upstream(1, True)
        with patch('model_monitor.collector.ssl.create_default_context', return_value=trusted), \
             patch('model_monitor.collector.ssl._create_default_https_context', return_value=trusted):
            proxy = self.serve(make_server(self.store, 0, '127.0.0.1', upstream.server_port, True))
            with socket.create_connection(('127.0.0.1', proxy.server_port), timeout=5) as client:
                client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nthread-id: t\r\n\r\n' % proxy.server_port).encode('ascii'))
                headers = b''
                while not headers.endswith(b'\r\n\r\n'):
                    chunk = client.recv(1)
                    self.assertTrue(chunk)
                    headers += chunk
                self.assertTrue(headers.startswith(b'HTTP/1.1 101'), headers.split(b'\r\n')[0])
                self.assertEqual(client.recv(len(payload)+2), bytes([0x81,len(payload)])+payload)
            release.set()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(requests, ['GET'])
        self.assertEqual(self.store.counts['websocket_connect_retry'], 1)
        self.assertEqual(self.store.counts['websocket_connect_recovered'], 1)
        self.assertEqual(self.store.counts['websocket_error'], 0)

    def test_websocket_upgrade_sent_is_never_replayed(self):
        upstream, trusted, attempts, requests, release, payload = self.tls_upstream(0, True, True)
        with patch('model_monitor.collector.ssl.create_default_context', return_value=trusted), \
             patch('model_monitor.collector.ssl._create_default_https_context', return_value=trusted):
            proxy = self.serve(make_server(self.store, 0, '127.0.0.1', upstream.server_port, True))
            client = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
            self.addCleanup(client.close)
            client.request('GET', '/backend-api/codex/responses', headers={'Upgrade':'websocket'})
            response = client.getresponse()
            self.assertEqual(response.status, 502)
            response.read()
        self.assertEqual(requests, ['GET'])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.store.counts['websocket_connect_retry'], 0)
        self.assertEqual(self.store.counts['websocket_error'], 1)

    def test_terminal_outcome_explains_pending_without_certifying_it(self):
        observer = Observer(self.store, 't', 'websocket')
        observer.event({'type':'response.incomplete', 'response':{
            'id':'incomplete-r', 'model':'A', 'incomplete_details':{'reason':'max_output_tokens'}, 'output':'PRIVATE'}})
        observer.event({'type':'response.failed', 'response':{
            'id':'failed-r', 'model':'A', 'error':{'code':'server_error','message':'PRIVATE'}}})
        records = [json.loads(line) for line in (self.path/'upstream.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['terminal_type'] for r in records], ['response.incomplete','response.failed'])
        self.assertEqual([r['terminal_reason'] for r in records], ['max_output_tokens','server_error'])
        self.assertTrue(all(r['capture_complete'] and not r['completed'] for r in records))
        self.assertNotIn('PRIVATE', json.dumps(records))

    def test_untrusted_certificate_is_not_retried_or_accepted(self):
        upstream, trusted, attempts, requests, release, payload = self.tls_upstream(0, False)
        proxy = self.serve(make_server(self.store, 0, '127.0.0.1', upstream.server_port, True))
        client = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
        self.addCleanup(client.close)
        client.request('POST', '/backend-api/codex/responses', body=b'unique')
        response = client.getresponse()
        self.assertEqual(response.status, 502)
        response.read()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(requests, [])
        self.assertEqual(self.store.counts['http_connect_retry'], 0)
        self.assertEqual(self.store.counts['http_error'], 1)

    def test_fragmented_http_request_is_forwarded_in_full_once(self):
        body = b'unique-fragmented-request-' * 4096
        received, timeouts = [], []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.connection.settimeout(.5)
                try:
                    received.append(self.rfile.read(int(self.headers['Content-Length'])))
                except socket.timeout:
                    timeouts.append(True)
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()
        upstream = self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        proxy = self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=3) as client:
            headers = 'POST /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: %d\r\n\r\n' % (proxy.server_port,len(body))
            client.sendall(headers.encode('ascii')+body[:17])
            time.sleep(.1)
            client.sendall(body[17:])
            response = http.client.HTTPResponse(client)
            response.begin()
            self.assertEqual(response.status,200)
            response.read()
        self.assertEqual(received,[body])
        self.assertEqual(timeouts,[])
        self.assertEqual(self.store.counts['http_error'],0)

    def test_incomplete_client_body_never_reaches_upstream(self):
        requests = []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                requests.append(True)
                self.connection.settimeout(.2)
                try:self.rfile.read(int(self.headers['Content-Length']))
                except socket.timeout:
                    self.close_connection=True
                    return
                self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
        upstream = self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        proxy = self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=3) as client:
            client.sendall(('POST /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 200\r\n\r\npartial' % proxy.server_port).encode('ascii'))
            client.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(client);response.begin()
            self.assertEqual(response.status,400)
            response.read()
        self.assertEqual(requests,[])
        self.assertEqual(self.store.counts['http_request_incomplete'],1)

    def test_sse_without_content_type_is_captured_from_actual_events(self):
        wire = b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"missing-header-r","model":"A"}}\n\n'
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.send_header('Content-Length',str(len(wire)));self.end_headers()
                for offset in range(0,len(wire),3):
                    self.wfile.write(wire[offset:offset+3]);self.wfile.flush()
        upstream = self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        proxy = self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        client = http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=3)
        self.addCleanup(client.close)
        client.request('POST','/backend-api/codex/responses',body=b'{}',headers={'thread-id':'t'})
        response=client.getresponse();self.assertEqual(response.read(),wire)
        records=[json.loads(line) for line in (self.path/'upstream.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[0]['upstream_model'],'A')
        self.assertTrue(records[0]['capture_complete'] and records[0]['completed'])
        self.assertEqual(self.store.counts['http_content_type_inferred'],1)
        self.assertEqual(self.store.counts['http_content_type_error'],0)

    def test_missing_content_type_does_not_accept_html_or_broken_events(self):
        from model_monitor.collector import HTTPObservation
        html=HTTPObservation(Observer(self.store,'t','http'),'','')
        html.feed(b'<html>PRIVATE</html>');html.finish()
        invalid=HTTPObservation(Observer(self.store,'t','http'),'','')
        invalid.feed(b'data: {broken}\n\n');invalid.finish()
        self.assertEqual(self.store.counts['http_content_type_error'],1)
        self.assertEqual(self.store.counts['bad_json'],1)
        self.assertFalse((self.path/'upstream.jsonl').exists())
        self.assertNotIn('PRIVATE',(self.path/'collector-status.json').read_text(encoding='utf-8'))

    def test_tls_uses_existing_proxy_connect_without_exposing_authorization(self):
        upstream, trusted, attempts, requests, release, payload = self.tls_upstream(0,False)
        tunnels=[]
        class Tunnel(BaseHTTPRequestHandler):
            rbufsize=0
            def log_message(self,*args): pass
            def do_CONNECT(self):
                tunnels.append((self.path,self.headers.get('Authorization')))
                with socket.create_connection(('127.0.0.1',upstream.server_port),timeout=3) as remote:
                    self.send_response(200);self.end_headers()
                    while True:
                        ready,_,_=select.select([self.connection,remote],[],[],3)
                        if not ready:return
                        for source in ready:
                            chunk=source.recv(65536)
                            if not chunk:return
                            (remote if source is self.connection else self.connection).sendall(chunk)
        tunnel=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Tunnel))
        with patch('urllib.request.getproxies',return_value={'https':'http://127.0.0.1:%d'%tunnel.server_port}), \
             patch('urllib.request.proxy_bypass',return_value=False), \
             patch('model_monitor.collector.ssl.create_default_context',return_value=trusted):
            proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,True))
            client=http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=5)
            self.addCleanup(client.close)
            client.request('POST','/backend-api/codex/responses',body=b'{}',headers={'Authorization':'PRIVATE','thread-id':'t'})
            response=client.getresponse();self.assertEqual(response.status,200);self.assertEqual(response.read(),payload)
        self.assertEqual(tunnels,[('127.0.0.1:%d'%upstream.server_port,None)])
        self.assertEqual(requests,['POST'])

    def test_diagnostic_history_survives_recent_window_and_restart(self):
        for number in range(45):
            self.store.status('http_error',transport='http',stage='tcp_connect',errno=number)
        restarted=Store(self.path)
        restarted.status('http_error',transport='http',stage='tcp_connect',errno=45)
        journal=[json.loads(line) for line in (self.path/'collector-diagnostics.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([event['errno'] for event in journal],list(range(46)))
        self.assertEqual(restarted.counts['http_error'],46)
        self.assertEqual(len(restarted.recent),40)
