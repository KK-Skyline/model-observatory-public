"""Serve a separate frontend with the existing authenticated API and scanner."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from model_monitor import app

BASE = Path(__file__).resolve().parent


def demo_snapshot(directory):
    """Synthetic fixture uses real parsing and lives only in this preview's data."""
    folder = Path(directory)/'demo'
    trace = folder/'traces/synthetic'
    trace.mkdir(parents=True, exist_ok=True)
    db = folder/'state.sqlite'
    events = []
    now = int(time.time()*1000)
    with sqlite3.connect(str(db)) as connection:
        connection.executescript('CREATE TABLE IF NOT EXISTS projects(id TEXT,name TEXT); '
            'CREATE TABLE IF NOT EXISTS threads(id TEXT,rollout_path TEXT,name TEXT,title TEXT,project_id TEXT,archived INTEGER,updated_at INTEGER); '
            'DELETE FROM projects; DELETE FROM threads;')
        connection.execute('INSERT INTO projects VALUES (?,?)', ('demo','合成演示项目'))
        for i, (title, model) in enumerate([('一致样例','A'), ('不一致样例','B'), ('待验证样例',None)]):
            tid = 'synthetic-'+str(i)
            rollout = folder/(tid+'.jsonl')
            rollout.write_text(json.dumps({'type':'turn_context','payload':{'turn_id':'demo-turn','model':'A'}})+'\n', encoding='utf-8')
            connection.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',
                (tid,str(rollout),'[合成] '+title,'','demo',0,now//1000))
            (trace/(tid+'-req.json')).write_text('{"model":"A"}', encoding='utf-8')
            body = {'response_id':'response-'+tid}
            if model:
                body['model'] = model
            (trace/(tid+'-resp.json')).write_text(json.dumps(body), encoding='utf-8')
            for step, typ in enumerate(['inference_started','inference_completed']):
                payload = {'type':typ,'inference_call_id':'call-'+tid}
                if step:
                    payload.update(response_id=body['response_id'], response_payload={'path':tid+'-resp.json'})
                else:
                    payload['request_payload'] = {'path':tid+'-req.json'}
                events.append({'seq':len(events)+1,'thread_id':tid,'codex_turn_id':'demo-turn',
                               'wall_time_unix_ms':now+i,'payload':payload})
    (trace/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events), encoding='utf-8')
    result = app.Scanner(db,folder/'traces').scan()
    result['demo'] = True
    return result


def make_preview(scanner, directory, token, port, audit=None):
    server = app.make_server(scanner, directory, token, port, audit=audit)
    parent = server.RequestHandlerClass
    import threading
    demo_lock = threading.Lock()

    class Handler(parent):
        def do_GET(self):
            path = urlsplit(self.path).path
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            icons = {('/icons/' + p.name): p for p in (BASE/'static/icons').glob('*.svg')}
            if path == '/api/demo':
                if self.allowed():
                    with demo_lock:
                        self.respond(200, demo_snapshot(directory))
            elif path == '/app.js':
                if self.allowed(auth=False):
                    # Serve the original controller, then add presentation-only behavior.
                    # 直接提供原控制器，再追加仅影响表现的交互。
                    code = (app.BASE/'static/app.js').read_bytes()
                    self.respond(200, code + b'\n' + (BASE/'static/shell.js').read_bytes(),
                                 'text/javascript; charset=utf-8')
            elif path in assets:
                if self.allowed(auth=False):
                    filename, mime = assets[path]
                    self.respond(200, (BASE/'static'/filename).read_bytes(), mime)
            elif path in icons:
                if self.allowed(auth=False):
                    self.respond(200, icons[path].read_bytes(), 'image/svg+xml')
            else:
                super().do_GET()

    server.RequestHandlerClass = Handler
    return server


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Independent macOS design preview')
    parser.add_argument('--port', type=int, default=18085)
    args = parser.parse_args()
    if args.port in (18082, 18083, 18084):
        parser.error('该端口保留给现役查看器、面板或采集器')
    directory = BASE/'data'
    directory.mkdir(exist_ok=True, mode=0o700)
    scanner = app.Scanner(Path.home()/'.codex/state_5.sqlite',
                          Path.home()/'.local/share/codex-request-inspector/native-traces',
                          Path.home()/'.local/share/codex-request-inspector/model-monitor-capture/metadata')
    token = secrets.token_urlsafe(32)
    server = make_preview(scanner, directory, token, args.port)
    state = directory/'session.json'
    state.write_text(json.dumps({'port':server.server_port, 'pid':os.getpid(), 'token':token}), encoding='utf-8')
    state.chmod(0o600)
    print('设计预览已启动：127.0.0.1:%d；私有授权不打印。' % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
