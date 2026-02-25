import * as api from '../core/api.js';
import * as emitter from '../core/emitter.js';
import { qs, setText, setHtml } from '../helpers/dom.js';
import { formatClock } from '../helpers/format.js';

let interval = null;

export function init() {
    var btn = qs('#containerRefresh');
    if (btn) btn.addEventListener('click', refresh);
    emitter.on('refresh', refresh);
    refresh();
    interval = setInterval(refresh, 5000);
}

export function destroy() {
    emitter.off('refresh', refresh);
    if (interval) { clearInterval(interval); interval = null; }
}

async function refresh() {
    var data = await api.getContainers();
    if (!data || !qs('#containersGrid')) return;

    setText('containerLastUpdate', 'Updated ' + formatClock());

    var containers = data.containers || [];
    var totalCpu = 0;
    var totalMem = 0;

    containers.forEach(function(c) {
        totalCpu += c.cpu_percent || 0;
        totalMem += c.memory_used_mb || 0;
    });

    setText('totalRunning', String(data.running || containers.length));
    setText('totalCpu', totalCpu.toFixed(1) + '%');
    setText('totalMem', totalMem.toFixed(0) + ' MB');

    if (!containers.length) {
        setHtml('containersGrid', '<div class="empty-placeholder"><span class="text-muted">No containers found</span></div>');
        return;
    }

    var html = '';
    containers.forEach(function(c) {
        var cpuColor = c.cpu_percent > 80 ? 'var(--red)' : c.cpu_percent > 50 ? 'var(--amber)' : 'var(--cyan)';
        var memPct = c.memory_percent || 0;
        var memColor = memPct > 80 ? 'var(--red)' : memPct > 50 ? 'var(--amber)' : 'var(--green)';

        html += '<div class="container-detail-card">' +
            '<div class="container-detail-header">' +
                '<span class="container-detail-name"><span class="status-dot"></span>' + escHtml(c.name) + '</span>' +
                '<span class="text-xs text-muted mono">' + c.pids + ' PIDs</span>' +
            '</div>' +
            '<div class="container-gauges">' +
                '<div class="container-gauge">' +
                    '<div class="container-gauge-value" style="color:' + cpuColor + '">' + c.cpu_percent.toFixed(1) + '%</div>' +
                    '<div class="container-gauge-label">CPU</div>' +
                '</div>' +
                '<div class="container-gauge">' +
                    '<div class="container-gauge-value" style="color:' + memColor + '">' + c.memory_used_mb.toFixed(0) + ' MB</div>' +
                    '<div class="container-gauge-label">Memory (' + memPct.toFixed(1) + '%)</div>' +
                '</div>' +
            '</div>' +
            '<div style="margin-top:12px">' +
                '<div style="display:flex;justify-content:space-between;font-size:11px;color:rgba(255,255,255,0.4);margin-bottom:4px">' +
                    '<span>Memory</span><span>' + c.memory_used_mb.toFixed(0) + ' / ' + c.memory_limit_mb.toFixed(0) + ' MB</span>' +
                '</div>' +
                '<div class="progress-bar"><div class="progress-fill" style="width:' + Math.min(100, memPct) + '%;background:' + memColor + '"></div></div>' +
            '</div>' +
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px;font-size:11px">' +
                '<div style="color:rgba(255,255,255,0.4)">Net RX: <span style="color:rgba(255,255,255,0.7)" class="mono">' + fmtBytes(c.network_rx_bytes) + '</span></div>' +
                '<div style="color:rgba(255,255,255,0.4)">Net TX: <span style="color:rgba(255,255,255,0.7)" class="mono">' + fmtBytes(c.network_tx_bytes) + '</span></div>' +
                '<div style="color:rgba(255,255,255,0.4)">Blk Read: <span style="color:rgba(255,255,255,0.7)" class="mono">' + fmtBytes(c.block_read_bytes) + '</span></div>' +
                '<div style="color:rgba(255,255,255,0.4)">Blk Write: <span style="color:rgba(255,255,255,0.7)" class="mono">' + fmtBytes(c.block_write_bytes) + '</span></div>' +
            '</div>' +
        '</div>';
    });

    setHtml('containersGrid', html);
}

function escHtml(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtBytes(b) {
    if (b == null || b === 0) return '0 B';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
}
