/**
 * overview.js — Three-tier polling with smooth DOM updates
 *
 * TIER 1 — WebSocket push (2s): status_update → charts, risk score, alerts
 * TIER 2 — Fast poll  (3s):  /dashboard-fast → CPU %, memory %, net rates, disk I/O
 * TIER 3 — Full poll  (60s): /dashboard-data → static info, containers, all details
 *
 * Static data (hostname, OS, IP addresses, disk capacity) is fetched ONCE on init
 * and refreshed every 60s.  Dynamic data (CPU %, speeds) updates every 3s.
 * DOM updates use element reuse + CSS transitions for buttery smooth rendering.
 */
import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import { qs, setText, setHtml } from '../helpers/dom.js';
import { formatPercent, formatBytesPerSec, formatClock } from '../helpers/format.js';

/* ── State ─────────────────────────────────────────────── */
let fastInterval = null;    // 3s — dynamic metrics
let fullInterval = null;    // 60s — full data + containers
let statusInterval = null;  // 15s — /status fallback
let cpuChart = null, netChart = null, memChart = null, loadChart = null;
let cpuHistory = [], netHistory = [], memHistory = [], loadHistory = [];
const MAX_POINTS = 60;

// Client-side caches for network rate calculation
let _prevNetCounters = {};  // { iface: { rx, tx, ts } }

// Track rendered element counts to avoid rebuilds
let _renderedCoreCount = 0;
let _renderedDiskKeys = '';
let _renderedNetKeys = '';
let _renderedContainerKeys = '';

const SERVICES = [
    { key: 'redis', label: 'Redis' },
    { key: 'db', label: 'Database' },
    { key: 'collectors', label: 'Collectors' },
    { key: 'ml_engine', label: 'ML Engine' },
    { key: 'policy_engine', label: 'Policy Engine' },
    { key: 'action_engine', label: 'Action Engine' },
    { key: 'webhook_service', label: 'Webhook Service' },
    { key: 'api', label: 'API' },
    { key: 'dashboard', label: 'Dashboard' },
];

/* ── Lifecycle ─────────────────────────────────────────── */
export function init() {
    var btn = qs('#overviewRefresh');
    if (btn) btn.addEventListener('click', refreshAll);

    emitter.on('refresh', refreshAll);
    emitter.on('ws:status_update', handleStatusUpdate);

    initCharts();

    // Initial full load — populates everything
    refreshFull();
    refreshStatus();

    // TIER 2: fast dynamic poll every 3s
    fastInterval = setInterval(refreshFast, 3000);
    // TIER 3: full data + containers every 60s
    fullInterval = setInterval(refreshFull, 60000);
    // Status fallback (charts) every 15s
    statusInterval = setInterval(refreshStatus, 15000);
}

export function destroy() {
    emitter.off('refresh', refreshAll);
    emitter.off('ws:status_update', handleStatusUpdate);
    if (fastInterval) { clearInterval(fastInterval); fastInterval = null; }
    if (fullInterval) { clearInterval(fullInterval); fullInterval = null; }
    if (statusInterval) { clearInterval(statusInterval); statusInterval = null; }
    if (cpuChart) { cpuChart.dispose(); cpuChart = null; }
    if (netChart) { netChart.dispose(); netChart = null; }
    if (memChart) { memChart.dispose(); memChart = null; }
    if (loadChart) { loadChart.dispose(); loadChart = null; }
    cpuHistory = []; netHistory = []; memHistory = []; loadHistory = [];
    _renderedCoreCount = 0; _renderedDiskKeys = ''; _renderedNetKeys = ''; _renderedContainerKeys = '';
    _prevNetCounters = {};
}

/* ── TIER 1: WebSocket push (2s) ───────────────────────── */
function handleStatusUpdate(data) {
    if (!qs('#riskScore')) return;
    setText('lastUpdate', 'Updated ' + formatClock());
    if (data.health) renderHealth(data.health);
    if (data.metrics) {
        renderMetrics(data.metrics);
        updateCharts(data.metrics);
    }
}

