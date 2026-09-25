const $=id=>document.getElementById(id);
export const factNames={active:'当前有效',disputed:'冲突待确认',retracted:'已撤回',superseded:'已被替代',pending:'待确认',rejected:'未采纳'};
export const syncNames={done:'索引已同步',pending:'等待同步',running:'正在同步',failed:'同步失败，可重试'};
let current=null;
export function clearFact(){current=null;$('fact-edit-dialog').close();}
export function showFact(data,ui){
  current={data,ui,user:ui.user()};
  const {esc,date}=ui;
  let html=`<p>${esc(factNames[data.fact_status])} · 修订 ${data.revision} · ${esc(syncNames[data.sync_status])}</p>`;
  html+=data.subject?`<p>对象：${esc(data.subject)} / 属性：${esc(data.attribute)}</p>`:'<p>旧记录未分类，可按记录 ID 更新；不会仅凭相似文字自动合并。</p>';
  if(data.can_delete)html+=`<div class="relationship-actions"><button data-fact-action="edit">更新 / 纠错</button>${data.fact_status!=='retracted'?'<button data-fact-action="retract">撤回事实（保留历史）</button>':''}${data.sync_status!=='done'?'<button data-fact-action="retry">重试索引同步</button>':''}</div>`;
  if(data.fact_status==='disputed'){
    html+=`<p class="fact-warning">以下说法存在冲突，已暂停作为有效事实使用。${data.can_delete?'请选择一个版本并说明原因。':'请由记录创建者或家庭创建者处理。'}</p>`;
    if(data.can_delete)html+='<label>采用方式 <select id="fact-resolution-kind"><option value="change">事实发生变化</option><option value="correction">纠正原来的错误</option></select></label><label class="stack">处理说明<input id="fact-resolution-reason" maxlength="1000" placeholder="例如 用户确认从今天开始改变偏好"></label>';
  }
  html+='<h3>全部版本</h3>'+data.versions.map(v=>`<article class="detail-turn"><div class="tags"><span class="tag">版本 ${v.version}</span><span class="tag">${esc(data.fact_status==='disputed'&&v.status==='active'?'原有效版本（争议中）':factNames[v.status]||v.status)}</span></div><div class="detail-text">${esc(v.text)}</div><p class="muted">${v.kind==='legacy'?'旧记录时间（实际发生时间未知）':'发生时间'}：${date(v.effective_at)}${v.valid_to?' · 有效至 '+date(v.valid_to):''}<br>记录时间：${date(v.recorded_at)} · 提交者：${esc(v.actor)}</p>${v.reason?`<p class="detail-text">原因：${esc(v.reason)}</p>`:''}${v.source_quote?`<details><summary>用户原文依据</summary><div class="detail-text">${esc(v.source_quote)}</div></details>`:''}${data.fact_status==='disputed'&&data.can_delete&&['active','pending'].includes(v.status)?`<button data-fact-action="resolve" data-version="${v.version}">${v.version===data.current_version?'保留原记录':'采用此版本'}</button>`:''}</article>`).join('');
  if(data.legacy_history?.length)html+='<details><summary>旧版变更历史</summary>'+data.legacy_history.map(h=>'<pre class="detail-text">'+esc(JSON.stringify(h,null,2))+'</pre>').join('')+'</details>';
  if(data.events.length)html+='<details><summary>操作记录</summary>'+data.events.map(e=>`<p>${date(e.created_at)} · ${esc(e.actor)} · ${esc(({created:'新增',conflict_proposed:'提出冲突',change:'更新',correction:'纠错',resolved_change:'确认更新',resolved_correction:'确认纠错',retracted:'撤回'})[e.action]||e.action)}</p>`).join('')+'</details>';
  $('detail-content').insertAdjacentHTML('beforeend',html);
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-fact-action]');
  if(!button||!current)return;
  const {data,ui,user}=current;
  if(user!==ui.user())return;
  const path='/api/manage/facts/'+encodeURIComponent(data.id),action=button.dataset.factAction;
  if(action==='edit'){$('fact-edit-form').reset();$('fact-text').value=data.memory;$('fact-subject').value=data.subject;$('fact-attribute').value=data.attribute;$('fact-edit-dialog').showModal();return;}
  button.disabled=true;
  try{
    let body={};
    if(action==='retract'){if(!confirm('撤回这条事实？它会停止参与检索，完整历史仍可查看。'))return;body={revision:data.revision};}
    if(action==='resolve'){
      const reason=$('fact-resolution-reason').value.trim();if(!reason)throw new Error('请填写处理说明');
      body={revision:data.revision,version:Number(button.dataset.version),kind:$('fact-resolution-kind').value,reason};
    }
    await ui.api(path+'/'+action,{method:'POST',body:JSON.stringify(body)});
    if(user!==ui.user())return;
    await ui.refresh();await ui.detail(data.id);ui.notify(action==='retry'?'已加入重试队列':'已保存');
  }catch(error){ui.failure(error);}finally{button.disabled=false;}
});
$('fact-edit-form').onsubmit=async event=>{
  event.preventDefault();if(!current)return;
  const {data,ui,user}=current;if(user!==ui.user())return;
  $('fact-save').disabled=true;
  try{
    const value=$('fact-occurred').value;
    await ui.api('/api/manage/facts/'+encodeURIComponent(data.id),{method:'PATCH',body:JSON.stringify({revision:data.revision,text:$('fact-text').value.trim(),subject:$('fact-subject').value.trim(),attribute:$('fact-attribute').value.trim(),kind:$('fact-kind').value,occurred_at:value?new Date(value).toISOString():'',reason:$('fact-reason').value.trim()})});
    if(user!==ui.user())return;
    $('fact-edit-dialog').close();await ui.refresh();await ui.detail(data.id);ui.notify('版本已保存');
  }catch(error){ui.failure(error);}finally{$('fact-save').disabled=false;}
};
