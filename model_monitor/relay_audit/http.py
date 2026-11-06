"""Versioned audit API; shared by standalone and both existing frontends."""
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .core import conditions

PREFIX='/api/audit/v1'
ASSETS=Path(__file__).parent

def dispatch(handler,manager):
    path=urlsplit(handler.path).path
    if path in ('/audit.js','/audit.css') and handler.command=='GET':
        if handler.allowed(auth=False):
            name=path[1:];handler.respond(200,b'' if manager is None else (ASSETS/name).read_bytes(),'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/css; charset=utf-8')
        return True
    if manager is None or not path.startswith(PREFIX): return False
    if not handler.allowed(): return True
    try:
        suffix=path[len(PREFIX):]
        if handler.command=='GET':
            if suffix=='/capabilities':
                data=manager.diagnose();data['conditions']={p:conditions(p) for p in data['protocols']}
                data['operations']=['GET capabilities','GET coverage','POST baselines','POST jobs','GET jobs','GET jobs/{id}','POST jobs/{id}/cancel']
            elif suffix=='/coverage': data={'baselines':manager.coverage()}
            elif suffix=='/jobs': data={'jobs':manager.list()}
            elif suffix.startswith('/jobs/'): data=manager.get(suffix[6:])
            else: raise KeyError(suffix)
        elif handler.command=='POST':
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=131072: raise ValueError('Body must be 1..131072 bytes')
            body=json.loads(handler.rfile.read(size))
            if suffix=='/jobs':
                if set(body)!={'spec','api_key'}: raise ValueError('Expected spec and in-memory api_key')
                data=manager.submit(body['spec'],body['api_key'])
            elif suffix=='/baselines': data=manager.import_baseline(body)
            elif suffix.startswith('/jobs/') and suffix.endswith('/cancel'):
                if body!={}: raise ValueError('Cancel body must be empty')
                data=manager.cancel(suffix[6:-7])
            else: raise KeyError(suffix)
        else: raise ValueError('Unsupported method')
        handler.respond(200,{'schema_version':1,'data':data})
    except KeyError: handler.respond(404,{'schema_version':1,'error':'not_found'})
    except (ValueError,TypeError,OverflowError): handler.respond(400,{'schema_version':1,'error':'invalid_input'})
    except Exception:
        handler.respond(503,{'schema_version':1,'error':'audit_backend_unavailable'})
        raise
    return True

def make_server(manager,token,port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): return
        def respond(self,status,data,content_type='application/json; charset=utf-8'):
            body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status)
            for k,v in {'Content-Type':content_type,'Content-Length':str(len(body)),'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'}.items(): self.send_header(k,v)
            self.end_headers();self.wfile.write(body)
        def allowed(self,auth=True):
            host='127.0.0.1:'+str(self.server.server_port)
            if self.headers.get('Host')!=host or self.headers.get('Origin','http://'+host)!='http://'+host:
                self.respond(403,{'error':'forbidden_host_origin'});return False
            if auth and not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.respond(401,{'error':'unauthorized'});return False
            return True
        def do_GET(self):
            if not dispatch(self,manager): self.respond(404,{'error':'not_found'})
        def do_POST(self):
            if not dispatch(self,manager): self.respond(404,{'error':'not_found'})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)
