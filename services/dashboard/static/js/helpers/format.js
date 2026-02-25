export function formatBytes(bytes) {
    if (bytes == null || isNaN(bytes)) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    let value = Math.abs(bytes);
    while (value >= 1024 && i < units.length - 1) {
        value /= 1024;
        i++;
    }
    return value.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
}

export function formatBytesPerSec(bytes) {
    return formatBytes(bytes) + '/s';
}

export function formatPercent(value, decimals = 1) {
    if (value == null || isNaN(value)) return '--%';
    return Number(value).toFixed(decimals) + '%';
}

export function formatNumber(value) {
    if (value == null || isNaN(value)) return '--';
    return Number(value).toLocaleString();
}

export function formatDuration(seconds) {
    if (seconds == null || isNaN(seconds)) return '--:--:--';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return [h, m, s].map(v => String(v).padStart(2, '0')).join(':');
}

export function formatTimeAgo(timestamp) {
    if (!timestamp) return '--';
    try {
        const now = Date.now();
        const then = new Date(timestamp).getTime();
        const diff = (now - then) / 1000;

        if (diff < 0) return 'just now';
        if (diff < 5) return 'just now';
        if (diff < 60) return Math.round(diff) + 's ago';
        if (diff < 3600) return Math.round(diff / 60) + 'm ago';
        if (diff < 86400) return Math.round(diff / 3600) + 'h ago';
        return Math.round(diff / 86400) + 'd ago';
    } catch {
        return '--';
    }
}

export function formatTime(timestamp) {
    if (!timestamp) return '--:--:--';
    try {
        const d = new Date(timestamp);
        return d.toLocaleTimeString();
    } catch {
        return '--:--:--';
    }
}

export function formatClock() {
    const now = new Date();
    return [now.getHours(), now.getMinutes(), now.getSeconds()]
        .map(v => String(v).padStart(2, '0'))
        .join(':');
}

export function truncate(str, max) {
    if (!str) return '';
    return str.length <= max ? str : str.slice(0, max) + '...';
}
