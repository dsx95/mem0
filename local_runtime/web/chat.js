const $ = id => document.getElementById(id);
const state = {user: localStorage.getItem('knowin-chat-user') || 'chat_default', family: localStorage.getItem('knowin-chat-family') || '', users: [], session: null, busy: false, epoch: 0, recovered: null, diaryStatus: '', diaryEpoch: 0};
const escape = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const timestamp = value => {if(!value)return '时间未知';const date=new Date(value);return Number.isNaN(date.getTime())?'时间未知':date.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});};
const diaryTimestamp = value => {if(!value)return '时间未知';const date=new Date(value);return Number.isNaN(date.getTime())?'时间未知':date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});};
const icon = name => `<svg aria-hidden="true"><use href="#${name}"/></svg>`;
const emptyMemories = $('personal-memories').innerHTML;
let activeTurn = null;
const sessionKey = () => 'knowin-chat-session-v2-'+JSON.stringify([state.user,state.family]);
const identityPayload = () => ({user_id:state.user,family_id:state.family});
function ensureUserOption(user){if(![...$('user-id').options].some(option=>option.value===user))$('user-id').add(new Option(user,user),$('user-id').querySelector('[value="__new_user__"]'));}
async function users(){
  const selected=$('user-id').value || state.user;const data=await api('/users');state.users=data.items;
  $('user-id').replaceChildren();$('known-users').replaceChildren();
  for(const item of data.items){
    const label=item.user_id+' · '+item.memory_count+' 条'+(item.read_only?' · 公开资料 / 只读':'');
    $('user-id').add(new Option(label,item.user_id));$('known-users').append(new Option(label,item.user_id));
  }
  ensureUserOption(state.user);ensureUserOption(selected);$('user-id').add(new Option('＋ 输入新用户 ID…','__new_user__'));$('user-id').value=selected;
}
function showIdentity(){
  ensureUserOption(state.user);
  $('user-id').value=state.user;$('family-id').value=state.family;
  $('identity-label').textContent=state.user+(state.family?' · '+state.family:'');
  const readOnly=state.user==='knowin_public';
  $('diary-button').disabled=readOnly;
  document.querySelector('.panel-heading h2').firstChild.textContent=readOnly?'公开资料 ':'个人与家庭记忆 ';
  $('identity-context').textContent=readOnly?'当前：knowin_public · 公开资料只读，保存记忆请切换个人用户':`当前：${state.user}${state.family?' / '+state.family:' / 无家庭'} · 身份会随每条消息发送`;
  $('family-id').disabled=state.busy || readOnly;$('remember-scope').disabled=state.busy || readOnly;$('library-toggle').disabled=state.busy || readOnly;
  const familyOption=$('remember-scope').querySelector('[value="family"]');familyOption.disabled=!state.family;
  if(!state.family)$('remember-scope').value='personal';
}
async function applyIdentity(){
  if(state.busy)return false;
  $('user-id').value=$('user-id').value.trim();$('family-id').value=$('family-id').value.trim();
  if(!$('user-id').reportValidity() || !$('family-id').reportValidity())return false;
  const user=$('user-id').value,family=$('family-id').value;
  if(user===state.user && family===state.family)return true;
  const previous={user:state.user,family:state.family,session:state.session};
  state.user=user;state.family=family;state.session=null;
  try{await newSession();localStorage.setItem('knowin-chat-user',user);localStorage.setItem('knowin-chat-family',family);showIdentity();return true;}
  catch(e){Object.assign(state,previous);showIdentity();throw e;}
}