/* ── TIER 2: Fast poll (3s) — dynamic only ─────────────── */
async function refreshFast() {
    var data = await api.getDashboardFast();
    if (!data || !qs('#riskScore')) return;

    // CPU
    smoothText('cpuTotalPct', formatPercent(data.cpu_total || 0));
    smoothCoreUpdate(data.cpu_per_core || []);

    // Memory
    if (data.memory) {
        var mem = data.memory;
        smoothText('memSummary', fmtMB(mem.used_mb) + ' / ' + fmtMB(mem.total_mb));
        var total = mem.total_mb || 1;
        smoothBar('memUsedBar', (mem.used_mb || 0) / total * 100);
        smoothBar('memCachedBar', (mem.cached_mb || 0) / total * 100);
        smoothBar('memBuffersBar', (mem.buffers_mb || 0) / total * 100);
        smoothText('memUsed', fmtMB(mem.used_mb));
        smoothText('memAvail', fmtMB(mem.available_mb));
        smoothText('memCached', fmtMB(mem.cached_mb));
        smoothText('memBuffers', fmtMB(mem.buffers_mb));
    }

    // Network rates (client-side rate calculation from byte counters)
    if (data.network && data.network.length) {
        var now = Date.now() / 1000;
        data.network.forEach(function(n) {
            var prev = _prevNetCounters[n.interface];
            if (prev) {
                var dt = now - prev.ts;
                if (dt > 0) {
                    var recvRate = Math.max(0, (n.rx_bytes - prev.rx) / dt);
                    var sendRate = Math.max(0, (n.tx_bytes - prev.tx) / dt);
                    // Update speed values in existing net cards
                    var card = qs('[data-net="' + n.interface + '"]');
                    if (card) {
                        var upEl = card.querySelector('.net-speed-value.up');
                        var downEl = card.querySelector('.net-speed-value.down');
                        if (upEl) smoothTextEl(upEl, formatBytesPerSec(sendRate));
                        if (downEl) smoothTextEl(downEl, formatBytesPerSec(recvRate));
                    }
                }
            }
            _prevNetCounters[n.interface] = { rx: n.rx_bytes, tx: n.tx_bytes, ts: now };
        });
    }

    // Disk I/O speed updates
    if (data.disk_io && data.disk_io.length) {
        data.disk_io.forEach(function(d) {
            var card = qs('[data-disk="' + d.device + '"]');
            if (!card) {
                // Try matching by name substring
                card = qs('[data-disk*="' + d.device + '"]');
            }
            if (card) {
                var readEl = card.querySelector('.disk-read-val');
                var writeEl = card.querySelector('.disk-write-val');
                if (readEl) smoothTextEl(readEl, d.read_mbps.toFixed(1) + ' MB/s');
                if (writeEl) smoothTextEl(writeEl, d.write_mbps.toFixed(1) + ' MB/s');
            }
        });
    }

    // Load average for charts
    if (data.load_1m != null) {
        var now2 = new Date();
        var label = timeLabel(now2);
        pushHistory(loadHistory, { label: label, l1: data.load_1m, l5: data.load_5m || 0, l15: data.load_15m || 0 });
        if (loadChart) {
            loadChart.setOption({
                xAxis: { data: loadHistory.map(function(p) { return p.label; }) },
                series: [
                    { data: loadHistory.map(function(p) { return p.l1; }) },
                    { data: loadHistory.map(function(p) { return p.l5; }) },
                    { data: loadHistory.map(function(p) { return p.l15; }) },
                ],
            });
        }
    }

    setText('lastUpdate', 'Updated ' + formatClock());
}

/* ── TIER 3: Full poll (60s) — everything ──────────────── */
async function refreshFull() {
    var data = await api.getDashboardData();
    if (!data || !qs('#riskScore')) return;
    if (data.cpu) renderCpuInfo(data.cpu);
    if (data.memory) renderMemoryInfo(data.memory);
    if (data.disks) renderDiskInfo(data.disks);
    if (data.network) renderNetworkInfo(data.network);
    if (data.system) renderSystemBar(data.system);
    if (data.gpu) renderGpuInfo(data.gpu);
    if (data.containers) renderContainers(data);
    setText('lastUpdate', 'Updated ' + formatClock());
}

async function refreshStatus() {
    var data = await api.getStatus();
    if (!data || !qs('#riskScore')) return;
    if (data.health) renderHealth(data.health);
    if (data.metrics) {
        renderMetrics(data.metrics);
        updateCharts(data.metrics);
    }
}

function refreshAll() {
    refreshFull();
    refreshStatus();
}

/* ── Smooth DOM helpers ─────────────────────────────────── */

