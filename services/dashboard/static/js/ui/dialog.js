import { escapeHtml } from '../helpers/dom.js';

let toastWrap = null;
let notifyWrap = null;

function ensureContainers() {
    if (!toastWrap) {
        toastWrap = document.createElement('div');
        toastWrap.className = 'toast-wrap';
        document.body.appendChild(toastWrap);
    }
    if (!notifyWrap) {
        notifyWrap = document.createElement('div');
        notifyWrap.className = 'notify-wrap';
        document.body.appendChild(notifyWrap);
    }
}

function themeColor(type) {
    var colors = {
        info: 'var(--color-info)',
        success: 'var(--color-success)',
        warning: 'var(--color-warning)',
        error: 'var(--color-danger)',
        danger: 'var(--color-danger)',
    };
    return colors[type] || colors.info;
}

/* Alert dialog - simple OK */
export function alert(title, message, type) {
    return new Promise(function (resolve) {
        var overlay = document.createElement('div');
        overlay.className = 'dialog-overlay';
        overlay.setAttribute('tabindex', '-1');

        var borderColor = themeColor(type || 'info');

        overlay.innerHTML =
            '<div class="dialog-box">' +
            '<div class="dialog-head" style="border-bottom-color:' + borderColor + '">' +
            '<span class="dialog-head-title">' + escapeHtml(title) + '</span>' +
            '<button class="dialog-close" data-action="close">&times;</button>' +
            '</div>' +
            '<div class="dialog-body"><p>' + message + '</p></div>' +
            '<div class="dialog-footer">' +
            '<button class="btn btn-primary btn-sm" data-action="ok">OK</button>' +
            '</div>' +
            '</div>';

        document.body.appendChild(overlay);

        function close() {
            overlay.classList.add('closing');
            setTimeout(function () { overlay.remove(); }, 200);
            resolve();
        }

        overlay.querySelector('[data-action="ok"]').addEventListener('click', close);
        overlay.querySelector('[data-action="close"]').addEventListener('click', close);
        overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
        overlay.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
        overlay.focus();
    });
}

/* Confirm dialog - OK/Cancel */
export function confirm(title, message, options) {
    options = options || {};
    return new Promise(function (resolve) {
        var overlay = document.createElement('div');
        overlay.className = 'dialog-overlay';
        overlay.setAttribute('tabindex', '-1');

        var confirmText = options.confirmText || 'Confirm';
        var cancelText = options.cancelText || 'Cancel';
        var borderColor = themeColor(options.theme || 'warning');

        overlay.innerHTML =
            '<div class="dialog-box">' +
            '<div class="dialog-head" style="border-bottom-color:' + borderColor + '">' +
            '<span class="dialog-head-title">' + escapeHtml(title) + '</span>' +
            '<button class="dialog-close" data-action="close">&times;</button>' +
            '</div>' +
            '<div class="dialog-body"><p>' + message + '</p></div>' +
            '<div class="dialog-footer">' +
            '<button class="btn btn-sm" data-action="cancel">' + escapeHtml(cancelText) + '</button>' +
            '<button class="btn btn-danger btn-sm" data-action="confirm">' + escapeHtml(confirmText) + '</button>' +
            '</div>' +
            '</div>';

        document.body.appendChild(overlay);

        function done(val) {
            overlay.classList.add('closing');
            setTimeout(function () { overlay.remove(); }, 200);
            resolve(val);
        }

        overlay.querySelector('[data-action="confirm"]').addEventListener('click', function () { done(true); });
        overlay.querySelector('[data-action="cancel"]').addEventListener('click', function () { done(false); });
        overlay.querySelector('[data-action="close"]').addEventListener('click', function () { done(false); });
        overlay.addEventListener('click', function (e) { if (e.target === overlay) done(false); });
        overlay.addEventListener('keydown', function (e) { if (e.key === 'Escape') done(false); });
        overlay.focus();
    });
}

/* Toast - auto-dismissing notification */
export function toast(message, type, duration) {
    ensureContainers();
    type = type || 'info';
    duration = duration != null ? duration : 4000;

    var item = document.createElement('div');
    item.className = 'toast toast--' + type;
    item.innerHTML =
        '<span class="toast-msg">' + escapeHtml(message) + '</span>' +
        '<button class="toast-close" data-action="close">&times;</button>';

    toastWrap.appendChild(item);

    function dismiss() {
        item.classList.add('removing');
        setTimeout(function () { item.remove(); }, 300);
    }

    item.querySelector('[data-action="close"]').addEventListener('click', dismiss);

    if (duration > 0) {
        setTimeout(dismiss, duration);
    }
}

/* Notify - persistent notification */
export function notify(title, message, type, options) {
    ensureContainers();
    type = type || 'error';
    options = options || {};

    var item = document.createElement('div');
    item.className = 'notify-card notify-card--' + type;
    item.innerHTML =
        '<div class="notify-head">' +
        '<span class="notify-title">' + escapeHtml(title) + '</span>' +
        '<button class="notify-close" data-action="close">&times;</button>' +
        '</div>' +
        '<div class="notify-body">' + message + '</div>';

    notifyWrap.appendChild(item);

    function dismiss() {
        item.classList.add('removing');
        setTimeout(function () { item.remove(); }, 300);
    }

    item.querySelector('[data-action="close"]').addEventListener('click', dismiss);

    var dur = options.duration || 8000;
    if (!options.persistent && dur > 0) {
        setTimeout(dismiss, dur);
    }

    return { dismiss: dismiss };
}
