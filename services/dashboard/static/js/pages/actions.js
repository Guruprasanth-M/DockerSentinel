import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import { qs, setHtml, escapeHtml } from '../helpers/dom.js';
import { formatTimeAgo } from '../helpers/format.js';
import * as dialog from '../ui/dialog.js';

let interval = null;

export function init() {
    var blockBtn = qs('#blockBtn');
    var killBtn = qs('#killBtn');
    var refreshBtn = qs('#actionRefresh');

    if (blockBtn) blockBtn.addEventListener('click', handleBlockIp);
    if (killBtn) killBtn.addEventListener('click', handleKillProcess);
    if (refreshBtn) refreshBtn.addEventListener('click', refreshHistory);

    emitter.on('refresh', refreshHistory);

    refreshHistory();
    interval = setInterval(refreshHistory, 10000);
}

export function destroy() {
    emitter.off('refresh', refreshHistory);
    if (interval) { clearInterval(interval); interval = null; }
}

async function handleBlockIp(e) {
    if (e) e.preventDefault();

    var ip = qs('#blockIp');
    var duration = qs('#blockDuration');
    var reason = qs('#blockReason');

    if (!ip || !ip.value.trim()) {
        dialog.toast('Enter an IP address', 'warning');
        return;
    }

    var durationVal = duration ? parseInt(duration.value, 10) : 30;

    var confirmed = await dialog.confirm(
        'Block IP Address',
        'Block ' + ip.value.trim() + ' for ' + durationVal + ' minutes?'
    );

    if (!confirmed) return;

    var result = await api.triggerAction('block_ip', ip.value.trim(), {
        duration_minutes: durationVal,
        reason: reason ? reason.value.trim() : 'Manual block from dashboard',
    });

    if (result) {
        dialog.toast('IP blocked successfully', 'success');
        if (ip) ip.value = '';
        if (reason) reason.value = '';
        refreshHistory();
    } else {
        dialog.toast('Failed to block IP', 'error');
    }
}

async function handleKillProcess(e) {
    if (e) e.preventDefault();

    var pid = qs('#killPid');
    var reason = qs('#killReason');

    if (!pid || !pid.value.trim()) {
        dialog.toast('Enter a PID', 'warning');
        return;
    }

    var confirmed = await dialog.confirm(
        'Kill Process',
        'Terminate process ' + pid.value.trim() + '? This cannot be undone.',
        { confirmText: 'Kill', confirmClass: 'btn--danger' }
    );

    if (!confirmed) return;

    var result = await api.triggerAction('kill_process', pid.value.trim(), {
        reason: reason ? reason.value.trim() : 'Manual kill from dashboard',
    });

    if (result) {
        dialog.toast('Process terminated', 'success');
        if (pid) pid.value = '';
        if (reason) reason.value = '';
        refreshHistory();
    } else {
        dialog.toast('Failed to kill process', 'error');
    }
}

async function refreshHistory() {
    var data = await api.getActions();
    if (!data || !qs('#actionBody')) return;

    var actions = Array.isArray(data) ? data : (data.actions || []);

    var html = '';
    actions.forEach(function (a) {
        html += '<tr>' +
            '<td class="mono text-xs">' + (a.timestamp ? formatTimeAgo(a.timestamp) : 'N/A') + '</td>' +
            '<td>' + escapeHtml(a.action || a.type || 'unknown') + '</td>' +
            '<td class="mono">' + escapeHtml(String(a.target || '')) + '</td>' +
            '<td><span class="badge badge--' + actionStatus(a.status) + '">' + escapeHtml(a.status || 'pending') + '</span></td>' +
            '<td class="text-xs">' + escapeHtml(a.triggered_by || 'manual') + '</td>' +
            '<td class="text-xs">' + escapeHtml(a.reason || a.details || '') + '</td>' +
            '</tr>';
    });

    if (actions.length === 0) {
        html = '<tr><td colspan="6" class="text-center text-muted">No actions recorded</td></tr>';
    }

    setHtml('actionBody', html);
}

function actionStatus(s) {
    if (s === 'completed' || s === 'success') return 'success';
    if (s === 'failed' || s === 'error') return 'danger';
    if (s === 'pending') return 'warning';
    return 'info';
}
