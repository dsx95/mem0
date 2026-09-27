const migrationElement = id => document.getElementById(id);
let migrationUpload = null, migrationBusy = false;
function migrationMessage(text,error=false){const el=migrationElement('migration-message');el.textContent=text;el.classList.toggle('migration-error',error);}
async function migrationAPI(path,options={}){
  const response=await fetch('/api/migration/'+path,{...options,headers:{'X-Memory-Client':'dashboard','X-Memory-Migration-Key':migrationElement('migration-key').value.trim(),'Content-Type':'application/json',...options.headers}});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:`操作失败（${response.status}）`);
  return data;
}
function migrationCounts(c){return `${c.users} 个用户 · ${c.memories} 条向量记录${c.facts!==undefined?` · ${c.facts} 条长期事实 / ${c.fact_versions} 个版本 / ${c.fact_tasks} 项未完成同步`:''} · ${c.todos!==undefined?`${c.todos} 条待办 / ${c.todo_events} 条待办历史 · `:''}${c.sessions} 段对话 · ${c.turns} 轮对话 · ${c.diary_entries} 条日记原文 · ${c.source_files} 个源文件`;}
async function migrationTask(action){
  if(migrationBusy)return;
  migrationBusy=true;migrationElement('migration-dialog').setAttribute('aria-busy','true');
  migrationElement('migration-tools').disabled=true;migrationElement('migration-unlock').disabled=true;
  try{await action();}catch(error){migrationMessage(error.message,true);}finally{migrationBusy=false;migrationElement('migration-dialog').removeAttribute('aria-busy');migrationElement('migration-tools').disabled=false;migrationElement('migration-unlock').disabled=false;}
}
migrationElement('migration-open').onclick=()=>migrationElement('migration-dialog').showModal();
migrationElement('migration-close').onclick=()=>{if(!migrationBusy)migrationElement('migration-dialog').close();};
migrationElement('migration-dialog').addEventListener('cancel',e=>{if(migrationBusy)e.preventDefault();});
migrationElement('migration-auth').onsubmit=async e=>{
  e.preventDefault();migrationElement('migration-unlock').disabled=true;
  try{const status=await migrationAPI('status');migrationElement('migration-tools').disabled=false;
    migrationMessage(status.pending_restart?'已准备恢复，正在暂停写入。请重启服务，或取消恢复。':'已连接本机迁移工具。导出包含整个系统的用户数据，请妥善保管迁移包。');
    if(!status.pending_restart&&status.last_restore?.restored)migrationMessage('上次恢复已完成。迁移包包含：'+migrationCounts(status.last_restore.counts)+'。恢复前备份：'+status.last_restore.backup);
    migrationElement('migration-cancel').hidden=!status.pending_restart;
  }catch(error){migrationElement('migration-tools').disabled=true;migrationMessage(error.message,true);}finally{migrationElement('migration-unlock').disabled=false;}
};
migrationElement('migration-export').onclick=()=>migrationTask(async()=>{
  migrationMessage('正在生成完整迁移包。资料较多时需要几分钟，请保持页面打开。');
  const data=await migrationAPI('export',{method:'POST',body:JSON.stringify({include_api_keys:migrationElement('migration-secrets').checked})});
  const link=migrationElement('migration-download');link.href=data.download_url;link.download=data.filename;link.hidden=false;
  migrationMessage('导出完成：'+migrationCounts(data.counts)+'。点击“下载迁移包”保存到新机器。');
});
migrationElement('migration-import-form').onsubmit=e=>{e.preventDefault();migrationTask(async()=>{
  const file=migrationElement('migration-file').files[0];if(!file)throw new Error('请选择 .tar.gz 迁移包');
  migrationUpload=null;migrationElement('migration-restore').disabled=true;
  migrationMessage('正在上传并校验迁移包，尚未修改当前数据…');
  migrationUpload=await migrationAPI('imports',{method:'POST',body:file,headers:{'Content-Type':'application/gzip'}});
  migrationElement('migration-restore').disabled=false;
  migrationMessage('校验通过：'+migrationCounts(migrationUpload.counts)+'。Embedding：'+migrationUpload.embedding.model+' / '+migrationUpload.embedding.dimensions+' 维。');
});};
migrationElement('migration-file').onchange=()=>{migrationUpload=null;migrationElement('migration-restore').disabled=true;};
migrationElement('migration-restore').onclick=()=>{
  if(!migrationUpload||!confirm('准备整库恢复？下次启动时会先备份当前数据库和资料，再替换成迁移包。当前服务将暂停写入，已有登录会失效。'))return;
  migrationTask(async()=>{
    const result=await migrationAPI('restore',{method:'POST',body:JSON.stringify({upload_id:migrationUpload.upload_id,keep_local_identities:migrationElement('migration-keep-identities').checked})});
    migrationMessage(result.message+' 重启命令：bash start.sh --port '+(location.port||'18580'));
    migrationElement('migration-cancel').hidden=false;
  });
};
migrationElement('migration-cancel').onclick=()=>migrationTask(async()=>{
  await migrationAPI('cancel',{method:'POST'});migrationElement('migration-cancel').hidden=true;migrationMessage('已取消待恢复任务，当前数据未替换，可以继续使用。');
});
