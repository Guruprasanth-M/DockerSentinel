const BASE = '/api';
let token = localStorage.getItem('sentinel_token') || '';

export function setToken(val) {
    token = val;
    localStorage.setItem('sentinel_token', val);
}

export function getToken() {
    return token;
}

async function request(endpoint, options = {}) {
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers,
    };

    if (token) {
        headers['X-Sentinel-Token'] = token;
    }

    var maxRetries = options.method === 'POST' ? 1 : 2;

    for (var attempt = 0; attempt <= maxRetries; attempt++) {
        try {
            var controller = new AbortController();
            var timeout = setTimeout(function () { controller.abort(); }, 10000);

            var res = await fetch(BASE + endpoint, {
                ...options,
                headers: headers,
                signal: controller.signal,
            });
            clearTimeout(timeout);

            if (res.status === 401 || res.status === 403) {
                console.warn('[api] Auth error:', res.status);
                return null;
            }

            if (res.status === 429) {
                console.warn('[api] Rate limited');
                return null;
            }

            if (res.status >= 500 && attempt < maxRetries) {
                await new Promise(function (r) { setTimeout(r, 500 * (attempt + 1)); });
                continue;
            }

            if (!res.ok) {
                console.error('[api] Error:', res.status, res.statusText);
                return null;
            }

            return await res.json();
        } catch (err) {
            if (attempt < maxRetries) {
                await new Promise(function (r) { setTimeout(r, 500 * (attempt + 1)); });
                continue;
            }
            console.error('[api] Request failed:', err.message);
            return null;
        }
    }

    return null;
}

export function getHealth() {
    return request('/health');
}

export function getMetrics() {
    return request('/metrics');
}

export function getStatus() {
    return request('/status');
}

export function getProcesses(params = {}) {
    const p = new URLSearchParams();
    p.set('sort', params.sort || 'cpu');
    p.set('limit', params.limit || 50);
    if (params.flagged) p.set('flagged_only', 'true');
    return request('/processes?' + p);
}

export function getPorts() {
    return request('/ports');
}

export function getLogs(params = {}) {
    const p = new URLSearchParams();
    p.set('limit', params.limit || 100);
    if (params.level) p.set('level', params.level);
    if (params.source) p.set('source', params.source);
    return request('/logs?' + p);
}

export function getLogSources() {
    return request('/logs/sources');
}

export function getConfig() {
    return request('/config');
}

export function getAlerts(severity = '', since = '') {
    const p = new URLSearchParams();
    if (severity) p.set('severity', severity);
    if (since) p.set('since', since);
    return request('/alerts?' + p);
}

export function getActions(triggeredBy = '', since = '') {
    const p = new URLSearchParams();
    if (triggeredBy) p.set('triggered_by', triggeredBy);
    if (since) p.set('since', since);
    return request('/actions?' + p);
}

export function triggerAction(actionType, target, extra = {}) {
    return request('/action', {
        method: 'POST',
        body: JSON.stringify({
            action: actionType,
            target: target,
            duration_minutes: extra.duration_minutes || 30,
            reason: extra.reason || 'Manual action from dashboard',
        }),
    });
}

export function getSystemInfo() {
    return request('/system-info');
}

export function getContainers() {
    return request('/containers');
}

export function getDashboardData() {
    return request('/dashboard-data');
}

export function getDashboardFast() {
    return request('/dashboard-fast');
}
