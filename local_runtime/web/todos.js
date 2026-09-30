const $=id=>document.getElementById(id);
export const todoStatus={draft:'待确认',pending:'待办',in_progress:'进行中',blocked:'受阻',done:'已完成',cancelled:'已取消'};
const priorities={low:'低',normal:'普通',high:'高',urgent:'紧急'};
const fields={title:'标题',description:'详细说明',list_name:'清单',status:'状态',priority:'优先级',assignee_user_id:'负责人',start_at:'计划开始时间',due_at:'截止时间',due_date:'截止日期',timezone:'时区',location:'地点',estimated_minutes:'预计耗时',tags:'标签',checklist:'子任务'};
const selections=(values)=>Object.entries(values).map(([v,t])=>`<option value="${v}">${t}</option>`).join('');
function wall(value,tz){
  if(!value)return '';
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:tz,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value));
  const p=Object.fromEntries(parts.map(p=>[p.type,p.value]));return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}
function zonedISO(value,tz){
  if(!value)return '';
  const target=Date.parse(value+'Z');let candidate=target;
  for(let i=0;i<4;i++)candidate+=target-Date.parse(wall(new Date(candidate).toISOString(),tz)+'Z');
  if(wall(new Date(candidate).toISOString(),tz)!==value)throw new Error('该时区不存在这个时间，请调整时间。');
  if([-120,-90,-60,-30,30,60,90,120].some(m=>wall(new Date(candidate+m*60000).toISOString(),tz)===value))throw new Error('该时间处于夏令时重复区间，请改用 UTC 时区填写明确时间。');
  return new Date(candidate).toISOString();
}
export function createTodos(ui){
  const {api,state,esc,date,tags,notify,failure,refresh}=ui;
  let current=null,owner='',generation=0,requestId='',busy=false;
  $('type-hint').insertAdjacentHTML('afterend',`<div id="todo-filters" class="todo-filters" hidden><label>显示<select id="todo-filter-trash"><option value="false">当前待办</option><option value="true">回收站</option></select></label><label>状态<select id="todo-filter-status"><option value="all">全部状态</option><option value="open" selected>所有未完成</option>${selections(todoStatus)}</select></label><label>截止范围<select id="todo-filter-period"><option value="all">全部时间</option><option value="today">今天截止</option><option value="upcoming">未来 7 天</option><option value="overdue">已逾期</option><option value="unscheduled">未设截止时间</option></select></label><label>优先级<select id="todo-filter-priority"><option value="">全部优先级</option>${selections(priorities)}</select></label><label>清单名称<input id="todo-filter-list" maxlength="80" placeholder="全部清单"></label><label>负责人<select id="todo-filter-assignee"><option value="">全部负责人</option><option value="me">指派给我</option><option value="__none__">尚未指派</option></select></label></div>`);
  document.body.insertAdjacentHTML('beforeend',`<dialog id="todo-dialog" class="todo-dialog wide"><div class="dialog-top"><div><p class="eyebrow">TO DO LIST</p><h2 id="todo-heading">创建待办</h2></div><button id="todo-close" aria-label="关闭待办">×</button></div><p class="muted" id="todo-permission-note"></p><p id="todo-error" class="todo-error" role="alert" hidden></p><form id="todo-form" class="stack">
  <fieldset class="todo-section"><legend>事项与计划</legend><div class="todo-grid"><label class="todo-full">待办标题 *<input id="todo-title" maxlength="240" required placeholder="例如 准备周末旅行用品"></label><label>所属清单 *<input id="todo-list" maxlength="80" value="默认清单" required placeholder="例如 购物清单、旅行准备"></label><label>状态<select id="todo-status">${selections(todoStatus)}</select></label><label>优先级<select id="todo-priority">${selections(priorities)}</select></label><label>地点<input id="todo-location" maxlength="240" placeholder="可选，例如 超市、书房"></label><label class="todo-full">详细说明与备注<textarea id="todo-description" rows="4" maxlength="6000" placeholder="补充目标、数量、注意事项或完成条件"></textarea></label><label>标签（逗号分隔，最多 10 个）<input id="todo-tags" maxlength="420" placeholder="家庭，采购"></label><label>预计耗时（分钟）<input id="todo-estimated" type="number" min="1" max="10080" placeholder="可不填写"></label></div></fieldset>
  <fieldset class="todo-section"><legend>时间安排</legend><div class="todo-grid"><label>时区<input id="todo-timezone" required maxlength="80" value="Asia/Shanghai" placeholder="Asia/Shanghai"></label><label>计划开始时间<input id="todo-start" type="datetime-local"></label><label>截止方式<select id="todo-due-mode"><option value="none">不设截止时间</option><option value="date">某天截止（当天结束前）</option><option value="time">精确到时间</option></select></label><label id="todo-date-label" hidden>截止日期<input id="todo-date" type="date"></label><label id="todo-time-label" hidden>截止时间<input id="todo-due" type="datetime-local"></label></div><p class="muted">时间按上方时区解释。仅指定日期时，截止到当地当天结束；逾期不会自动标记完成。此版本不发送主动提醒。</p></fieldset>
  <fieldset class="todo-section"><legend>归属与协作</legend><div class="todo-grid"><label>所属家庭<select id="todo-family"></select></label><label>可见范围<select id="todo-visibility"><option value="personal">仅自己可见</option><option value="family">家庭成员共享</option></select></label><label>来源设备<select id="todo-device"></select></label><label>负责人<select id="todo-assignee"></select></label></div><p class="muted">私人待办只属于本人。共享待办的负责人可更新状态和子任务；创建者或家庭管理员可编辑全部内容及删除。创建后归属范围固定。</p></fieldset>
  <fieldset class="todo-section"><legend>子任务清单</legend><div id="todo-checklist" class="todo-checklist"></div><button type="button" id="todo-check-add">＋ 添加子任务</button><small>每行一个步骤；勾选子任务不会自动改变整条待办的状态。</small></fieldset>
  <section class="todo-section"><h3>记录信息</h3><dl id="todo-record-info" class="todo-info"></dl><div id="todo-source" class="muted"></div></section><div class="todo-save-row"><button type="button" id="todo-delete" class="danger" hidden>移入回收站</button><button type="button" id="todo-restore" hidden>恢复待办</button><button type="button" id="todo-purge" class="danger" hidden>彻底删除</button><button class="primary" id="todo-save">保存待办</button></div></form><section id="todo-history" class="todo-section" hidden><h3>操作历史</h3><div id="todo-history-items"></div></section></dialog>`);
  const error=e=>{$('todo-error').textContent=e.message;$('todo-error').hidden=false;notify(e.message);};
  const setOptions=(id,rows)=>{$(id).replaceChildren(...rows.map(([v,t])=>new Option(t,v)));};
  const isCurrent=()=>owner===state.me?.user_id;
  function clear(){generation++;current=null;owner='';$('todo-dialog').close();$('todo-form').reset();$('todo-checklist').replaceChildren();$('todo-source').replaceChildren();$('todo-history-items').replaceChildren();}
  function dueMode(){const mode=$('todo-due-mode').value;$('todo-date-label').hidden=mode!=='date';$('todo-time-label').hidden=mode!=='time';$('todo-date').required=mode==='date';$('todo-due').required=mode==='time';}
  function assignees(selected=''){
    const family=state.me.families.find(f=>f.family_id===$('todo-family').value);
    const shared=$('todo-visibility').value==='family';
    setOptions('todo-assignee',shared?[['','暂不指派'],...(family?.members||[]).map(m=>[m.user_id,`${m.name} (${m.user_id})`])]:[[state.me.user_id,'自己（'+state.me.name+'）']]);
    if([...$('todo-assignee').options].some(o=>o.value===selected))$('todo-assignee').value=selected;
  }
  function familyChanged(){
    const family=$('todo-family').value;
    setOptions('todo-device',[['','未标记设备'],...state.me.devices.filter(d=>d.family_id===family).map(d=>[d.device_id,d.name])]);
    $('todo-visibility').querySelector('[value="family"]').disabled=!family;
    if(!family)$('todo-visibility').value='personal';assignees();
  }
  function addCheck(item={id:crypto.randomUUID(),text:'',done:false}){
    const row=document.createElement('div');row.className='todo-check-row';row.dataset.id=item.id;
    row.innerHTML=`<input type="checkbox" aria-label="子任务已完成" ${item.done?'checked':''}><input type="text" aria-label="子任务内容" maxlength="300" required value="${esc(item.text)}" placeholder="例如 确认车票"><button type="button" aria-label="移除子任务">×</button>`;
    row.querySelector('button').onclick=()=>row.remove();$('todo-checklist').append(row);
  }
  function permissions(){
    const manage=!current||(current.can_manage&&!current.deleted_at),edit=!current||current.can_edit;
    document.querySelectorAll('#todo-form input,#todo-form select,#todo-form textarea').forEach(el=>{el.disabled=!manage;});
    if(edit){$('todo-status').disabled=false;document.querySelectorAll('#todo-checklist input').forEach(el=>el.disabled=false);}
    ['todo-family','todo-device','todo-visibility'].forEach(id=>$(id).disabled=Boolean(current));
    $('todo-check-add').disabled=!edit;document.querySelectorAll('#todo-checklist button').forEach(b=>b.disabled=!edit);
    $('todo-save').hidden=!edit;$('todo-delete').hidden=!current?.can_delete||Boolean(current?.deleted_at);$('todo-restore').hidden=$('todo-purge').hidden=!(current?.deleted_at&&current.can_delete);
    $('todo-permission-note').textContent=!current?'把要做的事落到清单中，时间和负责人可稍后补充。':manage?'你可以维护此待办的内容、状态和计划。':edit?'此事项由你负责。你可以更新状态和子任务；其他内容由创建者管理。':'你可以查看此家庭共享待办，当前没有编辑权限。';
  }
  function history(item){
    $('todo-history').hidden=!item?.events?.length;
    const val=(k,v)=>k==='status'?todoStatus[v]:k==='priority'?priorities[v]:typeof v==='object'?JSON.stringify(v):String(v??'未填写');
    $('todo-history-items').innerHTML=(item?.events||[]).map(e=>`<details class="todo-event"><summary>${date(e.created_at)} · ${esc(e.actor)} · ${({created:'创建',updated:'更新',trashed:'移入回收站',restored:'恢复',member_left:'成员退出，解除指派'}[e.action]||e.action)} · 版本 ${e.revision}</summary>${e.action==='created'?'<p class="muted">创建了这条待办。</p>':Object.entries(e.changes).map(([k,v])=>`<p class="detail-text"><strong>${esc(fields[k]||k)}</strong>：${esc(val(k,v.before))} → ${esc(val(k,v.after))}</p>`).join('')}</details>`).join('');
  }
  function fill(item=null){
    current=item;owner=state.me.user_id;requestId=crypto.randomUUID();$('todo-form').reset();$('todo-error').hidden=true;
    $('todo-heading').textContent=item?'待办详情':'创建待办';$('todo-checklist').replaceChildren();
    setOptions('todo-family',[['','个人空间'],...state.me.families.map(f=>[f.family_id,f.name])]);
    $('todo-family').value=item?.family_id||($('family-filter').value==='__none__'?'':$('family-filter').value);familyChanged();
    $('todo-visibility').value=item?.visibility||'personal';assignees(item?.assignee_user_id||'');
    $('todo-device').value=item?.device_id||($('device-filter').value==='__none__'?'':$('device-filter').value);
    for(const [key,id] of Object.entries({title:'todo-title',description:'todo-description',list_name:'todo-list',status:'todo-status',priority:'todo-priority',location:'todo-location',timezone:'todo-timezone',estimated_minutes:'todo-estimated'})){
      $(id).value=item?.[key]??({list_name:'默认清单',status:'pending',priority:'normal',timezone:'Asia/Shanghai'}[key]||'');
    }
    $('todo-tags').value=(item?.tags||[]).join('，');
    $('todo-start').value=wall(item?.start_at,item?.timezone||'Asia/Shanghai');
    $('todo-due-mode').value=item?.due_date?'date':item?.due_at?'time':'none';
    $('todo-date').value=item?.due_date||'';$('todo-due').value=wall(item?.due_at,item?.timezone||'Asia/Shanghai');dueMode();
    (item?.checklist||[]).forEach(addCheck);
    const infos=item?{'创建时间':date(item.created_at),'更新时间':date(item.updated_at),'完成时间':item.completed_at?date(item.completed_at):'尚未完成','取消时间':item.cancelled_at?date(item.cancelled_at):'未取消','移入回收站时间':item.deleted_at?date(item.deleted_at):'未删除','删除操作人':item.deleted_by||'—','创建者':item.owner_name,'版本':item.revision}:{'创建时间':'首次保存时自动记录','更新时间':'每次变更时自动记录','完成时间':'标记完成时自动记录','取消时间':'取消事项时自动记录'};
    $('todo-record-info').innerHTML=Object.entries(infos).map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('');
    $('todo-source').innerHTML=item?`<p>来源：${item.source.type==='chat'?'聊天创建':'网页或接口创建'} · 记录 ID：${esc(item.id)}</p>${item.source.quote?`<details><summary>创建时的用户原话（仅创建者可见）</summary><p class="detail-text">${esc(item.source.quote)}</p></details>`:''}`:'<p>创建时间和操作历史由系统记录，无需手动填写。</p>';
    history(item);permissions();if(item?.deleted_at)$('todo-permission-note').textContent='此待办已移入回收站，不参与当前待办查询。可由创建者或家庭管理员恢复，或确认后彻底删除。';if(!$('todo-dialog').open)$('todo-dialog').showModal();
  }
  async function open(id){const token=++generation,user=state.me.user_id;const item=await api('/api/todos/'+encodeURIComponent(id));if(token!==generation||user!==state.me?.user_id)return;fill(item);}
  function create(){generation++;fill();}
  async function remove(){
    if(!current||!isCurrent()||busy||!confirm('将“'+current.title+'”移入回收站？之后可以恢复。'))return;
    const item=current,user=owner;busy=true;$('todo-delete').disabled=true;
    try{await api('/api/todos/'+encodeURIComponent(item.id)+'?revision='+item.revision,{method:'DELETE'});if(user!==state.me?.user_id)return;clear();await refresh();notify('已移入回收站，可在回收站恢复');}catch(e){error(e);}finally{busy=false;$('todo-delete').disabled=false;}
  }
  $('todo-form').onsubmit=async event=>{
    event.preventDefault();if(!isCurrent()||busy)return;
    const user=owner,record=current;busy=true;$('todo-save').disabled=true;$('todo-error').hidden=true;
    try{
      const checklist=[...$('todo-checklist').children].map(row=>({id:row.dataset.id,text:row.querySelector('input[type=text]').value.trim(),done:row.querySelector('input[type=checkbox]').checked}));
      let data={status:$('todo-status').value,checklist};
      if(!record||record.can_manage){
        const tz=$('todo-timezone').value.trim(),mode=$('todo-due-mode').value;
        data={...data,title:$('todo-title').value.trim(),description:$('todo-description').value.trim(),list_name:$('todo-list').value.trim(),priority:$('todo-priority').value,location:$('todo-location').value.trim(),timezone:tz,
          start_at:zonedISO($('todo-start').value,tz),due_date:mode==='date'?$('todo-date').value:'',due_at:mode==='time'?zonedISO($('todo-due').value,tz):'',
          assignee_user_id:$('todo-assignee').value,estimated_minutes:$('todo-estimated').value?Number($('todo-estimated').value):null,tags:$('todo-tags').value.split(/[,，]/).map(t=>t.trim()).filter(Boolean)};
      }
      const body=record?{revision:record.revision,changes:data}:{...data,family_id:$('todo-family').value,device_id:$('todo-device').value,visibility:$('todo-visibility').value,request_id:requestId};
      const saved=await api('/api/todos'+(record?'/'+encodeURIComponent(record.id):''),{method:record?'PATCH':'POST',body:JSON.stringify(body)});
      if(user!==state.me?.user_id)return;await refresh();await open(saved.id);notify(record?'待办已更新':'待办已创建');
    }catch(e){if(user===state.me?.user_id)error(e);}finally{busy=false;$('todo-save').disabled=false;}
  };
  $('todo-close').onclick=()=>{if(!busy){generation++;$('todo-dialog').close();}};
  $('todo-dialog').addEventListener('cancel',e=>{if(busy)e.preventDefault();else generation++;});
  $('todo-family').onchange=familyChanged;$('todo-visibility').onchange=()=>assignees();$('todo-due-mode').onchange=dueMode;
  $('todo-check-add').onclick=()=>{if($('todo-checklist').children.length>=50){error(new Error('最多添加 50 个子任务'));return;}addCheck();};
  $('todo-delete').onclick=remove;
  async function recycle(action){
    if(!current||!isCurrent()||busy)return;
    const item=current,user=owner;
    const title=action==='purge'?prompt('彻底删除后无法恢复。原始聊天和备份仍单独保留。请输入完整标题确认：'+item.title):'';
    if(action==='purge'&&title!==item.title){if(title!==null)error(new Error('标题不一致，未删除'));return;}
    busy=true;$('todo-restore').disabled=$('todo-purge').disabled=true;
    try{await api('/api/todos/'+encodeURIComponent(item.id)+'/'+action,{method:'POST',body:JSON.stringify({revision:item.revision,...(action==='purge'?{title}:{})})});if(user!==state.me?.user_id)return;clear();await refresh();notify(action==='purge'?'已彻底删除':'已恢复到当前待办');}catch(e){if(user===state.me?.user_id)error(e);}finally{busy=false;$('todo-restore').disabled=$('todo-purge').disabled=false;}
  }
  $('todo-restore').onclick=()=>recycle('restore');$('todo-purge').onclick=()=>recycle('purge');
  document.querySelectorAll('#todo-filters select,#todo-filters input').forEach(el=>el.onchange=()=>{state.page=1;refresh();});
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-todo-open],[data-todo-complete]');if(!button)return;
    const user=state.me?.user_id;button.disabled=true;
    try{
      if(button.dataset.todoOpen)await open(button.dataset.todoOpen);
      else {await api('/api/todos/'+encodeURIComponent(button.dataset.todoComplete),{method:'PATCH',body:JSON.stringify({revision:Number(button.dataset.revision),changes:{status:'done'}})});if(user===state.me?.user_id){await refresh();notify('已标记完成');}}
    }catch(e){if(user===state.me?.user_id)failure(e);}finally{button.disabled=false;}
  });
  const deadline=item=>item.due_date?`${item.due_date} 当天结束前 · ${item.timezone}`:item.due_at?`${wall(item.due_at,item.timezone).replace('T',' ')} · ${item.timezone}`:'未设截止时间';
  function card(item){return `<article class="card todo-card ${item.overdue?'todo-overdue':''}">${tags({...item,scope:item.visibility})}${item.deleted_at?'<span class="tag">回收站 · '+esc(date(item.deleted_at))+'</span>':''}<div class="tags"><span class="tag todo-status-${esc(item.status)}">${todoStatus[item.status]}</span><span class="tag todo-priority-${esc(item.priority)}">${priorities[item.priority]}优先级</span>${item.overdue?'<span class="tag todo-late">已逾期</span>':''}</div><small>${esc(item.list_name)}</small><h3>${esc(item.title)}</h3>${item.description?`<p class="preview">${esc(item.description)}</p>`:''}<div class="todo-card-meta"><span>截止：${esc(deadline(item))}</span><span>负责人：${esc(item.assignee_name||'暂未指派')}</span><span>创建：${date(item.created_at)}</span>${item.checklist.length?`<span>子任务：${item.checklist.filter(c=>c.done).length} / ${item.checklist.length} 完成</span>`:''}</div><div class="card-actions"><button data-todo-open="${esc(item.id)}">详情 / 管理</button>${item.can_edit&&!['done','cancelled'].includes(item.status)?`<button data-todo-complete="${esc(item.id)}" data-revision="${item.revision}">标记完成</button>`:''}</div></article>`;}
  function filters(params){
    const trashed=$('todo-filter-trash').value==='true';params.set('trashed',String(trashed));$('todo-filter-status').disabled=$('todo-filter-period').disabled=trashed;params.set('status',trashed?'all':$('todo-filter-status').value);params.set('period',trashed?'all':$('todo-filter-period').value);params.set('priority',$('todo-filter-priority').value);params.set('list_name',$('todo-filter-list').value.trim());
    params.set('assignee_user_id',$('todo-filter-assignee').value==='me'?state.me.user_id:$('todo-filter-assignee').value);
  }
  return {card,create,clear,filters};
}
