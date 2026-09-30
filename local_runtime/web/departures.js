const $=id=>document.getElementById(id);
export function familyControls(family,user,esc){
  const owner=family.owner_user_id===user;
  return `<div class="relationship-actions"><button data-family-leave="${esc(family.family_id)}">${owner?'转交并退出家庭':'退出家庭'}</button>${owner?family.members.filter(m=>m.user_id!==user).map(m=>`<button class="danger" data-family-remove="${esc(family.family_id)}" data-member="${esc(m.user_id)}">移除 ${esc(m.name)}</button>`).join(''):''}</div>`;
}
export function installDepartures({api,state,after,failure,notify}){
  let target=null,busy=false;
  document.body.insertAdjacentHTML('beforeend',`<dialog id="departure-dialog"><div class="dialog-top"><h2 id="departure-title">退出家庭</h2><button id="departure-close" aria-label="关闭">×</button></div><p id="departure-description"></p><p class="muted">离开后将无法访问家庭共享记忆、待办和家庭设备。个人记录保留；共享待办保留在家庭中，原指派给离开成员的事项变为未指派。已有备份和历史对话不会自动擦除。</p><form id="departure-form" class="stack"><label id="departure-successor-label">接任管理员<select id="departure-successor"></select></label><p id="departure-error" role="alert"></p><button class="danger" id="departure-confirm">确认退出</button></form></dialog>`);
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-family-leave],[data-family-remove]');if(!button||busy)return;
    const family=state.me.families.find(f=>f.family_id===(button.dataset.familyLeave||button.dataset.familyRemove));if(!family)return;
    const member=button.dataset.member||state.me.user_id,owner=member===family.owner_user_id;
    target={family:family.family_id,member,user:state.me.user_id,owner};
    $('departure-title').textContent=member===target.user?'退出家庭':'移除成员';
    $('departure-description').textContent=`家庭：${family.name} (${family.family_id})；离开成员：${family.members.find(m=>m.user_id===member)?.name||member} (${member})`;
    $('departure-successor-label').hidden=!owner;
    $('departure-successor').replaceChildren(new Option('请选择接任管理员',''),...family.members.filter(m=>m.user_id!==member).map(m=>new Option(m.name+' ('+m.user_id+')',m.user_id)));
    $('departure-successor').required=owner;
    $('departure-confirm').disabled=owner&&family.members.length===1;
    $('departure-error').textContent=owner&&family.members.length===1?'你是最后一位成员。请先添加成员并转交管理权限，再退出。':'';
    $('departure-confirm').textContent=member===target.user?'确认退出':'确认移除';$('departure-dialog').showModal();
  });
  $('departure-close').onclick=()=>{if(!busy)$('departure-dialog').close();};
  $('departure-dialog').addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  $('departure-form').onsubmit=async event=>{
    event.preventDefault();if(busy||!target)return;
    if(target.user!==state.me?.user_id){$('departure-dialog').close();return;}
    busy=true;$('departure-confirm').disabled=true;
    try{
      const path='/api/identity/families/'+encodeURIComponent(target.family);
      const result=await api(path+(target.member===target.user?'/leave':'/members/'+encodeURIComponent(target.member)),target.member===target.user?{method:'POST',body:JSON.stringify({successor_user_id:target.owner?$('departure-successor').value:''})}:{method:'DELETE'});
      $('departure-dialog').close();if(target.user!==state.me?.user_id)return;
      await after();notify('成员已退出；'+result.unassigned_todos+' 条共享待办已解除指派');
    }catch(error){$('departure-error').textContent=error.message;failure(error);}finally{busy=false;$('departure-confirm').disabled=false;}
  };
}
