import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import { qs, setText, setHtml, escapeHtml } from '../helpers/dom.js';
import { formatDuration } from '../helpers/format.js';
import * as dialog from '../ui/dialog.js';

let interval = null;
let editingWebhook = null; // null = create mode, string = editing name

export function init() {
    var saveBtn = qs('#saveToken');
    if (saveBtn) saveBtn.addEventListener('click', handleTokenSave);

    var tokenInput = qs('#apiToken');
    if (tokenInput) {
        var saved = localStorage.getItem('sentinel_token') || '';
        tokenInput.value = saved;
    }

    // Webhook UI bindings
    var addBtn = qs('#addWebhookBtn');
    if (addBtn) addBtn.addEventListener('click', showAddWebhookForm);

    var saveWh = qs('#whSaveBtn');
    if (saveWh) saveWh.addEventListener('click', handleWebhookSave);

    var cancelWh = qs('#whCancelBtn');
    if (cancelWh) cancelWh.addEventListener('click', hideWebhookForm);

    emitter.on('refresh', refresh);

    refresh();
    loadWebhooks();
    interval = setInterval(refresh, 10000);
}

export function destroy() {
    emitter.off('refresh', refresh);
    if (interval) { clearInterval(interval); interval = null; }
}

async function refresh() {
    var health = await api.getHealth();
    if (!health || !qs('#svcCount')) return;

    var svc = health.services || {};
    var onlineCount = Object.values(svc).filter(function (v) { return v === 'connected' || v === 'active' || v === 'healthy'; }).length;

    setText('svcCount', onlineCount + '/' + Object.keys(svc).length);
    setText('sysUptime', health.uptime_seconds ? formatDuration(health.uptime_seconds) : 'N/A');
    setText('redisStatus', svc.redis === 'connected' ? 'Connected' : 'Disconnected');
    setText('mlModel', health.model_version || (svc.ml_engine === 'active' ? 'Active' : 'Inactive'));

    renderVersions(health);
}

function renderVersions(health) {
    var svc = health.services || {};
    var services = [
        { name: 'API Server', version: health.version || '0.1.0', status: svc.api === 'healthy' ? 'running' : 'stopped' },
        { name: 'Database', version: '16-alpine', status: svc.db === 'connected' ? 'running' : 'stopped' },
        { name: 'Collectors', version: '0.1.0', status: svc.collectors === 'active' || svc.collectors === 'unknown' ? 'running' : 'stopped' },
        { name: 'ML Engine', version: '0.1.0', status: svc.ml_engine === 'active' ? 'running' : 'stopped' },
        { name: 'Policy Engine', version: '0.1.0', status: svc.policy_engine === 'active' ? 'running' : 'stopped' },
        { name: 'Action Engine', version: '0.1.0', status: svc.action_engine === 'active' ? 'running' : 'stopped' },
        { name: 'Webhook Service', version: '0.1.0', status: svc.webhook_service === 'active' ? 'running' : 'stopped' },
        { name: 'Redis', version: '7.2', status: svc.redis === 'connected' ? 'running' : 'stopped' },
        { name: 'Dashboard', version: '0.1.0', status: 'running' },
    ];

    var now = new Date().toLocaleTimeString();
    var html = '';
    services.forEach(function (s) {
        html += '<tr>' +
            '<td>' + escapeHtml(s.name) + '</td>' +
            '<td><span class="badge badge--' + (s.status === 'running' ? 'success' : 'danger') + '">' + s.status + '</span></td>' +
            '<td class="mono">' + escapeHtml(s.version) + '</td>' +
            '<td class="mono text-xs">' + now + '</td>' +
            '</tr>';
    });

    setHtml('serviceVersions', html);
}

function handleTokenSave(e) {
    if (e) e.preventDefault();
    var input = qs('#apiToken');
    if (!input) return;

    var token = input.value.trim();
    if (token) {
        localStorage.setItem('sentinel_token', token);
        api.setToken(token);
        dialog.toast('API token saved', 'success');
    } else {
        localStorage.removeItem('sentinel_token');
        api.setToken('');
        dialog.toast('API token cleared', 'info');
    }
}

// ── Webhook Management ──────────────────────────────────────────

async function loadWebhooks() {
    var container = qs('#webhookList');
    if (!container) return;

    try {
        var res = await fetch('/api/webhooks', {
            headers: { 'X-Sentinel-Token': localStorage.getItem('sentinel_token') || '' }
        });
        if (!res.ok) {
            container.innerHTML = '<div class="empty-placeholder"><span class="text-muted text-sm">Failed to load webhooks (check API token)</span></div>';
            return;
        }
        var data = await res.json();
        renderWebhooks(data.webhooks || []);
    } catch(e) {
        container.innerHTML = '<div class="empty-placeholder"><span class="text-muted text-sm">Error loading webhooks</span></div>';
    }
}

