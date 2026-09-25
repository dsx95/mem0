const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {me:null, users:[], tab:'longterm', page:1, epoch:0, relationship:'family', items:[], busy:false};
const types = {builtin:['内置资料','让资料，成为随时可用的知识。','说明书、参考资料，以及家庭和设备的预置知识。','共享资料可以对自己隐藏；有管理权限的记录可以删除。'],longterm:['长期记忆','值得记住的，都在这里。','查看你的偏好、约定与家庭共享记忆，决定留下什么。','删除长期记忆后，不会再被检索；原始对话可另行删除；资料源文件暂时保留。'],conversations:['短期与对话','每一段对话，都有来处。','只有你能查看自己的原始对话，不因家庭或设备共享而公开。','模型短期上下文使用最近 12 轮已完成对话（最多 32000 字符）；这里可管理完整记录。'],diaries:['每日记事','把日常，留在时间里。','按北京时间归档对话与当天的新偏好。','删除日记会同时删除对应原始对话轮次，避免再次回填；独立长期记忆会保留。']};
const date = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '时间未知';
const statusName = value => ({complete:'已完成',completed:'已完成',running:'回复中',failed:'失败',cancelled:'已停止',interrupted:'已中断'}[value] || value);
const familyName = id => state.me?.families.find(f=>f.family_id===id)?.name || id || '个人空间';
const deviceName = id => state.me?.devices.find(d=>d.device_id===id)?.name || id || '未标记设备';
function notify(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(notify.timer);notify.timer=setTimeout(()=>$('toast').hidden=true,4200);}
function failure(error){$('alert').textContent=error.message;$('alert').hidden=false;notify(error.message);}
async function api(path,options={}){
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json','X-Memory-Client':'dashboard',...(state.me?{'X-Memory-User':state.me.user_id}:{}),...options.headers}});
  const data=await response.json().catch(()=>({}));
  if(!response.ok){if(response.status===401){state.me=null;await chooseUser();}throw new Error(typeof data.detail==='string'?data.detail:`请求失败（${response.status}）`);}
  return data;
}
const post=(path,body)=>api(path,{method:'POST',body:JSON.stringify(body)});
function options(select,rows,initial){select.replaceChildren(...initial.map(([value,label])=>new Option(label,value)),...rows.map(([value,label])=>new Option(label,value)));}
function filterDevices(){
  const old=$('device-filter').value,family=$('family-filter').value;
  options($('device-filter'),state.me.devices.filter(d=>!family||d.family_id===(family==='__none__'?'':family)).map(d=>[d.device_id,d.name]),[['','全部设备'],['__none__','未标记设备']]);
  if([...$('device-filter').options].some(o=>o.value===old))$('device-filter').value=old;
}
function renderProfile(){
  $('profile-name').textContent=state.me.name;$('profile-id').textContent=state.me.user_id+' · 无密码模式';$('avatar').textContent=state.me.name.slice(0,1);
  options($('family-filter'),state.me.families.map(f=>[f.family_id,f.name]),[['','全部家庭与个人空间'],['__none__','个人空间 / 无家庭']]);filterDevices();
  localStorage.setItem('knowin-chat-user',state.me.user_id);
}
async function chooseUser(){
  const result=await api('/api/identity/users');state.users=result.items;
  $('user-list').innerHTML=result.items.map(u=>`<button data-user="${esc(u.user_id)}">${esc(u.name)}<small>${esc(u.user_id)}</small></button>`).join('');
  if(!$('users-dialog').open)$('users-dialog').showModal();
}
async function selectUser(id){
  state.epoch++;state.items=[];$('records').replaceChildren();$('job-list').replaceChildren();$('device-filter').value='';$('detail-dialog').close();
  state.me=await post('/api/identity/select',{user_id:id});state.page=1;renderProfile();$('users-dialog').close();$('query').value='';$('include-hidden').checked=false;
  await load();
}
function tags(item){return `<div class="tags"><span class="tag ${item.scope==='family'?'shared':item.scope==='public'?'public':''}">${item.scope==='public'?'公共内置':item.scope==='family'?'家庭共享':'仅自己'}</span><span class="tag">${esc(familyName(item.family_id))}</span><span class="tag">${esc(deviceName(item.device_id))}</span>${item.hidden?'<span class="tag">已对我隐藏</span>':''}</div>`;}
function memoryCard(item){return `<article class="card ${item.hidden?'hidden-record':''}">${tags(item)}<p class="preview">${esc(item.memory)}</p><small>${item.metadata.source_file?esc(item.metadata.source_file)+' · ':''}${date(item.created_at)}${item.updated_at&&item.updated_at!==item.created_at?' · 更新 '+date(item.updated_at):''}</small><div class="card-actions"><button data-detail="${esc(item.id)}" ${item.hidden?'disabled':''}>查看详情</button><button data-hide="${esc(item.id)}" data-hidden="${!item.hidden}">${item.hidden?'恢复显示':'对我隐藏'}</button>${item.can_delete?`<button class="danger" data-delete-memory="${esc(item.id)}">删除</button>`:''}</div></article>`;}
function conversationCard(item){return `<article class="card">${tags({...item,scope:'personal'})}<h3>${esc(item.title)}</h3><small>${item.turn_count} 轮对话 · 更新 ${date(item.updated_at)}</small><div class="card-actions"><button data-conversation="${esc(item.id)}">查看记录</button><a href="/chat?session=${encodeURIComponent(item.id)}">继续对话 ↗</a><button class="danger" data-delete-session="${esc(item.id)}">删除对话</button></div></article>`;}
function diaryCard(item,index){return `<article class="card"><div class="tags"><span class="tag">私人日记</span><span class="tag">${esc(familyName(item.family_id))}</span>${item.device_ids.map(d=>`<span class="tag">${esc(deviceName(d))}</span>`).join('')}</div><h3>${esc(item.date)}</h3><p class="preview">${esc(item.summary|| (item.device_filtered?'已筛选设备。请查看该设备的原始对话。':'完整对话已保存；日记尚未整理。'))}</p><small>${item.turn_count} 轮对话 · 北京时间</small><div class="card-actions"><button data-diary="${index}">查看日记</button><button class="danger" data-delete-diary="${index}">删除当日记录</button></div></article>`;}
async function load(){
  if(!state.me)return;
  const epoch=++state.epoch,tab=state.tab;const type=types[tab];
  $('breadcrumb').textContent=type[0];$('title').textContent=type[1];$('subtitle').textContent=type[2];$('type-hint').textContent=type[3];$('alert').hidden=true;
  $('add-record').hidden=!['builtin','longterm'].includes(tab);$('add-record').textContent=tab==='builtin'?'＋ 添加资料':'＋ 添加记忆';$('hidden-control').hidden=!['builtin','longterm'].includes(tab);$('imports').hidden=tab!=='builtin';$('query').disabled=tab==='diaries';
  document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  $('records').innerHTML='<div class="empty">正在读取你的记录…</div>';
  const params=new URLSearchParams({family_id:$('family-filter').value,device_id:$('device-filter').value,page:state.page,q:$('query').value.trim()});
  let endpoint='/api/manage/'+(tab==='conversations'?'conversations':tab==='diaries'?'diaries':'memories');
  if(['builtin','longterm'].includes(tab)){params.set('memory_type',tab);params.set('include_hidden',$('include-hidden').checked);}
  try{
    const data=await api(endpoint+'?'+params);if(epoch!==state.epoch)return;
    if(state.page>1&&!data.items.length){state.page--;return load();}
    state.items=data.items;$('result-count').textContent=data.total;$('scope-caption').textContent=' · '+state.me.name+'可见';
    $('records').innerHTML=data.items.length?data.items.map(tab==='conversations'?conversationCard:tab==='diaries'?diaryCard:memoryCard).join(''):'<div class="empty">这里还没有记录。<br>试试切换家庭或设备，或添加一条属于你的记忆。</div>';
    $('page-label').textContent=`第 ${state.page} / ${Math.max(1,Math.ceil(data.total/20))} 页`;$('previous').disabled=state.page===1;$('next').disabled=state.page*20>=data.total;
    if(tab==='builtin')await jobs();
  }catch(e){if(epoch===state.epoch){$('records').innerHTML='<div class="empty">读取失败，请重试。</div>';failure(e);}}
}
async function jobs(){
  const epoch=state.epoch,data=await api('/api/jobs');if(epoch!==state.epoch)return;
  $('job-list').innerHTML=data.items.slice(0,20).map(j=>`<div class="job">${esc(j.filename)} · ${esc(j.message)} ${j.error?' · '+esc(j.error):''}${j.status==='failed'?`<button data-retry="${esc(j.id)}">重试</button>`:''}</div>`).join('')||'<p class="muted">暂无资料导入任务。</p>';
}
async function detail(id){
  const epoch=state.epoch,data=await api('/api/memories/'+encodeURIComponent(id));if(epoch!==state.epoch)return;
  $('detail-title').textContent='记忆详情';$('detail-content').innerHTML=`${tags(data)}<div class="detail-text">${esc(data.memory)}</div><p>创建 ${date(data.created_at)} · 更新 ${date(data.updated_at)}</p>${data.metadata.source_file?`<p>来源：${esc(data.metadata.source_file)}</p><a href="/api/memories/${encodeURIComponent(id)}/file?download=true" target="_blank" rel="noopener">下载源文件 ↗</a>`:''}<p>记忆 ID：${esc(id)}</p>`;$('detail-dialog').showModal();
}
async function conversation(id){
  const epoch=state.epoch,data=await api('/api/chat/sessions/'+encodeURIComponent(id));if(epoch!==state.epoch)return;
  $('detail-title').textContent=data.title;
  $('detail-content').innerHTML=`<p>${esc(familyName(data.family_id))} · ${esc(deviceName(data.device_id))} · 仅自己可见</p>`+data.turns.map(t=>`<article class="detail-turn"><small>${date(t.created_at)} · ${esc(statusName(t.status))}</small><p><strong>你</strong></p><div class="detail-text">${esc(t.user_text)}</div><p><strong>助手</strong></p><div class="detail-text">${esc(t.answer||'暂无完整回答')}</div><button class="danger" data-delete-turn="${esc(t.id)}" data-session="${esc(id)}">删除这一轮及日记副本</button></article>`).join('');
  if(!$('detail-dialog').open)$('detail-dialog').showModal();
}
function diaryURL(item){const params=new URLSearchParams({date:item.date});const device=$('device-filter').value;if(device)params.set('device_id',device==='__none__'?'':device);return '/api/chat/sessions/'+encodeURIComponent(item.session_id)+'/diary?'+params;}
async function diary(index){
  const item=state.items[index],epoch=state.epoch,data=await api(diaryURL(item));if(epoch!==state.epoch)return;
  $('detail-title').textContent=data.date+' · 日记';
  $('detail-content').innerHTML=(data.summary?'<div class="detail-text">'+esc(data.summary)+'</div>':'<p>以下为完整原始记录。</p>')+data.entries.map(e=>`<article class="detail-turn"><small>${date(e.created_at)} · ${esc(deviceName(e.device_id))}</small><p><strong>你</strong></p><div class="detail-text">${esc(e.user_text)}</div><p><strong>助手</strong></p><div class="detail-text">${esc(e.answer)}</div></article>`).join('');$('detail-dialog').showModal();
}
async function relationships(){
  state.me=await api('/api/identity/me');
  $('relationship-list').innerHTML=state.me.families.map(f=>`<div class="relationship"><strong>${esc(f.name)}</strong> · ${esc(f.family_id)}${f.owner_user_id===state.me.user_id?' · 我是创建者':''}<small>成员：${f.members.map(u=>esc(u.name)).join('、')}</small><small>设备：${state.me.devices.filter(d=>d.family_id===f.family_id).map(d=>esc(d.name)+' ('+esc(d.device_id)+')').join('、')||'暂无'}</small></div>`).join('')+`<div class="relationship"><strong>个人设备</strong><small>${state.me.devices.filter(d=>!d.family_id).map(d=>esc(d.name)+' ('+esc(d.device_id)+')').join('、')||'暂无个人设备'}</small></div>`;
  $('relationship-form').hidden=true;if(!$('relationships-dialog').open)$('relationships-dialog').showModal();
}
async function relationshipForm(kind){
  state.relationship=kind;$('relationship-form').reset();$('relationship-form').hidden=false;
  $('relationship-title').textContent={family:'新建家庭',device:'添加设备',member:'添加家庭成员'}[kind];
  $('relationship-family-label').hidden=kind==='family';$('relationship-id-label').hidden=kind==='member';$('relationship-id').required=kind!=='member';$('relationship-name-label').hidden=kind==='member';$('relationship-user-label').hidden=kind!=='member';
  options($('relationship-family'),state.me.families.filter(f=>f.owner_user_id===state.me.user_id).map(f=>[f.family_id,f.name]),kind==='device'?[['','个人设备']]:[]);
  if(kind==='member'){const data=await api('/api/identity/users');options($('relationship-user'),data.items.filter(u=>u.user_id!==state.me.user_id).map(u=>[u.user_id,u.name+' ('+u.user_id+')']),[]);}
}
function recordDevices(){const family=$('record-family').value;options($('record-device'),state.me.devices.filter(d=>d.family_id===family).map(d=>[d.device_id,d.name]),[['','未标记设备']]);const option=$('record-scope').querySelector('[value="family"]');option.disabled=!family;if(!family)$('record-scope').value='personal';}
function newRecord(){
  $('record-form').reset();$('record-title').textContent=state.tab==='builtin'?'添加内置资料':'添加长期记忆';$('file-label').hidden=state.tab!=='builtin';
  options($('record-family'),state.me.families.map(f=>[f.family_id,f.name]),[['','个人空间']]);
  const family=$('family-filter').value;if(family&&family!=='__none__')$('record-family').value=family;recordDevices();const device=$('device-filter').value;if(device&&device!=='__none__')$('record-device').value=device;
  $('record-dialog').showModal();
}
$('new-user-form').onsubmit=async e=>{e.preventDefault();try{const body=Object.fromEntries(new FormData(e.target));await post('/api/identity/users',body);await selectUser(body.user_id);e.target.reset();}catch(e){failure(e);}};
$('relationship-form').onsubmit=async e=>{e.preventDefault();try{const kind=state.relationship,id=$('relationship-id').value.trim(),name=$('relationship-name').value.trim(),family=$('relationship-family').value;const body=kind==='family'?{family_id:id,name}:kind==='device'?{device_id:id,name,family_id:family}:{user_id:$('relationship-user').value};await post(kind==='member'?'/api/identity/families/'+encodeURIComponent(family)+'/members':'/api/identity/'+(kind==='family'?'families':'devices'),body);await relationships();renderProfile();await load();notify('已保存');}catch(e){failure(e);}};
$('record-form').onsubmit=async e=>{
  e.preventDefault();if(state.busy)return;state.busy=true;$('save-record').disabled=true;
  try{const context={family_id:$('record-family').value,device_id:$('record-device').value,scope:$('record-scope').value};const file=$('record-file').files[0];
    if(state.tab==='builtin'&&file){await api('/api/uploads?'+new URLSearchParams({...context,filename:file.name}),{method:'POST',body:file,headers:{'Content-Type':'application/octet-stream'}});notify('已上传，正在后台导入');}
    else{await post('/api/manage/notes',{...context,text:$('record-text').value.trim(),memory_type:state.tab});notify('已保存');}
    $('record-dialog').close();await load();
  }catch(e){failure(e);}finally{state.busy=false;$('save-record').disabled=false;}
};
$('record-dialog').addEventListener('cancel',e=>{if(state.busy)e.preventDefault();});
$('record-family').onchange=recordDevices;
$('switch-user').onclick=()=>chooseUser().catch(failure);$('relationships').onclick=()=>relationships().catch(failure);
$('new-family').onclick=()=>relationshipForm('family');$('new-device').onclick=()=>relationshipForm('device');$('add-member').onclick=()=>relationshipForm('member').catch(failure);
$('add-record').onclick=newRecord;$('refresh').onclick=load;
$('family-filter').onchange=()=>{filterDevices();state.page=1;load();};$('device-filter').onchange=$('include-hidden').onchange=()=>{state.page=1;load();};
$('search-form').onsubmit=e=>{e.preventDefault();state.page=1;load();};$('previous').onclick=()=>{state.page--;load();};$('next').onclick=()=>{state.page++;load();};
document.addEventListener('click',async e=>{
  const button=e.target.closest('button');if(!button)return;
  try{
    if(button.dataset.close){if(state.busy&&button.dataset.close==='record-dialog')return;$(button.dataset.close).close();}
    if(button.dataset.user)await selectUser(button.dataset.user);
    if(button.dataset.tab){state.tab=button.dataset.tab;state.page=1;$('query').value='';await load();}
    if(button.dataset.detail)await detail(button.dataset.detail);
    if(button.dataset.conversation)await conversation(button.dataset.conversation);
    if(button.dataset.diary!==undefined)await diary(Number(button.dataset.diary));
    if(button.dataset.hide){await post('/api/memories/'+encodeURIComponent(button.dataset.hide)+'/visibility',{hidden:button.dataset.hidden==='true'});await load();}
    if(button.dataset.deleteMemory&&confirm('删除这条记忆及其修改历史？删除后不可撤销。原始对话可在“短期与对话”中另行删除；资料源文件仍保留。')){button.disabled=true;await api('/api/memories/'+encodeURIComponent(button.dataset.deleteMemory),{method:'DELETE'});await load();notify('记忆已删除');}
    if(button.dataset.deleteSession&&confirm('删除整段对话及对应日记副本？删除后不可撤销。已独立保存的长期记忆仍保留。')){button.disabled=true;await api('/api/chat/sessions/'+encodeURIComponent(button.dataset.deleteSession),{method:'DELETE'});await load();notify('对话及日记副本已删除');}
    if(button.dataset.deleteTurn&&confirm('删除这一轮原始对话及日记副本？长期记忆仍保留。')){await api('/api/chat/sessions/'+encodeURIComponent(button.dataset.session)+'/turns/'+encodeURIComponent(button.dataset.deleteTurn),{method:'DELETE'});await conversation(button.dataset.session);await load();notify('这一轮对话已删除');}
    if(button.dataset.deleteDiary!==undefined&&confirm('删除筛选范围内当天的日记和对应原始对话？长期记忆仍保留；此操作不可撤销。')){const item=state.items[Number(button.dataset.deleteDiary)];await api(diaryURL(item),{method:'DELETE'});await load();notify('当日记录已删除');}
    if(button.dataset.retry){await post('/api/jobs/'+encodeURIComponent(button.dataset.retry)+'/retry',{});await jobs();}
  }catch(e){button.disabled=false;failure(e);}
});
let polling=false;setInterval(async()=>{if(document.hidden||polling||state.tab!=='builtin'||!state.me)return;polling=true;try{await jobs();}catch{}finally{polling=false;}},4000);
try{const response=await fetch('/api/identity/me');if(response.ok){state.me=await response.json();renderProfile();await load();if(new URLSearchParams(location.search).has("users"))await chooseUser();}else await chooseUser();}catch(e){failure(e);}
