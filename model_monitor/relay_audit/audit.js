'use strict';
(()=>{
 const host=document.querySelector('main');
 const area=document.createElement('section');area.id='route-audit';
 area.innerHTML=`<h2>线路抽样审计 · 隔离候选</h2><p>逐请求声明与抽样行为分开阅读。仅本机合成端点、费用 0；不认证对话中的任何请求或模型真身。</p>
 <form id="audit-form"><label>目标接口<input name="endpoint" required placeholder="http://127.0.0.1:动态端口/v1"></label><label>模型原名<input name="model" value="A" required></label><label>协议<select name="protocol"><option value="chat">Chat Completions</option><option value="responses">Responses</option><option value="anthropic">Anthropic Messages</option></select></label><label>样本预算<input name="samples" type="number" min="1" max="200" value="40" required></label><label>合成端点密钥（仅内存）<input name="key" type="password" autocomplete="off" required></label><button id="audit-start" type="submit">启动抽样</button><button id="audit-coverage" type="button">查看基线覆盖</button></form>
 <p id="audit-message" role="status">正在发现候选引擎能力…</p><div id="audit-history"></div><details><summary>基线覆盖与条件</summary><pre id="audit-baselines"></pre></details>`;
 host.append(area);
 const get=id=>document.getElementById(id);let capabilities=null, busy=false;
 async function request(path,body){const r=await fetch('/api/audit/v1/'+path,{headers:{...headers,'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});const d=await r.json();if(!r.ok)throw new Error(d.error||'HTTP '+r.status);return d.data;}
 function message(s){get('audit-message').textContent=s;}
 async function coverage(){get('audit-baselines').textContent=JSON.stringify(await request('coverage'),null,2);}
 async function refresh(){
  if(busy)return;busy=true;
  try{const data=await request('jobs');const box=get('audit-history');box.replaceChildren();
   for(const job of data.jobs.slice().reverse()){
    const card=document.createElement('article');const title=document.createElement('h3');title.textContent=`${job.verdict} · ${job.state} · ${job.declaration}`;card.append(title);
    const meta=document.createElement('p');meta.textContent=`${job.target.endpoint} | ${job.target.model} | ${job.target.protocol} | ${job.valid_samples}/${job.target.samples} 样本 | ${job.errors} 错误 | ${job.skipped} 跳过 | ${job.updated_at}`;card.append(meta);
    if(['queued','running','cancelling'].includes(job.state)){const cancel=document.createElement('button');cancel.textContent='取消抽样';cancel.onclick=async()=>{try{await request('jobs/'+job.id+'/cancel',{});message('已请求取消；等待在途请求结束。');await refresh();}catch(e){message(e.message);}};card.append(cancel);}
    const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='绑定条件、基线哈希与完整结果';const pre=document.createElement('pre');pre.textContent=JSON.stringify(job,null,2);details.append(summary,pre);card.append(details);box.append(card);
   }
  }catch(e){message('状态读取失败：'+e.message+'；当前界面不能表示引擎仍可用。');}finally{busy=false;}
 }
 get('audit-form').onsubmit=async e=>{e.preventDefault();const f=e.target;const key=f.elements.key.value;f.elements.key.value='';
  try{if(!capabilities)throw new Error('引擎能力未就绪');const p=f.elements.protocol.value;
   const body={spec:{endpoint:f.elements.endpoint.value,model:f.elements.model.value,protocol:p,samples:Number(f.elements.samples.value),timeout:1,token_budget:60000,conditions:capabilities.conditions[p]},api_key:key};
   await request('jobs',body);message('抽样已提交。5 秒刷新只读取状态，不派发探针。');await refresh();
  }catch(error){message('提交失败：'+error.message);}
 };
 get('audit-coverage').onclick=()=>coverage().catch(e=>message(e.message));
 request('capabilities').then(async c=>{capabilities=c;message('引擎已连接；仅显式启动会发探针。基线必须来自独立可信来源。');await coverage();await refresh();}).catch(e=>message('引擎不可用：'+e.message));
 setInterval(refresh,5000);
})();
