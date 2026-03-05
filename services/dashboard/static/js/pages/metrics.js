import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import { qs, setText, setHtml, escapeHtml } from '../helpers/dom.js';

let interval = null;
let charts = {};
let history = { cpu: [], mem: [], net: [], anomaly: [] };
const MAX_POINTS = 60;
const POLL_MS = 15000;

export function init() {
    emitter.on('refresh', refresh);
    // Listen for real-time WS status updates
    emitter.on('ws:status_update', handleStatusUpdate);
    document.addEventListener('visibilitychange', _onVisChange);
    initCharts();
    refresh(); // Initial HTTP fetch
    interval = setInterval(refresh, POLL_MS); // Fallback polling
}

var _metricsVisTimer = null;

function _onVisChange() {
    if (document.hidden) {
        if (_metricsVisTimer) { clearTimeout(_metricsVisTimer); _metricsVisTimer = null; }
        if (interval) { clearInterval(interval); interval = null; }
    } else {
        // Delay refresh to let WS reconnect and deliver replay first
        if (_metricsVisTimer) clearTimeout(_metricsVisTimer);
        _metricsVisTimer = setTimeout(function () {
            _metricsVisTimer = null;
            refresh();
            if (!interval) interval = setInterval(refresh, POLL_MS);
        }, 800);
    }
}

export function destroy() {
    document.removeEventListener('visibilitychange', _onVisChange);
    emitter.off('refresh', refresh);
    emitter.off('ws:status_update', handleStatusUpdate);
    if (interval) { clearInterval(interval); interval = null; }
    if (_metricsVisTimer) { clearTimeout(_metricsVisTimer); _metricsVisTimer = null; }
    Object.keys(charts).forEach(function (k) {
        if (charts[k]) { charts[k].dispose(); charts[k] = null; }
    });
    history = { cpu: [], mem: [], net: [], anomaly: [] };
}

function handleStatusUpdate(data) {
    if (!data.metrics || !qs('#mCpuPct')) return;
    var m = data.metrics;
    setText('mCpuPct', formatPercent(m.cpu_percent || 0));
    setText('mMemUsed', formatNumber(m.memory_used_mb || 0, 0) + ' MB');
    setText('mDiskPct', formatPercent(m.disk_percent || 0));
    setText('mLoadAvg', formatNumber(m.load_1m || 0, 2));
    updateCharts(m);
    renderTable(m);
}

async function refresh() {
    var data = await api.getMetrics();
    if (!data || !qs('#mCpuPct')) return;

    setText('mCpuPct', formatPercent(data.cpu_percent || 0));
    setText('mMemUsed', formatNumber(data.memory_used_mb || 0, 0) + ' MB');
    setText('mDiskPct', formatPercent(data.disk_percent || 0));
    setText('mLoadAvg', formatNumber(data.load_1m || 0, 2));

    updateCharts(data);
    renderTable(data);
}

function formatPercent(v) { return (v || 0).toFixed(1) + '%'; }
function formatNumber(v, d) { return Number(v || 0).toFixed(d != null ? d : 0); }

