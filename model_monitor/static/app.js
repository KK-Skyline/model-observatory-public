'use strict';
const $=id=>document.getElementById(id);
const auth=location.hash.slice(1);
if(auth) sessionStorage.setItem('model-monitor-session',auth);
history.replaceState(null,'',location.pathname);
const headers={Authorization:'Bearer '+(auth||sessionStorage.getItem('model-monitor-session')||'')};
let snapshot=null, selected=null, demo=false, refreshing=false, lastFetch=null, wallpaperBusy=false, settingsError='', readError='', diagnosticOrigin=null, diagnosticScroll=0;
async function api(path,body){
 const response=await fetch(path,{headers:{...headers,'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),signal:AbortSignal.timeout(15000)});
 const data=await response.json(); if(!response.ok)throw new Error(data.error||'HTTP '+response.status); return data;
}
function text(tag,value,cls){const e=document.createElement(tag);e.textContent=value??'未知';if(cls)e.className=cls;return e;}
function badge(value){return text('span',value,'badge '+({'一致':'good','不一致':'bad','超时':'timeout'}[value]||'unknown'));}
function date(ms){return ms?new Date(ms).toLocaleString('zh-CN',{hour12:false}):'无记录';}
function status(t){return readError?'待验证':t.records[0]?.[$('layer').value]||'待验证';}
function defaultIgnored(t){return t.ignore_default??t.ignore_connection_failure;}
function render(){
 if(!snapshot)return;
 const ignored=snapshot.threads.filter(defaultIgnored);
 const all=$('include-disconnected')?.checked?snapshot.threads:snapshot.threads.filter(t=>!defaultIgnored(t));
 if(selected&&!all.some(t=>t.id===selected))selected=null;
 const unavailable=!demo&&!all.some(t=>t.records.some(r=>r.upstream_model));$('upstream-evidence').hidden=!unavailable;$('upstream-evidence').textContent='上游取证未完成：现有原生记录未保存独立上游模型字段，不能据此回答一致或不一致。待验证不是正在等待刷新；需从真实响应采集 model / openai-model 与稳定请求 ID。';
 $('total').textContent=all.length;
 for(const [id,s] of [['matched','一致'],['mismatch','不一致'],['pending','待验证']]) $(''+id).textContent=all.filter(t=>(t.records[0]?.upstream_status||'待验证')===s).length;
 const old=$('project').value;const projects=[...new Set(all.map(t=>t.project))].sort();
 $('project').replaceChildren(new Option('所有项目',''),...projects.map(p=>new Option(p,p)));$('project').value=projects.includes(old)?old:'';
 const q=$('search').value.trim().toLowerCase();
 const visible=all.filter(t=>($('archived').checked||!t.archived)&&(!$('project').value||t.project===$('project').value)&&(!$('state').value||status(t)===$('state').value)&&(!q||JSON.stringify(t).toLowerCase().includes(q)));
 $('rows').replaceChildren();
 for(const t of visible){
  const r=t.records[0],tr=document.createElement('tr'),name=document.createElement('td'),button=text('button',t.name+(t.archived?'（归档）':''));
  button.onclick=()=>{selected=t.id;renderDetail();$('detail').scrollIntoView({behavior:'smooth',block:'nearest'});};
  name.append(button,text('small',t.project));
  if(t.execution_state==='timeout')name.append(badge('超时'));else if(t.timeout_history_count)name.append(text('small',`历史超时 ${t.timeout_history_count} 次`,'timeout-history')); name.title=t.project_source||'合成演示';tr.append(name);
  if(t.ignore_cancelled_without_response)name.append(text('small','已取消 · 无完成或部分回包'));
  else if(t.connection&&['failed','reconnecting'].includes(t.connection.state))name.append(text('small',(t.connection.state==='reconnecting'?'正在重连':'连接失败')+` · ${t.connection.reconnect_count} 次建连重试 · ${t.connection.failed_attempts} 次失败调用`));
  for(const key of ['selected_model','request_model','upstream_model'])tr.append(text('td',r?.[key]));
  for(const key of ['request_status','upstream_status']){const td=document.createElement('td');td.append(badge(r?.[key]||'待验证'));td.title=(r?.[key==='request_status'?'request_reason':'upstream_reason']||'没有请求记录')+(key==='upstream_status'&&r?.evidence_diagnosis?.labels.length?'；'+r.evidence_diagnosis.labels.join('；'):'');tr.append(td);}
  tr.append(text('td',date(r?.updated_at)));$('rows').append(tr);
 }
 if(!visible.length){const td=text('td','没有符合条件的对话');td.colSpan=7;const tr=document.createElement('tr');tr.append(td);$('rows').append(tr);}
 $('count').textContent=`显示 ${visible.length} / ${all.length} 个对话 · ${$('include-disconnected')?.checked?'已恢复显示':'默认忽略'} ${ignored.length} 个（连接失败 ${ignored.filter(t=>t.ignore_connection_failure).length} / 已取消无响应 ${ignored.filter(t=>t.ignore_cancelled_without_response).length}） · 历史与告警保留`;
 updateDiagnostics();
 $('demo-banner').hidden=!demo;$('demo').textContent=demo?'返回真实数据':'演示三态（合成）';
 renderDetail();
 if(readError)markInterrupted();
}
function renderDetail(){
 const t=snapshot?.threads.find(t=>t.id===selected);$('detail').hidden=!t;if(!t)return;
 $('detail-interruption').hidden=!readError;
 const opened=new Set([...document.querySelectorAll('#history details[open]')].map(d=>d.dataset.id));
 $('detail-title').textContent=t.name+' / '+t.project+'（'+(t.project_source||'合成演示')+'）';$('history').replaceChildren();
 if(!t.records.length)$('history').append(text('p','没有原生请求记录；该轮选择和上游均待验证。'));
 for(const r of t.records){const d=document.createElement('details');d.className='history-entry';d.dataset.id=r.id;d.open=opened.has(r.id);const s=text('summary',date(r.updated_at)+' · '+(r.selected_model||'未知')+' → '+(r.request_model||'未知')+' → '+(r.upstream_model||'未知')+'　');s.append(badge(readError?'待验证':r.upstream_status));if(r.execution_state==='timeout')s.append(badge('超时'));d.append(s,text('pre',JSON.stringify(r,null,2)));$('history').append(d);}
}
async function refresh(){
 if(refreshing)return;refreshing=true;
 try{snapshot=await api(demo?'/api/demo':'/api/data');lastFetch=Date.now();readError='';render();$('connection').textContent=(demo?'合成演示':'本机连接正常')+' · 读取于 '+date(snapshot.scanned_at)+' · 最后完整读取 '+date(snapshot.last_success)+' · 5 秒刷新';}
 catch(e){readError=e.message;markInterrupted();updateDiagnostics();}
 finally{refreshing=false;}
}
function diagnosticWarnings(){return [...(snapshot?.alerts||[]),...(settingsError?[settingsError]:[]),...(readError?[readError+'；保留上次画面，旧结果不能代表当前状态。']:[])];}
function updateDiagnostics(){
 const warnings=diagnosticWarnings(),button=$('diagnostic-toggle');
 button.hidden=!warnings.length;button.textContent=`异常 · ${warnings.length} 类`;button.setAttribute('aria-label',`查看异常详情，${warnings.length} 类告警，含历史累计`);
 $('warning').hidden=!readError&&!settingsError;$('warning').textContent=readError?'读取中断 · 查看异常详情':settingsError?'设置异常 · 查看详情':'';
 $('diagnostic-status').textContent=(readError?'读取中断，旧结果仅供追查。':'历史告警持续保留，不等于当前请求失败。')+' 最后完整读取：'+date(snapshot?.last_success);
 $('diagnostic-alerts').replaceChildren(...(warnings.length?warnings:['当前没有告警']).map(value=>text('li',value)));
 const pending=snapshot?.pending_diagnostics,latest=pending?.latest_default;
 $('pending-summary').textContent=latest?`${latest.records} 条最近推理中 ${latest.pending} 条待验证；另有 ${latest.unrecorded_threads} 个对话无请求记录。`:'暂无原因统计';
 $('pending-breakdown').replaceChildren();
 for(const [code,count] of Object.entries(latest?.blockers||{}))$('pending-breakdown').append(text('dt',pending.labels[code]||code),text('dd',count));
 $('timeout-events').replaceChildren();
 for(const item of snapshot?.timeout_diagnostics||[]){
  const row=document.createElement('li');row.append(badge('超时'),text('span',` ${date(item.at)} · ${item.transport||'未知传输'} / ${item.stage||'未知阶段'} · ${item.wait_limit_seconds??'未知'} 秒无数据 · 线程 ${item.thread_id||'未记录'} · 连接 ${item.connection_id||'未记录'}`));$('timeout-events').append(row);
 }
 if(!$('timeout-events').children.length)$('timeout-events').append(text('li','暂无带来源的上游超时事件；历史累计仍以采集器诊断为准。'));
 $('project-breakdown').replaceChildren();
 for(const group of pending?.project_readings||[]){
  const reasons=Object.entries(group.blockers).map(([code,count])=>(pending.labels[code]||code)+' '+count).join(' / ');
  $('project-breakdown').append(text('dt',group.project),text('dd',`${(group.selection_read_ms/1000).toFixed(3)} 秒 · ${group.selection_files} 文件 · ${group.pending_threads}/${group.threads} 待验证`));
  if(reasons)$('project-breakdown').lastChild.title=reasons;
 }
 $('diagnostic-title').textContent=`完整诊断元数据 · 读取异常 ${snapshot?.error_count??'未知'} 条`;
 $('diagnostics').textContent=JSON.stringify({errors:snapshot?.errors,collector_status:snapshot?.collector_status,connection_visibility:snapshot?.connection_visibility,unmatched_captures:snapshot?.unmatched_captures,evidence_gaps:snapshot?.gaps,timeout_diagnostics:snapshot?.timeout_diagnostics,pending_diagnostics:pending,scan_timing:snapshot?.scan_timing,unrecorded_threads:snapshot?.unrecorded_threads,issues:snapshot?.issues,read_error:readError||null,last_success:date(snapshot?.last_success),scanned_at:date(snapshot?.scanned_at)},null,2);
}
function openDiagnostics(event){
 if($('diagnostic-page').hidden){diagnosticOrigin=event?.currentTarget||document.activeElement;diagnosticScroll=scrollY;}
 $('diagnostic-page').hidden=false;$('monitor-page').hidden=true;$('diagnostic-page').focus({preventScroll:true});scrollTo({top:0,behavior:'instant'});
 if(typeof updateShell==='function')updateShell();
}
function closeDiagnostics(){
 if($('diagnostic-page').hidden)return;
 $('diagnostic-page').hidden=true;$('monitor-page').hidden=false;
 diagnosticOrigin?.focus({preventScroll:true});scrollTo({top:diagnosticScroll,behavior:'instant'});
 if(typeof updateShell==='function')updateShell();
}
function markInterrupted(){
 $('detail-interruption').hidden=false;
 for(const b of document.querySelectorAll('#rows .badge:not(.timeout), #history .badge:not(.timeout)')){b.textContent='待验证';b.className='badge unknown';b.title='监控中断；上次结果仅供历史参考';}
 for(const id of ['matched','mismatch','pending'])$(id).textContent='—';
 $('connection').textContent='监控中断 · 最后收到数据 '+date(lastFetch);
}
for(const id of ['diagnostic-toggle','diagnostic-open','warning'])$(id)?.addEventListener('click',openDiagnostics);
$('diagnostic-back').addEventListener('click',closeDiagnostics);
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('diagnostic-page').hidden&&!$('wallpaper-dialog').open){closeDiagnostics();}});
let pendingImport=null,previewURL=null,backgroundURL=null;
async function loadWallpaper(name){
 if(name.startsWith('import-')){
  const response=await fetch('/api/wallpaper?id='+encodeURIComponent(name),{headers});
  if(!response.ok)throw new Error('已导入壁纸读取失败；保留上一张');
  const blob=await response.blob(),url=URL.createObjectURL(blob);
  try{await decodeImage(url);}catch(e){URL.revokeObjectURL(url);throw e;}
  return url;
 }
 if(name!=='default')await decodeImage('/'+name+'.svg');
 return null;
}
function decodeImage(url){return new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>{if(img.naturalWidth*img.naturalHeight>40000000)reject(new Error('图片尺寸过大，最多 4000 万像素'));else resolve();};img.onerror=()=>reject(new Error('图片无法解码；保留上一张'));img.src=url;});}
function showWallpaper(name,url){
 const old=backgroundURL;backgroundURL=url;
 $('imported-background').hidden=!url;if(url)$('imported-background').src=url;
 document.body.dataset.wallpaper=name;if(old)URL.revokeObjectURL(old);
}
$('wallpaper-file').onchange=async()=>{
 pendingImport=null;$('import-apply').disabled=true;
 try{
  const file=$('wallpaper-file').files[0];if(!file)return;
  if(!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size>8*1024*1024)throw new Error('请选择小于 8 MiB 的 PNG / JPEG / WebP');
  const url=URL.createObjectURL(file);try{await decodeImage(url);}catch(e){URL.revokeObjectURL(url);throw e;}
  if(previewURL)URL.revokeObjectURL(previewURL);previewURL=url;
  $('import-preview').src=url;$('import-preview').hidden=false;pendingImport=file;$('import-apply').disabled=false;$('wallpaper-status').textContent='预览已就绪，点击应用后保存';
 }catch(e){$('wallpaper-status').textContent=e.message;}
};
$('import-apply').onclick=async()=>{
 if(wallpaperBusy||!pendingImport)return;wallpaperBusy=true;
 try{
  const response=await fetch('/api/wallpaper',{method:'POST',headers:{...headers,'Content-Type':pendingImport.type},body:pendingImport,signal:AbortSignal.timeout(15000)});
  const data=await response.json();if(!response.ok)throw new Error(data.error);
  const url=await loadWallpaper(data.wallpaper);
  try{await api('/api/settings',data);}catch(e){if(url)URL.revokeObjectURL(url);throw e;}
  showWallpaper(data.wallpaper,url);settingsError='';$('wallpaper-status').textContent='导入图片已保存并应用';
 }catch(e){$('wallpaper-status').textContent=e.message;}finally{wallpaperBusy=false;}
};
async function applyWallpaper(name){
 if(wallpaperBusy)return;wallpaperBusy=true;
 try{
  const url=await loadWallpaper(name);
  await api('/api/settings',{wallpaper:name});settingsError='';showWallpaper(name,url);$('wallpaper-status').textContent='已保存：'+name;
 }catch(e){$('wallpaper-status').textContent=e.message;}
 finally{wallpaperBusy=false;}
}
for(const id of ['search','project','state','layer','archived'])$(id).addEventListener(id==='search'?'input':'change',render);
$('include-disconnected')?.addEventListener('change',render);
$('refresh').onclick=refresh;$('detail-close').onclick=()=>{selected=null;renderDetail();};
$('demo').onclick=async()=>{if(refreshing)return;demo=!demo;selected=null;await refresh();};
$('wallpaper-open').onclick=()=>$('wallpaper-dialog').showModal();$('wallpaper-close').onclick=()=>$('wallpaper-dialog').close();
for(const b of document.querySelectorAll('[data-apply]'))b.onclick=()=>applyWallpaper(b.dataset.apply);
api('/api/settings').then(async s=>showWallpaper(s.wallpaper,await loadWallpaper(s.wallpaper))).catch(e=>{settingsError=e.message;$('wallpaper-status').textContent=e.message;updateDiagnostics();});
refresh();setInterval(refresh,5000);
