import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import { qs, setText, setHtml, escapeHtml } from '../helpers/dom.js';
import { formatPercent, formatBytes } from '../helpers/format.js';

let interval = null;
const POLL_MS = 10000;

export function init() {
    var sortSel = qs('#procSort');
    var limitSel = qs('#procLimit');
    var flagged = qs('#procFlagged');
    var btn = qs('#procRefresh');

    if (sortSel) sortSel.addEventListener('change', refresh);
    if (limitSel) limitSel.addEventListener('change', refresh);
    if (flagged) flagged.addEventListener('change', refresh);
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
    var sortSel = qs('#procSort');
    var limitSel = qs('#procLimit');
    var flagged = qs('#procFlagged');

    var params = {};
    if (sortSel) params.sort = sortSel.value;
    if (limitSel) params.limit = parseInt(limitSel.value, 10);
    if (flagged && flagged.checked) params.flagged = true;

    var data = await api.getProcesses(params);
    if (!data || !qs('#procBody')) return;

    var processes = Array.isArray(data) ? data : (data.processes || []);

    setText('procCount', processes.length + ' processes');

    var html = '';
    if (processes.length === 0) {
        html = '<tr><td colspan="7" class="text-center text-muted">No processes found</td></tr>';
    } else {
        processes.forEach(function (p) {
            var cpuVal = p.cpu_percent || 0;
            var memVal = p.memory_mb || p.memory_percent || 0;
            var risk = p.risk_flag ? 'high' : 'low';
            var status = p.status || 'running';
            var conns = p.connections || p.num_connections || 0;

            html += '<tr>' +
                '<td class="mono">' + (p.pid || 0) + '</td>' +
                '<td>' + escapeHtml(p.name || 'unknown') + '</td>' +
                '<td class="' + (cpuVal > 80 ? 'text-red' : cpuVal > 50 ? 'text-amber' : '') + '">' + cpuVal.toFixed(1) + '%</td>' +
                '<td>' + (typeof memVal === 'number' ? memVal.toFixed(1) + ' MB' : memVal) + '</td>' +
                '<td>' + conns + '</td>' +
                '<td><span class="badge badge--' + statusBadge(status) + '">' + status + '</span></td>' +
                '<td><span class="badge badge--' + riskBadge(risk) + '">' + risk + '</span></td>' +
                '</tr>';
        });
    }

    setHtml('procBody', html);
}

function statusBadge(s) {
    if (s === 'running') return 'success';
    if (s === 'sleeping') return 'info';
    return 'warning';
}

function riskBadge(r) {
    if (r === 'high' || r === 'critical') return 'danger';
    if (r === 'medium') return 'warning';
    return 'success';
}