/** Update text only if changed — prevents layout thrashing */
function smoothText(id, value) {
    var el = document.getElementById(id);
    if (el && el.textContent !== value) {
        el.textContent = value;
        el.classList.add('value-flash');
        setTimeout(function() { el.classList.remove('value-flash'); }, 600);
    }
}

/** Same but with direct element reference */
function smoothTextEl(el, value) {
    if (el && el.textContent !== value) {
        el.textContent = value;
        el.classList.add('value-flash');
        setTimeout(function() { el.classList.remove('value-flash'); }, 600);
    }
}

/** Smoothly transition a bar width via CSS transition */
function smoothBar(id, pct) {
    var el = document.getElementById(id);
    if (el) el.style.width = Math.min(100, Math.max(0, pct)) + '%';
}

/** Update per-core bars in-place (reuse DOM elements) */
function smoothCoreUpdate(cores) {
    var grid = qs('#cpuCoresGrid');
    if (!grid || !cores.length) return;

    if (cores.length !== _renderedCoreCount) {
        var html = '';
        cores.forEach(function(usage, i) {
            var pct = clamp(usage);
            html += '<div class="core-bar" data-core="' + i + '">' +
                '<div class="core-bar-label"><span>Core ' + i + '</span><span class="core-pct">' + pct.toFixed(1) + '%</span></div>' +
                '<div class="core-bar-track"><div class="core-bar-fill' + (pct > 80 ? ' hot' : '') + '" style="width:' + pct + '%"></div></div>' +
                '</div>';
        });
        grid.innerHTML = html;
        _renderedCoreCount = cores.length;
        return;
    }

    // In-place update — smooth CSS transitions
    cores.forEach(function(usage, i) {
        var pct = clamp(usage);
        var bar = grid.querySelector('[data-core="' + i + '"]');
        if (!bar) return;
        var fill = bar.querySelector('.core-bar-fill');
        var pctEl = bar.querySelector('.core-pct');
        if (fill) {
            fill.style.width = pct + '%';
            if (pct > 80) fill.classList.add('hot');
            else fill.classList.remove('hot');
        }
        if (pctEl) smoothTextEl(pctEl, pct.toFixed(1) + '%');
    });
}

/* ── Health ─────────────────────────────────────────────── */
function renderHealth(data) {
    var online = 0;
    var total = SERVICES.length;
    var html = '';
    var svcMap = data.services || {};

    SERVICES.forEach(function (svc) {
        var status = 'unknown';
        var val = svcMap[svc.key];
        if (svc.key === 'dashboard') { status = 'healthy'; online++; }
        else if (val === 'connected' || val === 'active' || val === 'healthy' || val === 'running' || val === true) { status = 'healthy'; online++; }
        else if (val === false || val === 'error' || val === 'disconnected') { status = 'error'; }
        else if (val === 'degraded') { status = 'warning'; }
        else if (val === 'unknown') { status = 'healthy'; online++; }

        html += '<div class="health-item">' +
            '<span class="pulse-dot pulse-dot--' + (status === 'healthy' ? 'live' : status === 'error' ? 'error' : 'off') + '"></span>' +
            '<span class="text-sm">' + svc.label + '</span>' +
            '</div>';
    });
    setText('servicesUp', online + '/' + total);
    setHtml('healthGrid', html);
}

/* ── Metrics ───────────────────────────────────────────── */
function renderMetrics(data) {
    var riskScore = data.risk_score != null ? Math.round(data.risk_score * 100) : 0;
    smoothText('riskScore', String(riskScore));
    smoothText('anomalyCount', String(data.anomaly_count || 0));
    smoothText('alertCount', String(data.alert_count || 0));
}

/* ── System Info Bar ───────────────────────────────────── */
function renderSystemBar(sys) {
    setText('sysHostname', sys.hostname || '--');
    setText('sysOS', sys.os || '--');
    setText('sysKernel', sys.kernel || '--');
    setText('sysUptime', sys.uptime_formatted || '--');
    setText('sysArch', sys.architecture || '--');
    smoothText('sysProcs', String(sys.processes || 0));
    smoothText('sysThreads', String(sys.threads || 0));
}

