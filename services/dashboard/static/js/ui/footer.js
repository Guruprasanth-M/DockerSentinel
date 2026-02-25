import { setText } from '../helpers/dom.js';
import * as emitter from '../core/emitter.js';

let lastUpdate = Date.now();
let statusInterval = null;

export function init() {
    // Listen for data refresh events to show "Fetching..." → "Updated"
    emitter.on('refresh', function () {
        setText('dataStatus', 'Fetching...');
        lastUpdate = Date.now();
    });

    emitter.on('ws:message', function () {
        lastUpdate = Date.now();
    });

    statusInterval = setInterval(function () {
        var age = Math.round((Date.now() - lastUpdate) / 1000);
        if (age < 2) {
            setText('dataStatus', 'Live');
        } else if (age < 10) {
            setText('dataStatus', 'Updated ' + age + 's ago');
        } else {
            setText('dataStatus', 'Stale (' + age + 's)');
        }
    }, 1000);
}

export function destroy() {
    if (statusInterval) clearInterval(statusInterval);
}
