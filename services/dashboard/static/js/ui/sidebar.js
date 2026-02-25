import { qs, show, hide } from '../helpers/dom.js';
import * as emitter from '../core/emitter.js';

let alertTotal = 0;
let wsAccumulated = 0;

export function init() {
    // When the alerts page reports its actual count, use that as truth
    emitter.on('alerts:count', function (count) {
        alertTotal = count;
        wsAccumulated = 0; // Reset WS accumulator since we have real data
        updateBadge();
    });

    // Increment on anomaly events only when NOT on alerts page
    emitter.on('ws:anomaly_detected', function () {
        wsAccumulated++;
        alertTotal = wsAccumulated;
        updateBadge();
    });

    // Reset badge when navigating to alerts page (user has seen them)
    emitter.on('page:change', function (page) {
        if (page === 'alerts') {
            wsAccumulated = 0;
        }
    });
}

function updateBadge() {
    var badge = qs('#alertBadge');
    if (!badge) return;

    if (alertTotal > 0) {
        badge.textContent = alertTotal > 99 ? '99+' : String(alertTotal);
        show(badge);
    } else {
        hide(badge);
    }
}

export function setCount(n) {
    alertTotal = n;
    updateBadge();
}