/* ── CPU (full render — called on TIER 3) ──────────────── */
function renderCpuInfo(cpu) {
    setText('cpuModel', cpu.model || '--');
    smoothText('cpuTotalPct', formatPercent(cpu.total_usage || 0));
    setText('cpuCores', (cpu.physical_cores || 0) + ' / ' + (cpu.logical_cores || 0));

    var freq = cpu.frequency_mhz || {};
    var curFreq = freq.current || 0;
    setText('cpuFreq', curFreq > 1000 ? (curFreq / 1000).toFixed(2) + ' GHz' : Math.round(curFreq) + ' MHz');

    var cache = cpu.cache || {};
    setText('cpuL1d', cache.l1d || '--');
    setText('cpuL2', cache.l2 || '--');
    setText('cpuL3', cache.l3 || '--');

    smoothCoreUpdate(cpu.per_core_usage || []);
}

/* ── Memory (full render — called on TIER 3) ───────────── */
function renderMemoryInfo(mem) {
    smoothText('memSummary', fmtMB(mem.used_mb) + ' / ' + fmtMB(mem.total_mb));
    var total = mem.total_mb || 1;
    smoothBar('memUsedBar', (mem.used_mb || 0) / total * 100);
    smoothBar('memCachedBar', (mem.cached_mb || 0) / total * 100);
    smoothBar('memBuffersBar', (mem.buffers_mb || 0) / total * 100);

    smoothText('memUsed', fmtMB(mem.used_mb));
    smoothText('memAvail', fmtMB(mem.available_mb));
    smoothText('memCached', fmtMB(mem.cached_mb));
    smoothText('memBuffers', fmtMB(mem.buffers_mb));
    smoothText('memActive', fmtMB(mem.active_mb));
    smoothText('memInactive', fmtMB(mem.inactive_mb));
    smoothText('memSwapUsed', fmtMB(mem.swap_used_mb));
    smoothText('memSwapTotal', fmtMB(mem.swap_total_mb));
    smoothText('memCommitted', fmtMB(mem.committed_mb));
    smoothText('memSlab', fmtMB(mem.slab_mb));
    smoothText('memPageTables', fmtMB(mem.page_tables_mb));
    smoothText('memMapped', fmtMB(mem.mapped_mb));
}

/* ── Disk (structural render — called on TIER 3) ────────── */
function renderDiskInfo(disks) {
    var container = qs('#diskCards');
    if (!container || !disks.length) return;

    var keys = disks.map(function(d) { return d.device; }).join(',');
    if (keys === _renderedDiskKeys) {
        // Structure unchanged — update values in place
        disks.forEach(function(d) {
            var card = qs('[data-disk="' + (d.name || d.device) + '"]');
            if (!card) return;
            var fill = card.querySelector('.progress-fill');
            if (fill) {
                fill.style.width = d.percent + '%';
                fill.className = 'progress-fill' + (d.percent > 85 ? ' progress-fill--red' : d.percent > 70 ? ' progress-fill--amber' : ' progress-fill--green');
            }
            updateStat(card, '.disk-used-val', d.used_gb.toFixed(1) + ' GB (' + d.percent + '%)');
            updateStat(card, '.disk-free-val', d.free_gb.toFixed(1) + ' GB');
            updateStat(card, '.disk-read-val', d.read_speed_mbps.toFixed(1) + ' MB/s');
            updateStat(card, '.disk-write-val', d.write_speed_mbps.toFixed(1) + ' MB/s');
            updateStat(card, '.disk-active-val', d.active_time_percent.toFixed(1) + '%');
        });
        return;
    }

    // Structure changed — full rebuild
    var html = '';
    disks.forEach(function(d) {
        var devId = d.name || d.device;
        html += '<div class="disk-card card-enter" data-disk="' + escHtml(devId) + '">' +
            '<div class="disk-card-header">' +
                '<span class="disk-card-name">' + escHtml(d.device) + ' → ' + escHtml(d.mount_point || d.mount || '/') + '</span>' +
                '<span class="disk-card-type ' + (d.type === 'SSD' ? 'disk-type-ssd' : d.type === 'HDD' ? 'disk-type-hdd' : '') + '">' + (d.type || 'Virtual') + '</span>' +
            '</div>' +
            '<div class="progress-bar"><div class="progress-fill' + (d.percent > 85 ? ' progress-fill--red' : d.percent > 70 ? ' progress-fill--amber' : ' progress-fill--green') + '" style="width:' + d.percent + '%"></div></div>' +
            '<div class="disk-stats-row">' +
                dStat('Total', d.total_gb.toFixed(1) + ' GB', '') +
                dStat('Used', d.used_gb.toFixed(1) + ' GB (' + d.percent + '%)', 'disk-used-val') +
                dStat('Free', d.free_gb.toFixed(1) + ' GB', 'disk-free-val') +
                dStat('Read', d.read_speed_mbps.toFixed(1) + ' MB/s', 'disk-read-val') +
                dStat('Write', d.write_speed_mbps.toFixed(1) + ' MB/s', 'disk-write-val') +
                dStat('Active', d.active_time_percent.toFixed(1) + '%', 'disk-active-val') +
            '</div>' +
        '</div>';
    });
    container.innerHTML = html;
    _renderedDiskKeys = keys;
}

