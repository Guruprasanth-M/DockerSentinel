import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import { qs, escapeHtml } from '../helpers/dom.js';
import { formatTime } from '../helpers/format.js';

let interval = null;
let lastHash = '';
let paused = false;
let logEntries = [];
let searchDebounce = null;
const MAX_LOG_ENTRIES = 500;
const POLL_MS = 15000;

/* ── Search helpers ─────────────────────────────────────── */

/** Parse the search input. /pattern/ → regex, else case-insensitive substring */
function parseSearch(raw) {
    if (!raw) return null;
    var m = raw.match(/^\/(.+)\/([gimsuy]*)$/);
    if (m) {
        try { return { type: 'regex', re: new RegExp(m[1], m[2] || 'i') }; }
        catch (_) { /* invalid regex, fall through */ }
    }
    return { type: 'text', term: raw.toLowerCase() };
}

/** Test if a log entry matches the current search filter */
function matchesSearch(entry, search) {
    if (!search) return true;
    var msg = (entry.message || entry.msg || '').toString();
    if (search.type === 'regex') return search.re.test(msg);
    return msg.toLowerCase().includes(search.term);
}

/** Highlight matching portions in escaped HTML */
function highlightMatch(escapedMsg, search) {
    if (!search) return escapedMsg;
    try {
        if (search.type === 'regex') {
            // Re-create regex with 'g' for replaceAll
            var flags = search.re.flags.includes('g') ? search.re.flags : search.re.flags + 'g';
            var re = new RegExp(search.re.source, flags);
            return escapedMsg.replace(re, '<mark class="log-highlight">$&</mark>');
        }
        // Plain text highlight (case-insensitive)
        var esc = search.term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return escapedMsg.replace(new RegExp(esc, 'gi'), '<mark class="log-highlight">$&</mark>');
    } catch (_) { return escapedMsg; }
}

/* ── Lifecycle ──────────────────────────────────────────── */

async function populateSources(selectEl) {
    try {
        var data = await api.getLogSources();
        if (!data || !data.sources) return;
        // Preserve current selection
        var current = selectEl.value;
        selectEl.innerHTML = '<option value="">All Sources</option>';
        data.sources.forEach(function (src) {
            var opt = document.createElement('option');
            opt.value = src;
            opt.textContent = src;
            selectEl.appendChild(opt);
        });
        if (current) selectEl.value = current;
    } catch (_) { /* keep static fallback */ }
}

function handleVisibility() {
    paused = document.hidden;
    if (document.hidden) {
        if (interval) { clearInterval(interval); interval = null; }
    } else {
        refresh();
        if (!interval) interval = setInterval(refresh, POLL_MS);
    }
}

export function init() {
    var levelSel = qs('#logLevel');
    var sourceSel = qs('#logSource');
    var limitSel = qs('#logLimit');
    var searchInput = qs('#logSearch');
    var refreshBtn = qs('#logsRefresh');

    if (levelSel) levelSel.addEventListener('change', refresh);
    if (sourceSel) sourceSel.addEventListener('change', refresh);
    if (limitSel) limitSel.addEventListener('change', refresh);
    if (refreshBtn) refreshBtn.addEventListener('click', function () { lastHash = ''; refresh(); });

    // Populate source dropdown dynamically from API
    if (sourceSel) populateSources(sourceSel);

    // Debounced search — client-side filter, no API call needed
    if (searchInput) {
        searchInput.addEventListener('input', function () {
            clearTimeout(searchDebounce);
            searchDebounce = setTimeout(function () { renderLogs(logEntries); }, 200);
        });
        // Enter key triggers immediate re-render
        searchInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { clearTimeout(searchDebounce); renderLogs(logEntries); }
        });
    }

    document.addEventListener('visibilitychange', handleVisibility);
    emitter.on('refresh', refresh);
    emitter.on('ws:log_event', handleLogEvent);

    refresh();
    interval = setInterval(refresh, POLL_MS);
}

export function destroy() {
    emitter.off('refresh', refresh);
    emitter.off('ws:log_event', handleLogEvent);
    if (interval) { clearInterval(interval); interval = null; }
    clearTimeout(searchDebounce);
    document.removeEventListener('visibilitychange', handleVisibility);
    lastHash = '';
    logEntries = [];
}

/* ── WebSocket real-time log event ──────────────────────── */

