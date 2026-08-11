const byId = (id) => document.getElementById(id);
const seriesId = location.pathname.split('/').filter(Boolean).pop();

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

function humanSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function playFile(file, button) {
  const player = byId('previewPlayer');
  player.src = file.media_url;
  player.hidden = false;
  byId('noVideo').hidden = true;
  document.querySelectorAll('.video-item').forEach((item) => item.classList.remove('active'));
  button?.classList.add('active');
  player.load();
}

async function init() {
  await portalSession();
  const response = await portalFetch(`/api/library/${seriesId}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '短剧读取失败');
  document.title = `${data.display_title} · 短剧试看`;
  byId('previewTitle').textContent = data.display_title;
  if (data.title_zh && data.title_zh !== data.title) {
    byId('previewOriginalTitle').hidden = false;
    byId('previewOriginalTitle').textContent = `原名：${data.title}`;
  }
  byId('previewBio').textContent = data.bio_zh || data.bio || '暂无简介';
  byId('previewPlatform').textContent = data.platform || '未记录';
  byId('previewDate').textContent = data.published_at;
  byId('previewDownloads').textContent = `${data.download_count} 次`;
  byId('previewFiles').textContent = `${data.files.length} 个`;
  byId('previewTags').innerHTML = `<span>${escapeHtml(data.audience_category)}</span><span>${escapeHtml(data.setting_category)}</span>`;
  byId('previewDownload').href = data.download_url;
  byId('previewDownload').addEventListener('click', () => {
    const next = Number((byId('previewDownloads').textContent.match(/\d+/) || ['0'])[0]) + 1;
    byId('previewDownloads').textContent = `${next} 次`;
  });

  const videos = data.files.filter((file) => file.kind === 'video');
  const list = byId('videoList');
  if (!videos.length) {
    byId('previewPlayer').hidden = true;
    byId('noVideo').hidden = false;
    list.innerHTML = '<p class="scope-note">没有视频文件。</p>';
  } else {
    list.innerHTML = videos.map((file, index) => `<button class="video-item${index === 0 ? ' active' : ''}" data-index="${index}"><strong>${escapeHtml(file.relative_path)}</strong><small>${humanSize(file.size)}</small></button>`).join('');
    list.querySelectorAll('.video-item').forEach((button) => button.addEventListener('click', () => playFile(videos[Number(button.dataset.index)], button)));
    playFile(videos[0], list.querySelector('.video-item'));
  }

  const images = data.files.filter((file) => file.kind === 'image');
  if (images.length) {
    byId('imageSection').hidden = false;
    byId('imageList').innerHTML = images.map((file) => `<a href="${file.media_url}" target="_blank" title="${escapeHtml(file.relative_path)}"><img src="${file.media_url}" loading="lazy" alt="${escapeHtml(file.name)}"></a>`).join('');
  }
}

init().catch((error) => {
  byId('previewTitle').textContent = error.message;
  byId('previewPlayer').hidden = true;
  byId('noVideo').hidden = false;
  byId('noVideo').textContent = '无法打开此短剧，请返回视频库重新扫描。';
});
