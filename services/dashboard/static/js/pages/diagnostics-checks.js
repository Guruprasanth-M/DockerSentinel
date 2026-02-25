import * as api from '../core/api.js';
import { qs, setHtml, setText, escapeHtml } from '../helpers/dom.js';
import { formatDuration } from '../helpers/format.js';
import { CSS_FILES, JS_MODULES, TEMPLATES, API_ENDPOINTS, SERVICES, FIXES } from './diagnostics-constants.js';
import { row, apiRow, svcRow, wsRow, setBadge, log } from './diagnostics-ui.js';

export async function checkFrontend() {
    var results = { pass: 0, warn: 0, fail: 0 };
    var rows = [];

    // Check CSS files via document.styleSheets
    CSS_FILES.forEach(function (file) {
        var found = false;
        try {
            for (var i = 0; i < document.styleSheets.length; i++) {
                var href = document.styleSheets[i].href || '';
                if (href.indexOf(file) !== -1) { found = true; break; }
            }
        } catch (e) { /* cross-origin */ }

        if (found) {
            rows.push(row(file, 'CSS', 'pass', 'Loaded', ''));
            results.pass++;
            log('pass', 'CSS loaded: ' + file);
        } else {
            rows.push(row(file, 'CSS', 'fail', 'Not found in document', FIXES.css_missing));
            results.fail++;
            log('fail', 'CSS missing: ' + file);
        }
    });

    // Check JS modules via fetch HEAD
    for (var j = 0; j < JS_MODULES.length; j++) {
        var mod = JS_MODULES[j];
        try {
            var res = await fetch(mod.path, { method: 'HEAD' });
            if (res.ok) {
                rows.push(row(mod.name, 'JS', 'pass', 'Available (' + res.status + ')', ''));
                results.pass++;
                log('pass', 'JS available: ' + mod.name);
            } else {
                rows.push(row(mod.name, 'JS', 'fail', 'HTTP ' + res.status, FIXES.js_missing));
                results.fail++;
                log('fail', 'JS unavailable: ' + mod.name + ' (HTTP ' + res.status + ')');
            }
        } catch (e) {
            rows.push(row(mod.name, 'JS', 'fail', 'Fetch error', FIXES.js_missing));
            results.fail++;
            log('fail', 'JS fetch error: ' + mod.name);
        }
    }

    // Check HTML templates via fetch HEAD
    for (var k = 0; k < TEMPLATES.length; k++) {
        var tpl = TEMPLATES[k];
        try {
            var tres = await fetch('/templates/' + tpl + '.html', { method: 'HEAD' });
            if (tres.ok) {
                rows.push(row(tpl + '.html', 'HTML', 'pass', 'Available', ''));
                results.pass++;
                log('pass', 'Template available: ' + tpl + '.html');
            } else {
                rows.push(row(tpl + '.html', 'HTML', 'fail', 'HTTP ' + tres.status, FIXES.template_missing));
                results.fail++;
                log('fail', 'Template missing: ' + tpl + '.html');
            }
        } catch (e) {
            rows.push(row(tpl + '.html', 'HTML', 'fail', 'Fetch error', FIXES.template_missing));
            results.fail++;
        }
    }

    setHtml('diagFrontendBody', rows.join(''));
    setBadge('diagFrontendStatus', results);
    return results;
}