function renderWebhooks(webhooks) {
    var container = qs('#webhookList');
    if (!container) return;

    if (!webhooks.length) {
        container.innerHTML = '<div class="empty-placeholder"><span class="text-muted text-sm">No webhooks configured. Click "+ Add Webhook" to create one.</span></div>';
        return;
    }

    var html = '';
    webhooks.forEach(function(wh) {
        var statusClass = wh.enabled ? 'badge--success' : 'badge--danger';
        var statusText = wh.enabled ? 'Enabled' : 'Disabled';
        var events = (wh.events || []).map(function(e) { return '<span class="badge badge--info" style="font-size:10px;padding:1px 6px;">' + escapeHtml(e) + '</span>'; }).join(' ');

        html += '<div class="settings-row" style="display:flex;align-items:center;gap:var(--space-3);padding:var(--space-3);background:rgba(255,255,255,0.03);border-radius:var(--radius-md);margin-bottom:var(--space-2);">' +
            '<div style="flex:1;min-width:0;">' +
                '<div style="display:flex;align-items:center;gap:var(--space-2);margin-bottom:4px;">' +
                    '<strong class="text-sm">' + escapeHtml(wh.name) + '</strong>' +
                    '<span class="badge ' + statusClass + '" style="font-size:10px">' + statusText + '</span>' +
                    (wh.sign_payloads ? '<span class="badge badge--cyan" style="font-size:10px">Signed</span>' : '') +
                '</div>' +
                '<div class="text-xs text-muted mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escapeHtml(wh.url) + '</div>' +
                '<div style="margin-top:4px;">' + events + '</div>' +
            '</div>' +
            '<div style="display:flex;gap:var(--space-1);flex-shrink:0;">' +
                '<button class="btn btn-sm" onclick="window.__whEdit(\'' + escapeHtml(wh.name) + '\')">Edit</button>' +
                '<button class="btn btn-sm" onclick="window.__whTest(\'' + escapeHtml(wh.name) + '\')">Test</button>' +
                '<button class="btn btn-sm btn-danger" onclick="window.__whDelete(\'' + escapeHtml(wh.name) + '\')">Delete</button>' +
            '</div>' +
        '</div>';
    });

    container.innerHTML = html;

    // Register global handlers (since innerHTML doesn't have closures)
    window.__whEdit = function(name) { editWebhook(name); };
    window.__whTest = function(name) { testWebhook(name); };
    window.__whDelete = function(name) { deleteWebhook(name); };
}

function showAddWebhookForm() {
    editingWebhook = null;
    setText('webhookFormTitle', 'Add Webhook');
    var section = qs('#webhookFormSection');
    if (section) section.classList.remove('hidden');

    qs('#whName').value = '';
    qs('#whUrl').value = '';
    qs('#whEvents').value = 'attack_detected, critical_alert';
    qs('#whEnabled').checked = true;
    qs('#whSign').checked = false;
}

function hideWebhookForm() {
    var section = qs('#webhookFormSection');
    if (section) section.classList.add('hidden');
    editingWebhook = null;
}

async function editWebhook(name) {
    try {
        var res = await fetch('/api/webhooks', {
            headers: { 'X-Sentinel-Token': localStorage.getItem('sentinel_token') || '' }
        });
        var data = await res.json();
        var wh = (data.webhooks || []).find(function(w) { return w.name === name; });
        if (!wh) return;

        editingWebhook = name;
        setText('webhookFormTitle', 'Edit Webhook: ' + name);
        var section = qs('#webhookFormSection');
        if (section) section.classList.remove('hidden');

        qs('#whName').value = wh.name || '';
        qs('#whUrl').value = wh.url || '';
        qs('#whEvents').value = (wh.events || []).join(', ');
        qs('#whEnabled').checked = wh.enabled !== false;
        qs('#whSign').checked = wh.sign_payloads === true;
    } catch(e) {
        dialog.toast('Error loading webhook', 'error');
    }
}

async function handleWebhookSave() {
    var name = qs('#whName').value.trim();
    var url = qs('#whUrl').value.trim();
    var events = qs('#whEvents').value.split(',').map(function(e) { return e.trim(); }).filter(Boolean);
    var enabled = qs('#whEnabled').checked;
    var signPayloads = qs('#whSign').checked;

    if (!name || !url) {
        dialog.toast('Name and URL are required', 'error');
        return;
    }

    var token = localStorage.getItem('sentinel_token') || '';
    var body = { name: name, url: url, events: events, enabled: enabled, sign_payloads: signPayloads };

    try {
        var endpoint = editingWebhook ? '/api/webhooks/' + editingWebhook : '/api/webhooks';
        var method = editingWebhook ? 'PUT' : 'POST';

        var res = await fetch(endpoint, {
            method: method,
            headers: { 'Content-Type': 'application/json', 'X-Sentinel-Token': token },
            body: JSON.stringify(body),
        });

        if (res.ok) {
            dialog.toast(editingWebhook ? 'Webhook updated' : 'Webhook created', 'success');
            hideWebhookForm();
            loadWebhooks();
        } else {
            var err = await res.json();
            dialog.toast(err.detail || 'Failed to save webhook', 'error');
        }
    } catch(e) {
        dialog.toast('Error saving webhook: ' + e.message, 'error');
    }
}

async function testWebhook(name) {
    var token = localStorage.getItem('sentinel_token') || '';
    try {
        dialog.toast('Sending test...', 'info');
        var res = await fetch('/api/webhooks/' + name + '/test', {
            method: 'POST',
            headers: { 'X-Sentinel-Token': token },
        });
        var data = await res.json();
        if (data.status === 'sent') {
            dialog.toast('Test sent! HTTP ' + data.http_status, 'success');
        } else {
            dialog.toast('Test failed: ' + (data.message || 'Unknown error'), 'error');
        }
    } catch(e) {
        dialog.toast('Error: ' + e.message, 'error');
    }
}

async function deleteWebhook(name) {
    if (!confirm('Delete webhook "' + name + '"?')) return;

    var token = localStorage.getItem('sentinel_token') || '';
    try {
        var res = await fetch('/api/webhooks/' + name, {
            method: 'DELETE',
            headers: { 'X-Sentinel-Token': token },
        });
        if (res.ok) {
            dialog.toast('Webhook deleted', 'success');
            loadWebhooks();
        } else {
            dialog.toast('Failed to delete webhook', 'error');
        }
    } catch(e) {
        dialog.toast('Error: ' + e.message, 'error');
    }
}
