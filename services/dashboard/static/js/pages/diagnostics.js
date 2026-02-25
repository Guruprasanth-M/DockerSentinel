import { qs, setText, setHtml } from '../helpers/dom.js';
import { formatDuration } from '../helpers/format.js';
import { clearLogLines, log } from './diagnostics-ui.js';
import { checkFrontend, checkApiEndpoints, checkServices, checkWebSocket, checkDataPipeline } from './diagnostics-checks.js';

let clockInterval = null;
let startTime = Date.now();

export function init() {
    var btn = qs('#diagRunAll');
    if (btn) btn.addEventListener('click', runAllChecks);

    var clearBtn = qs('#diagClearLog');
    if (clearBtn) clearBtn.addEventListener('click', function () {
        clearLogLines();
        setHtml('diagLog', '<div class="log-line text-muted">Log cleared</div>');
    });

    clockInterval = setInterval(updateClock, 1000);
    updateClock();

    runAllChecks();
}

export function destroy() {
    if (clockInterval) { clearInterval(clockInterval); clockInterval = null; }
}

function updateClock() {
    var now = new Date();
    var dateStr = now.getFullYear() + '-' +
        String(now.getMonth() + 1).padStart(2, '0') + '-' +
        String(now.getDate()).padStart(2, '0');
    var timeStr = String(now.getHours()).padStart(2, '0') + ':' +
        String(now.getMinutes()).padStart(2, '0') + ':' +
        String(now.getSeconds()).padStart(2, '0');

    setText('diagDate', dateStr);
    setText('diagTime', timeStr);
    setText('diagTimezone', Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');

    var elapsed = (Date.now() - startTime) / 1000;
    setText('diagUptime', formatDuration(elapsed));

    setText('diagServerTime', dateStr + ' ' + timeStr);
}

async function runAllChecks() {
    clearLogLines();
    log('info', 'Starting diagnostic checks...');

    var results = { pass: 0, warn: 0, fail: 0 };

    log('info', '--- Frontend Resource Checks ---');
    var fr = await checkFrontend();
    results.pass += fr.pass; results.warn += fr.warn; results.fail += fr.fail;

    log('info', '--- API Endpoint Checks ---');
    var ar = await checkApiEndpoints();
    results.pass += ar.pass; results.warn += ar.warn; results.fail += ar.fail;

    log('info', '--- Service Health Checks ---');
    var sr = await checkServices();
    results.pass += sr.pass; results.warn += sr.warn; results.fail += sr.fail;

    log('info', '--- WebSocket Checks ---');
    var wr = checkWebSocket();
    results.pass += wr.pass; results.warn += wr.warn; results.fail += wr.fail;

    log('info', '--- Data Pipeline Checks ---');
    var dr = await checkDataPipeline();
    results.pass += dr.pass; results.warn += dr.warn; results.fail += dr.fail;

    var total = results.pass + results.warn + results.fail;
    setText('diagPassCount', String(results.pass));
    setText('diagWarnCount', String(results.warn));
    setText('diagFailCount', String(results.fail));
    setText('diagTotalCount', String(total));

    var passCard = qs('#diagPassCard');
    if (passCard) {
        passCard.className = 'stat-card stat-card--green';
    }

    log('info', 'Diagnostics complete: ' + results.pass + ' passed, ' + results.warn + ' warnings, ' + results.fail + ' failed');
}