function dStat(label, value, cls) {
    return '<div class="disk-stat-item"><span class="label">' + label + '</span><span class="value ' + cls + '">' + value + '</span></div>';
}

function updateStat(parent, selector, value) {
    var el = parent.querySelector(selector);
    if (el) smoothTextEl(el, value);
}

/* ── Network (structural render — called on TIER 3) ────── */
function renderNetworkInfo(interfaces) {
    var container = qs('#netCards');
    if (!container || !interfaces.length) return;

    var keys = interfaces.map(function(n) { return n.interface; }).join(',');
    if (keys === _renderedNetKeys) {
        // Structure unchanged — update values in place
        interfaces.forEach(function(n) {
            var card = qs('[data-net="' + n.interface + '"]');
            if (!card) return;
            var upEl = card.querySelector('.net-speed-value.up');
            var downEl = card.querySelector('.net-speed-value.down');
            if (upEl) smoothTextEl(upEl, formatBytesPerSec(n.send_rate_bps || 0));
            if (downEl) smoothTextEl(downEl, formatBytesPerSec(n.recv_rate_bps || 0));
        });
        // Seed counters for rate calculation
        var now = Date.now() / 1000;
        interfaces.forEach(function(n) {
            _prevNetCounters[n.interface] = { rx: n.rx_bytes, tx: n.tx_bytes, ts: now };
        });
        return;
    }

    // Full rebuild
    var html = '';
    interfaces.forEach(function(n) {
        var statusDot = n.status === 'up' ? 'up' : 'down';
        html += '<div class="net-card card-enter" data-net="' + escHtml(n.interface) + '">' +
            '<div class="net-card-header">' +
                '<span class="net-card-name">' + escHtml(n.interface) + '</span>' +
                '<span class="net-card-status"><span class="net-status-dot ' + statusDot + '"></span>' + (n.status || 'unknown') + '</span>' +
            '</div>' +
            '<div class="net-info-grid">' +
                nInfo('Type', n.type || 'Unknown') +
                nInfo('Speed', n.speed_mbps > 0 ? n.speed_mbps + ' Mbps' : 'N/A') +
                nInfo('IPv4', n.ipv4 || 'N/A') +
                nInfo('IPv6', (n.ipv6 || 'N/A').substring(0, 24)) +
                nInfo('MAC', n.mac || 'N/A') +
                nInfo('MTU', String(n.mtu || 'N/A')) +
            '</div>' +
            '<div class="net-speed-row">' +
                '<div class="net-speed-item">' +
                    '<span class="net-speed-arrow up">↑</span>' +
                    '<span class="net-speed-value up">' + formatBytesPerSec(n.send_rate_bps || 0) + '</span>' +
                '</div>' +
                '<div class="net-speed-item">' +
                    '<span class="net-speed-arrow down">↓</span>' +
                    '<span class="net-speed-value down">' + formatBytesPerSec(n.recv_rate_bps || 0) + '</span>' +
                '</div>' +
            '</div>' +
            (n.dns_servers && n.dns_servers.length ? '<div style="margin-top:8px;font-size:11px;color:rgba(255,255,255,0.4)">DNS: ' + n.dns_servers.join(', ') + '</div>' : '') +
        '</div>';
    });
    container.innerHTML = html;
    _renderedNetKeys = keys;

    // Seed counters
    var now2 = Date.now() / 1000;
    interfaces.forEach(function(n) {
        _prevNetCounters[n.interface] = { rx: n.rx_bytes, tx: n.tx_bytes, ts: now2 };
    });
}