export async function checkApiEndpoints() {
    var results = { pass: 0, warn: 0, fail: 0 };
    var rows = [];

    for (var i = 0; i < API_ENDPOINTS.length; i++) {
        var ep = API_ENDPOINTS[i];
        var t0 = performance.now();
        try {
            var token = localStorage.getItem('sentinel_token') || '';
            var headers = {};
            if (token) headers['X-Sentinel-Token'] = token;

            var res = await fetch('/api' + ep.path, { headers: headers });
            var elapsed = Math.round(performance.now() - t0);

            if (res.ok) {
                var statusClass = elapsed > 500 ? 'warn' : 'pass';
                rows.push(apiRow(ep.name, ep.path, ep.method, statusClass, res.status, elapsed + 'ms', elapsed > 500 ? 'Slow response. Check API load.' : ''));
                if (statusClass === 'pass') results.pass++; else results.warn++;
                log(statusClass, 'API ' + ep.path + ': ' + res.status + ' (' + elapsed + 'ms)');
            } else {
                rows.push(apiRow(ep.name, ep.path, ep.method, 'fail', res.status, elapsed + 'ms', FIXES.api_error));
                results.fail++;
                log('fail', 'API ' + ep.path + ': HTTP ' + res.status);
            }
        } catch (e) {
            rows.push(apiRow(ep.name, ep.path, ep.method, 'fail', 'ERR', '--', FIXES.api_unreachable));
            results.fail++;
            log('fail', 'API ' + ep.path + ': unreachable');
        }
    }

    // Check WebSocket upgrade endpoint
    var t1 = performance.now();
    try {
        var wres = await fetch('/ws/live', { headers: { 'Connection': 'Upgrade', 'Upgrade': 'websocket' } });
        var we = Math.round(performance.now() - t1);
        rows.push(apiRow('WebSocket', '/ws/live', 'WS', 'pass', wres.status, we + 'ms', ''));
        results.pass++;
        log('pass', 'WebSocket endpoint reachable');
    } catch (e) {
        rows.push(apiRow('WebSocket', '/ws/live', 'WS', 'fail', 'ERR', '--', FIXES.ws_disconnected));
        results.fail++;
    }

    setHtml('diagApiBody', rows.join(''));
    setBadge('diagApiStatus', results);

    // Update API uptime
    try {
        var health = await api.getHealth();
        if (health && health.uptime_seconds) {
            setText('diagApiUptime', formatDuration(health.uptime_seconds));
        }
    } catch (e) {}

    return results;
}

export async function checkServices() {
    var results = { pass: 0, warn: 0, fail: 0 };
    var rows = [];

    try {
        var health = await api.getHealth();
        if (!health || !health.services) {
            rows.push('<tr><td colspan="5" class="text-center text-muted">Cannot reach API - unable to check services</td></tr>');
            results.fail++;
            setHtml('diagServiceBody', rows.join(''));
            setBadge('diagServiceStatus', results);
            return results;
        }

        var svc = health.services;
        SERVICES.forEach(function (s) {
            var actual = svc[s.key] || 'not found';
            var ok = actual === s.expected || (s.key === 'collectors' && actual === 'unknown');
            var status = ok ? 'pass' : (actual === 'unknown' ? 'warn' : 'fail');
            var fix = '';

            if (status === 'fail') {
                fix = FIXES.service_down.replace(/SERVICE_NAME/g, s.key);
            } else if (status === 'warn') {
                fix = FIXES.service_unknown.replace(/SERVICE_NAME/g, s.key);
            }

            rows.push(svcRow(s.name, s.expected, actual, status, fix));

            if (status === 'pass') results.pass++;
            else if (status === 'warn') results.warn++;
            else results.fail++;

            log(status, 'Service ' + s.name + ': expected=' + s.expected + ' actual=' + actual);
        });

        // Dashboard always running (we're looking at it)
        rows.push(svcRow('Dashboard', 'running', 'running', 'pass', ''));
        results.pass++;

    } catch (e) {
        rows.push('<tr><td colspan="5" class="text-center text-muted">Error checking services: ' + escapeHtml(e.message) + '</td></tr>');
        results.fail++;
    }

    setHtml('diagServiceBody', rows.join(''));
    setBadge('diagServiceStatus', results);
    return results;
}

export function checkWebSocket() {
    var results = { pass: 0, warn: 0, fail: 0 };
    var rows = [];

    // Check if WebSocket is connected
    var connText = qs('#connText');
    var wsStatus = connText ? connText.textContent : 'unknown';
    var wsConnected = wsStatus === 'LIVE';

    if (wsConnected) {
        rows.push(wsRow('Connection', 'pass', 'WebSocket is LIVE', ''));
        results.pass++;
        log('pass', 'WebSocket: connected (LIVE)');
    } else {
        rows.push(wsRow('Connection', 'fail', 'Status: ' + wsStatus, FIXES.ws_disconnected));
        results.fail++;
        log('fail', 'WebSocket: ' + wsStatus);
    }

    // Check event rate
    var evtEl = qs('#evtValue');
    var evtRate = evtEl ? parseInt(evtEl.textContent, 10) : 0;
    if (evtRate > 0) {
        rows.push(wsRow('Event Stream', 'pass', evtRate + ' events/sec', ''));
        results.pass++;
        log('pass', 'WebSocket events: ' + evtRate + ' evt/s');
    } else if (wsConnected) {
        rows.push(wsRow('Event Stream', 'warn', 'No events received yet', FIXES.ws_no_events));
        results.warn++;
        log('warn', 'WebSocket: connected but no events');
    } else {
        rows.push(wsRow('Event Stream', 'fail', 'No connection', FIXES.ws_disconnected));
        results.fail++;
    }

    // Check uptime counter is ticking
    var uptimeEl = qs('#uptimeValue');
    var uptime = uptimeEl ? uptimeEl.textContent : '--:--:--';
    if (uptime !== '--:--:--' && uptime !== '00:00:00') {
        rows.push(wsRow('Uptime Tracking', 'pass', 'Active: ' + uptime, ''));
        results.pass++;
    } else {
        rows.push(wsRow('Uptime Tracking', 'warn', 'Just started or not tracking', 'Refresh the page'));
        results.warn++;
    }

    setHtml('diagWsBody', rows.join(''));
    setBadge('diagWsStatus', results);
    return results;
}

