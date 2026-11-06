"""Shared jobs, budgets and baseline provenance for HTTP and CLI clients."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import uuid

CACHE=Path.home()/'Library/Caches/model-observatory-aig'
ENGINE_PYTHON=CACHE/'venv/bin/python'
PROTOCOLS=('responses','chat','anthropic')
PROMPT='请从1到355之间随机选择一个数字，只输出这个数字，不要有任何其他内容。'
NOTICE='抽样只描述指定接口与条件下的行为，不证明模型真身，也不认证任何一条对话请求。'

def conditions(protocol):
    if protocol not in PROTOCOLS:
        raise ValueError('Explicit protocol required')
    return {'language':'zh','prompt_sha256':hashlib.sha256(PROMPT.encode()).hexdigest(),
            'temperature':None if protocol=='responses' else 1.0,'reasoning':'unspecified','max_output_tokens':10,'stream':False}

def canonical(data):
    return json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)

def now():
    return datetime.now(timezone.utc).isoformat()

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None

class ProbeError(Exception):
    pass

def _unique_history_object(pairs):
    # Reject duplicate keys at every JSON object level before any recovery.
    # 在任何恢复前拒绝各层JSON对象的重复键。
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('Duplicate history key')
        result[key]=value
    return result

class Manager:
    def __init__(self,directory,allow_remote=False):
        if type(allow_remote)!=bool: raise ValueError("allow_remote must be boolean")
        self.allow_remote=allow_remote
        self.context=threading.local()
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.engine_python=ENGINE_PYTHON
        self.lock=threading.RLock()
        self.events={}; self.threads={}; self.jobs={}; self.baselines=[]
        self.history=self.directory/'history.json'; self.baseline_path=self.directory/'baselines.json'
        if self.baseline_path.exists():
            loaded=json.loads(self.baseline_path.read_text(encoding='utf-8'))
            if not isinstance(loaded,list): raise ValueError('Baseline store must be a list')
            for entry in loaded:
                body={k:v for k,v in entry.items() if k!='hash'}
                if hashlib.sha256(canonical(body).encode()).hexdigest()!=entry['hash']:
                    raise ValueError('Baseline integrity mismatch')
                self._validate_baseline(body)
                self.baselines.append(copy.deepcopy(entry))
        if self.history.exists():
            try:
                loaded=json.loads(self.history.read_text(encoding='utf-8'),object_pairs_hook=_unique_history_object)
                self._validate_history(loaded)
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
                raise ValueError('Invalid audit history schema; original file preserved') from None
            self.jobs=loaded
            for j in self.jobs.values():
                if j['state'] not in ('completed','failed','cancelled'):
                    j.update(state='failed',verdict='证据不足',errors=j['errors']+1,failures=j['failures']+['interrupted_by_restart'])
            self._save()
    def _validate_history(self,history):
        # Validate the entire input before applying restart recovery or writing.
        # 先校验整份输入，再执行重启恢复或写盘。
        if not isinstance(history,dict): raise ValueError('Invalid history')
        canonical(history)
        required={'schema_version','id','state','verdict','declaration','target','created_at','updated_at',
                  'valid_samples','sample_attempts','requests','errors','skipped','failures','baseline_hashes',
                  'usage_tokens','reserved_tokens','notice'}
        optional={'context','engine_result','calibrated_baseline'}
        for ident,j in history.items():
            if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident): raise ValueError('Invalid job ID')
            if not isinstance(j,dict) or not required<=set(j) or set(j)-required-optional: raise ValueError('Invalid job fields')
            if type(j['schema_version'])!=int or j['schema_version']!=1 or j['id']!=ident: raise ValueError('Invalid job version/ID')
            if j['state'] not in ('queued','running','cancelling','completed','failed','cancelled'): raise ValueError('Invalid state')
            if j['verdict'] not in ('未发现异常','发现偏离','证据不足') or j['declaration'] not in ('声明一致','声明冲突','声明缺失'): raise ValueError('Invalid verdict')
            self._validate(j['target'])
            for field in ('created_at','updated_at'):
                if not isinstance(j[field],str) or datetime.fromisoformat(j[field]).tzinfo is None: raise ValueError('Invalid date')
            for field in ('valid_samples','sample_attempts','requests','errors','skipped','usage_tokens','reserved_tokens'):
                if type(j[field])!=int or j[field]<0: raise ValueError('Invalid count')
            if not j['valid_samples']<=j['sample_attempts']<=j['target']['samples'] or j['requests']>j['target']['samples']+1 or j['skipped']>j['target']['samples']: raise ValueError('Invalid sample counts')
            if not isinstance(j['notice'],str) or not j['notice']: raise ValueError('Invalid notice')
            if not isinstance(j['failures'],list) or any(not isinstance(x,str) for x in j['failures']): raise ValueError('Invalid failures')
            if not isinstance(j['baseline_hashes'],list) or any(not isinstance(x,str) or not re.fullmatch(r'[0-9a-f]{64}',x) for x in j['baseline_hashes']): raise ValueError('Invalid baseline hashes')
            if 'context' in j and j['context'] is not None:
                c=j['context']
                if not isinstance(c,dict) or set(c)!={'saw_start','saw_mid','saw_end'} or any(type(x)!=bool for x in c.values()): raise ValueError('Invalid context')
            if 'engine_result' in j:
                self._validate_history_result(j['engine_result'],j['valid_samples'])
            elif j['verdict']!='证据不足':
                raise ValueError('History verdict has no engine evidence')
            if 'calibrated_baseline' in j:
                b=j['calibrated_baseline']
                fields={'schema_version','source','model','protocol','date','conditions','hash','sample_count'}
                if not isinstance(b,dict) or set(b)!=fields or type(b['schema_version'])!=int or b['schema_version']!=1: raise ValueError('Invalid calibration')
                if type(b['sample_count'])!=int or not 40<=b['sample_count']<=10000: raise ValueError('Invalid baseline count')
                if not isinstance(b['hash'],str) or not re.fullmatch(r'[0-9a-f]{64}',b['hash']): raise ValueError('Invalid baseline hash')
                if not isinstance(b['source'],str) or not 1<=len(b['source'])<=200 or not isinstance(b['model'],str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,100}',b['model']): raise ValueError('Invalid provenance')
                if not isinstance(b['date'],str): raise ValueError('Invalid baseline date')
                datetime.fromisoformat(b['date']);self._conditions(b['protocol'],b['conditions'])
    def _validate_history_result(self,result,samples):
        # Validate persisted evidence, without rescoring or extending status policy.
        # 校验持久化依据，不重新评分或扩展状态策略。
        if not isinstance(result,dict): raise ValueError('Invalid engine result')
        if type(result.get('sample_size'))!=int or result['sample_size']!=samples: raise ValueError('Invalid result sample count')
        for field in ('best_model_name','best_model_model','second_model_name','evidence_level'):
            if not isinstance(result.get(field),str) or not result[field]: raise ValueError('Invalid result identity')
        def numeric(value):
            return type(value) in (int,float) and math.isfinite(value)
        if not numeric(result.get('best_posterior')) or not 0<=result['best_posterior']<=1 or not numeric(result.get('log_margin')): raise ValueError('Invalid result score')
        top=result.get('top5')
        if not isinstance(top,list) or not top: raise ValueError('Invalid ranked results')
        for row in top:
            if not isinstance(row,dict) or any(not isinstance(row.get(k),str) or not row[k] for k in ('name','model')): raise ValueError('Invalid ranked identity')
            if not numeric(row.get('posterior')) or not 0<=row['posterior']<=1 or not numeric(row.get('log_predictive')): raise ValueError('Invalid ranked score')
        forgery=result.get('forgery')
        if not isinstance(forgery,dict) or not isinstance(forgery.get('status'),str) or not forgery['status']: raise ValueError('Missing result decision')
        for field in ('claimed_model','best_model'):
            if type(forgery.get(field))!=int or forgery[field]<0: raise ValueError('Invalid result index')
        for field in ('known_alt_log_bf','g2','g2_threshold','tau_accept','tau_reject'):
            if not numeric(forgery.get(field)): raise ValueError('Missing decision evidence')
        for field in ('claimed_model_name','best_model_name'):
            if not isinstance(forgery.get(field),str) or not forgery[field]: raise ValueError('Missing decision identity')
    def _write(self,path,data):
        temporary=path.with_suffix('.tmp')
        temporary.write_text(canonical(data)+'\n',encoding='utf-8'); temporary.chmod(0o600); temporary.replace(path)
    def _save(self):
        self._write(self.history,self.jobs)
    def _execute(self,argv,payload,timeout):
        event=getattr(self.context,'event',None)
        deadline=min(time.monotonic()+timeout,getattr(self.context,'deadline',float('inf')))
        if event is not None and event.is_set(): raise ProbeError('cancelled')
        process=None
        try:
            process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            first=True
            while True:
                if event is not None and event.is_set(): raise ProbeError('cancelled')
                remaining=deadline-time.monotonic()
                if remaining<=0: raise ProbeError('request_or_job_timeout')
                try:
                    out,err=process.communicate(input=canonical(payload) if first else None,timeout=min(.02,remaining))
                    return subprocess.CompletedProcess(argv,process.returncode,out,err)
                except subprocess.TimeoutExpired:
                    first=False
        except OSError as error:
            raise ProbeError('worker_unavailable:'+type(error).__name__) from None
        finally:
            if process is not None and process.poll() is None:
                process.kill();process.communicate()
    def engine(self,data):
        r=self._execute([str(self.engine_python),'-I',str(Path(__file__).with_name('engine.py'))],data,20)
        if r.returncode:
            try:
                detail=json.loads(r.stdout)
                error=detail.get('error_type','failure')
                dependency=detail.get('dependency','')
                if not re.fullmatch(r'[A-Za-z0-9_.]{0,100}',dependency): dependency=''
                if error not in ('ModuleNotFoundError','RuntimeError','ValueError'): error='failure'
            except (ValueError,AttributeError):
                error='failure';dependency=''
            raise ProbeError('engine_exit_%s:%s:%s' % (r.returncode,error,dependency))
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise ProbeError('engine_invalid_json') from None
    def diagnose(self):
        info=self.engine({'action':'describe'})
        if info['prompt']!=PROMPT or info['min_samples']!=40:
            raise ProbeError('engine_contract_mismatch')
        return {'schema_version':1,'engine':'Tencent/AI-Infra-Guard services/api_checker','commit':'3b12a868d865e007bb098221d0c9129def10929a','protocols':PROTOCOLS,'min_samples':40,'zero_cost_mode':'remote execution disabled by default; explicit operator startup allow_remote required','remote_execution_enabled':self.allow_remote,'notice':NOTICE}
    def _validate_baseline(self,b):
        required={'schema_version','source','model','protocol','date','conditions','samples'}
        if set(b)!=required or b['schema_version']!=1:
            raise ValueError('Baseline schema mismatch')
        if not isinstance(b['source'],str) or not 1<=len(b['source'])<=200 or not isinstance(b['model'],str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,100}',b['model']):
            raise ValueError('Baseline provenance/model invalid')
        datetime.fromisoformat(b['date'])
        self._conditions(b['protocol'],b['conditions'])
        if not isinstance(b['samples'],list) or not 40<=len(b['samples'])<=10000 or any(type(n)!=int or not 1<=n<=355 for n in b['samples']):
            raise ValueError('Baseline needs 40..10000 valid number samples')
    def import_baseline(self,b):
        self._validate_baseline(b)
        h=hashlib.sha256(canonical(b).encode()).hexdigest()
        entry=copy.deepcopy(b); entry['hash']=h
        with self.lock:
            if not any(x['hash']==h for x in self.baselines):
                self.baselines.append(entry); self._write(self.baseline_path,self.baselines)
        return self._metadata(entry)
    def _metadata(self,b):
        return {**{k:v for k,v in b.items() if k!='samples'},'sample_count':len(b['samples'])}
    def coverage(self):
        with self.lock:
            return [self._metadata(b) for b in self.baselines]
    def _conditions(self,protocol,c):
        expected=conditions(protocol)
        if not isinstance(c,dict) or set(c)!=set(expected):
            raise ValueError('Conditions schema mismatch')
        if c['language']!='zh' or c['prompt_sha256']!=expected['prompt_sha256'] or c['reasoning']!='unspecified' or c['max_output_tokens']!=10 or c['stream'] is not False:
            raise ValueError('Unsupported probe conditions; no silent fallback')
        t=c['temperature']
        if t is not None and (type(t) not in (int,float) or not math.isfinite(t) or not 0<=t<=1):
            raise ValueError('Invalid temperature')
    def _validate(self,s):
        required={'endpoint','model','protocol','samples','timeout','token_budget','conditions'}
        if not isinstance(s,dict) or not required<=set(s) or set(s)-required-{'mode','source'}:
            raise ValueError('Job schema mismatch; no file or command arguments')
        u=urlsplit(s['endpoint'])
        local=u.scheme=='http' and u.hostname=='127.0.0.1' and u.port and u.port not in range(18082,18086)
        remote=u.scheme=='https' and bool(u.hostname) and u.hostname not in ('localhost','127.0.0.1','::1')
        if not (local or remote) or u.username or u.password or u.query or u.fragment or u.path.rstrip('/')!='/v1':
            raise ValueError('Use an isolated loopback HTTP or explicit HTTPS /v1 endpoint')
        if not re.fullmatch(r'[A-Za-z0-9_./:-]{1,100}',s['model']):
            raise ValueError('Invalid model ID')
        for key,low,high in (('samples',1,200),('token_budget',1,100000)):
            if type(s[key])!=int or not low<=s[key]<=high:
                raise ValueError('Invalid '+key)
        if type(s['timeout']) not in (int,float) or not .05<=s['timeout']<=5:
            raise ValueError('Timeout must be .05..5 seconds')
        self._conditions(s['protocol'],s['conditions'])
        if s.get('mode','audit') not in ('audit','calibrate'):
            raise ValueError('Invalid mode')
        if s.get('mode')=='calibrate' and (not isinstance(s.get('source'),str) or not s['source'] or len(s['source'])>200):
            raise ValueError('Calibration requires explicit source provenance')
        return copy.deepcopy(s)
    def submit(self,spec,key):
        s=self._validate(spec)
        if urlsplit(s['endpoint']).scheme=='https' and not self.allow_remote:
            raise ValueError('Remote execution disabled; explicit operator startup authorization required')
        if not isinstance(key,str) or not 1<=len(key)<=2048 or '\n' in key or '\r' in key:
            raise ValueError('Key required in memory')
        with self.lock:
            if any(j['state'] in ('queued','running','cancelling') for j in self.jobs.values()):
                raise ValueError('One active job permitted')
            ident=uuid.uuid4().hex; event=threading.Event(); self.events[ident]=event
            j={'schema_version':1,'id':ident,'state':'queued','verdict':'证据不足','declaration':'声明缺失','target':s,'created_at':now(),'updated_at':now(),'valid_samples':0,'sample_attempts':0,'requests':0,'errors':0,'skipped':0,'failures':[],'baseline_hashes':[],'usage_tokens':0,'reserved_tokens':0,'notice':NOTICE}
            self.jobs[ident]=j; self._save()
            t=threading.Thread(target=self._run,args=(ident,s,key,event),daemon=True);self.threads[ident]=t;t.start()
            return copy.deepcopy(j)
    def get(self,ident):
        with self.lock:
            return copy.deepcopy(self.jobs[ident])
    def list(self):
        with self.lock:
            return copy.deepcopy(list(self.jobs.values()))
    def cancel(self,ident):
        with self.lock:
            j=self.jobs[ident]
            if j['state'] in ('queued','running','cancelling'):
                self.events[ident].set();j['state']='cancelling';self._save()
            return copy.deepcopy(j)
    def _request(self,ident,s,key,event,prompt,max_tokens):
        # Reserving before dispatch bounds costs even when a provider omits usage.
        # 派发前预留上限，供应商不返回用量时仍受预算约束。
        with self.lock:
            j=self.jobs[ident]
            if event.is_set():
                raise ProbeError('cancelled')
            reserve=len(prompt.encode('utf-8'))+len(s['model'])+max_tokens+64
            if j['usage_tokens']>=s['token_budget'] or j['reserved_tokens']+reserve>s['token_budget']:
                raise ProbeError('token_budget_exhausted')
            j['reserved_tokens']+=reserve; j['requests']+=1
        p=s['protocol']; body={'model':s['model'],'stream':False}
        body['input' if p=='responses' else 'messages']=[{'role':'user','content':prompt}]
        body['max_output_tokens' if p=='responses' else 'max_tokens' if p=='anthropic' else 'max_completion_tokens']=max_tokens
        if s['conditions']['temperature'] is not None:
            body['temperature']=s['conditions']['temperature']
        headers={'Content-Type':'application/json','Authorization':'Bearer '+key} if p!='anthropic' else {'Content-Type':'application/json','x-api-key':key,'anthropic-version':'2023-06-01'}
        path={'responses':'responses','chat':'chat/completions','anthropic':'messages'}[p]
        response=self._execute([sys.executable,'-I',str(Path(__file__).with_name('transport.py'))],
            {'url':s['endpoint'].rstrip('/')+'/'+path,'body':body,'headers':headers,'timeout':s['timeout']},s['timeout'])
        if response.returncode: raise ProbeError('transport_worker_failed')
        try: envelope=json.loads(response.stdout)
        except ValueError: raise ProbeError('transport_worker_invalid_json') from None
        if 'error' in envelope: raise ProbeError(envelope['error'])
        d=envelope['payload']
        try:
            if 'error' in d: raise ProbeError('provider_error')
            if p=='responses':
                if d['status']!='completed': raise ProbeError('incomplete_response')
                text=''.join(c['text'] for o in d['output'] if o['type']=='message' for c in o['content'] if c['type']=='output_text')
            elif p=='anthropic':
                if d['stop_reason']!='end_turn': raise ProbeError('incomplete_response')
                text=''.join(c['text'] for c in d['content'] if c['type']=='text')
            else:
                if d['choices'][0]['finish_reason']!='stop': raise ProbeError('incomplete_response')
                text=d['choices'][0]['message']['content']
            if not isinstance(text,str) or not text: raise ProbeError('empty_response')
            usage=d['usage']; a=usage['prompt_tokens' if p=='chat' else 'input_tokens']; b=usage['completion_tokens' if p=='chat' else 'output_tokens']
            if type(a)!=int or type(b)!=int or min(a,b)<0: raise ProbeError('invalid_usage')
            returned=d.get('model')
            declaration='声明缺失' if not isinstance(returned,str) or not returned else '声明一致' if returned==s['model'] else '声明冲突'
            with self.lock:
                j=self.jobs[ident]; j['usage_tokens']+=a+b
                if j['declaration']!='声明冲突': j['declaration']=declaration
                if j['usage_tokens']>s['token_budget']: raise ProbeError('reported_usage_over_budget')
            return text
        except (KeyError,IndexError,TypeError):
            raise ProbeError('invalid_response_shape_or_usage') from None
    def _run(self,ident,s,key,event):
        texts=[];deadline=time.monotonic()+120
        self.context.event=event;self.context.deadline=deadline
        with self.lock: self.jobs[ident]['state']='running'
        try:
            self.diagnose()
            for i in range(s['samples']):
                if event.is_set(): break
                if time.monotonic()>deadline: raise ProbeError('job_timeout_120s')
                with self.lock: self.jobs[ident]['sample_attempts']+=1
                try:
                    text=self._request(ident,s,key,event,PROMPT,10)
                    if not re.fullmatch(r'\s*(?:[1-9]|[1-9][0-9]|[12][0-9]{2}|3[0-4][0-9]|35[0-5])\s*',text):
                        raise ProbeError('invalid_number_sample')
                    texts.append(text)
                except ProbeError as e:
                    if str(e)=='cancelled': break
                    self._error(ident,str(e))
                with self.lock:
                    self.jobs[ident].update(valid_samples=len(texts),updated_at=now()); self._save()
            context=None
            if not event.is_set():
                canaries=['CANARY_'+uuid.uuid4().hex[:10] for _ in range(3)]
                prompt=('\n'+('The quick brown fox jumps over the lazy dog. '*8)+'\n').join(canaries)+'\nRepeat back ONLY the three canary tokens, each on its own line.'
                try:
                    text=self._request(ident,s,key,event,prompt,64)
                    context=dict(zip(('saw_start','saw_mid','saw_end'),(c in text for c in canaries)))
                except ProbeError as e: self._error(ident,str(e))
            with self.lock:
                baselines=copy.deepcopy([b for b in self.baselines if b['protocol']==s['protocol'] and b['conditions']==s['conditions'] and len(b['samples'])>=40])
                j=self.jobs[ident];j['context']=context;j['baseline_hashes']=[b['hash'] for b in baselines];j['skipped']=s['samples']-j['sample_attempts']
            if event.is_set():
                with self.lock: j.update(state='cancelled',verdict='证据不足')
            elif s.get('mode')=='calibrate':
                if len(texts)<40 or j['errors']: raise ProbeError('calibration_insufficient')
                imported=self.import_baseline({'schema_version':1,'source':s['source'],'model':s['model'],'protocol':s['protocol'],'date':now(),'conditions':s['conditions'],'samples':[int(t) for t in texts]})
                with self.lock: j.update(state='completed',calibrated_baseline=imported)
            else:
                reason=None
                if len(texts)<40: reason='sample_count_below_40'
                elif len({b['model'] for b in baselines})<2: reason='need_two_compatible_reference_models'
                elif s['model'] not in [b['model'] for b in baselines]: reason='claimed_model_not_covered'
                if reason:
                    with self.lock: j['failures'].append(reason)
                else:
                    unique={b['model']:b for b in baselines}
                    aig=[self.engine({'action':'baseline','model':b['model'],'protocol':b['protocol'],'texts':[str(n) for n in b['samples']]}) for b in unique.values()]
                    result=self.engine({'action':'evaluate','model':s['model'],'texts':texts,'baselines':aig})
                    status=result['forgery']['status']
                    verdict={'supported':'未发现异常','suspected_known':'发现偏离','unknown_anomaly':'发现偏离','insufficient':'证据不足'}.get(status,'证据不足')
                    if context is not None and not all(context.values()): verdict='发现偏离'
                    if j['errors'] or context is None: verdict='证据不足'
                    with self.lock: j.update(verdict=verdict,engine_result=result)
                with self.lock: j['state']='completed'
        except Exception as e:
            self._error(ident,str(e) if isinstance(e,ProbeError) else 'internal_'+type(e).__name__)
            with self.lock: self.jobs[ident].update(state='cancelled' if event.is_set() else 'failed',verdict='证据不足')
        finally:
            key=None
            with self.lock:
                self.jobs[ident]['updated_at']=now();self.jobs[ident]['skipped']=s['samples']-self.jobs[ident]['sample_attempts'];self._save()
    def _error(self,ident,reason):
        with self.lock:
            j=self.jobs[ident];j['errors']+=1;j['failures'].append(reason)
    def close(self):
        for event in self.events.values(): event.set()
        for thread in self.threads.values(): thread.join(25)
