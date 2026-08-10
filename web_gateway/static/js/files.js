const videoSuffixPattern=/\.(mp4|mov|mkv|avi|webm|m4v)$/i;
const projectMaterialPattern=/\.(png|txt)$/i;
function relativeParts(file){return String(file.webkitRelativePath||'').replace(/\\/g,'/').split('/').filter(Boolean)}
function isDirectFolderVideo(file){const parts=relativeParts(file);return videoSuffixPattern.test(file.name)&&parts.length===2}
function selectedVideoFiles(){if(sourceDirectoryHandle)return directoryVideoFiles;const folder=[...$('folderFiles').files].filter(isDirectFolderVideo);return $('folderFiles').files.length?folder:[...$('files').files].filter(file=>videoSuffixPattern.test(file.name))}
function commonPrefix(values){if(!values.length)return'';let prefix=values[0];for(const value of values)while(prefix&&!value.startsWith(prefix))prefix=prefix.slice(0,-1);return prefix}
function inferredSeriesName(files){if(!files.length)return'';if(selectedSourceFolderName)return selectedSourceFolderName;const relative=files[0].webkitRelativePath||'';if(relative.includes('/'))return relative.split('/')[0];const stems=files.map(file=>file.name.replace(/\.[^.]+$/,''));if(stems.length===1)return stems[0];const prefix=commonPrefix(stems).replace(/[\s_.-]*\d*$/,'').replace(/[\s_.-]+$/,'');return prefix||stems[0]}
function selectedProjectFiles(){if(sourceDirectoryHandle)return directoryProjectFiles;const files=[...$('folderFiles').files].filter(file=>{const parts=relativeParts(file);return projectMaterialPattern.test(file.name)&&parts.length===2&&!/^TikTok发布信息\.txt$/i.test(file.name)});const png=files.filter(x=>/\.png$/i.test(x.name)).sort((a,b)=>a.name.localeCompare(b.name))[0],txt=files.filter(x=>/\.txt$/i.test(x.name)).sort((a,b)=>a.name.localeCompare(b.name))[0];return[png,txt].filter(Boolean)}
async function readProjectMaterials(handle){const files=[];for await(const entry of handle.values())if(entry.kind==='file'&&projectMaterialPattern.test(entry.name)&&!/^TikTok发布信息\.txt$/i.test(entry.name))files.push(await entry.getFile());const png=files.filter(x=>/\.png$/i.test(x.name)).sort((a,b)=>a.name.localeCompare(b.name))[0],txt=files.filter(x=>/\.txt$/i.test(x.name)).sort((a,b)=>a.name.localeCompare(b.name))[0];return[png,txt].filter(Boolean)}
function renderVideoSelection(){const files=selectedVideoFiles(),materials=selectedProjectFiles(),ignored=sourceDirectoryHandle?0:[...$('folderFiles').files].filter(file=>videoSuffixPattern.test(file.name)&&!isDirectFolderVideo(file)).length;$('fileList').innerHTML=files.map(file=>`<li>${escapeHtml(file.webkitRelativePath||file.name)} · ${(file.size/1048576).toFixed(1)} MB</li>`).join('')+materials.map(file=>`<li>发布素材：${escapeHtml(file.name)} · ${(file.size/1048576).toFixed(2)} MB</li>`).join('')+(ignored?`<li class="muted">已忽略子文件夹中的 ${ignored} 个视频</li>`:'');if(files.length){$('series').value=inferredSeriesName(files);$('recapProjectName').value=$('series').value;loadPreview(files[0])}persist()}
function clearDirectorySelection(){sourceDirectoryHandle=null;directoryVideoFiles=[];directorySubtitleFiles=[];directoryProjectFiles=[];selectedSourceFolderName=''}
$('files').addEventListener('change',()=>{if($('files').files.length){$('folderFiles').value='';clearDirectorySelection()}renderVideoSelection()});
$('folderFiles').addEventListener('change',()=>{if($('folderFiles').files.length){$('files').value='';clearDirectorySelection()}renderVideoSelection()});
const subtitleLanguageCodes={Chinese:'zh',English:'en',Arabic:'ar',Spanish:'es',French:'fr',German:'de',Portuguese:'pt',Japanese:'ja',Korean:'ko',Russian:'ru',Turkish:'tr',Indonesian:'id',Vietnamese:'vi',Thai:'th'};
function subtitleLanguageCode(language){const text=String(language||'').trim();return subtitleLanguageCodes[text]||text.toLowerCase().split(/[-_]/)[0]}
function escapeRegExp(text){return String(text).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}
function activeSubtitleTargetLanguage(){return $('enableSubtitles').checked?value('targetLanguage'):($('enableRecap').checked?value('recapTargetLanguage'):value('targetLanguage'))}
function activeSubtitleFiles(videos=selectedVideoFiles()){
  const targetCode=subtitleLanguageCode(activeSubtitleTargetLanguage()),sourceSetting=value('sourceLanguage'),ocrSetting=value('ocrLanguage'),preferredSource=subtitleLanguageCode(sourceSetting!=='auto'?sourceSetting:(ocrSetting!=='auto'?ocrSetting:'')),selected=[],used=new Set();
  for(const video of videos){
    const prefixes=[video.name,video.name.replace(/\.[^.]+$/,'')],matches=[];
    for(const file of directorySubtitleFiles){
      let match=null;
      for(const prefix of prefixes){
        const candidate=new RegExp(`^${escapeRegExp(prefix)}\\.(source|final)\\.([^.]+)\\.srt$`,'i').exec(file.name);
        if(candidate){match=candidate;break}
      }
      if(match)matches.push({file,role:match[1].toLowerCase(),language:subtitleLanguageCode(match[2])})
    }
    const sources=matches.filter(item=>item.role==='source').sort((a,b)=>(b.language===preferredSource)-(a.language===preferredSource)||b.file.lastModified-a.file.lastModified||a.file.name.localeCompare(b.file.name));
    const finals=matches.filter(item=>item.role==='final'&&item.language===targetCode).sort((a,b)=>b.file.lastModified-a.file.lastModified||a.file.name.localeCompare(b.file.name));
    for(const item of [sources[0],finals[0]]){
      if(item&&!used.has(item.file.name.toLowerCase())){used.add(item.file.name.toLowerCase());selected.push(item.file)}
    }
  }
  return selected
}
function selectedEntries(){const entries=selectedVideoFiles().map(file=>({file,role:'video'}));for(const file of activeSubtitleFiles())entries.push({file,role:'subtitle_final'});for(const file of selectedProjectFiles())entries.push({file,role:/\.png$/i.test(file.name)?'cover':'series_info'});const single=[['musicFile','music'],['borderFile','border'],['effectFile','effect']];for(const [id,role] of single){const file=$(id).files[0];if(file)entries.push({file,role})}for(const [id,role] of [['musicPool','music_pool'],['effectPool','effect_pool']])for(const file of $(id).files)entries.push({file,role});return entries}

