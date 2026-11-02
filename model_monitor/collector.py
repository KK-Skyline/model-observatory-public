"""Loopback reverse observer. Persists only whitelisted response metadata."""
import argparse
import collections
import errno
import http.client
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import socket
import ssl
import struct
import secrets
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import zlib

BASE=Path(__file__).resolve().parent
ID_CHARS=set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.')
# Match Codex's 300-second provider idle budget; capacity queues may exceed 150 seconds.
# 对齐 Codex 的 300 秒流空闲预算，容量排队可能超过 150 秒。
STREAM_IDLE_TIMEOUT_SECONDS=300
def identifier(v):
    return v if isinstance(v,str) and 0<len(v)<=200 and set(v)<=ID_CHARS else None


class Store:
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.RLock();self.counts=collections.Counter()
        self.current_counts=collections.Counter();self.recent=[];self.started_at=int(time.time()*1000)
        self.legacy_counts={}
        path=self.directory/'collector-status.json'
        if path.exists():
            previous=json.loads(path.read_text(encoding='utf-8'))
            counts=previous.get('counts')
            if not isinstance(counts,dict) or any(not isinstance(k,str) or type(v) is not int or v<0 for k,v in counts.items()):
                raise ValueError('collector-status.json counts invalid; refusing to erase history')
            self.counts.update(counts)
            self.legacy_counts=previous.get('legacy_counts',dict(counts))
            self.recent=previous.get('recent_diagnostics',[])
            if not isinstance(self.legacy_counts,dict) or not isinstance(self.recent,list):
                raise ValueError('collector-status.json diagnostics invalid')
    def status(self,kind,**context):
        allowed={'transport','stage','exception','errno','http_status','reason','payload_kind','payload_bytes','content_type','content_encoding','peer','attempt','thread_id','connection_id','route','wait_limit_seconds','close_code'}
        if set(context)-allowed:raise ValueError('unsupported diagnostic context')
        with self.lock:
            self.counts[kind]+=1
            self.current_counts[kind]+=1
            now=int(time.time()*1000)
            if any(word in kind for word in ('error','bad_','rejected','invalid','incomplete','missing','too_large','disconnected','retry','recovered','timeout')) or '_connect_' in kind or kind in ('websocket_upgrade_succeeded','websocket_peer_closed','websocket_close_frame'):
                event={'at':now,'pid':os.getpid(),'kind':kind,**context}
                fd=os.open(str(self.directory/'collector-diagnostics.jsonl'),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
                with os.fdopen(fd,'w',encoding='utf-8') as f:
                    os.fchmod(f.fileno(),0o600);f.write(json.dumps(event)+'\n');f.flush();os.fsync(f.fileno())
                self.recent.append(event);self.recent=self.recent[-40:]
            p=self.directory/'collector-status.json';tmp=p.with_suffix('.tmp')
            data={'pid':os.getpid(),'updated_at':now,'counts':dict(self.counts),'schema_version':2,
                'process_started_at':self.started_at,'current_counts':dict(self.current_counts),
                'legacy_counts':self.legacy_counts,'recent_diagnostics':self.recent}
            fd=os.open(str(tmp),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                os.fchmod(f.fileno(),0o600);json.dump(data,f);f.flush();os.fsync(f.fileno())
            os.replace(str(tmp),str(p))
    def write(self,data):
        with self.lock:
            fd=os.open(str(self.directory/'upstream.jsonl'),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                os.fchmod(f.fileno(),0o600);f.write(json.dumps(data,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
            self.status('responses_recorded')


class Observer:
    def __init__(self,store,thread_id,transport,headers=None,on_event=None):
        self.store=store;self.thread_id=identifier(thread_id);self.transport=transport
        self.headers=headers or {};self.responses={};self.buffer=b'';self.corrupt=False
        self.on_event=on_event
        self.defer_writes=False;self.pending_records=[]
    def event(self,value):
        if not isinstance(value,dict):self.store.status('bad_event');self.corrupt=True;return
        typ=value.get('type');response=value.get('response',{})
        if not isinstance(response,dict):response={}
        rid=identifier(response.get('id') or value.get('response_id'))
        if not rid:
            if typ=='error' and self.on_event:self.on_event(typ)
            if typ in ('response.created','response.completed','response.incomplete','response.failed'):self.store.status('missing_response_id')
            return
        if self.on_event:self.on_event(typ)
        record=self.responses.setdefault(rid,{'models':set(),'fields':set(),'request_id':None})
        sources=[]
        if response.get('model') is not None:sources.append(('response.model',response['model']))
        for prefix,h in [('response.headers',response.get('headers',{})),('event.headers',value.get('headers',{}))]+([('http_response_header',self.headers)] if self.transport=='http' else []):
            if isinstance(h,dict):
                for k,v in h.items():
                    if k.lower() in ('openai-model','x-openai-model'):sources.append((prefix+'.'+k.lower(),v))
                    if k.lower()=='x-request-id':record['request_id']=identifier(v)
        for field,model in sources:
            model=identifier(model)
            if model:record['models'].add(model);record['fields'].add(field)
            else:self.store.status('invalid_model');self.corrupt=True
        if typ in ('response.completed','response.incomplete','response.failed'):
            models=sorted(record['models'])
            detail=response.get('incomplete_details' if typ=='response.incomplete' else 'error')
            reason=detail.get('reason' if typ=='response.incomplete' else 'code') if isinstance(detail,dict) else None
            allowed_reasons={'max_output_tokens','content_filter','server_error','rate_limit_exceeded','invalid_prompt','cancelled','context_length_exceeded'}
            reason=reason if isinstance(reason,str) and reason in allowed_reasons else 'other' if reason is not None else None
            captured={'thread_id':self.thread_id,'response_id':rid,'upstream_request_id':record['request_id'],
                'upstream_model':models[0] if len(models)==1 else None,'model_declarations':models,
                'source_fields':sorted(record['fields']),'transport':self.transport,
                'capture_complete':not self.corrupt,'completed':typ=='response.completed','conflict':len(models)>1,
                'terminal_type':typ,'terminal_reason':reason}
            if self.defer_writes:self.pending_records.append(captured)
            else:self.store.write(captured)
            self.responses.pop(rid,None)
    def flush_pending(self,complete):
        for captured in self.pending_records:
            captured['capture_complete']=captured['capture_complete'] and complete
            self.store.write(captured)
        self.pending_records=[]
    def json_event(self,body):
        try:self.event(json.loads(body))
        except (ValueError,UnicodeError) as error:
            kind='gzip' if body.startswith(b'\x1f\x8b') else 'zstd' if body.startswith(b'\x28\xb5\x2f\xfd') else 'html' if body.lstrip().startswith(b'<') else 'other'
            self.store.status('bad_json',transport=self.transport,stage='event_decode',exception=type(error).__name__,payload_kind=kind,payload_bytes=len(body));self.corrupt=True
    def sse(self,chunk):
        self.buffer+=chunk
        if len(self.buffer)>8*1024*1024:
            self.store.status('event_too_large');self.corrupt=True;self.buffer=b'';return
        self.buffer=self.buffer.replace(b'\r\n',b'\n')
        while b'\n\n' in self.buffer:
            event,self.buffer=self.buffer.split(b'\n\n',1)
            data=b'\n'.join(line[5:].lstrip() for line in event.split(b'\n') if line.startswith(b'data:'))
            if data and data!=b'[DONE]':self.json_event(data)
    def finish(self):
        if self.responses or self.buffer.strip():
            self.store.status('incomplete_capture',transport=self.transport,stage='stream_eof',
                reason='response_without_terminal' if self.responses else 'partial_event',payload_bytes=len(self.buffer))


class HTTPObservation:
    """Decode a bounded observation copy; forward the original response bytes unchanged."""
    def __init__(self,observer,content_type,content_encoding):
        self.observer=observer;self.disabled=False;self.decoder=None
        self.kind=content_type.split(';',1)[0].strip().lower()
        self.encoding=content_encoding.strip().lower() or 'identity'
        self.is_sse=self.kind=='text/event-stream'
        self.detecting=not self.kind;self.prefix=b''
        if self.kind and not self.is_sse and self.kind!='application/json' and not self.kind.endswith('+json'):
            self.fail('http_content_type_error','unsupported_type')
        elif self.encoding in ('gzip','x-gzip'):self.decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
        elif self.encoding=='deflate':self.decoder=zlib.decompressobj()
        elif self.encoding!='identity':self.fail('http_encoding_error','unsupported_encoding')
        if self.decoder:self.observer.defer_writes=True
    def fail(self,kind,reason):
        self.disabled=True;self.observer.corrupt=True
        encoding=self.encoding if self.encoding in ('identity','gzip','x-gzip','deflate','br','zstd') else 'other'
        self.observer.store.status(kind,transport='http',stage='observation_decode',reason=reason,content_encoding=encoding,
            content_type='sse' if self.is_sse else 'json' if self.kind=='application/json' or self.kind.endswith('+json') else 'other')
    def feed(self,data):
        if self.disabled:return
        try:decoded=self.decoder.decompress(data,8*1024*1024+1) if self.decoder else data
        except zlib.error:self.fail('http_encoding_error','decompression_failed');return
        if len(decoded)>8*1024*1024:self.fail('event_too_large','decoded_size_limit');return
        if self.detecting:
            # Some successful Codex streams omit Content-Type; validate the actual framing.
            # 部分成功的 Codex 流缺少 Content-Type，需按实际事件格式验证。
            self.prefix+=decoded;start=self.prefix.lstrip()
            if start.startswith((b'data:',b'event:',b':')):
                self.is_sse=True;self.kind='text/event-stream'
            elif start.startswith(b'{'):self.kind='application/json'
            elif len(self.prefix)<=4096 and any(marker.startswith(start) for marker in (b'data:',b'event:',b':',b'{')):return
            else:self.fail('http_content_type_error','unrecognized_body');return
            self.detecting=False;decoded=self.prefix;self.prefix=b''
            self.observer.store.status('http_content_type_inferred',transport='http',stage='observation_decode',
                reason='missing_header',content_type='sse' if self.is_sse else 'json')
        if self.is_sse:self.observer.sse(decoded)
        else:
            self.observer.buffer+=decoded
            if len(self.observer.buffer)>8*1024*1024:self.fail('event_too_large','json_size_limit')
    def finish(self):
        if self.detecting and not self.disabled:self.fail('http_content_type_error','unrecognized_body')
        if self.disabled:self.observer.flush_pending(False);return
        if self.decoder and not self.decoder.eof:self.fail('http_encoding_error','incomplete_compressed_body')
        if not self.is_sse and not self.disabled:
            if self.observer.buffer:self.observer.json_event(self.observer.buffer);self.observer.buffer=b''
            else:self.fail('http_missing_payload','empty_body')
        # Only metadata waits for compressed-stream integrity; wire bytes remain streaming.
        # 仅元数据等待压缩流完整性验证；原始转发始终保持流式。
        self.observer.flush_pending(not self.disabled and not self.observer.corrupt)
        self.observer.finish()
    def abort(self):
        self.observer.corrupt=True;self.observer.flush_pending(False);self.observer.finish()


class RequestWait:
    """Track sent create events in memory, without using request models as evidence."""
    def __init__(self,store,thread_id,connection_id):
        self.store=store;self.thread_id=identifier(thread_id);self.connection_id=connection_id
        self.pending=0;self.since=0;self.last_response_at=0
    def json_event(self,body):
        try:
            value=json.loads(body)
            if not isinstance(value,dict):raise ValueError('invalid_client_event')
        except (ValueError,UnicodeError) as error:
            self.store.status('websocket_request_parse_error',transport='websocket',stage='client_event_decode',exception=type(error).__name__,
                thread_id=self.thread_id,connection_id=self.connection_id)
            return
        if value.get('type')=='response.create':
            if not self.pending:self.since=time.monotonic()
            self.pending+=1
    def server_event(self,typ):
        if typ in ('response.completed','response.incomplete','response.failed','error') and self.pending:
            self.pending-=1
    def progress(self):self.last_response_at=time.monotonic()


class Frames:
    def __init__(self,observer,max_bytes=32*1024*1024,on_data=None,on_close=None):
        self.observer=observer;self.buffer=b'';self.message=b'';self.max_bytes=max_bytes;self.on_data=on_data;self.on_close=on_close
    def feed(self,data):
        self.buffer+=data
        while len(self.buffer)>=2:
            a,b=self.buffer[:2];n=b&127;pos=2
            if n==126:
                if len(self.buffer)<4:return
                n=struct.unpack('!H',self.buffer[2:4])[0];pos=4
            elif n==127:
                if len(self.buffer)<10:return
                n=struct.unpack('!Q',self.buffer[2:10])[0];pos=10
            if n>self.max_bytes:raise ValueError('frame_too_large')
            masked=bool(b&128);end=pos+(4 if masked else 0)+n
            # Data fragments extend waiting; control-frame heartbeats do not.
            # 数据分片延长等待，控制帧心跳不延长等待。
            if data and (a&15) in (0,1) and self.on_data and min(len(self.buffer),end)>pos+(4 if masked else 0):self.on_data()
            if len(self.buffer)<end:return
            mask=self.buffer[pos:pos+4] if masked else None
            if masked:pos+=4
            payload=self.buffer[pos:end];self.buffer=self.buffer[end:]
            if mask:payload=bytes(x^mask[i%4] for i,x in enumerate(payload))
            if a&0x70:raise ValueError('unsupported_compression')
            opcode=a&15
            if opcode==8 and self.on_close:self.on_close(struct.unpack('!H',payload[:2])[0] if len(payload)>=2 else None)
            if opcode in (0,1):
                self.message+=payload
                if len(self.message)>self.max_bytes:raise ValueError('message_too_large')
                if a&128:self.observer.json_event(self.message);self.message=b''


class ClientResetWatch:
    """Abort an upstream wait on proven client RST; FIN alone may be a half-close."""
    def __init__(self,client,upstream,store,transport,thread_id,connection_id):
        self.client=client;self.upstream=upstream;self.store=store;self.transport=transport
        self.thread_id=identifier(thread_id);self.connection_id=connection_id
        self.reset=False;self.failure=None;self.abort_failure=None;self.stopped=threading.Event();self.guard=threading.Lock()
        self.worker=threading.Thread(target=self.run,daemon=True)
    def start(self):self.worker.start();return self
    def stop(self):
        with self.guard:self.stopped.set()
        self.worker.join()
        if self.abort_failure:raise RuntimeError('client watcher failed and upstream abort failed') from self.failure
        if self.failure:raise self.failure
    def diagnostic(self,error,stage):
        self.store.status(self.transport+'_client_probe_error',transport=self.transport,stage=stage,
            exception=type(error).__name__,errno=getattr(error,'errno',None),thread_id=self.thread_id,connection_id=self.connection_id)
    def run(self):
        try:self.observe()
        except Exception as error:
            # Propagate worker failures and unblock the owning request, never leave it waiting.
            # 后台异常须传回请求线程，并解除其等待，不能静默挂到超时。
            self.failure=error
            try:self.upstream.shutdown(socket.SHUT_RDWR)
            except OSError as abort_error:
                if abort_error.errno not in (errno.ENOTCONN,errno.EBADF):self.abort_failure=abort_error
    def observe(self):
        while not self.stopped.is_set():
            try:
                if not select.select([self.client],[],[],.05)[0]:continue
                # Peek never consumes a pipelined request or treats a valid FIN as cancellation.
                # 窥读不消耗流水线请求，也不把合法FIN半关闭当作取消。
                pending_error=self.client.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)
                if pending_error==errno.ECONNRESET:raise ConnectionResetError(pending_error,'client reset')
                if pending_error:raise OSError(pending_error,'client socket failure')
                self.client.recv(1,socket.MSG_PEEK)
                # Queued data or FIN must not disable detection of a later reset.
                # 已排队数据或FIN不能停用后续RST检测。
                self.stopped.wait(.05)
            except ConnectionResetError:
                with self.guard:
                    if self.stopped.is_set():return
                    self.reset=True
                    try:self.upstream.shutdown(socket.SHUT_RDWR)
                    except OSError as error:
                        if error.errno in (errno.ENOTCONN,errno.EBADF):
                            self.store.status(self.transport+'_client_probe_shutdown_race',transport=self.transport,stage='upstream_abort',
                                errno=error.errno,thread_id=self.thread_id,connection_id=self.connection_id)
                        else:self.diagnostic(error,'upstream_abort')
                return
            except OSError as error:
                if not self.stopped.is_set():self.diagnostic(error,'client_probe')
                return


class PrefixedReader(io.RawIOBase):
    """Let the standard HTTP parser consume an already-read handshake and its body."""
    def __init__(self,prefix,stream):self.prefix=memoryview(prefix);self.stream=stream
    def readable(self):return True
    def readinto(self,target):
        if self.prefix:
            n=min(len(target),len(self.prefix));target[:n]=self.prefix[:n];self.prefix=self.prefix[n:];return n
        return self.stream.readinto(target)
    def close(self):
        self.stream.close();super().close()


class HandshakeSocket:
    def __init__(self,prefix,connection):self.prefix=prefix;self.connection=connection
    def makefile(self,mode):return io.BufferedReader(PrefixedReader(self.prefix,self.connection.makefile(mode,buffering=0)))


def make_server(store,port=18084,upstream_host='chatgpt.com',upstream_port=443,tls=True):
    proxy=None
    if tls and not urllib.request.proxy_bypass(upstream_host):
        configured=urllib.request.getproxies().get('https')
        if configured:
            parsed=urlsplit(configured)
            if parsed.scheme!='http' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
                raise ValueError('upstream proxy must be a credential-free HTTP CONNECT endpoint')
            proxy=(parsed.hostname,parsed.port or 80)
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        rbufsize=0
        def log_message(self,*args):return
        def failure(self,code=502):
            self.send_response(code);self.send_header('Content-Length','0');self.send_header('Connection','close');self.end_headers();self.close_connection=True
        def valid(self):
            if self.headers.get('Host')!='127.0.0.1:'+str(self.server.server_port):self.failure(403);return False
            if not self.path.startswith('/backend-api/') or self.path.startswith('//'):self.failure(404);return False
            return True
        def do_GET(self):self.forward()
        def do_POST(self):self.forward()
        def connect_upstream(self,transport):
            # Retry only TCP/TLS establishment; never replay HTTP or WebSocket requests.
            # 仅重试 TCP/TLS 建连，绝不重放 HTTP 或 WebSocket 请求。
            self.connection_id=secrets.token_hex(12)
            context={'thread_id':identifier(self.headers.get('thread-id')),'connection_id':self.connection_id,
                'route':'configured_proxy' if proxy else 'direct'}
            for attempt in range(3):
                target=proxy or (upstream_host,upstream_port)
                conn=http.client.HTTPSConnection(*target,timeout=30,context=ssl.create_default_context()) if tls else http.client.HTTPConnection(*target,timeout=30)
                if proxy:conn.set_tunnel(upstream_host,upstream_port)
                self.connect_stage='upstream_connect'
                try:conn.connect()
                except OSError as error:
                    self.connect_stage='tls_handshake' if isinstance(error,ssl.SSLError) else 'tcp_connect'
                    conn.close()
                    if attempt==2 or isinstance(error,ssl.SSLCertVerificationError):raise
                    store.status(transport+'_connect_retry',transport=transport,stage=self.connect_stage,
                        exception=type(error).__name__,errno=getattr(error,'errno',None),reason='before_request',attempt=attempt+1,**context)
                    time.sleep(.25*(2**attempt))
                    continue
                store.status(transport+('_connect_recovered' if attempt else '_connect_succeeded'),transport=transport,
                    stage='upstream_connect',reason='before_request',attempt=attempt+1,**context)
                return conn
        def forward(self):
            if not self.valid():return
            if self.headers.get('Upgrade','').lower()=='websocket':self.websocket();return
            conn=None;observer=None;watch=None;sent_headers=False;stage='request_read'
            try:
                n=int(self.headers.get('Content-Length','0'))
                if n<0 or n>64*1024*1024 or self.headers.get('Transfer-Encoding'):self.failure(400);return
                # An unbuffered socket read may return only one TCP fragment.
                # 无缓冲 socket 单次读取可能只返回一个 TCP 分片。
                body=bytearray()
                while len(body)<n:
                    chunk=self.rfile.read(min(65536,n-len(body)))
                    if not chunk:
                        store.status('http_request_incomplete',transport='http',stage='request_read',reason='client_eof',payload_bytes=len(body))
                        self.failure(400);return
                    body.extend(chunk)
                body=bytes(body) if n else None
                headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','accept-encoding','proxy-authorization')}
                headers['Host']=upstream_host;headers['Accept-Encoding']='identity'
                stage='upstream_connect';conn=self.connect_upstream('http')
                conn.sock.settimeout(STREAM_IDLE_TIMEOUT_SECONDS)
                stage='upstream_send';conn.request(self.command,self.path,body=body,headers=headers)
                watch=ClientResetWatch(self.connection,conn.sock,store,'http',self.headers.get('thread-id'),self.connection_id).start()
                stage='upstream_headers';response=conn.getresponse()
                kind=response.getheader('Content-Type','');observe='/responses' in self.path and response.status==200
                observer=HTTPObservation(Observer(store,self.headers.get('thread-id'),'http',dict(response.getheaders())),kind,response.getheader('Content-Encoding','')) if observe else None
                stage='client_write';self.send_response(response.status)
                for k,v in response.getheaders():
                    if k.lower() not in ('transfer-encoding','connection','content-length'):self.send_header(k,v)
                self.send_header('Connection','close');self.end_headers();self.close_connection=True;sent_headers=True
                while True:
                    stage='upstream_read'
                    data=response.read1(65536)
                    if watch and watch.reset:raise ConnectionResetError(errno.ECONNRESET,'client reset while awaiting upstream')
                    if not data:break
                    if observer:observer.feed(data)
                    stage='client_write';self.wfile.write(data);self.wfile.flush()
                if observer:observer.finish()
                store.status('http_forwarded')
            except (OSError,ValueError,http.client.HTTPException) as error:
                if stage=='upstream_connect':stage=getattr(self,'connect_stage',stage)
                if observer:observer.abort()
                if isinstance(error,socket.timeout) and stage in ('upstream_headers','upstream_read') and not (watch and watch.reset):
                    store.status('upstream_wait_timeout',transport='http',stage=stage,reason='no_upstream_data',
                        wait_limit_seconds=STREAM_IDLE_TIMEOUT_SECONDS,thread_id=identifier(self.headers.get('thread-id')),
                        connection_id=getattr(self,'connection_id',None))
                client_closed=bool(watch and watch.reset) or stage=='client_write' and isinstance(error,(BrokenPipeError,ConnectionResetError))
                store.status('http_client_disconnected' if client_closed else 'http_error',transport='http',stage=stage,exception=type(error).__name__,errno=getattr(error,'errno',None),
                    thread_id=identifier(self.headers.get('thread-id')),connection_id=getattr(self,'connection_id',None),reason='client_reset_during_upstream_wait' if watch and watch.reset else None);self.close_connection=True
                if not sent_headers and not client_closed:
                    try:self.failure(502)
                    except OSError:store.status('http_client_disconnected',transport='http',stage='failure_reply',reason='client_closed')
            finally:
                try:
                    if watch:watch.stop()
                finally:
                    if conn:conn.close()
        def websocket(self):
            conn=None;upstream=None;observer=None;frames=None;watch=None;sent_headers=False;stage='upstream_connect'
            handshake=b'';failure_reason=None;close_seen={'client':False,'upstream':False}
            try:
                conn=self.connect_upstream('websocket');upstream=conn.sock;upstream.settimeout(STREAM_IDLE_TIMEOUT_SECONDS)
                watch=ClientResetWatch(self.connection,upstream,store,'websocket',self.headers.get('thread-id'),self.connection_id).start()
                headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','sec-websocket-extensions','proxy-authorization')}
                headers['Host']=upstream_host
                request='GET '+self.path+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers.items())+'\r\n'
                stage='upgrade_send';upstream.sendall(request.encode('latin1'))
                stage='upgrade_read'
                handshake=b''
                while not handshake.endswith(b'\r\n\r\n'):
                    chunk=upstream.recv(1)
                    if not chunk:
                        failure_reason='upgrade_eof';raise ValueError('invalid_handshake')
                    if len(handshake)>65536:
                        failure_reason='upgrade_header_too_large';raise ValueError('invalid_handshake')
                    handshake+=chunk
                try:status=int(handshake.split(b'\r\n',1)[0].split()[1])
                except (ValueError,IndexError):
                    failure_reason='upgrade_status_invalid';raise ValueError('invalid_handshake_status')
                watch.stop();watch=None
                if status!=101:
                    store.status('websocket_upgrade_rejected',transport='websocket',stage='upgrade',http_status=status)
                    response=http.client.HTTPResponse(HandshakeSocket(handshake,upstream));response.begin()
                    stage='client_write';self.send_response(response.status)
                    for k,v in response.getheaders():
                        if k.lower() not in ('transfer-encoding','connection','content-length'):self.send_header(k,v)
                    self.send_header('Connection','close');self.end_headers();sent_headers=True
                    while True:
                        stage='upstream_read'
                        chunk=response.read1(65536)
                        if not chunk:break
                        stage='client_write';self.wfile.write(chunk);self.wfile.flush()
                    response.close();return
                store.status('websocket_upgrade_succeeded',transport='websocket',stage='upgrade',http_status=101,
                    thread_id=identifier(self.headers.get('thread-id')),connection_id=self.connection_id)
                stage='client_write';self.connection.sendall(handshake);sent_headers=True
                request_wait=RequestWait(store,self.headers.get('thread-id'),self.connection_id)
                def closed(peer,code):
                    close_seen[peer]=True
                    store.status('websocket_close_frame',transport='websocket',stage='close_frame',peer=peer,close_code=code,
                        thread_id=identifier(self.headers.get('thread-id')),connection_id=self.connection_id)
                observer=Observer(store,self.headers.get('thread-id'),'websocket',on_event=request_wait.server_event);frames=Frames(observer,on_data=request_wait.progress,on_close=lambda code:closed('upstream',code))
                # Inspect only event types for waiting; requests never become model evidence.
                # 只读取事件类型判断是否等待，请求绝不作为模型核对证据。
                client_frames=Frames(request_wait,64*1024*1024,on_close=lambda code:closed('client',code))
                upstream.settimeout(None);self.connection.settimeout(None)
                while True:
                    # TLS may already hold decrypted bytes even when select sees no socket data.
                    # 即使 select 看不到 socket 数据，TLS 缓冲仍可能有已解密字节。
                    pending=isinstance(upstream,ssl.SSLSocket) and upstream.pending()>0
                    # Client traffic must not extend an active response's upstream idle budget.
                    # 客户端流量不得延长活跃响应等待上游数据的空闲预算。
                    waiting=bool(observer.responses or request_wait.pending)
                    budget=max(0,STREAM_IDLE_TIMEOUT_SECONDS-(time.monotonic()-max(request_wait.last_response_at,request_wait.since))) if waiting else STREAM_IDLE_TIMEOUT_SECONDS
                    readable=select.select([upstream,self.connection],[],[],0 if pending else budget)[0] if pending or budget>0 else []
                    if pending and upstream not in readable:readable.append(upstream)
                    if not readable:
                        if waiting:
                            context={'transport':'websocket','stage':'upstream_read' if observer.responses else 'upstream_first_response','reason':'no_upstream_data',
                                'wait_limit_seconds':STREAM_IDLE_TIMEOUT_SECONDS,'thread_id':observer.thread_id,'connection_id':self.connection_id}
                            store.status('upstream_wait_timeout',**context)
                            store.status('websocket_error',**context)
                        else:store.status('websocket_idle_closed')
                        break
                    for source in readable:
                        stage='upstream_read' if source is upstream else 'client_read'
                        data=source.recv(65536)
                        if not data:
                            store.status('websocket_peer_closed',transport='websocket',stage=stage,peer='upstream' if source is upstream else 'client',
                                reason='after_close_frame' if any(close_seen.values()) else 'eof_without_close_frame',
                                thread_id=observer.thread_id,connection_id=self.connection_id)
                            return
                        if source is upstream:
                            try:frames.feed(data)
                            except (ValueError,UnicodeError) as error:observer.corrupt=True;store.status('websocket_parse_error',transport='websocket',stage='frame_parse',exception=type(error).__name__)
                            stage='client_write';self.connection.sendall(data)
                        else:
                            stage='upstream_write';upstream.sendall(data)
                            if client_frames:
                                try:client_frames.feed(data)
                                except (ValueError,UnicodeError) as error:
                                    store.status('websocket_request_parse_error',transport='websocket',stage='client_frame_parse',exception=type(error).__name__,
                                        thread_id=observer.thread_id,connection_id=self.connection_id)
                                    client_frames=None
            except (OSError,ValueError,http.client.HTTPException) as error:
                if stage=='upstream_connect':stage=getattr(self,'connect_stage',stage)
                client_closed=bool(watch and watch.reset) or stage in ('client_write','client_read') and isinstance(error,(BrokenPipeError,ConnectionResetError))
                if isinstance(error,socket.timeout) and stage=='upgrade_read' and not (watch and watch.reset):
                    store.status('upstream_wait_timeout',transport='websocket',stage='upgrade_read',reason='no_upstream_data',
                        wait_limit_seconds=STREAM_IDLE_TIMEOUT_SECONDS,thread_id=identifier(self.headers.get('thread-id')),connection_id=self.connection_id)
                reason='client_reset_during_upgrade' if watch and watch.reset else failure_reason or ('after_client_close_frame' if close_seen['client'] else 'after_upstream_close_frame' if close_seen['upstream'] else 'peer_reset' if isinstance(error,ConnectionResetError) else None)
                store.status('websocket_client_disconnected' if client_closed else 'websocket_error',transport='websocket',stage=stage,exception=type(error).__name__,errno=getattr(error,'errno',None),
                    thread_id=identifier(self.headers.get('thread-id')),connection_id=getattr(self,'connection_id',None),reason=reason,
                    payload_bytes=len(handshake) if stage=='upgrade_read' else None)
                if not sent_headers and not client_closed:
                    try:self.failure(502)
                    except OSError:store.status('websocket_client_disconnected',transport='websocket',stage='failure_reply',reason='client_closed')
            finally:
                try:
                    if watch:watch.stop()
                finally:
                    self.close_connection=True
                    try:
                        if observer:
                            if frames.buffer or frames.message:store.status('incomplete_capture',transport='websocket',stage='frame_eof')
                            observer.finish()
                    finally:
                        if conn:conn.close()
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def main():
    os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18084);p.add_argument('--directory',type=Path,default=BASE/'data/capture');a=p.parse_args()
    store=Store(a.directory);server=make_server(store,a.port);store.status('started')
    print('Metadata-only upstream observer ready on 127.0.0.1:%d'%server.server_port,flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:store.status('stopped');server.server_close()
if __name__=='__main__':main()
