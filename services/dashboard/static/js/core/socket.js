import * as emitter from './emitter.js';

let ws = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
const MAX_DELAY = 30000;

export function connect(token) {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let url = proto + '//' + window.location.host + '/ws/live';
    if (token) {
        url += '?token=' + encodeURIComponent(token);
    }

    emitter.emit('ws:status', 'connecting');

    try {
        ws = new WebSocket(url);
    } catch (err) {
        console.error('[ws] Failed to create WebSocket:', err);
        scheduleReconnect(token);
        return;
    }

    ws.onopen = function () {
        reconnectDelay = 1000;
        emitter.emit('ws:status', 'connected');
    };

    ws.onmessage = function (event) {
        try {
            const msg = JSON.parse(event.data);
            const type = msg.type || msg.event || 'unknown';
            emitter.emit('ws:message', msg);
            emitter.emit('ws:' + type, msg.data || msg);
        } catch (err) {
            console.warn('[ws] Bad message:', err);
        }
    };

    ws.onclose = function () {
        emitter.emit('ws:status', 'disconnected');
        scheduleReconnect(token);
    };

    ws.onerror = function () {
        emitter.emit('ws:status', 'error');
    };
}

function scheduleReconnect(token) {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_DELAY);
        connect(token);
    }, reconnectDelay);
}

export function disconnect() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    if (ws) {
        ws.onclose = null;
        ws.close();
        ws = null;
    }
}

export function isConnected() {
    return ws != null && ws.readyState === WebSocket.OPEN;
}

export function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
    }
}
