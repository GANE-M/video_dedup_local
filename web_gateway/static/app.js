// Page bootstrap. Feature code lives in focused modules under /static/js.
const frontendBuildVersion='20260727-28';
let dismissedBackendBuild='';
const refreshNotice=document.getElementById('versionRefreshNotice');

function showVersionRefreshNotice(backendBuild){
  if(!refreshNotice||dismissedBackendBuild===backendBuild)return;
  const label=backendBuild
    ?'服务器已经更新，请按 Ctrl+F5 刷新页面。'
    :'网页已更新，但后端仍是旧版本。请等待服务器重启后按 Ctrl+F5 刷新。';
  refreshNotice.querySelector('strong').textContent=label;
  refreshNotice.classList.remove('hidden');
}

refreshNotice?.addEventListener('mouseenter',()=>{
  dismissedBackendBuild=refreshNotice.dataset.backendBuild||'legacy';
  refreshNotice.classList.add('hidden');
});

async function checkBackendVersion(){
  try{
    const response=await fetch(`/health?_=${Date.now()}`,{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const payload=await response.json(),backendBuild=String(payload.build_version||'');
    document.getElementById('health').textContent=`服务正常 · 字幕${payload.limits.subtitle_workers}并发 · 纯剪辑${payload.limits.video_workers}并发`;
    if(refreshNotice)refreshNotice.dataset.backendBuild=backendBuild||'legacy';
    if(backendBuild!==frontendBuildVersion)showVersionRefreshNotice(backendBuild);
  }catch(error){
    document.getElementById('health').textContent='服务不可用';
  }
}

checkBackendVersion();
setInterval(checkBackendVersion,60000);

const startControl=document.getElementById('start');
const messageControl=document.getElementById('message');
const modulesReady=
  typeof connect==='function' &&
  typeof selectedEntries==='function' &&
  typeof refreshJobs==='function' &&
  typeof startControl?.onclick==='function';

if(!modulesReady){
  if(startControl)startControl.disabled=true;
  if(messageControl)messageControl.textContent='网页脚本版本不一致，请按 Ctrl+F5 强制刷新后重试';
}else if(document.getElementById('apiKey').value){
  connect();
}
