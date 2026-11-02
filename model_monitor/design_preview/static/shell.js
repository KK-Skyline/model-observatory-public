'use strict';
// Connect navigation to the original filters; comparison remains in the existing API.
// 将导航接到原筛选器；一致性判断仍由原接口负责。
const viewNames={all:'全部对话',pending:'待验证',mismatch:'不一致',archived:'含归档对话'};
function updateShell(){
 const current=$('archived').checked?'archived':$('state').value==='待验证'?'pending':$('state').value==='不一致'?'mismatch':'all';
 document.querySelectorAll('[data-view]').forEach(b=>{
  if($('diagnostic-page').hidden&&b.dataset.view===current)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');
 });
 if($('diagnostic-page').hidden)$('diagnostic-open').removeAttribute('aria-current');else $('diagnostic-open').setAttribute('aria-current','page');
 $('view-title').textContent=$('diagnostic-page').hidden?viewNames[current]:'异常详情';
 const connection=$('connection').textContent;
 document.querySelector('.connection-strip').classList.toggle('interrupted',connection.startsWith('监控中断'));
 document.querySelector('.connection-strip').classList.toggle('connected',connection.includes('正常')||connection.startsWith('合成演示'));
}
for(const button of document.querySelectorAll('[data-view]'))button.addEventListener('click',()=>{
 closeDiagnostics();
 $('state').value=button.dataset.view==='pending'?'待验证':button.dataset.view==='mismatch'?'不一致':'';
 $('archived').checked=button.dataset.view==='archived';
 render();updateShell();
});
for(const id of ['state','archived'])$(id).addEventListener('change',updateShell);
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'&&!$('wallpaper-dialog').open){e.preventDefault();$('search').focus();}});
$('rows').addEventListener('click',e=>{if(e.target.closest('button')){$('detail').focus({preventScroll:true});updateShell();}});
$('detail-close').addEventListener('click',()=>{$('search').focus({preventScroll:true});updateShell();});
const shellObserver=new MutationObserver(updateShell);
shellObserver.observe($('connection'),{childList:true,subtree:true,characterData:true});
const header=document.querySelector('.titlebar');
addEventListener('scroll',()=>header.classList.toggle('scrolled',scrollY>8),{passive:true});
updateShell();