function initCharts() {
    var ids = ['mCpuChart', 'mMemChart', 'mNetChart', 'mScoreChart'];
    ids.forEach(function (id) {
        var el = qs('#' + id);
        if (!el || typeof echarts === 'undefined') return;
        charts[id] = echarts.init(el);
    });

    var base = {
        backgroundColor: 'transparent',
        grid: { left: 50, right: 20, top: 20, bottom: 30 },
        textStyle: { color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace', fontSize: 11 },
        xAxis: { type: 'category', data: [], boundaryGap: false, axisLine: { lineStyle: { color: '#1e293b' } }, axisTick: { show: false }, axisLabel: { show: false } },
        yAxis: { type: 'value', min: 0, axisLine: { show: false }, splitLine: { lineStyle: { color: '#1e293b', type: 'dashed' } } },
    };

    function lineSeries(color) {
        return { type: 'line', smooth: true, symbol: 'none', lineStyle: { color: color, width: 2 }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: color.replace(')', ',0.15)').replace('rgb', 'rgba') }, { offset: 1, color: 'rgba(0,0,0,0)' }] } }, data: [] };
    }

    if (charts.mCpuChart) charts.mCpuChart.setOption(Object.assign({}, base, { yAxis: Object.assign({}, base.yAxis, { max: 100, axisLabel: { formatter: '{value}%' } }), series: [lineSeries('rgb(0,212,255)')] }));
    if (charts.mMemChart) charts.mMemChart.setOption(Object.assign({}, base, { yAxis: Object.assign({}, base.yAxis, { max: 100, axisLabel: { formatter: '{value}%' } }), series: [lineSeries('rgb(139,92,246)')] }));
    if (charts.mNetChart) charts.mNetChart.setOption(Object.assign({}, base, { series: [
        { type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#22c55e', width: 1.5 }, data: [] },
        { type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#f59e0b', width: 1.5 }, data: [] },
    ] }));
    if (charts.mScoreChart) charts.mScoreChart.setOption(Object.assign({}, base, { yAxis: Object.assign({}, base.yAxis, { max: 1, axisLabel: { formatter: '{value}' } }), series: [lineSeries('rgb(239,68,68)')] }));

    window.addEventListener('resize', function () {
        Object.keys(charts).forEach(function (k) { if (charts[k]) charts[k].resize(); });
    });
}

function updateCharts(data) {
    var now = new Date();
    var label = [now.getHours(), now.getMinutes(), now.getSeconds()].map(function (v) { return String(v).padStart(2, '0'); }).join(':');

    push(history.cpu, label, data.cpu_percent || 0);
    push(history.mem, label, data.memory_percent || 0);
    push(history.net, label, { inVal: data.network_bytes_recv_per_sec || data.net_bytes_recv_rate || 0, outVal: data.network_bytes_sent_per_sec || data.net_bytes_sent_rate || 0 });
    push(history.anomaly, label, data.risk_score || data.score || data.anomaly_score || 0);

    if (charts.mCpuChart) charts.mCpuChart.setOption({ xAxis: { data: labels(history.cpu) }, series: [{ data: vals(history.cpu) }] });
    if (charts.mMemChart) charts.mMemChart.setOption({ xAxis: { data: labels(history.mem) }, series: [{ data: vals(history.mem) }] });
    if (charts.mNetChart) charts.mNetChart.setOption({ xAxis: { data: labels(history.net) }, series: [{ data: history.net.map(function (p) { return p.value.inVal; }) }, { data: history.net.map(function (p) { return p.value.outVal; }) }] });
    if (charts.mScoreChart) charts.mScoreChart.setOption({ xAxis: { data: labels(history.anomaly) }, series: [{ data: vals(history.anomaly) }] });
}

function push(arr, label, value) { arr.push({ label: label, value: value }); if (arr.length > MAX_POINTS) arr.shift(); }
function labels(arr) { return arr.map(function (p) { return p.label; }); }
function vals(arr) { return arr.map(function (p) { return p.value; }); }

function renderTable(data) {
    var rows = [
        ['cpu_percent', data.cpu_percent, '%', pctStatus(data.cpu_percent, 80, 50)],
        ['memory_percent', data.memory_percent, '%', pctStatus(data.memory_percent, 85, 70)],
        ['memory_used_mb', data.memory_used_mb, 'MB', 'info'],
        ['disk_percent', data.disk_percent, '%', pctStatus(data.disk_percent, 90, 75)],
        ['load_1m', data.load_1m, '', data.load_1m > 4 ? 'danger' : data.load_1m > 2 ? 'warning' : 'success'],
        ['network_recv', formatNumber(data.network_bytes_recv_per_sec || data.net_bytes_recv_rate || 0, 0), 'B/s', 'info'],
        ['network_sent', formatNumber(data.network_bytes_sent_per_sec || data.net_bytes_sent_rate || 0, 0), 'B/s', 'info'],
        ['risk_score', formatNumber(data.risk_score || data.score || 0, 4), '', (data.risk_score || data.score || 0) > 0.7 ? 'danger' : 'success'],
        ['risk_level', data.risk_level || 'normal', '', (data.risk_level === 'critical' || data.risk_level === 'high') ? 'danger' : 'success'],
    ];

    var html = '';
    rows.forEach(function (r) {
        html += '<tr><td>' + r[0] + '</td><td>' + formatNumber(r[1], 2) + '</td><td>' + r[2] + '</td>' +
            '<td><span class="badge badge--' + r[3] + '">' + r[3] + '</span></td></tr>';
    });

    setHtml('metricsTable', html);
}

function pctStatus(v, high, mid) {
    if (v > high) return 'danger';
    if (v > mid) return 'warning';
    return 'success';
}
