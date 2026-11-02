"""Independent synthetic files for a visibly labeled browser demonstration."""
import json
from pathlib import Path
import sqlite3
import time
from model_monitor.app import Scanner


def build():
    base=Path(__file__).resolve().parent/'data/demo'
    base.mkdir(exist_ok=True)
    bundle=base/'traces/synthetic'
    bundle.mkdir(parents=True,exist_ok=True)
    db=base/'state.sqlite'
    with sqlite3.connect(str(db)) as c:
        c.executescript('CREATE TABLE IF NOT EXISTS threads(id TEXT,rollout_path TEXT,name TEXT,title TEXT,project_id TEXT,archived INTEGER,updated_at INTEGER,model TEXT); CREATE TABLE IF NOT EXISTS projects(id TEXT,name TEXT); DELETE FROM threads; DELETE FROM projects;')
        c.execute('INSERT INTO projects VALUES (?,?)',('demo','合成演示项目'))
        events=[]
        for i,(name,upstream) in enumerate([('一致样例','A'),('不一致样例','B'),('待验证样例',None)]):
            tid='synthetic-'+str(i);rollout=base/(tid+'.jsonl')
            rollout.write_text(json.dumps({'type':'turn_context','payload':{'turn_id':'demo-turn','model':'A'}})+'\n',encoding='utf-8')
            c.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)',(tid,str(rollout),'[合成] '+name,'','demo',0,int(time.time()),'UNUSED'))
            (bundle/(tid+'-req.json')).write_text('{"model":"A"}',encoding='utf-8')
            body={'response_id':'response-'+tid}
            if upstream:body['model']=upstream
            (bundle/(tid+'-resp.json')).write_text(json.dumps(body),encoding='utf-8')
            for j,typ in enumerate(['inference_started','inference_completed']):
                payload={'type':typ,'inference_call_id':'call-'+tid}
                if j:payload.update(response_id='response-'+tid,response_payload={'path':tid+'-resp.json'})
                else:payload.update(model='A',request_payload={'path':tid+'-req.json'})
                events.append({'seq':len(events)+1,'thread_id':tid,'codex_turn_id':'demo-turn','wall_time_unix_ms':int(time.time()*1000)+i,'payload':payload})
        (bundle/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    result=Scanner(db,base/'traces').scan();result['demo']=True
    return result
