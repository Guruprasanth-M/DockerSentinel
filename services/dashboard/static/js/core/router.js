import { qs, qsa } from '../helpers/dom.js';
import * as emitter from './emitter.js';

const PAGES = ['overview', 'metrics', 'alerts', 'containers', 'processes', 'ports', 'logs', 'actions', 'settings', 'diagnostics'];
const CACHE_BUST = 'v=' + Date.now();
const templateCache = new Map();
const controllerCache = new Map();

let currentPage = null;
let currentController = null;

function getPageFromPath() {
    var path = window.location.pathname.replace(/^\/+/, '').replace(/\/+$/, '');
    // Also support legacy hash URLs for backwards compat
    if (!path && window.location.hash) {
        path = window.location.hash.slice(1);
    }
    return PAGES.includes(path) ? path : 'overview';
}

export function getCurrentPage() {
    return currentPage;
}

export async function navigate(pageName, pushState) {
    if (pushState === undefined) pushState = true;

    if (!PAGES.includes(pageName)) {
        pageName = 'overview';
    }

    if (pageName === currentPage) return;

    const main = qs('#main');
    if (!main) return;

    // Destroy current page controller and clean up its emitter listeners
    if (currentController && typeof currentController.destroy === 'function') {
        try {
            currentController.destroy();
        } catch (err) {
            console.error('[router] Destroy error:', err);
        }
    }
    currentController = null;

    // Update sidebar active state
    qsa('.sidebar-link').forEach(function (link) {
        link.classList.toggle('active', link.dataset.page === pageName);
    });

    // Update URL without reload
    if (pushState) {
        var newPath = '/' + (pageName === 'overview' ? '' : pageName);
        if (newPath === '/') newPath = '/';
        window.history.pushState({ page: pageName }, '', newPath);
    }

    // Show loading
    main.innerHTML = '<div class="page-loader"><div class="spinner"></div><span class="mono text-muted text-sm">Loading</span></div>';

    // Load template
    let html = templateCache.get(pageName);
    if (!html) {
        try {
            const res = await fetch('/templates/' + pageName + '.html?' + CACHE_BUST);
            if (!res.ok) throw new Error('Template not found: ' + pageName);
            html = await res.text();
            templateCache.set(pageName, html);
        } catch (err) {
            console.error('[router] Template load failed:', err);
            main.innerHTML = '<div class="empty-state"><div class="empty-text">Failed to load page: ' + pageName + '</div></div>';
            return;
        }
    }

    // Insert template
    main.innerHTML = html;
    currentPage = pageName;

    // Load controller
    let controller = controllerCache.get(pageName);
    if (!controller) {
        try {
            controller = await import('/js/pages/' + pageName + '.js?' + CACHE_BUST);
            controllerCache.set(pageName, controller);
        } catch (err) {
            console.error('[router] Controller load failed:', err);
        }
    }

    if (controller && typeof controller.init === 'function') {
        try {
            controller.init(main);
            currentController = controller;
        } catch (err) {
            console.error('[router] Init error:', err);
        }
    }

    // Scroll to top
    main.scrollTop = 0;

    emitter.emit('page:change', pageName);
}

export function init() {
    // Handle browser back/forward
    window.addEventListener('popstate', function () {
        var page = getPageFromPath();
        navigate(page, false);
    });

    // Handle sidebar clicks
    qsa('.sidebar-link').forEach(function (link) {
        link.addEventListener('click', function (e) {
            e.preventDefault();
            var page = link.dataset.page;
            if (page) {
                navigate(page);
            }
        });
    });

    // Handle any internal links with data-page
    document.addEventListener('click', function (e) {
        var target = e.target.closest('[data-page]');
        if (target && target.dataset.page) {
            e.preventDefault();
            navigate(target.dataset.page);
        }
    });

    // Navigate to initial page from URL path
    var initial = getPageFromPath();
    navigate(initial, false);
}
