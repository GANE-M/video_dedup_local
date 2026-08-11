const PORTAL_PREFIX = '/beidou';

function portalPath(path) {
  if (/^https?:\/\//i.test(path)) return path;
  if (path.startsWith(PORTAL_PREFIX + '/')) return path;
  return `${PORTAL_PREFIX}${path.startsWith('/') ? path : `/${path}`}`;
}

function portalHeaders(headers = {}) {
  const result = new Headers(headers);
  const key = localStorage.getItem('videoGatewayKey') || '';
  if (key) result.set('X-API-Key', key);
  return result;
}

async function portalFetch(path, options = {}) {
  return fetch(portalPath(path), {
    ...options,
    credentials: 'same-origin',
    headers: portalHeaders(options.headers || {}),
  });
}

async function portalSession() {
  const key = localStorage.getItem('videoGatewayKey') || '';
  if (!key) throw new Error('请先返回处理工作台，填写访问密钥并连接服务。');
  const response = await portalFetch('/api/session', {method: 'POST'});
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}