async function digest(buffer){const hash=await crypto.subtle.digest('SHA-256',buffer);return[...new Uint8Array(hash)].map(x=>x.toString(16).padStart(2,'0')).join('')}
const uploadChunkMaximumAttempts=5;
const uploadCompletionMaximumAttempts=4;
function uploadDelay(milliseconds){return new Promise(resolve=>setTimeout(resolve,milliseconds))}
function chunkBounds(file,index,chunkSize){const start=index*chunkSize,end=Math.min(file.size,(index+1)*chunkSize);return{start,end,size:Math.max(0,end-start)}}
function updateUploadProgress(sent,total,note=''){const percent=total?Math.min(100,sent/total*100):100;$('uploadProgress').value=percent;$('message').textContent=`上传 ${(sent/1048576).toFixed(1)} / ${(total/1048576).toFixed(1)} MB${note?` · ${note}`:''}`}
async function uploadChunkTask(task,chunkSize){
  const bounds=chunkBounds(task.entry.file,task.chunkIndex,chunkSize),buffer=await task.entry.file.slice(bounds.start,bounds.end).arrayBuffer(),hash=await digest(buffer);
  await api(`/api/v1/jobs/${current.id}/uploads/${task.upload.id}/chunks/${task.chunkIndex}`,{method:'PUT',headers:{'X-Chunk-SHA256':hash,'Content-Type':'application/octet-stream'},body:buffer,signal:AbortSignal.timeout(120000)});
  return buffer.byteLength
}
async function completeUploadWithRetry(upload){
  if(upload.status==='completed')return;
  let lastError=null;
  for(let attempt=1;attempt<=uploadCompletionMaximumAttempts;attempt++){
    try{await api(`/api/v1/jobs/${current.id}/uploads/${upload.id}/complete`,{method:'POST',signal:AbortSignal.timeout(120000)});return}
    catch(error){
      lastError=error;
      const reconciled=await api(`/api/v1/jobs/${current.id}`),saved=reconciled.uploads.find(item=>item.id===upload.id);
      if(saved?.status==='completed')return;
      if(attempt<uploadCompletionMaximumAttempts){$('message').textContent=`文件确认失败，已排到确认队尾重试 ${attempt}/${uploadCompletionMaximumAttempts}`;await uploadDelay(Math.min(8000,1000*2**(attempt-1)))}
    }
  }
  throw new Error(`文件 ${upload.name} 完成确认失败：${lastError?.message||'未知错误'}`)
}
async function uploadPool(entries,uploads,chunkSize){
  if(entries.length!==uploads.length)throw new Error('本地文件与服务器上传清单数量不一致，请重新创建任务');
  const total=entries.reduce((sum,item)=>sum+item.file.size,0),queue=[];let sent=0,cursor=0,fatalError=null;
  entries.forEach((entry,index)=>{
    const upload=uploads[index],savedChunks=new Set(upload.uploaded_chunks||[]);
    if(upload.status==='completed'){sent+=entry.file.size;return}
    for(let chunkIndex=0;chunkIndex<upload.total_chunks;chunkIndex++){
      const bounds=chunkBounds(entry.file,chunkIndex,chunkSize);
      if(savedChunks.has(chunkIndex))sent+=bounds.size;
      else queue.push({entry,upload,chunkIndex,attempt:0,readyAt:0,size:bounds.size})
    }
  });
  updateUploadProgress(sent,total);
  const worker=async()=>{
    while(!fatalError){
      const queueIndex=cursor++;
      if(queueIndex>=queue.length)return;
      const task=queue[queueIndex],wait=Math.max(0,task.readyAt-Date.now());
      if(wait)await uploadDelay(wait);
      try{
        const bytes=await uploadChunkTask(task,chunkSize);
        sent+=bytes;
        updateUploadProgress(sent,total)
      }catch(error){
        task.attempt+=1;
        if(task.attempt>=uploadChunkMaximumAttempts){fatalError=new Error(`${task.entry.file.name} 第 ${task.chunkIndex+1} 块连续失败 ${task.attempt} 次：${error.message}`);return}
        task.readyAt=Date.now()+Math.min(16000,1000*2**(task.attempt-1));
        queue.push(task);
        updateUploadProgress(sent,total,`${task.entry.file.name} 第 ${task.chunkIndex+1} 块失败，已排到队尾重试 ${task.attempt}/${uploadChunkMaximumAttempts}`)
      }
    }
  };
  await Promise.all(Array.from({length:Math.min(3,Math.max(1,queue.length))},worker));
  if(fatalError)throw fatalError;
  let completionCursor=0;
  const completionWorker=async()=>{while(completionCursor<uploads.length){const index=completionCursor++;await completeUploadWithRetry(uploads[index])}};
  await Promise.all(Array.from({length:Math.min(3,uploads.length)},completionWorker));
  const reconciled=await api(`/api/v1/jobs/${current.id}`);
  if(reconciled.uploads.some(item=>item.status!=='completed'))throw new Error('仍有文件未完成上传，请保持页面开启后重试');
  current=reconciled;
  updateUploadProgress(total,total,'全部分块上传完成')
}
