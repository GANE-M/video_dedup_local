function formatBytes(bytes){
  const value=Number(bytes||0);
  if(value<1024)return `${value} B`;
  if(value<1024**2)return `${(value/1024).toFixed(1)} KiB`;
  if(value<1024**3)return `${(value/1024**2).toFixed(1)} MiB`;
  return `${(value/1024**3).toFixed(2)} GiB`;
}

async function refreshStorage(){
  try{
    const payload=await api('/api/v1/storage');
    $('storageSummary').innerHTML=[
      ['项目',payload.projects],
      ['任务',payload.jobs],
      ['运行时',formatBytes(payload.runtime_bytes)],
      ['已发布/记录',formatBytes(payload.published_bytes)],
      ['总占用',formatBytes(payload.total_bytes)]
    ].map(([label,value])=>`<span>${label} <b>${escapeHtml(value)}</b></span>`).join('');
    return payload;
  }catch(error){
    $('storageLog').textContent=error.message;
    throw error;
  }
}

async function cleanupStorage(dryRun){
  const category=value('cleanupCategory'),days=Number(value('cleanupDays'));
  if(!dryRun&&!confirm(`确定清理当前账号服务器存储中的“${$('cleanupCategory').selectedOptions[0].textContent}”吗？`))return;
  try{
    const payload=await api('/api/v1/storage/cleanup',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({categories:[category],older_than_days:days,dry_run:dryRun})
    });
    $('storageLog').textContent=dryRun
      ?`预计可释放 ${formatBytes(payload.reclaimable_bytes)}，涉及 ${payload.candidate_count} 个目录。`
      :`已释放约 ${formatBytes(payload.reclaimable_bytes)}，删除 ${payload.deleted.length} 个目录。`;
    if(!dryRun)await refreshStorage();
  }catch(error){
    $('storageLog').textContent=error.message;
  }
}

$('refreshStorage').onclick=refreshStorage;
$('previewCleanup').onclick=()=>cleanupStorage(true);
$('executeCleanup').onclick=()=>cleanupStorage(false);