async function api(path, options = {}) {
  const response = await fetch('/api/chat' + path, {...options, headers:{'Content-Type':'application/json','X-Memory-Client':'dashboard',...options.headers}});
  if (!response.ok) {const body = await response.json().catch(()=>({})); throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败（${response.status}）`);}
  return response.json();
}
function error(message = '') {$('error-banner').textContent = message; $('error-banner').hidden = !message;}
function scroll(force = false) {const el=$('conversation-scroll'); if(force || el.scrollHeight-el.scrollTop-el.clientHeight<220) el.scrollTop=el.scrollHeight;}
function inline(text) {return escape(text).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');}
function markdown(text) {
  return String(text).split(/(```[\s\S]*?```)/g).map(part=>{
    if(part.startsWith('```')) return '<pre><code>'+escape(part.replace(/^```[^\n]*\n?/, '').replace(/```$/, ''))+'</code></pre>';
    return part.split(/\n\s*\n/).filter(Boolean).map(block=>{
      if(/^#{1,4} /.test(block)) return '<h3>'+inline(block.replace(/^#{1,4} /,''))+'</h3>';
      const lines=block.split('\n');
      if(lines.every(line=>/^> ?/.test(line)))return '<blockquote>'+lines.map(line=>inline(line.replace(/^> ?/,''))).join('<br>')+'</blockquote>';
      if(lines.every(line=>/^[-*] /.test(line))) return '<ul>'+lines.map(line=>'<li>'+inline(line.slice(2))+'</li>').join('')+'</ul>';
      return '<p>'+lines.map(inline).join('<br>')+'</p>';
    }).join('');
  }).join('');
}
function busy(value) {
  state.busy=value; $('stop-button').hidden=!value; $('send-button').hidden=value;
  $('send-button').disabled=value || !$('message-input').value.trim(); $('reply-status').hidden=!value;
  $('library-toggle').disabled=value; $('identity-button').disabled=value; $('delete-chat').disabled=value;
  ['user-id','family-id','remember-scope','apply-identity'].forEach(id=>$(id).disabled=value);
  if(state.user==='knowin_public'){['family-id','remember-scope','library-toggle'].forEach(id=>$(id).disabled=true);}
}
function closePanels(){document.body.classList.remove('show-sidebar','show-memory');$('shade').hidden=true;}
async function sessions(){
  const data=await api('/sessions?'+new URLSearchParams(identityPayload()));
  $('session-count').textContent=data.items.length;
  $('session-list').replaceChildren();
  if(!data.items.length){$('session-list').innerHTML='<p class="sidebar-empty">每次对话，都从这里继续。</p>';return;}
  for(const item of data.items){const button=document.createElement('button');button.textContent=item.title;button.title=item.title;button.classList.toggle('active',state.session?.id===item.id);button.onclick=()=>loadSession(item.id).catch(e=>error(e.message));$('session-list').append(button);}
}
function toolCard(event){
  const details=document.createElement('details'); details.className='tool-card '+event.status; details.dataset.call=event.id;
  const action=event.arguments?.action, remember=action==='remember';
  const name=action==='diary'?'查看每日记事':remember?(event.result?.scope==='family'?'保存家庭共享记忆':'保存一条记忆'):event.arguments?.scope==='library'?'检索资料原文':'查找相关记忆';
  const count=event.result?.memories?.length;
  const caption=event.status==='running'?'进行中':event.status==='error'?'未完成':action==='diary'?`${event.result?.turn_count ?? 0} 轮对话`:remember?'已保存':`找到 ${count ?? 0} 条`;
  const ranking=event.result?.rerank?.status,rankingCaption=ranking==='applied'?' · 已重排':ranking==='fallback'?' · 重排失败，使用原排序':'';
  details.innerHTML=`<summary>${icon(remember?'spark':'search')}<span>${name}</span><span class="tool-caption">${escape(caption+rankingCaption)}</span></summary><div class="tool-data"><label>调用 ${escape(event.name || 'mem0')}</label><pre>${escape(JSON.stringify(event.arguments,null,2))}</pre>${event.result?'<label>返回结果</label><pre>'+escape(JSON.stringify(event.result,null,2))+'</pre>':''}</div>`;
  return details;
}
function renderGrounding(el, grounding={}){
  const box=el.querySelector('.grounding');box.replaceChildren();box.hidden=!grounding.status;
  if(!grounding.status)return;
  const captions={verified:'已核对引用原文',conflict:'资料存在不同表述，请核对版本',insufficient:'本次证据不足',validation_failed:'原文核验未通过',retrieval_failed:'本次资料检索失败',legacy_unverified:'历史回答未做原文核验，请重新提问核对'};
  const label=document.createElement('p');label.className='grounding-label '+(grounding.status==='verified'?'verified':'unverified');label.textContent=captions[grounding.status] || '';box.append(label);
  for(const citation of grounding.citations || []){
    const details=document.createElement('details');details.className='source-evidence';
    const summary=document.createElement('summary');summary.textContent=citation.source+(citation.page?' · 第 '+citation.page+' 页':'');
    const quote=document.createElement('blockquote');quote.textContent=citation.quote;details.append(summary,quote);
    if(/^\/api\/memories\/[a-zA-Z0-9-]+\/file\?asset=preview$/.test(citation.url || '')){const link=document.createElement('a');link.href=citation.url+(/^\d+$/.test(String(citation.page || ''))?'#page='+citation.page:'');link.target='_blank';link.rel='noopener';link.textContent='打开来源预览 ↗';details.append(link);}
    box.append(details);
  }
}
function renderTurn(turn){
  const el=document.createElement('article');el.className='turn';el.dataset.turn=turn.id || '';
  el.innerHTML=`<div class="user-message">${escape(turn.user_text)}</div><div class="assistant-message"><span class="assistant-mark">${icon('spark')}</span><div class="assistant-body"><div class="assistant-name">KNOWIN</div><div class="tool-events"></div><div class="answer"></div><div class="grounding" hidden></div><div class="turn-error" hidden></div><div class="turn-meta"></div></div></div>`;
  if(turn.input_context?.user_id){const label=document.createElement('div');label.className='message-context';label.textContent=turn.input_context.user_id+(turn.input_context.family_id?' / '+turn.input_context.family_id:'')+' · '+(turn.input_context.user_id==='knowin_public'?'公开资料 · 只读':turn.input_context.remember_scope==='family'?'可保存到家庭共享':'可保存到个人记忆');el.prepend(label);}
  el.querySelector('.answer').innerHTML=markdown(turn.answer || '');
  for(const event of turn.events || []) el.querySelector('.tool-events').append(toolCard(event));
  if(turn.error){el.querySelector('.turn-error').hidden=false;el.querySelector('.turn-error').textContent=turn.error;}
  el.querySelector('.turn-meta').textContent=(turn.status==='complete'?'已保存到对话 · ':'')+timestamp(turn.created_at);
  renderGrounding(el,turn.grounding);
  $('messages').append(el);return el;
}
async function memories(){
  if(!state.session){$('personal-memories').innerHTML=emptyMemories;$('memory-count').textContent='0';return;}
  const id=state.session.id;const data=await api('/sessions/'+id+'/memories'); if(state.session?.id!==id)return;
  $('memory-count').textContent=data.total;
  $('personal-memories').innerHTML=data.items.length?data.items.map(item=>`<article class="memory-card"><span class="memory-scope ${item.scope==='family'?'family':''}">${item.scope==='family'?'家庭共享':item.scope==='library'?'公开资料 · 只读':'个人记忆'}</span><p>${escape(item.text)}</p><small title="${escape(item.created_at||'')}">创建：${timestamp(item.created_at)}${item.updated_at&&item.updated_at!==item.created_at?' · 更新：'+timestamp(item.updated_at):''} · 长期记忆</small></article>`).join(''):emptyMemories;
}
function renderDiary(data){
  state.diaryStatus=data.summary_status;
  $('diary-date').value=data.date;
  $('diary-download').href='/api/chat/sessions/'+encodeURIComponent(state.session.id)+'/diary.md?'+new URLSearchParams({date:data.date});
  const status={complete:'已整理',pending:'正在等待整理',failed:'整理暂未完成，可点击重新整理',empty:'这一天还没有对话'}[data.summary_status]||data.summary_status;
  const added=[...new Map(data.entries.flatMap(entry=>entry.new_memories||[]).map(item=>[item.id,item])).values()];
  $('diary-content').innerHTML=`<div class="diary-status">${escape(data.date)} · ${data.turn_count} 轮对话 · ${escape(status)}</div>
    <h3>当日整理</h3><p>${escape(data.summary||'完整对话已落盘；摘要生成后会显示在这里。')}</p>
    <h3>新偏好</h3>${data.new_preferences.length?'<ul>'+data.new_preferences.map(item=>'<li>'+escape(item.text)+' <small>· 对话 '+escape(item.turn_id.slice(0,8))+'</small></li>').join('')+'</ul>':'<p>尚未从对话中确认新的偏好。</p>'}
    <h3>新增长期记忆</h3>${added.length?'<ul>'+added.map(item=>'<li>'+escape(item.text)+' <small>('+escape(item.scope)+')</small></li>').join('')+'</ul>':'<p>这一天没有新增长期记忆。</p>'}
    <h3>完整对话记录</h3>${data.entries.length?data.entries.map(entry=>`<article class="diary-entry"><time>${diaryTimestamp(entry.created_at)} · ${escape(entry.status)} · 对话 ${escape(entry.turn_id.slice(0,8))}</time><strong>用户</strong><pre>${escape(entry.user_text)}</pre><strong>助手</strong><pre>${escape(entry.answer||'（无完整回答）')}</pre>${entry.error?'<p class="diary-error">'+escape(entry.error)+'</p>':''}</article>`).join(''):'<p>这一天还没有对话。</p>'}`;
}
async function loadDiary(date='today'){
  if(!state.session)return;
  const ticket=++state.diaryEpoch;
  $('diary-content').innerHTML='<p>正在读取日记…</p>';
  const sid=state.session.id;
  try{
    const data=await api('/sessions/'+sid+'/diary?'+new URLSearchParams({date}));
    if(state.session?.id===sid&&ticket===state.diaryEpoch)renderDiary(data);
  }catch(e){if(ticket===state.diaryEpoch)$('diary-content').innerHTML='<p class="diary-error">'+escape(e.message)+'</p>';}
}
async function loadSession(id){
  if(state.busy){error('请先等待当前回复完成，或点击停止回复。');return;}
  const epoch=++state.epoch;const session=await api('/sessions/'+id);if(epoch!==state.epoch)return;
  if(session.user_id!==state.user || (session.family_id || '')!==state.family)throw new Error('此对话属于另一个身份，请切换 user_id 和 family_id 后查看。');
  state.session=session;localStorage.setItem(sessionKey(),id);showIdentity();
  $('conversation-title').textContent=session.title;$('delete-chat').hidden=false;
  $('library-toggle').checked=Boolean(session.use_library);$('messages').replaceChildren();
  $('welcome').hidden=session.turns.length>0;
  session.turns.forEach(renderTurn);closePanels();error();
  await Promise.all([sessions(),memories()]);scroll(true);
  if(session.turns.some(turn=>turn.status==='running')){busy(true);recover(id);}
}
async function recover(id){
  clearTimeout(state.recovered);
  try{
    const data=await api('/sessions/'+id);if(state.session?.id!==id)return;
    $('messages').replaceChildren();data.turns.forEach(renderTurn);scroll();
    if(data.turns.some(turn=>turn.status==='running')){state.recovered=setTimeout(()=>recover(id),1300);return;}
    busy(false);await memories();
  }catch(e){busy(false);error(e.message);}
}
async function newSession(){
  if(state.busy){error('请先等待当前回复完成，或点击停止回复。');return;}
  const session=await api('/sessions',{method:'POST',body:JSON.stringify({...identityPayload(),use_library:$('library-toggle').checked})});
  await loadSession(session.id);await users();$('message-input').focus();
}
async function send(text){
  if(state.busy || !text.trim())return;
  if(!await applyIdentity())return;
  error(); if(!state.session)await newSession(); if(!state.session)return;
  const inputContext={...identityPayload(),remember_scope:$('remember-scope').value};
  const id=state.session.id;busy(true);$('welcome').hidden=true;
  $('message-input').value='';$('message-input').style.height='auto';
  activeTurn=renderTurn({user_text:text,answer:'',events:[],status:'running',input_context:inputContext});scroll(true);
  let answer='', completed=false;
  try{
    const response=await fetch('/api/chat/sessions/'+id+'/messages',{method:'POST',headers:{'Content-Type':'application/json','X-Memory-Client':'dashboard'},body:JSON.stringify({text,request_id:crypto.randomUUID(),...inputContext})});
    if(!response.ok){const payload=await response.json();throw new Error(typeof payload.detail==='string'?payload.detail:'发送失败');}
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
    while(true){
      const {value,done}=await reader.read();buffer+=decoder.decode(value || new Uint8Array(),{stream:!done});
      const frames=buffer.split('\n\n');buffer=frames.pop();
      for(const frame of frames){
        const data=frame.split('\n').find(line=>line.startsWith('data: '));if(!data)continue;
        const event=JSON.parse(data.slice(6));
        if(event.type==='started')activeTurn.dataset.turn=event.turn_id;
        if(event.type==='status')$('reply-status').lastElementChild.textContent=event.text;
        if(event.type==='delta'){answer+=event.text;activeTurn.querySelector('.answer').innerHTML=markdown(answer);scroll();}
        if(event.type==='tool_start' || event.type==='tool_end'){
          const parent=activeTurn.querySelector('.tool-events');const previous=[...parent.children].find(el=>el.dataset.call===event.event.id);const next=toolCard(event.event);if(previous){next.open=previous.open;previous.replaceWith(next);}else parent.append(next);
          $('reply-status').lastElementChild.textContent=event.type==='tool_start'?(event.event.arguments?.action==='remember'?'正在保存记忆…':event.event.arguments?.action==='diary'?'正在读取日记…':'正在查找记忆…'):'正在组织回答…';
          if(event.type==='tool_end' && event.event.result?.saved)memories().catch(()=>{});scroll();
        }
        if(event.type==='done'){
          completed=true;answer=event.answer;activeTurn.querySelector('.answer').innerHTML=markdown(answer);
          renderGrounding(activeTurn,event.grounding);
          if(event.error){const node=activeTurn.querySelector('.turn-error');node.hidden=false;node.textContent=event.error;}
          activeTurn.querySelector('.turn-meta').textContent=event.status==='complete'?`已保存到对话 · ${(event.duration_ms/1000).toFixed(1)} 秒`:'';
        }
      }
      if(done)break;
    }
    if(!completed)throw new Error('连接已中断，正在重新读取对话结果。');
  }catch(e){
    error(e.message);busy(false);await loadSession(id);return;
  }finally{if(completed)busy(false);}
  const detail=await api('/sessions/'+id);state.session=detail;$('conversation-title').textContent=detail.title;
  await Promise.all([sessions(),memories()]);scroll();$('message-input').focus();
}
$('composer').addEventListener('submit',e=>{e.preventDefault();send($('message-input').value).catch(e=>{busy(false);error(e.message);});});
$('message-input').addEventListener('input',()=>{$('send-button').disabled=state.busy || !$('message-input').value.trim();$('message-input').style.height='auto';$('message-input').style.height=Math.min($('message-input').scrollHeight,160)+'px';});
$('message-input').addEventListener('keydown',e=>{if(e.key==='Enter' && !e.shiftKey && !e.isComposing){e.preventDefault();$('composer').requestSubmit();}});
document.querySelectorAll('[data-prompt]').forEach(button=>button.onclick=()=>{if(state.busy)return;$('message-input').value=button.dataset.prompt;$('message-input').dispatchEvent(new Event('input'));$('message-input').focus();});
$('new-chat').onclick=async()=>{try{const changed=$('user-id').value.trim()!==state.user || $('family-id').value.trim()!==state.family;if(!await applyIdentity())return;if(!changed)await newSession();}catch(e){error(e.message);}};
$('apply-identity').onclick=()=>applyIdentity().catch(e=>error(e.message));
$('user-id').onchange=async()=>{
  const selected=$('user-id').value;
  if(selected==='__new_user__'){$('user-id').value=state.user;$('identity-input').value='';$('identity-family').value=state.family;$('identity-dialog').showModal();$('identity-input').focus();return;}
  const item=state.users.find(item=>item.user_id===selected);
  if(item && !item.family_ids.includes($('family-id').value))$('family-id').value=item.family_ids[0] || '';
  if(selected==='knowin_public'){$('family-id').value='';$('remember-scope').value='personal';$('library-toggle').checked=true;}
  try{await applyIdentity();}catch(e){showIdentity();error(e.message);}
};
$('family-id').oninput=()=>{const hasFamily=Boolean($('family-id').value.trim());$('remember-scope').querySelector('[value="family"]').disabled=!hasFamily;if(!hasFamily)$('remember-scope').value='personal';};
$('stop-button').onclick=async()=>{if(state.session){try{await api('/sessions/'+state.session.id+'/cancel',{method:'POST'});$('reply-status').lastElementChild.textContent='正在停止…';}catch(e){error(e.message);}}};
$('library-toggle').onchange=async()=>{if(!state.session)return;try{state.session=await api('/sessions/'+state.session.id,{method:'PATCH',body:JSON.stringify({...identityPayload(),use_library:$('library-toggle').checked})});}catch(e){$('library-toggle').checked=Boolean(state.session.use_library);error(e.message);}};
$('delete-chat').onclick=async()=>{if(!state.session || state.busy)return;if(!confirm('删除这段对话？对应日记条目会一起移除，长期记忆会保留。'))return;try{await api('/sessions/'+state.session.id,{method:'DELETE'});$('diary-dialog').close();state.session=null;localStorage.removeItem(sessionKey());$('messages').replaceChildren();$('welcome').hidden=false;$('conversation-title').textContent='新的对话';$('delete-chat').hidden=true;await newSession();}catch(e){error(e.message);}};
$('diary-button').onclick=async()=>{if(state.user==='knowin_public')return;$('diary-dialog').showModal();await loadDiary('today');};
$('close-diary').onclick=()=>$('diary-dialog').close();
$('diary-refresh').onclick=()=>loadDiary($('diary-date').value||'today');
$('diary-date').onchange=()=>$('diary-refresh').click();
$('diary-summarize').onclick=async()=>{if(!state.session)return;const ticket=++state.diaryEpoch;const button=$('diary-summarize');button.disabled=true;$('diary-content').innerHTML='<p>正在整理当天对话…</p>';try{const data=await api('/sessions/'+state.session.id+'/diary/summarize?'+new URLSearchParams({date:$('diary-date').value||'today'}),{method:'POST'});if(ticket===state.diaryEpoch)renderDiary(data);}catch(e){if(ticket===state.diaryEpoch)$('diary-content').innerHTML='<p class="diary-error">'+escape(e.message)+'</p>';}finally{button.disabled=false;}};
setInterval(()=>{if($('diary-dialog').open&&state.diaryStatus==='pending'&&!$('diary-summarize').disabled)loadDiary($('diary-date').value||'today');},5000);
$('identity-button').onclick=()=>{$('identity-input').value=state.user;$('identity-family').value=state.family;$('identity-dialog').showModal();};
$('close-identity').onclick=()=>$('identity-dialog').close();
$('identity-input').setAttribute('list','known-users');
$('identity-form').onsubmit=async e=>{e.preventDefault();if(state.busy)return;const user=$('identity-input').value.trim();ensureUserOption(user);$('user-id').value=user;$('family-id').value=user==='knowin_public'?'':$('identity-family').value;if(user==='knowin_public')$('library-toggle').checked=true;$('identity-dialog').close();try{await applyIdentity();}catch(e){error(e.message);}};
$('menu-button').onclick=()=>{document.body.classList.add('show-sidebar');$('shade').hidden=false;};
$('memory-toggle').onclick=()=>{if(innerWidth<=1050){document.body.classList.toggle('show-memory');$('shade').hidden=!document.body.classList.contains('show-memory');}else document.body.classList.toggle('is-hidden-panel');};
$('close-memory').onclick=closePanels;$('shade').onclick=closePanels;
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanels();});
async function init(){
  if(!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(state.user))state.user='chat_default';
  if(state.family && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(state.family))state.family='';
  showIdentity();
  const [config]=await Promise.all([api('/config'),users()]);$('model-name').textContent=config.model;
  const last=localStorage.getItem(sessionKey()) || (!state.family && localStorage.getItem('knowin-chat-session-'+state.user));
  if(last){try{await loadSession(last);return;}catch{localStorage.removeItem(sessionKey());localStorage.removeItem('knowin-chat-session-'+state.user);}}
  // Create a durable empty conversation so existing personal memories are visible immediately.
  await newSession();
}
init().catch(e=>{error(e.message);$('model-name').textContent='连接未完成';});
