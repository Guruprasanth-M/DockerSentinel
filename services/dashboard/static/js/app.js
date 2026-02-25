// Static imports for UNCHANGED shared modules (same instances across all modules)
import * as socket from './core/socket.js';
import * as emitter from './core/emitter.js';
import * as header from './ui/header.js';
import * as sidebar from './ui/sidebar.js';
import * as dialog from './ui/dialog.js';

// Dynamic imports for CHANGED modules (cache-busted to bypass stale browser cache)
const V = Date.now();

async function boot() {
    const [router, footer] = await Promise.all([
        import('./core/router.js?v=' + V),
        import('./ui/footer.js?v=' + V),
    ]);

    header.init();
    sidebar.init();
    footer.init();

    router.init();

    var token = localStorage.getItem('sentinel_token') || '';
    socket.connect(token);

    emitter.on('ws:anomaly_detected', function (data) {
        var score = data && data.anomaly_score ? data.anomaly_score.toFixed(3) : 'N/A';
        dialog.notify('Anomaly Detected', 'Score: ' + score, 'warning');
    });

    emitter.on('ws:service_status', function (data) {
        if (data && data.status === 'unhealthy') {
            dialog.toast('Service ' + (data.service || 'unknown') + ' is unhealthy', 'error');
        }
    });

    emitter.on('ws:alert', function (data) {
        if (data && data.severity === 'critical') {
            dialog.notify('Critical Alert', data.message || 'A critical alert was triggered', 'error');
        }
    });
}

boot();
