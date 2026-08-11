async function runEnvironmentPreflight(){
  try{
    if(!$('apiKey').value.trim())throw new Error('请先填写访问密钥');
    $('message').textContent='正在检查服务器环境…';
    const settings=schema?settingsPayload():{};
    const report=await api('/api/v1/preflight',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({settings})
    });
    const lines=report.checks.map(item=>`${item.ok?'✓':'✗'} ${item.name}: ${item.detail}`);
    $('message').textContent=report.ok?'服务器环境检查通过':`环境缺少：${report.required_failures.join('、')}`;
    alert(lines.join('\n'));
  }catch(error){
    $('message').textContent=`环境检查失败：${error.message}`;
  }
}

$('runPreflight').onclick=runEnvironmentPreflight;
