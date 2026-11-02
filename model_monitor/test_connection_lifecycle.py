import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,select,socket,ssl,struct,subprocess,sys,threading,time,unittest
from unittest.mock import patch
from model_monitor import test_collector_followup as fixtures
from model_monitor.collector import make_server


class ConnectionLifecycle(unittest.TestCase):
    def setUp(self):
        fixtures.CollectorFollowup.setUp(self)
        self.server_errors=[];self.expected_server_errors=[]
        self.addCleanup(lambda:self.assertEqual(self.server_errors,self.expected_server_errors))
        self.worker_errors=[]
        self.addCleanup(self.assertEqual,self.worker_errors,[])
        hook=patch('threading.excepthook',lambda args:self.worker_errors.append(args.exc_type.__name__))
        hook.start();self.addCleanup(hook.stop)

    def serve(self,server):
        def failed(request,address):self.server_errors.append(type(sys.exc_info()[1]).__name__)
        server.handle_error=failed
        return fixtures.CollectorFollowup.serve(self,server)

    def test_websocket_upgrade_uses_provider_wait_budget(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                time.sleep(.12)
                try:self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
                except (BrokenPipeError,ConnectionResetError):self.server.client_closed=True
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream));real=http.client.HTTPConnection
        class DeadlineSocket:
            def __init__(self,sock):self.sock=sock
            def __getattr__(self,name):return getattr(self.sock,name)
            def settimeout(self,value):self.sock.settimeout(value/1000 if value is not None and value>=30 else value)
        class ScaledConnection(real):
            def connect(self):super().connect();self.sock=DeadlineSocket(self.sock)
        with patch('model_monitor.collector.http.client.HTTPConnection',ScaledConnection):
            proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2) as client:
                client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n'%proxy.server_port).encode())
                received=b''
                while not received.endswith(b'\r\n\r\n'):received+=client.recv(1)
        self.assertTrue(received.startswith(b'HTTP/1.1 101'),received)
        self.assertEqual(self.store.counts['websocket_error'],0)

    def test_client_reset_aborts_header_wait_without_replaying_request(self):
        accepted=threading.Event();closed=threading.Event();requests=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers['Content-Length'])));accepted.set()
                self.connection.settimeout(.7)
                try:
                    if not self.connection.recv(1):closed.set()
                except socket.timeout:self.server.wait_expired=True
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        client=socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2)
        client.sendall(('POST /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 2\r\nthread-id: t\r\n\r\n{}'%proxy.server_port).encode())
        self.assertTrue(accepted.wait(1));client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));client.close()
        self.assertTrue(closed.wait(.4),'collector kept waiting after a proven client RST')
        self.assertEqual(requests,[b'{}'])
        deadline=time.monotonic()+.3
        while not self.store.counts['http_client_disconnected'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.store.counts['http_client_disconnected'],1)
        self.assertEqual(self.store.counts['upstream_wait_timeout'],0)

    def test_http_half_close_still_receives_response(self):
        wire=b'data: {"type":"response.completed","response":{"id":"half-close","model":"A"}}\n\n'
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));time.sleep(.08)
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream));proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2) as client:
            client.sendall(('POST /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 2\r\nthread-id: t\r\n\r\n{}'%proxy.server_port).encode());client.shutdown(socket.SHUT_WR)
            response=http.client.HTTPResponse(client);response.begin();self.assertEqual(response.status,200);self.assertEqual(response.read(),wire)
        self.assertEqual(self.store.counts['http_client_disconnected'],0)

    def test_upgrade_eof_keeps_fault_and_records_metadata_without_body(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.connection.sendall(b'HTTP/1.1 101 PRIVATE_HANDSHAKE_SECRET');self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream));proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2) as client:
            client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n'%proxy.server_port).encode())
            response=http.client.HTTPResponse(client);response.begin();self.assertEqual(response.status,502)
        event=next(e for e in self.store.recent if e['kind']=='websocket_error')
        self.assertEqual(event['reason'],'upgrade_eof');self.assertEqual(event['payload_bytes'],len(b'HTTP/1.1 101 PRIVATE_HANDSHAKE_SECRET'))
        self.assertNotIn('PRIVATE',json.dumps(self.store.recent))
        self.assertEqual(self.store.counts['websocket_error'],1)

    def test_websocket_client_reset_stops_upgrade_wait(self):
        accepted=threading.Event();closed=threading.Event();requests=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                requests.append(self.path);accepted.set();self.connection.settimeout(.7)
                try:
                    if not self.connection.recv(1):closed.set()
                except socket.timeout:self.server.wait_expired=True
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream));proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        client=socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2)
        client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nthread-id: t\r\n\r\n'%proxy.server_port).encode())
        self.assertTrue(accepted.wait(1));client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));client.close()
        self.assertTrue(closed.wait(.4));self.assertEqual(requests,['/backend-api/codex/responses'])
        deadline=time.monotonic()+.3
        while not self.store.counts['websocket_client_disconnected'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.store.counts['websocket_client_disconnected'],1)
        self.assertEqual(self.store.counts['upstream_wait_timeout'],0)

    def test_close_frame_code_is_retained_but_reason_text_is_not(self):
        from model_monitor.collector import Frames,Observer
        codes=[];body=struct.pack('!H',1000)+b'PRIVATE_CLOSE_REASON'
        Frames(Observer(self.store,'t','websocket'),on_close=codes.append).feed(bytes([0x88,len(body)])+body)
        self.assertEqual(codes,[1000]);self.assertNotIn('PRIVATE',json.dumps(self.store.recent))

    def test_reset_during_http_body_keeps_incomplete_evidence(self):
        accepted=threading.Event();closed=threading.Event()
        first=b'data: {"type":"response.created","response":{"id":"aborted-body","model":"A"}}\n\n'
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers();self.wfile.write(first);self.wfile.flush();accepted.set()
                self.connection.settimeout(.7)
                try:
                    if not self.connection.recv(1):closed.set()
                except socket.timeout:self.server.wait_expired=True
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream));proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        client=socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2)
        client.sendall(('POST /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 2\r\nthread-id: t\r\n\r\n{}'%proxy.server_port).encode());self.assertTrue(accepted.wait(1))
        headers=b''
        while not headers.endswith(b'\r\n\r\n'):headers+=client.recv(1)
        received=b''
        while len(received)<len(first):received+=client.recv(len(first)-len(received))
        client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));client.close()
        self.assertTrue(closed.wait(.4))
        deadline=time.monotonic()+.3
        while not self.store.counts['incomplete_capture'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.store.counts['incomplete_capture'],1);self.assertEqual(self.store.counts['http_forwarded'],0)
        self.assertFalse((self.path/'upstream.jsonl').exists())

    def test_tls_client_reset_unblocks_header_wait(self):
        accepted=threading.Event();closed=threading.Event()
        config=self.path/'openssl.cnf';config.write_text('[req]\ndistinguished_name=dn\n[dn]\n[v3]\nsubjectAltName=IP:127.0.0.1\n',encoding='utf-8')
        cert=self.path/'cert.pem';key=self.path/'key.pem'
        subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost','-config',str(config),'-extensions','v3','-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_context.load_cert_chain(str(cert),str(key));client_context=ssl.create_default_context(cafile=str(cert))
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));accepted.set();self.connection.settimeout(.7)
                try:
                    if not self.connection.recv(1):closed.set()
                except socket.timeout:self.server.wait_expired=True
                self.close_connection=True
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);upstream.socket=server_context.wrap_socket(upstream.socket,server_side=True);self.serve(upstream)
        with patch('model_monitor.collector.ssl.create_default_context',return_value=client_context):
            proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,True))
            client=socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2)
            client.sendall(('POST /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 2\r\nthread-id: t\r\n\r\n{}'%proxy.server_port).encode())
            self.assertTrue(accepted.wait(1));client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));client.close()
            self.assertTrue(closed.wait(.4),'TLS upstream was left waiting after client RST')

    def test_upgrade_budget_expiry_still_reports_real_timeout(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):time.sleep(.4);self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream));real=http.client.HTTPConnection
        class DeadlineSocket:
            def __init__(self,sock):self.sock=sock
            def __getattr__(self,name):return getattr(self.sock,name)
            def settimeout(self,value):self.sock.settimeout(value/1000 if value is not None and value>=30 else value)
        class ScaledConnection(real):
            def connect(self):super().connect();self.sock=DeadlineSocket(self.sock)
        with patch('model_monitor.collector.http.client.HTTPConnection',ScaledConnection):
            proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2) as client:
                client.sendall(('GET /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nthread-id: t\r\n\r\n'%proxy.server_port).encode())
                response=http.client.HTTPResponse(client);response.begin();self.assertEqual(response.status,502)
        self.assertEqual(self.store.counts['upstream_wait_timeout'],1)
        event=next(e for e in self.store.recent if e['kind']=='upstream_wait_timeout')
        self.assertEqual(event['stage'],'upgrade_read');self.assertEqual(event['wait_limit_seconds'],300)

    def test_buffered_pipeline_bytes_do_not_disable_later_reset_detection(self):
        accepted=threading.Event();closed=threading.Event();requests=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers['Content-Length'])));accepted.set();self.connection.settimeout(.9)
                try:
                    if not self.connection.recv(1):closed.set()
                except socket.timeout:self.server.wait_expired=True
                self.close_connection=True
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream));proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
        client=socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2)
        client.sendall(('POST /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 2\r\n\r\n{}'%proxy.server_port).encode());self.assertTrue(accepted.wait(1))
        client.sendall(b'GET /backend-api/another-request HTTP/1.1\r\n');time.sleep(.12)
        client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));client.close()
        self.assertTrue(closed.wait(.4),'buffered client bytes disabled later RST detection')
        self.assertEqual(requests,[b'{}'])

    def test_watch_diagnostic_write_failure_unblocks_owner_and_is_raised(self):
        from model_monitor.collector import ClientResetWatch
        local,peer=socket.socketpair();upstream,server=socket.socketpair()
        for connection in (local,peer,upstream,server):self.addCleanup(connection.close)
        class BadProbe:
            def fileno(self):return local.fileno()
            def getsockopt(self,*args):raise OSError('synthetic probe failure')
        peer.sendall(b'queued')
        with patch.object(self.store,'status',side_effect=OSError('synthetic audit write failure')):
            watcher=ClientResetWatch(BadProbe(),upstream,self.store,'http','t','probe-failure').start()
            server.settimeout(.4);self.assertEqual(server.recv(1),b'')
            with self.assertRaisesRegex(OSError,'synthetic audit write failure'):watcher.stop()
        self.assertFalse(watcher.reset)

    def owner_cleanup(self,websocket):
        from model_monitor.collector import ClientResetWatch
        closed=threading.Event();connections=[]
        class Upstream(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
            def do_GET(self):
                self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n');time.sleep(.1);self.close_connection=True
        real=http.client.HTTPConnection
        class TrackedConnection(real):
            def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);connections.append(self)
            def close(self):super().close();closed.set()
        stop=ClientResetWatch.stop
        def failed(watcher):stop(watcher);raise OSError('synthetic watcher stop failure')
        upstream=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Upstream))
        self.expected_server_errors.append('OSError')
        with patch('model_monitor.collector.http.client.HTTPConnection',TrackedConnection),patch('model_monitor.collector.ClientResetWatch.stop',failed):
            proxy=self.serve(make_server(self.store,0,'127.0.0.1',upstream.server_port,False))
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=2) as client:
                method='GET' if websocket else 'POST';extra='Upgrade: websocket\r\nConnection: Upgrade\r\n' if websocket else 'Content-Length: 2\r\n'
                client.sendall((method+' /backend-api/test HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'%proxy.server_port+extra+'\r\n'+('' if websocket else '{}')).encode())
                while client.recv(4096):pass
            deadline=time.monotonic()+.3
            while not self.server_errors and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(self.server_errors,['OSError'])
            self.assertTrue(closed.is_set(),'owner leaked upstream when watcher.stop raised')
            self.assertTrue(all(c.sock is None for c in connections))

    def test_http_owner_closes_upstream_even_when_watcher_stop_raises(self):self.owner_cleanup(False)

    def test_websocket_owner_closes_upstream_even_when_watcher_stop_raises(self):self.owner_cleanup(True)