export async function checkDataPipeline() {
    var results = { pass: 0, warn: 0, fail: 0 };
    var rows = [];

    // Check metrics data
    try {
        var metrics = await api.getMetrics();
        if (metrics && metrics.cpu_percent != null) {
            rows.push(wsRow('Metrics Data', 'pass', 'CPU: ' + metrics.cpu_percent.toFixed(1) + '%, Mem: ' + metrics.memory_percent.toFixed(1) + '%', ''));
            results.pass++;
            log('pass', 'Metrics data flowing');

            if (metrics.risk_score != null) {
                rows.push(wsRow('ML Scoring', 'pass', 'Risk score: ' + metrics.risk_score.toFixed(4) + ' (' + (metrics.risk_level || 'unknown') + ')', ''));
                results.pass++;
                log('pass', 'ML scoring active: ' + metrics.risk_level);
            } else {
                rows.push(wsRow('ML Scoring', 'warn', 'No risk score in metrics', 'ML engine may need time to initialize. docker compose logs ml'));
                results.warn++;
            }
        } else {
            rows.push(wsRow('Metrics Data', 'fail', 'No metrics available', FIXES.no_metrics));
            results.fail++;
            log('fail', 'No metric data');
        }
    } catch (e) {
        rows.push(wsRow('Metrics Data', 'fail', 'Error: ' + e.message, FIXES.no_metrics));
        results.fail++;
    }

    // Check process data
    try {
        var procs = await api.getProcesses();
        var plist = procs ? (procs.processes || []) : [];
        if (plist.length > 0) {
            rows.push(wsRow('Process Data', 'pass', plist.length + ' processes detected', ''));
            results.pass++;
        } else {
            rows.push(wsRow('Process Data', 'warn', 'No processes returned', FIXES.no_processes));
            results.warn++;
        }
    } catch (e) {
        rows.push(wsRow('Process Data', 'fail', 'Error: ' + e.message, FIXES.no_processes));
        results.fail++;
    }

    // Check port data
    try {
        var ports = await api.getPorts();
        var portlist = ports ? (ports.ports || []) : [];
        if (portlist.length > 0) {
            rows.push(wsRow('Port Scanner', 'pass', portlist.length + ' ports detected', ''));
            results.pass++;
        } else {
            rows.push(wsRow('Port Scanner', 'warn', 'No ports returned', 'Network collector may not have scanned yet. Wait 10s.'));
            results.warn++;
        }
    } catch (e) {
        rows.push(wsRow('Port Scanner', 'fail', 'Error', 'docker compose restart collectors'));
        results.fail++;
    }

    // Check alerts endpoint
    try {
        var alerts = await api.getAlerts();
        var alist = alerts ? (alerts.alerts || []) : [];
        rows.push(wsRow('Alert Pipeline', 'pass', alist.length + ' alerts', ''));
        results.pass++;
    } catch (e) {
        rows.push(wsRow('Alert Pipeline', 'fail', 'Error', 'docker compose restart policy'));
        results.fail++;
    }

    // Check logs
    try {
        var logs = await api.getLogs({ limit: 5 });
        var loglist = logs ? (logs.logs || []) : [];
        if (loglist.length > 0) {
            rows.push(wsRow('Log Collection', 'pass', loglist.length + ' recent entries', ''));
            results.pass++;
        } else {
            rows.push(wsRow('Log Collection', 'warn', 'No logs yet', 'Log collection starts after first events. Wait 30s.'));
            results.warn++;
        }
    } catch (e) {
        rows.push(wsRow('Log Collection', 'fail', 'Error', 'docker compose logs collectors --tail 20'));
        results.fail++;
    }

    setHtml('diagDataBody', rows.join(''));
    setBadge('diagDataStatus', results);
    return results;
}
