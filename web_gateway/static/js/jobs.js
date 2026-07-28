$('start').onclick=async()=>{try{const entries=selectedEntries(),videos=entries.filter(x=>x.role==='video'),series=$('series').value.trim(),subtitle=$('enableSubtitles').checked,recap=$('enableRecap').checked,dedup=$('enableDedup').checked;if(!subtitle&&!recap&&!dedup)throw new Error('请至少勾选字幕、解说或去重中的一个阶段');if(recap&&dedup)throw new Error('解说成片生成后才能执行二次去重；当前请先运行“字幕 + 解说”，成片确认后再单独运行去重');if(!$('apiKey').value.trim()||!series||!videos.length)throw new Error('请填写密钥、剧名并选择视频');if(dedup&&!outputDirectoryHandle)throw new Error('启用去重时请选择成片保存文件夹');if($('translationBackend').value==='api'&&subtitle&&$('llmApiKey').value.trim().length<8)throw new Error('API模式需要填写LLM API Key');$('start').disabled=true;$('message').textContent='创建任务…';const settings=settingsPayload();const body={series_name:series,project_name:selectedSourceFolderName||inferredSeriesName(videos.map(x=>x.file))||series,files:entries.map(x=>({name:x.file.name,size:x.file.size,role:x.role})),settings,llm_api_key:$('translationBackend').value==='api'&&subtitle?$('llmApiKey').value.trim():null};current=await api('/api/v1/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});showJob(current);if(subtitle&&$('translationBackend').value==='agent'){showAgent(current.agent_bootstrap)}$('resultCard').classList.remove('hidden');$('cancel').disabled=false;lastEvent=0;$('events').textContent='等待日志…';await uploadPool(entries,current.uploads,current.chunk_size);await api(`/api/v1/jobs/${current.id}/start`,{method:'POST'});$('message').textContent=recap?(subtitle?'上传完成，后端将依次执行字幕与解说':'上传完成，后端正在建立解说项目'):'上传完成，已加入处理队列';await refreshJobs();poll()}catch(error){$('message').textContent=error.message;$('start').disabled=false}};
$('cancel').onclick=async()=>{if(!current)return;try{await api(`/api/v1/jobs/${current.id}/cancel`,{method:'POST'});$('message').textContent='已请求停止任务'}catch(error){$('message').textContent=error.message}};

function showAgent(bootstrap){if(!bootstrap)return;$('agentCommand').textContent=bootstrap.command;$('agentCard').classList.remove('hidden')}
async function copyText(text){const value=String(text||'');if(!value)throw new Error('没有可复制的命令');if(navigator.clipboard?.writeText){try{await navigator.clipboard.writeText(value);return}catch{}}const textarea=document.createElement('textarea');textarea.value=value;textarea.setAttribute('readonly','');textarea.style.position='fixed';textarea.style.left='-9999px';textarea.style.opacity='0';document.body.append(textarea);textarea.select();textarea.setSelectionRange(0,textarea.value.length);let copied=false;try{copied=document.execCommand('copy')}finally{textarea.remove()}if(!copied)throw new Error('浏览器拒绝访问剪贴板，请点击下方“复制初始化命令”或手动选择命令')}
$('copyAgent').onclick=async()=>{try{await copyText($('agentCommand').textContent);$('message').textContent='Agent初始化命令已复制'}catch(error){$('message').textContent=error.message}};
$('webAgentInit').onclick=async()=>{try{const payload=await api('/api/v1/agent-session');$('webAgentCommand').textContent=payload.command;$('webAgentCommand').classList.remove('hidden');$('webAgentCopy').classList.remove('hidden');await copyText(payload.command);$('webAgentStatus').textContent=`初始化命令已自动复制 · 代次 ${payload.generation} · 最多并发 ${payload.maximum_parallel}`}catch(error){$('webAgentStatus').textContent=error.message}};
$('webAgentCopy').onclick=async()=>{try{await copyText($('webAgentCommand').textContent);$('webAgentStatus').textContent='初始化命令已复制'}catch(error){$('webAgentStatus').textContent=error.message}};
$('webAgentTest').onclick=async()=>{try{const state=await api('/api/v1/agent-session/status');const capability=state.capabilities_verified?' · 子 Agent 隔离已验证':' · 尚未验证子 Agent 能力';$('webAgentStatus').textContent=!state.initialized?'Agent 未初始化':state.connected?`通信正常 · 心跳约 ${Math.round(state.heartbeat_age_seconds)} 秒前${capability}`:state.enabled?`已初始化，但近3分钟没有监听心跳${capability}`:'监听已停止'}catch(error){$('webAgentStatus').textContent=error.message}};
$('webAgentStop').onclick=async()=>{if(!confirm('这会使当前 Agent 对话令牌立即失效，确定停止吗？'))return;try{const result=await api('/api/v1/agent-session/stop',{method:'POST'});$('webAgentStatus').textContent=result.note;$('webAgentCommand').classList.add('hidden');$('webAgentCopy').classList.add('hidden')}catch(error){$('webAgentStatus').textContent=error.message}};
function showJob(job){$('resultCard').classList.remove('hidden');$('jobId').textContent=job.id;$('version').textContent='v'+String(job.version).padStart(4,'0');$('jobStatus').textContent=job.status;$('queuePosition').textContent=(job.queue_position||'-')+(` / 等待${job.queue?.waiting??0}`)}
function showJobNoticeOnce(job,kind,title,detail,token=''){
  if(!job?.id)return;
  const marker=String(token||job.updated_at||job.status||'').replace(/[^\w:.-]/g,'_');
  const key=`video-tool-notice:${job.id}:${kind}:${marker}`;
  try{if(localStorage.getItem(key)==='acknowledged')return}catch{}
  alert(`${title}\n\n任务：${job.series_name||job.id}\n任务 ID：${job.id}\n${detail}`);
  try{localStorage.setItem(key,'acknowledged')}catch{}
}
async function refreshJobs(){try{const payload=await api('/api/v1/jobs?limit=100');const jobs=payload.jobs||payload;$('jobList').innerHTML=jobs.length?jobs.map(j=>`<div class="job-row" data-id="${j.id}"><strong>${escapeHtml(j.series_name)}</strong><span>v${String(j.version).padStart(4,'0')}</span><span>${escapeHtml(j.status)}</span><span>${escapeHtml(j.created_at)}</span></div>`).join(''):'<span class="muted">暂无任务</span>';$('jobList').querySelectorAll('.job-row').forEach(row=>row.onclick=()=>openJob(row.dataset.id))}catch(error){$('jobList').innerHTML=`<span class="muted">${escapeHtml(error.message)}</span>`}}
$('refreshJobs').onclick=refreshJobs;
async function fetchArtifactBlob(path){const response=await fetch(`/api/v1/jobs/${current.id}/artifacts/${encodeURI(path)}`,{headers:{'X-API-Key':$('apiKey').value.trim()}});if(!response.ok)throw new Error('下载失败');return response.blob()}
async function downloadArtifact(path){const blob=await fetchArtifactBlob(path),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=path.split('/').at(-1);a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function videoArtifacts(items=latestArtifacts){return items.filter(item=>item.path.startsWith('videos/')&&videoSuffixPattern.test(item.path))}
function recapVideoArtifacts(items=latestArtifacts){return videoArtifacts(items).filter(item=>item.path.startsWith('videos/recap/'))}
function processedVideoArtifacts(items=latestArtifacts){return videoArtifacts(items).filter(item=>!item.path.startsWith('videos/recap/'))}
async function readSubtitleFinals(projectHandle){const files=[];try{const folder=await projectHandle.getDirectoryHandle('字幕终稿');for await(const entry of folder.values())if(entry.kind==='file'&&/\.srt$/i.test(entry.name)&&!/__[^.]*\.srt$/i.test(entry.name))files.push(await entry.getFile())}catch(error){if(error.name!=='NotFoundError')throw error}return files}
async function chooseSourceFolder(){if(!window.showDirectoryPicker)throw new Error('当前浏览器不支持工程文件夹授权，请使用最新版 Chrome 或 Edge');const handle=await window.showDirectoryPicker({mode:'readwrite'}),files=[];for await(const entry of handle.values())if(entry.kind==='file'&&videoSuffixPattern.test(entry.name))files.push(await entry.getFile());files.sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true,sensitivity:'base'}));sourceDirectoryHandle=handle;directoryVideoFiles=files;directorySubtitleFiles=await readSubtitleFinals(handle);selectedSourceFolderName=handle.name;outputDirectoryHandle=await handle.getDirectoryHandle('processed',{create:true});$('outputFolderStatus').textContent=`默认保存到：${handle.name}/processed（可手动更换）`;$('files').value='';$('folderFiles').value='';renderVideoSelection();const activeCount=activeSubtitleFiles(files).length;$('message').textContent=`工程已载入：${files.length} 个一级视频；将上传 ${activeCount} 个活动字幕（每集最多原文+目标译文），已忽略历史版本、其他语言和JSON`;if(!files.length)throw new Error('所选文件夹第一层没有视频')}
async function chooseOutputFolder(){if(!window.showDirectoryPicker)throw new Error('当前浏览器不支持文件夹直写，请使用最新版 Chrome 或 Edge');outputDirectoryHandle=await window.showDirectoryPicker({mode:'readwrite'});$('outputFolderStatus').textContent=`保存到：${outputDirectoryHandle.name}`;$('saveArtifacts').disabled=!videoArtifacts().length}
async function requireWritePermission(handle,label){if(!handle)throw new Error(`请先选择${label}`);if(handle.queryPermission&&await handle.queryPermission({mode:'readwrite'})!=='granted'&&await handle.requestPermission({mode:'readwrite'})!=='granted')throw new Error(`没有获得${label}写入权限`)}
async function childDirectory(root,parts){let directory=root;for(const part of parts)directory=await directory.getDirectoryHandle(part,{create:true});return directory}
async function writeArtifact(root,relativePath,blob){const parts=relativePath.split('/').filter(Boolean),name=parts.pop(),directory=await childDirectory(root,parts),handle=await directory.getFileHandle(name,{create:true}),writer=await handle.createWritable();await writer.write(blob);await writer.close()}
async function saveVideosToFolder(items=latestArtifacts){const videos=processedVideoArtifacts(items),recaps=recapVideoArtifacts(items),projectArtifacts=items.filter(x=>/^(subtitles|logs|agent|config)\//.test(x.path));if(!videos.length&&!recaps.length&&!projectArtifacts.length)throw new Error('当前任务没有可保存的成片、字幕或记录');if(videos.length)await requireWritePermission(outputDirectoryHandle,'成片保存文件夹');if(sourceDirectoryHandle&&(recaps.length||projectArtifacts.length))await requireWritePermission(sourceDirectoryHandle,'工程文件夹');const recordName=current?.result?.record_name||current?.id||'unknown-job';let completed=0,projectFiles=0;for(const artifact of videos){$('message').textContent=`保存成片 ${completed+1}/${videos.length}：${artifact.path.split('/').at(-1)}`;await writeArtifact(outputDirectoryHandle,artifact.path.slice('videos/'.length),await fetchArtifactBlob(artifact.path));completed++}if(sourceDirectoryHandle){for(const artifact of recaps){const target=`解说/${artifact.path.slice('videos/recap/'.length)}`;$('message').textContent=`保存解说成片：${target}`;await writeArtifact(sourceDirectoryHandle,target,await fetchArtifactBlob(artifact.path));completed++}for(const artifact of projectArtifacts){const target=artifact.path.startsWith('subtitles/')?`字幕终稿/${artifact.path.slice('subtitles/'.length)}`:`任务记录/${recordName}/${artifact.path}`;$('message').textContent=`同步工程记录：${target}`;await writeArtifact(sourceDirectoryHandle,target,await fetchArtifactBlob(artifact.path));projectFiles++}}else if(recaps.length){for(const artifact of recaps){await writeArtifact(outputDirectoryHandle,`解说/${artifact.path.slice('videos/recap/'.length)}`,await fetchArtifactBlob(artifact.path));completed++}}$('message').textContent=(completed?`已保存 ${completed} 个视频成果`:'未生成视频')+(sourceDirectoryHandle?`，并同步 ${projectFiles} 个字幕/记录文件到 ${sourceDirectoryHandle.name}`:'')}
$('chooseOutputFolder').onclick=async()=>{try{await chooseOutputFolder()}catch(error){if(error.name!=='AbortError')$('message').textContent=error.message}};
$('chooseSourceFolder').onclick=async()=>{try{await chooseSourceFolder()}catch(error){if(error.name!=='AbortError')$('message').textContent=error.message}};
$('saveArtifacts').onclick=async()=>{try{await saveVideosToFolder()}catch(error){$('message').textContent=error.message}};
async function poll(){
  if(!current)return;
  clearTimeout(pollTimer);
  try{
    const job=await api(`/api/v1/jobs/${current.id}`);
    current=job;
    showJob(job);
    if(job.status==='waiting_agent')$('message').textContent='本地识别已完成，正在等待 Agent 翻译提交（任务未取消）';
    if(job.status==='waiting_recap_agent')$('message').textContent='字幕已完成，正在等待解说 Agent 编排完整时间轴';
    if(job.status==='recap_rendering')$('message').textContent='正在生成最终解说成片';
    if(job.status==='recap_ready'){
      if(!loadedRecapReadyJobs.has(job.id)){
        loadedRecapReadyJobs.add(job.id);
        await loadRecap(job.id);
        document.querySelector('.tab[data-tab="recap"]')?.click();
      }
      $('message').textContent='解说时间轴已就绪，请预览检查后生成最终成片';
      showJobNoticeOnce(job,'recap_ready','解说方案已完成','时间轴已通过校验。下一步：预览方案并生成最终成片。');
    }
    const eventPayload=await api(`/api/v1/jobs/${current.id}/events?after=${lastEvent}`);
    if(eventPayload.events.length){
      lastEvent=eventPayload.events.at(-1).id;
      const text=eventPayload.events.map(x=>`[${x.created_at}] ${x.message}`).join('\n');
      $('events').textContent=($('events').textContent==='等待日志…'?'':$('events').textContent+'\n')+text;
      $('events').scrollTop=$('events').scrollHeight;
    }
    if(['completed','failed','cancelled'].includes(job.status)){
      const artifacts=await api(`/api/v1/jobs/${current.id}/artifacts`);
      latestArtifacts=artifacts.artifacts;
      const hasProjectArtifacts=latestArtifacts.some(x=>/^(subtitles|logs|agent|config)\//.test(x.path));
      $('saveArtifacts').disabled=!(outputDirectoryHandle&&videoArtifacts().length)&&!(sourceDirectoryHandle&&hasProjectArtifacts);
      $('artifacts').innerHTML=latestArtifacts.map(x=>`<a href="#" data-path="${escapeHtml(x.path)}">${escapeHtml(x.path)} · ${(x.size/1048576).toFixed(2)} MB</a>`).join('')||'<span class="muted">没有可下载文件</span>';
      $('artifacts').querySelectorAll('a').forEach(a=>a.onclick=async event=>{event.preventDefault();try{await downloadArtifact(a.dataset.path)}catch(error){$('message').textContent=error.message}});
      $('message').textContent=job.status==='completed'?'任务完成':(job.error||job.status);
      if(job.status==='completed')showJobNoticeOnce(job,'completed','任务已全部完成','最终产物已经生成，请检查并保存成片。');
      if(job.status==='failed')showJobNoticeOnce(job,'failed','任务执行失败',job.error||'请查看任务日志后重试。');
      if(job.status==='cancelled')showJobNoticeOnce(job,'cancelled','任务已取消','任务已停止，未完成阶段不会继续运行。');
      if(job.status==='completed'&&$('autoSaveOutputs').checked&&(outputDirectoryHandle||sourceDirectoryHandle)&&!autoSavedJobs.has(job.id)){
        autoSavedJobs.add(job.id);
        try{await saveVideosToFolder(latestArtifacts)}catch(error){autoSavedJobs.delete(job.id);$('message').textContent=`任务完成，但自动保存失败：${error.message}`}
      }
      $('start').disabled=false;
      $('cancel').disabled=true;
      refreshJobs();
      return;
    }
  }catch(error){$('message').textContent=error.message}
  pollTimer=setTimeout(poll,3000);
}

// Selecting a historical job must also bind its recap project. Previously the
// job summary changed while recapProject stayed null/stale, which made the
// "submit/retry recap" button fail before it ever called the backend.
openJob=async function(id){
  try{
    current=await api(`/api/v1/jobs/${id}`);
    recapProject=null;
    recapProjectJobId=null;
    recapPlanning={status:'not_queued'};
    lastEvent=0;
    $('events').textContent='等待日志…';
    showJob(current);
    const bootstrap=await api(`/api/v1/jobs/${id}/agent-bootstrap`);
    showAgent(bootstrap);
    if(current.settings?.pipeline?.enable_recap){
      try{await loadRecap(id)}
      catch(error){if(error.status!==404)throw error}
    }
    $('cancel').disabled=['completed','failed','cancelled'].includes(current.status);
    poll();
  }catch(error){
    $('message').textContent=error.message;
  }
};
