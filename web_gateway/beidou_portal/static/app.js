const state = {
  page: 1,
  pageSize: 20,
  total: 0,
  items: [],
  selected: new Set(),
  enums: { languages: [], apps: [], cover_host: "https://bj-play.inbeidou.cn" },
  jobId: null,
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);
const PLATFORM_ORDER = ["TikTok", "Facebook", "Instagram", "YouTube"];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await portalFetch(path, options);
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.className = "toast"; }, 4200);
}

function setBadge(node, text, type = "neutral") {
  node.className = `status-badge ${type}`;
  node.innerHTML = `<span></span>${escapeHtml(text)}`;
}

function filterPayload() {
  return {
    language: Number($("language").value),
    date_from: $("dateFrom").value,
    date_to: $("dateTo").value,
    app_id: $("appId").value,
    search: $("search").value.trim(),
  };
}

function appName(appId) {
  return state.enums.apps.find((item) => item.app_id === appId)?.app_name || appId || "—";
}

function languageName(id) {
  return state.enums.languages.find((item) => Number(item.id) === Number(id))?.name || id || "—";
}

function coverUrl(path) {
  if (!path) return "";
  return path.startsWith("http") ? path : `${state.enums.cover_host.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
}

function accessibleCount(item) {
  const total = Number(item.episode_count || 0);
  const locked = Number(item.locked_point || 0);
  return locked > 1 ? Math.min(total, locked - 1) : total;
}

async function loadStatus() {
  const status = await api("/api/status");
  $("outputDir").value = status.default_output;
  $("sleepSeconds").value = status.sleep_seconds;
  $("maxWorkers").value = status.max_workers || 2;
  if (status.auth.usable) {
    setBadge($("authBadge"), `登录文件可用 · ${status.auth.source_name}`, "success");
    $("uploadHint").textContent = `当前：${status.auth.source_name}。重新选择文件可替换。`;
  } else if (status.auth.uploaded) {
    setBadge($("authBadge"), "已上传，但缺少 Token", "error");
  }
}

async function loadEnums() {
  state.enums = await api("/api/enums");
  $("language").innerHTML = state.enums.languages.map((item) =>
    `<option value="${Number(item.id)}" ${Number(item.id) === 10 ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.en_short_name)}</option>`
  ).join("");
  $("appId").innerHTML = `<option value="">全部平台</option>` + state.enums.apps.map((item) =>
    `<option value="${escapeHtml(item.app_id)}">${escapeHtml(item.app_name)}</option>`
  ).join("");
}

async function uploadAuth(file) {
  const data = new FormData();
  data.append("file", file);
  $("uploadHint").textContent = "正在读取登录文件……";
  try {
    const result = await api("/api/auth/upload", { method: "POST", body: data });
    $("uploadHint").textContent = result.message;
    setBadge($("authBadge"), result.usable ? `登录文件可用 · ${file.name}` : "已上传，但缺少 Token", result.usable ? "success" : "error");
    toast(result.message, !result.usable);
  } catch (error) {
    $("uploadHint").textContent = error.message;
    toast(error.message, true);
  }
}

async function searchCatalog(resetPage = false) {
  if (resetPage) {
    state.page = 1;
    state.selected.clear();
  }
  $("catalogBody").innerHTML = `<tr><td colspan="7" class="empty-state">正在读取目录……</td></tr>`;
  $("searchButton").disabled = true;
  try {
    const result = await api("/api/catalog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...filterPayload(), page: state.page, page_size: state.pageSize }),
    });
    state.total = result.total;
    state.items = result.items;
    renderCatalog();
  } catch (error) {
    $("catalogBody").innerHTML = `<tr><td colspan="7" class="empty-state">${escapeHtml(error.message)}</td></tr>`;
    toast(error.message, true);
  } finally {
    $("searchButton").disabled = false;
  }
}

function renderCatalog() {
  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("catalogSummary").textContent = `找到 ${state.total.toLocaleString()} 部符合条件的短剧。`;
  $("pageLabel").textContent = `第 ${state.page} / ${pageCount} 页`;
  $("prevPage").disabled = state.page <= 1;
  $("nextPage").disabled = state.page >= pageCount;
  $("downloadFiltered").disabled = state.total === 0;
  if (!state.items.length) {
    $("catalogBody").innerHTML = `<tr><td colspan="7" class="empty-state">没有符合条件的短剧</td></tr>`;
    updateSelection();
    return;
  }
  $("catalogBody").innerHTML = state.items.map((item) => {
    const id = Number(item.task_id);
    const checked = state.selected.has(id) ? "checked" : "";
    const cover = coverUrl(item.cover);
    const accessible = accessibleCount(item);
    return `<tr>
      <td class="check-cell"><input class="row-check" data-id="${id}" type="checkbox" ${checked} aria-label="选择 ${escapeHtml(item.title)}"></td>
      <td><div class="drama-cell">
        ${cover ? `<img class="cover" src="${escapeHtml(cover)}" alt="" loading="lazy">` : `<span class="cover"></span>`}
        <div><strong>${escapeHtml(item.title)}</strong><small>Task ${id}</small></div>
      </div></td>
      <td><span class="pill">${escapeHtml(languageName(item.language))}</span></td>
      <td>${escapeHtml(appName(item.app_id))}</td>
      <td>${escapeHtml(String(item.publish_at || "").slice(0, 10) || "—")}</td>
      <td>${Number(item.episode_count || 0)}</td>
      <td><span class="available">${accessible} 集</span></td>
    </tr>`;
  }).join("");
  document.querySelectorAll(".row-check").forEach((input) => {
    input.addEventListener("change", () => {
      const id = Number(input.dataset.id);
      input.checked ? state.selected.add(id) : state.selected.delete(id);
      updateSelection();
    });
  });
  updateSelection();
}

function updateSelection() {
  $("selectedCount").textContent = state.selected.size;
  $("downloadSelected").disabled = state.selected.size === 0;
  const pageIds = state.items.map((item) => Number(item.task_id));
  $("selectPage").checked = pageIds.length > 0 && pageIds.every((id) => state.selected.has(id));
  $("selectPage").indeterminate = pageIds.some((id) => state.selected.has(id)) && !$("selectPage").checked;
}

async function startJob(mode) {
  const outputDir = $("outputDir").value.trim();
  if (!outputDir) return toast("请填写保存目录。", true);
  const countText = mode === "filtered" ? `${state.total} 部筛选结果` : `${state.selected.size} 部已选短剧`;
  if (!confirm(`确认开始下载 ${countText}？\n\n同时下载 ${$("maxWorkers").value} 部短剧，每隔至少 ${$("sleepSeconds").value} 秒启动一部；已下载文件会自动跳过。`)) return;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...filterPayload(),
        mode,
        task_ids: [...state.selected],
        output_dir: outputDir,
        sleep_seconds: Number($("sleepSeconds").value || 5),
        max_workers: Number($("maxWorkers").value || 2),
        include_cps: true,
      }),
    });
    state.jobId = job.id;
    $("cancelJob").disabled = false;
    renderJob(job);
    beginPolling();
    toast("下载任务已开始。");
    document.querySelector(".job-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(error.message, true);
  }
}

function beginPolling() {
  clearInterval(state.pollTimer);
  pollJob();
  state.pollTimer = setInterval(pollJob, 1500);
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const job = await api(`/api/jobs/${state.jobId}`);
    renderJob(job);
    if (["complete", "complete_with_errors", "failed", "cancelled"].includes(job.status)) {
      clearInterval(state.pollTimer);
      $("cancelJob").disabled = true;
    }
  } catch (error) {
    clearInterval(state.pollTimer);
    toast(error.message, true);
  }
}

function renderJob(job) {
  const labels = {
    queued: ["等待启动", "running"], scanning: ["扫描目录", "running"], running: ["首轮下载中", "running"],
    retrying: [`失败重试中${job.retry_round ? ` · 第 ${job.retry_round}/3 轮` : ''}`, "running"],
    complete: ["已完成", "success"], complete_with_errors: ["完成 · 有放弃项", "error"], failed: ["任务失败", "error"], cancelled: ["已取消", "error"],
  };
  const [label, type] = labels[job.status] || [job.status, "neutral"];
  setBadge($("jobStatus"), label, type);
  $("currentTitle").textContent = job.current_title || "—";
  $("outputPath").textContent = job.output_dir || "";
  $("seriesMetric").textContent = `${job.series_complete || 0} / ${job.series_total || 0}`;
  $("episodeMetric").textContent = job.episode_complete || 0;
  $("skipMetric").textContent = (job.episode_skipped || 0) + (job.series_deduplicated || 0);
  $("retryMetric").textContent = job.series_retry_pending || 0;
  $("failMetric").textContent = job.series_abandoned || 0;
  const resolved = (job.series_complete || 0) + (job.series_abandoned || 0);
  const progress = job.series_total ? Math.min(100, Math.round((resolved / job.series_total) * 100)) : 0;
  $("progressBar").style.width = `${["complete", "complete_with_errors"].includes(job.status) ? 100 : Math.max(0, progress)}%`;
  $("logConsole").innerHTML = job.logs.length ? job.logs.map((line) =>
    `<p class="log-line ${escapeHtml(line.level)}"><span class="time">${escapeHtml(line.time)}</span>${escapeHtml(line.message)}</p>`
  ).join("") : `<p class="muted-log">任务正在准备……</p>`;
  $("logConsole").scrollTop = $("logConsole").scrollHeight;
}

async function cancelJob() {
  if (!state.jobId || !confirm("确认取消当前下载任务？已完成文件和断点记录会保留。")) return;
  try {
    await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
    toast("已发出取消请求。");
  } catch (error) { toast(error.message, true); }
}

function bindEvents() {
  $("authFile").addEventListener("change", (event) => event.target.files[0] && uploadAuth(event.target.files[0]));
  const uploadBox = $("uploadBox");
  ["dragenter", "dragover"].forEach((name) => uploadBox.addEventListener(name, (event) => { event.preventDefault(); uploadBox.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((name) => uploadBox.addEventListener(name, (event) => { event.preventDefault(); uploadBox.classList.remove("dragging"); }));
  uploadBox.addEventListener("drop", (event) => event.dataTransfer.files[0] && uploadAuth(event.dataTransfer.files[0]));
  $("searchButton").addEventListener("click", () => searchCatalog(true));
  $("search").addEventListener("keydown", (event) => { if (event.key === "Enter") searchCatalog(true); });
  $("prevPage").addEventListener("click", () => { state.page -= 1; searchCatalog(); });
  $("nextPage").addEventListener("click", () => { state.page += 1; searchCatalog(); });
  $("selectPage").addEventListener("change", (event) => {
    state.items.forEach((item) => event.target.checked ? state.selected.add(Number(item.task_id)) : state.selected.delete(Number(item.task_id)));
    renderCatalog();
  });
  $("downloadSelected").addEventListener("click", () => startJob("selected"));
  $("downloadFiltered").addEventListener("click", () => startJob("filtered"));
  $("cancelJob").addEventListener("click", cancelJob);
  $("clearLog").addEventListener("click", () => { $("logConsole").innerHTML = `<p class="muted-log">显示已清空；后台任务不受影响。</p>`; });
}

async function initialize() {
  await portalSession();
  bindEvents();
  try {
    await Promise.all([loadStatus(), loadEnums()]);
    await searchCatalog(true);
    const jobs = await api("/api/jobs");
    if (jobs.length) {
      state.jobId = jobs[0].id;
      renderJob(jobs[0]);
      if (["queued", "scanning", "running", "retrying"].includes(jobs[0].status)) {
        $("cancelJob").disabled = false;
        beginPolling();
      }
    }
  } catch (error) {
    toast(error.message, true);
  }
}

initialize();
