import { qs, setHtml, escapeHtml } from '../helpers/dom.js';

let logLines = [];

export function clearLogLines() {
    logLines = [];
}

export function getLogLines() {
    return logLines;
}

// --- HTML Row Builders ---

export function row(name, type, status, details, fix) {
    return '<tr>' +
        '<td class="mono">' + escapeHtml(name) + '</td>' +
        '<td>' + escapeHtml(type) + '</td>' +
        '<td><span class="badge badge--' + badge(status) + '">' + statusLabel(status) + '</span></td>' +
        '<td class="text-xs">' + escapeHtml(details) + '</td>' +
        '<td class="text-xs mono">' + (fix ? '<details><summary class="text-cyan" style="cursor:pointer;">How to fix</summary><p style="margin-top:4px;white-space:pre-wrap;">' + escapeHtml(fix) + '</p></details>' : '<span class="text-muted">OK</span>') + '</td>' +
        '</tr>';
}

export function apiRow(name, path, method, status, code, time, fix) {
    return '<tr>' +
        '<td>' + escapeHtml(name) + ' <span class="mono text-xs text-muted">' + escapeHtml(path) + '</span></td>' +
        '<td class="mono">' + escapeHtml(method) + '</td>' +
        '<td><span class="badge badge--' + badge(status) + '">' + code + '</span></td>' +
        '<td class="mono text-xs">' + escapeHtml(time) + '</td>' +
        '<td class="text-xs mono">' + (fix ? '<details><summary class="text-cyan" style="cursor:pointer;">How to fix</summary><p style="margin-top:4px;white-space:pre-wrap;">' + escapeHtml(fix) + '</p></details>' : '<span class="text-muted">OK</span>') + '</td>' +
        '</tr>';
}

export function svcRow(name, expected, actual, status, fix) {
    return '<tr>' +
        '<td>' + escapeHtml(name) + '</td>' +
        '<td class="mono text-xs">' + escapeHtml(expected) + '</td>' +
        '<td class="mono text-xs">' + escapeHtml(actual) + '</td>' +
        '<td><span class="badge badge--' + badge(status) + '">' + statusLabel(status) + '</span></td>' +
        '<td class="text-xs mono">' + (fix ? '<details><summary class="text-cyan" style="cursor:pointer;">How to fix</summary><p style="margin-top:4px;white-space:pre-wrap;">' + escapeHtml(fix) + '</p></details>' : '<span class="text-muted">OK</span>') + '</td>' +
        '</tr>';
}

export function wsRow(name, status, details, fix) {
    return '<tr>' +
        '<td>' + escapeHtml(name) + '</td>' +
        '<td><span class="badge badge--' + badge(status) + '">' + statusLabel(status) + '</span></td>' +
        '<td class="text-xs">' + escapeHtml(details) + '</td>' +
        '<td class="text-xs mono">' + (fix ? '<details><summary class="text-cyan" style="cursor:pointer;">How to fix</summary><p style="margin-top:4px;white-space:pre-wrap;">' + escapeHtml(fix) + '</p></details>' : '<span class="text-muted">OK</span>') + '</td>' +
        '</tr>';
}

// --- Badge & Status Helpers ---

export function badge(status) {
    if (status === 'pass') return 'success';
    if (status === 'warn') return 'warning';
    return 'danger';
}

export function statusLabel(status) {
    if (status === 'pass') return 'PASS';
    if (status === 'warn') return 'WARN';
    return 'FAIL';
}

export function setBadge(id, r) {
    var el = qs('#' + id);
    if (!el) return;
    if (r.fail > 0) {
        el.textContent = r.fail + ' failed';
        el.className = 'badge badge--danger';
    } else if (r.warn > 0) {
        el.textContent = r.warn + ' warnings';
        el.className = 'badge badge--warning';
    } else {
        el.textContent = 'all passed';
        el.className = 'badge badge--success';
    }
}

// --- Log Function ---

export function log(level, message) {
    var now = new Date();
    var ts = [now.getHours(), now.getMinutes(), now.getSeconds()].map(function (v) { return String(v).padStart(2, '0'); }).join(':');
    var levelClass = level === 'pass' ? 'log-level--info' : level === 'warn' ? 'log-level--warning' : level === 'fail' ? 'log-level--error' : 'log-level--info';
    var tag = level === 'pass' ? 'PASS ' : level === 'warn' ? 'WARN ' : level === 'fail' ? 'FAIL ' : 'INFO ';

    var line = '<div class="log-line">' +
        '<span class="log-time">' + ts + '</span>' +
        '<span class="log-level ' + levelClass + '">' + tag + '</span>' +
        '<span class="log-msg">' + escapeHtml(message) + '</span>' +
        '</div>';

    logLines.push(line);
    var el = qs('#diagLog');
    if (el) {
        el.innerHTML = logLines.join('');
        el.scrollTop = el.scrollHeight;
    }
}
