import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import * as dialog from '../ui/dialog.js';
import { qs, setText, setHtml, escapeHtml } from '../helpers/dom.js';
import { formatTimeAgo, formatTime } from '../helpers/format.js';

let interval = null;
let currentAlerts = [];
const POLL_MS = 30000;

export function init() {
    var filterSeverity = qs('#alertSeverity');
    var filterTime = qs('#alertTimeRange');
    var filterSearch = qs('#alertSearch');
    var btn = qs('#alertRefresh');

    if (filterSeverity) filterSeverity.addEventListener('change', refresh);
    if (filterTime) filterTime.addEventListener('change', refresh);
    if (filterSearch) filterSearch.addEventListener('input', debounceRefresh);
    if (btn) btn.addEventListener('click', refresh);

    // Click handler for alert cards → show notification detail
    var timeline = qs('#alertTimeline');
    if (timeline) {
        timeline.addEventListener('click', function (e) {
            var card = e.target.closest('.alert-card');
            if (!card) return;
            var idx = parseInt(card.dataset.index, 10);
            if (isNaN(idx) || !currentAlerts[idx]) return;
            showAlertDetail(currentAlerts[idx]);
        });
    }

    emitter.on('refresh', refresh);
    // Real-time alert streaming via WebSocket
    emitter.on('ws:alert', handleWsAlert);
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
    emitter.off('ws:alert', handleWsAlert);
    if (interval) { clearInterval(interval); interval = null; }
    currentAlerts = [];
}

function handleWsAlert(data) {
    if (!qs('#alertTimeline')) return;
    // Prepend new alert to the list and re-render
    currentAlerts.unshift(data);
    if (currentAlerts.length > 200) currentAlerts.pop();
    renderTimeline(currentAlerts);
}

var debounceTimer = null;
function debounceRefresh() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(refresh, 300);
}

async function refresh() {
    var data = await api.getAlerts();
    if (!data || !qs('#alertTimeline')) return;

    var alerts = Array.isArray(data) ? data : (data.alerts || []);

    var severity = qs('#alertSeverity');
    var time = qs('#alertTimeRange');
    var search = qs('#alertSearch');

    var sevFilter = severity ? severity.value : '';
    var timeFilter = time ? time.value : '';
    var searchFilter = search ? search.value.toLowerCase().trim() : '';

    var now = Date.now();
    var timeMs = timeFilter ? parseInt(timeFilter, 10) * 1000 : 0;

    var filtered = alerts.filter(function (a) {
        if (sevFilter && a.severity !== sevFilter) return false;
        if (timeMs && a.timestamp) {
            var ts = new Date(a.timestamp).getTime();
            if (now - ts > timeMs) return false;
        }
        if (searchFilter) {
            var text = ((a.type || '') + ' ' + (a.message || '') + ' ' + (a.source || '') + ' ' + (a.policy_name || '')).toLowerCase();
            if (text.indexOf(searchFilter) === -1) return false;
        }
        return true;
    });

    var counts = { critical: 0, high: 0, medium: 0 };
    filtered.forEach(function (a) {
        var s = a.severity || 'info';
        if (s === 'critical') counts.critical++;
        else if (s === 'warning' || s === 'high') counts.high++;
        else counts.medium++;
    });

    setText('aCritical', String(counts.critical));
    setText('aHigh', String(counts.high));
    setText('aMedium', String(counts.medium));
    setText('aTotal', String(filtered.length));

    emitter.emit('alerts:count', counts.critical + counts.high);

    currentAlerts = filtered;
    renderTimeline(filtered);
}

function renderTimeline(alerts) {
    if (alerts.length === 0) {
        setHtml('alertTimeline', '<div class="empty-state"><div class="empty-icon">&#9872;</div><p class="text-muted">No alerts matching filters</p></div>');
        return;
    }

    var html = '';
    alerts.forEach(function (a, index) {
        var sev = a.severity || 'info';
        var message = escapeHtml(a.message || a.type || a.policy_name || 'Unknown alert');
        var source = escapeHtml(a.source || a.policy_name || 'system');
        var time = a.timestamp ? formatTimeAgo(a.timestamp) : 'just now';

        html += '<div class="alert-card" data-severity="' + sev + '" data-index="' + index + '" style="cursor:pointer" title="Click for details">' +
            '<div class="alert-indicator"></div>' +
            '<div class="alert-body">' +
            '<div class="alert-head">' +
            '<span class="badge badge--' + badgeType(sev) + '">' + sev + '</span>' +
            '<span class="text-xs text-muted">' + time + '</span>' +
            '</div>' +
            '<p class="alert-message">' + message + '</p>' +
            '<span class="text-xs text-muted">Source: ' + source + '</span>' +
            '</div>' +
            '</div>';
    });

    setHtml('alertTimeline', html);
}

function showAlertDetail(alert) {
    var sev = alert.severity || 'info';
    var type = sev === 'critical' ? 'error' : sev === 'high' || sev === 'warning' ? 'warning' : 'info';

    var body = '<div style="text-align:left; line-height:1.8">';
    body += '<strong>Severity:</strong> <span class="badge badge--' + badgeType(sev) + '">' + sev + '</span><br>';
    body += '<strong>Message:</strong> ' + escapeHtml(alert.message || alert.type || 'N/A') + '<br>';
    if (alert.policy_name) body += '<strong>Policy:</strong> ' + escapeHtml(alert.policy_name) + '<br>';
    if (alert.source_ip) body += '<strong>Source IP:</strong> ' + escapeHtml(alert.source_ip) + '<br>';
    if (alert.anomaly_type) body += '<strong>Anomaly Type:</strong> ' + escapeHtml(alert.anomaly_type) + '<br>';
    if (alert.score != null) body += '<strong>Score:</strong> ' + Number(alert.score).toFixed(4) + '<br>';
    if (alert.risk_level) body += '<strong>Risk Level:</strong> ' + escapeHtml(alert.risk_level) + '<br>';
    if (alert.action) body += '<strong>Action:</strong> ' + escapeHtml(alert.action) + '<br>';
    if (alert.timestamp) body += '<strong>Time:</strong> ' + formatTime(alert.timestamp) + ' (' + formatTimeAgo(alert.timestamp) + ')<br>';
    if (alert.alert_id) body += '<strong>ID:</strong> <code>' + escapeHtml(alert.alert_id) + '</code><br>';
    body += '</div>';

    dialog.alert('Alert Details', body, type);
}

function badgeType(severity) {
    switch (severity) {
        case 'critical': return 'danger';
        case 'warning': case 'high': return 'warning';
        case 'info': return 'info';
        default: return 'success';
    }
}