function nInfo(label, value) {
    return '<div class="net-info-item"><span class="label">' + label + '</span><span class="value">' + escHtml(value) + '</span></div>';
}

/* ── Containers (structural render) ───────────────────── */
function renderContainers(data) {
    var list = qs('#containerList');
    if (!list) return;

    var running = data.containers_running || data.running || 0;
    smoothText('containerCount', running + ' running');

    var containers = data.containers || [];
    if (!containers.length) {
        list.innerHTML = '<div class="empty-placeholder"><span class="text-muted text-sm">No containers found</span></div>';
        _renderedContainerKeys = '';
        return;
    }

    var keys = containers.map(function(c) { return c.name; }).join(',');
    if (keys === _renderedContainerKeys) {
        // Update in place
        containers.forEach(function(c) {
            var row = list.querySelector('[data-ctr="' + c.name + '"]');
            if (!row) return;
            smoothTextEl(row.querySelector('.container-cpu'), c.cpu_percent.toFixed(1) + '%');
            smoothTextEl(row.querySelector('.container-mem'), c.memory_used_mb.toFixed(0) + ' MB');
            var fill = row.querySelector('.container-bar-fill');
            if (fill) fill.style.width = Math.min(100, c.memory_percent) + '%';
        });
        return;
    }

    var html = '';
    containers.forEach(function(c) {
        html += '<div class="container-row card-enter" data-ctr="' + escHtml(c.name) + '">' +
            '<span class="container-name" title="' + escHtml(c.name) + '">' + escHtml(c.name) + '</span>' +
            '<span class="container-cpu">' + c.cpu_percent.toFixed(1) + '%</span>' +
            '<span class="container-mem">' + c.memory_used_mb.toFixed(0) + ' MB</span>' +
            '<div class="container-bar"><div class="container-bar-fill" style="width:' + Math.min(100, c.memory_percent) + '%"></div></div>' +
        '</div>';
    });
    list.innerHTML = html;
    _renderedContainerKeys = keys;
}

/* ── GPU ────────────────────────────────────────────────── */
function renderGpuInfo(gpus) {
    if (!gpus || !gpus.length) return;
    var section = qs('#gpuSection');
    if (section) section.classList.remove('hidden');
    var container = qs('#gpuCards');
    if (!container) return;

    var html = '';
    gpus.forEach(function(g) {
        html += '<div class="gpu-card">' +
            '<div class="gpu-card-name">' + escHtml(g.name) + '</div>' +
            '<div class="gpu-stats">' +
                '<div class="mem-stat"><span class="mem-stat-label">Usage</span><span class="mem-stat-value mono">' + g.utilization_percent + '%</span></div>' +
                '<div class="mem-stat"><span class="mem-stat-label">Temp</span><span class="mem-stat-value mono">' + g.temperature_c + '°C</span></div>' +
                '<div class="mem-stat"><span class="mem-stat-label">VRAM Used</span><span class="mem-stat-value mono">' + g.memory_used_mb + ' MB</span></div>' +
                '<div class="mem-stat"><span class="mem-stat-label">VRAM Total</span><span class="mem-stat-value mono">' + g.memory_total_mb + ' MB</span></div>' +
            '</div>' +
        '</div>';
    });
    container.innerHTML = html;
}