function handleLogEvent(data) {
    if (paused || !qs('#logTerminal')) return;

    var levelSel = qs('#logLevel');
    var sourceSel = qs('#logSource');
    var level = data.level || 'info';
    var source = data.source || 'system';

    if (levelSel && levelSel.value && levelSel.value !== 'all' && level !== levelSel.value) return;
    if (sourceSel && sourceSel.value && sourceSel.value !== 'all' && source !== sourceSel.value) return;

    logEntries.push(data);
    if (logEntries.length > MAX_LOG_ENTRIES) logEntries.shift();

    // Check search filter before appending
    var searchInput = qs('#logSearch');
    var search = parseSearch(searchInput ? searchInput.value.trim() : '');
    if (!matchesSearch(data, search)) return;

    var terminal = qs('#logTerminal');
    var div = document.createElement('div');
    div.className = 'log-line card-enter';
    var message = data.message || data.msg || '';
    var ts = data.timestamp ? formatTime(data.timestamp) : '';
    var escapedMsg = escapeHtml(message);
    if (search) escapedMsg = highlightMatch(escapedMsg, search);

    div.innerHTML =
        '<span class="log-time">' + ts + '</span>' +
        '<span class="log-level log-level--' + level + '">' + level.toUpperCase().padEnd(5) + '</span>' +
        '<span class="log-source">[' + escapeHtml(source) + ']</span> ' +
        '<span class="log-msg">' + escapedMsg + '</span>';
    terminal.appendChild(div);

    while (terminal.children.length > MAX_LOG_ENTRIES) {
        terminal.removeChild(terminal.firstChild);
    }
    terminal.scrollTop = terminal.scrollHeight;
    updateCount();
}

/* ── Render + count helpers ─────────────────────────────── */

function updateCount() {
    var el = qs('#logCount');
    var terminal = qs('#logTerminal');
    if (el && terminal) {
        var visible = terminal.querySelectorAll('.log-line').length;
        el.textContent = visible + ' entries';
    }
}

function renderLogs(logs) {
    var terminal = qs('#logTerminal');
    if (!terminal) return;

    var searchInput = qs('#logSearch');
    var search = parseSearch(searchInput ? searchInput.value.trim() : '');

    var html = '';
    var shown = 0;
    logs.forEach(function (entry) {
        if (!matchesSearch(entry, search)) return;
        var level = entry.level || 'info';
        var source = entry.source || 'system';
        var message = entry.message || entry.msg || '';
        var ts = entry.timestamp ? formatTime(entry.timestamp) : '';
        var escapedMsg = escapeHtml(message);
        if (search) escapedMsg = highlightMatch(escapedMsg, search);

        html += '<div class="log-line">' +
            '<span class="log-time">' + ts + '</span>' +
            '<span class="log-level log-level--' + level + '">' + level.toUpperCase().padEnd(5) + '</span>' +
            '<span class="log-source">[' + escapeHtml(source) + ']</span> ' +
            '<span class="log-msg">' + escapedMsg + '</span>' +
            '</div>';
        shown++;
    });

    if (shown === 0) {
        html = '<div class="log-line text-muted">' +
            (search ? 'No entries matching "' + escapeHtml(searchInput.value) + '"' : 'No log entries') +
            '</div>';
    }

    terminal.innerHTML = html;
    terminal.scrollTop = terminal.scrollHeight;
    updateCount();
}

/* ── HTTP refresh ───────────────────────────────────────── */

async function refresh() {
    if (paused) return;

    var levelSel = qs('#logLevel');
    var sourceSel = qs('#logSource');
    var limitSel = qs('#logLimit');

    var params = {};
    if (levelSel && levelSel.value && levelSel.value !== 'all') params.level = levelSel.value;
    if (sourceSel && sourceSel.value && sourceSel.value !== 'all') params.source = sourceSel.value;
    if (limitSel) params.limit = parseInt(limitSel.value, 10);

    var data = await api.getLogs(params);
    if (!data || !qs('#logTerminal')) return;

    var logs = Array.isArray(data) ? data : (data.events || data.logs || []);

    var hash = logs.length + ':' + (logs[0] && logs[0].timestamp || '') + ':' + (logs[logs.length - 1] && logs[logs.length - 1].timestamp || '');
    if (hash === lastHash) return;
    lastHash = hash;

    logEntries = logs.slice(); // Store for client-side filtering
    renderLogs(logEntries);
}
