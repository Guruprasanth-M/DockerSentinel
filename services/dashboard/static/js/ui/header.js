import { qs, setText } from '../helpers/dom.js';
import { formatDuration, formatClock } from '../helpers/format.js';
import * as emitter from '../core/emitter.js';

let eventCount = 0;
let lastRateCheck = Date.now();
let startTime = Date.now();
let rateInterval = null;
let uptimeInterval = null;
let clockInterval = null;

export function init() {
    // Connection status
    emitter.on('ws:status', function (status) {
        var dot = qs('#connStatus .pulse-dot');
        var text = qs('#connText');
        if (!dot || !text) return;

        switch (status) {
            case 'connected':
                dot.className = 'pulse-dot pulse-dot--live';
                text.textContent = 'LIVE';
                break;
            case 'connecting':
                dot.className = 'pulse-dot pulse-dot--connecting';
                text.textContent = 'Connecting';
                break;
            case 'disconnected':
            case 'error':
                dot.className = 'pulse-dot pulse-dot--error';
                text.textContent = 'Offline';
                break;
        }
    });

    // Count events
    emitter.on('ws:message', function () {
        eventCount++;
    });

    // Event rate (1 second interval)
    rateInterval = setInterval(function () {
        var now = Date.now();
        var elapsed = (now - lastRateCheck) / 1000;
        if (elapsed >= 1) {
            var rate = Math.round(eventCount / elapsed);
            setText('evtValue', String(rate));
            eventCount = 0;
            lastRateCheck = now;
        }
    }, 1000);

    // Uptime
    uptimeInterval = setInterval(function () {
        var seconds = (Date.now() - startTime) / 1000;
        setText('uptimeValue', formatDuration(seconds));
    }, 1000);

    // Header clock and date
    function tickClock() {
        setText('headerClock', formatClock());
        var now = new Date();
        var dateStr = now.getFullYear() + '-' +
            String(now.getMonth() + 1).padStart(2, '0') + '-' +
            String(now.getDate()).padStart(2, '0');
        setText('headerDate', dateStr);
    }
    tickClock();
    clockInterval = setInterval(tickClock, 1000);

    // Global refresh button
    var btn = qs('#globalRefresh');
    if (btn) {
        btn.addEventListener('click', function () {
            btn.classList.add('spinning');
            emitter.emit('refresh');
            setTimeout(function () {
                btn.classList.remove('spinning');
            }, 800);
        });
    }
}

export function destroy() {
    if (rateInterval) clearInterval(rateInterval);
    if (uptimeInterval) clearInterval(uptimeInterval);
    if (clockInterval) clearInterval(clockInterval);
}