/* ── Charts ─────────────────────────────────────────────── */
function initCharts() {
    if (typeof echarts === 'undefined') return;

    var baseOpts = {
        backgroundColor: 'transparent',
        grid: { left: 50, right: 20, top: 20, bottom: 30 },
        textStyle: { color: 'rgba(255,255,255,0.5)', fontFamily: '-apple-system, BlinkMacSystemFont, sans-serif', fontSize: 11 },
        xAxis: { type: 'category', data: [], boundaryGap: false, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.1)' } }, axisTick: { show: false }, axisLabel: { show: false } },
        yAxis: { type: 'value', min: 0, axisLine: { show: false }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)', type: 'dashed' } } },
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(0,0,0,0.8)', borderColor: 'rgba(255,255,255,0.1)', textStyle: { color: '#fff', fontSize: 11 } },
        animation: true,
        animationDuration: 300,
        animationEasing: 'cubicOut',
    };

    var cpuEl = qs('#cpuChart');
    if (cpuEl) {
        cpuChart = echarts.init(cpuEl);
        cpuChart.setOption(Object.assign({}, baseOpts, {
            yAxis: Object.assign({}, baseOpts.yAxis, { max: 100, axisLabel: { formatter: '{value}%' } }),
            series: [{ name: 'CPU %', type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#007AFF', width: 2 }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(0,122,255,0.3)' }, { offset: 1, color: 'rgba(0,122,255,0)' }] } }, data: [] }],
        }));
    }

    var loadEl = qs('#loadChart');
    if (loadEl) {
        loadChart = echarts.init(loadEl);
        loadChart.setOption(Object.assign({}, baseOpts, {
            series: [
                { name: '1m', type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#007AFF', width: 1.5 }, data: [] },
                { name: '5m', type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#5856D6', width: 1.5 }, data: [] },
                { name: '15m', type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#FF9F0A', width: 1.5 }, data: [] },
            ],
        }));
    }

    var memEl = qs('#memChart');
    if (memEl) {
        memChart = echarts.init(memEl);
        memChart.setOption(Object.assign({}, baseOpts, {
            yAxis: Object.assign({}, baseOpts.yAxis, { max: 100, axisLabel: { formatter: '{value}%' } }),
            series: [{ name: 'Memory %', type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#5856D6', width: 2 }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(88,86,214,0.3)' }, { offset: 1, color: 'rgba(88,86,214,0)' }] } }, data: [] }],
        }));
    }

    var netEl = qs('#netChart');
    if (netEl) {
        netChart = echarts.init(netEl);
        netChart.setOption(Object.assign({}, baseOpts, {
            series: [
                { name: 'Recv', type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#34C759', width: 1.5 }, areaStyle: { color: 'rgba(52,199,89,0.1)' }, data: [] },
                { name: 'Send', type: 'line', smooth: true, symbol: 'none', lineStyle: { color: '#FF9F0A', width: 1.5 }, areaStyle: { color: 'rgba(255,159,10,0.1)' }, data: [] },
            ],
        }));
    }

    window.addEventListener('resize', function() {
        if (cpuChart) cpuChart.resize();
        if (netChart) netChart.resize();
        if (memChart) memChart.resize();
        if (loadChart) loadChart.resize();
    });
}

function updateCharts(data) {
    var now = new Date();
    var label = timeLabel(now);

    pushHistory(cpuHistory, { label: label, value: data.cpu_percent || 0 });
    if (cpuChart) cpuChart.setOption({ xAxis: { data: cpuHistory.map(function(p) { return p.label; }) }, series: [{ data: cpuHistory.map(function(p) { return p.value; }) }] });

    pushHistory(memHistory, { label: label, value: data.memory_percent || 0 });
    if (memChart) memChart.setOption({ xAxis: { data: memHistory.map(function(p) { return p.label; }) }, series: [{ data: memHistory.map(function(p) { return p.value; }) }] });

    pushHistory(loadHistory, { label: label, l1: data.load_1m || 0, l5: data.load_5m || 0, l15: data.load_15m || 0 });
    if (loadChart) loadChart.setOption({
        xAxis: { data: loadHistory.map(function(p) { return p.label; }) },
        series: [
            { data: loadHistory.map(function(p) { return p.l1; }) },
            { data: loadHistory.map(function(p) { return p.l5; }) },
            { data: loadHistory.map(function(p) { return p.l15; }) },
        ],
    });

    pushHistory(netHistory, { label: label, inVal: data.network_bytes_recv_per_sec || 0, outVal: data.network_bytes_sent_per_sec || 0 });
    if (netChart) netChart.setOption({
        xAxis: { data: netHistory.map(function(p) { return p.label; }) },
        series: [
            { data: netHistory.map(function(p) { return p.inVal; }) },
            { data: netHistory.map(function(p) { return p.outVal; }) },
        ],
    });
}

/* ── Utilities ──────────────────────────────────────────── */
function fmtMB(val) {
    if (val == null) return '--';
    if (val >= 1024) return (val / 1024).toFixed(1) + ' GB';
    return Math.round(val) + ' MB';
}

function clamp(v) { return Math.min(100, Math.max(0, v || 0)); }

function timeLabel(d) {
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map(function(v) { return String(v).padStart(2, '0'); }).join(':');
}

function pushHistory(arr, item) {
    arr.push(item);
    if (arr.length > MAX_POINTS) arr.shift();
}

function escHtml(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
