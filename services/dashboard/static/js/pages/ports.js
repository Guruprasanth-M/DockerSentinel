import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import { qs, setHtml, escapeHtml } from '../helpers/dom.js';

let interval = null;
const POLL_MS = 15000;

export function init() {
    var btn = qs('#portsRefresh');
    if (btn) btn.addEventListener('click', refresh);

    emitter.on('refresh', refresh);
    document.addEventListener('visibilitychange', _onVisChange);
    refresh();
    interval = setInterval(refresh, POLL_MS);
}

function _onVisChange() {
    if (document.hidden) {
        if (interval) { clearInterval(interval); interval = null; }
    } else {
        refresh();
        if (!interval) interval = setInterval(refresh, POLL_MS);
    }
}

export function destroy() {
    document.removeEventListener('visibilitychange', _onVisChange);
    emitter.off('refresh', refresh);
    if (interval) { clearInterval(interval); interval = null; }
}

async function refresh() {
    var data = await api.getPorts();
    if (!data || !qs('#portsBody')) return;

    var ports = Array.isArray(data) ? data : (data.ports || []);

    var html = '';
    ports.forEach(function (p) {
        var risk = p.risk_flag ? 'high' : 'low';
        var state = p.state || p.status || 'normal';
        html += '<tr>' +
            '<td class="mono">' + (p.port || p.laddr_port || 0) + '</td>' +
            '<td>' + escapeHtml(p.protocol || p.type || 'tcp') + '</td>' +
            '<td><span class="badge badge--' + stateBadge(state) + '">' + escapeHtml(state) + '</span></td>' +
            '<td>' + escapeHtml(p.service_hint || p.service || '') + '</td>' +
            '<td class="mono">' + (p.pid || '--') + '</td>' +
            '<td>' + escapeHtml(p.process || p.name || '') + '</td>' +
            '<td><span class="badge badge--' + riskBadge(risk) + '">' + risk + '</span></td>' +
            '</tr>';
    });

    if (ports.length === 0) {
        html = '<tr><td colspan="7" class="text-center text-muted">No open ports detected</td></tr>';
    }

    setHtml('portsBody', html);
}

function stateBadge(s) {
    var lower = (s || '').toLowerCase();
    if (lower === 'listen' || lower === 'established' || lower === 'normal') return 'success';
    if (lower === 'time_wait' || lower === 'close_wait') return 'warning';
    return 'info';
}

function riskBadge(r) {
    if (r === 'high' || r === 'critical') return 'danger';
    if (r === 'medium') return 'warning';
    return 'success';
}
