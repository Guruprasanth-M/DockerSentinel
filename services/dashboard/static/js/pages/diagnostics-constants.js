export const CSS_FILES = [
    'tokens.css', 'reset.css', 'base.css', 'layout.css',
    'components.css', 'pages.css', 'effects.css'
];

export const JS_MODULES = [
    { name: 'app.js', path: '/js/app.js' },
    { name: 'router.js', path: '/js/core/router.js' },
    { name: 'api.js', path: '/js/core/api.js' },
    { name: 'socket.js', path: '/js/core/socket.js' },
    { name: 'emitter.js', path: '/js/core/emitter.js' },
    { name: 'store.js', path: '/js/core/store.js' },
    { name: 'echarts', path: '/vendor/echarts.min.js' },
];

export const TEMPLATES = [
    'overview', 'metrics', 'alerts', 'processes',
    'ports', 'logs', 'actions', 'settings', 'diagnostics'
];

export const API_ENDPOINTS = [
    { path: '/health', method: 'GET', name: 'Health' },
    { path: '/metrics', method: 'GET', name: 'Metrics' },
    { path: '/processes', method: 'GET', name: 'Processes' },
    { path: '/ports', method: 'GET', name: 'Ports' },
    { path: '/logs', method: 'GET', name: 'Logs' },
    { path: '/alerts', method: 'GET', name: 'Alerts' },
    { path: '/actions', method: 'GET', name: 'Actions' },
];

export const SERVICES = [
    { key: 'redis', name: 'Redis', expected: 'connected' },
    { key: 'db', name: 'Database', expected: 'connected' },
    { key: 'collectors', name: 'Collectors', expected: 'active' },
    { key: 'ml_engine', name: 'ML Engine', expected: 'active' },
    { key: 'policy_engine', name: 'Policy Engine', expected: 'active' },
    { key: 'action_engine', name: 'Action Engine', expected: 'active' },
    { key: 'webhook_service', name: 'Webhook Service', expected: 'active' },
    { key: 'api', name: 'API Server', expected: 'healthy' },
];

export const FIXES = {
    css_missing: 'Check that the CSS file exists in /static/css/. Rebuild dashboard: docker compose build dashboard && docker compose up -d dashboard',
    js_missing: 'Check that the JS file exists in /static/js/. Rebuild dashboard: docker compose build dashboard && docker compose up -d dashboard',
    template_missing: 'Check that the template exists in /static/templates/. Rebuild dashboard: docker compose build dashboard && docker compose up -d dashboard',
    api_unreachable: 'Check if API container is running: docker compose ps api. Restart: docker compose restart api',
    api_error: 'Check API logs: docker compose logs api --tail 50',
    redis_down: 'Redis is not connected. Restart: docker compose restart redis. Check logs: docker compose logs redis',
    service_down: 'Service is not responding. Restart it: docker compose restart SERVICE_NAME. Check logs: docker compose logs SERVICE_NAME --tail 50',
    service_unknown: 'Service status is unknown. It may still be starting. Wait 30 seconds. If persists: docker compose restart SERVICE_NAME',
    ws_disconnected: 'WebSocket not connected. Check if API is running. Try refreshing the page. Check browser console for errors.',
    ws_no_events: 'WebSocket connected but no events received. Collectors may be inactive: docker compose restart collectors',
    no_metrics: 'No metric data available. Collectors may not be running: docker compose logs collectors --tail 20',
    no_processes: 'No process data. Check collector: docker compose restart collectors',
    stale_data: 'Data may be stale. Check if collectors are actively sending: docker compose logs collectors --tail 10',
};
