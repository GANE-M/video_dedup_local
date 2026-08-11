const state = { page: 1, pages: 1, total: 0 };
const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

function showToast(message, error = false) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.className = 'toast', 2800);
}

function categoryOptions(values, selected) {
  return values.map((value) => `<option${value === selected ? ' selected' : ''}>${value}</option>`).join('');
}

function render(items) {
  const grid = byId('libraryGrid');
  if (!items.length) {
    grid.innerHTML = '<div class="library-empty">没有发现符合条件的已处理短剧。<br>请确认目录中存在 processed 或 process 成品文件夹。</div>';
    return;
  }
  grid.innerHTML = items.map((item) => `
    <article class="drama-card" data-id="${item.id}">
      <a class="poster-link" href="${item.preview_url}" aria-label="试看 ${escapeHtml(item.display_title)}">
        ${item.cover_url ? `<img src="${item.cover_url}" alt="${escapeHtml(item.display_title)} 封面" loading="lazy">` : `<div class="poster-placeholder">${escapeHtml(item.display_title)}</div>`}
        <span class="play-badge">▶ ${item.video_count ? '试看' : '查看文件'}</span>
      </a>
      <div class="card-body">
        <h2 class="card-title" title="${escapeHtml(item.display_title)}">${escapeHtml(item.display_title)}</h2>
        ${item.title_zh && item.title_zh !== item.title ? `<p class="original-title" title="${escapeHtml(item.title)}">原名：${escapeHtml(item.title)}</p>` : ''}
        <p class="platform-line">${item.platform ? escapeHtml(item.platform) : '平台未记录'}</p>
        <p class="card-bio">${item.bio_zh || item.bio ? escapeHtml(item.bio_zh || item.bio) : '暂无简介'}</p>
        <div class="card-meta"><span>${escapeHtml(item.source_language || '语言未记录')}</span><span>${item.is_ai_generated === 'yes' ? 'AI 生成' : item.is_ai_generated === 'no' ? '非 AI 生成' : 'AI 状态未知'}</span></div>
        <div class="card-meta"><span>${escapeHtml(item.published_at)}</span><span>下载 ${item.download_count} 次</span></div>
        <div class="category-row">
          <select class="category-select audience-select" aria-label="受众分类">${categoryOptions(['男频','女频','中性'], item.audience_category)}</select>
          <select class="category-select setting-select" aria-label="题材分类">${categoryOptions(['魔幻','古装','现代'], item.setting_category)}</select>
        </div>
        <div class="card-actions">
          <a class="button secondary" href="${item.preview_url}">进入试看</a>
          <a class="button primary download-button" href="${item.download_url}">下载成品</a>
        </div>
      </div>
    </article>`).join('');

  grid.querySelectorAll('.category-select').forEach((select) => select.addEventListener('change', saveClassification));
  grid.querySelectorAll('.download-button').forEach((button) => button.addEventListener('click', () => {
    const meta = button.closest('.drama-card').querySelector('.card-meta span:last-child');
    const count = Number((meta.textContent.match(/\d+/) || ['0'])[0]) + 1;
    meta.textContent = `下载 ${count} 次`;
  }));
}

async function saveClassification(event) {
  const card = event.target.closest('.drama-card');
  const response = await portalFetch(`/api/library/${card.dataset.id}/classification`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      audience_category: card.querySelector('.audience-select').value,
      setting_category: card.querySelector('.setting-select').value,
    }),
  });
  if (!response.ok) return showToast((await response.json()).detail || '分类保存失败', true);
  showToast('分类已保存，后续扫描会保留人工设置。');
}

async function loadLibrary(resetPage = false) {
  if (resetPage) state.page = 1;
  const params = new URLSearchParams({
    page: state.page,
    page_size: 30,
    root: byId('libraryRoot').value.trim(),
    search: byId('librarySearch').value.trim(),
    audience: byId('audienceFilter').value,
    setting: byId('settingFilter').value,
  });
  byId('libraryGrid').innerHTML = '<div class="library-empty">正在扫描 processed / process 文件夹…</div>';
  byId('scanButton').disabled = true;
  try {
    const response = await portalFetch(`/api/library?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '视频库读取失败');
    state.pages = data.pages;
    state.total = data.total;
    byId('libraryRoot').value = data.root;
    localStorage.setItem('processedLibraryRoot', data.root);
    byId('libraryCount').className = `status-badge ${data.total ? 'success' : 'neutral'}`;
    byId('libraryCount').innerHTML = `<span></span>${data.total} 部成品短剧`;
    byId('libraryPageLabel').textContent = `第 ${state.page} / ${state.pages} 页 · 每页 30 部`;
    byId('libraryPrev').disabled = state.page <= 1;
    byId('libraryNext').disabled = state.page >= state.pages;
    render(data.items);
  } catch (error) {
    byId('libraryGrid').innerHTML = `<div class="library-empty">${escapeHtml(error.message)}</div>`;
    byId('libraryCount').className = 'status-badge error';
    byId('libraryCount').innerHTML = '<span></span>扫描失败';
    showToast(error.message, true);
  } finally {
    byId('scanButton').disabled = false;
  }
}

async function init() {
  await portalSession();
  const status = await portalFetch('/api/status').then((response) => response.json());
  byId('libraryRoot').value = localStorage.getItem('processedLibraryRoot') || status.default_library_root || status.default_output;
  byId('scanButton').addEventListener('click', () => loadLibrary(true));
  byId('filterButton').addEventListener('click', () => loadLibrary(true));
  byId('librarySearch').addEventListener('keydown', (event) => { if (event.key === 'Enter') loadLibrary(true); });
  byId('libraryPrev').addEventListener('click', () => { if (state.page > 1) { state.page--; loadLibrary(); } });
  byId('libraryNext').addEventListener('click', () => { if (state.page < state.pages) { state.page++; loadLibrary(); } });
  loadLibrary();
}

init().catch((error) => showToast(error.message, true));
