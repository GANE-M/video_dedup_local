const renderJobBase=showJob;
showJob=function(job){
  renderJobBase(job);
  const resumable=['failed','cancelled','paused'].includes(job.status);
  $('resume').disabled=!resumable;
  if(job.workflow?.next_stage){
    $('jobStatus').title=`下一阶段：${job.workflow.next_stage}`;
  }
};

$('resume').onclick=async()=>{
  if(!current)return;
  try{
    const payload=await api(`/api/v1/jobs/${current.id}/resume`,{method:'POST'});
    $('message').textContent=payload.status==='recap_ready'
      ?'已恢复到解说预览/最终渲染阶段'
      :'已从最后完成的阶段检查点加入队列';
    $('resume').disabled=true;
    lastEvent=0;
    $('events').textContent='等待恢复日志…';
    await refreshJobs();
    poll();
  }catch(error){
    $('message').textContent=`断点续做失败：${error.message}`;
  }
};
