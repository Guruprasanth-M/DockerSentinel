export function qs(selector, scope = document) {
    return scope.querySelector(selector);
}

export function qsa(selector, scope = document) {
    return Array.from(scope.querySelectorAll(selector));
}

export function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, val] of Object.entries(attrs)) {
        if (key === 'text') {
            node.textContent = val;
        } else if (key === 'html') {
            node.innerHTML = val;
        } else if (key === 'class') {
            node.className = val;
        } else if (key === 'style' && typeof val === 'object') {
            Object.assign(node.style, val);
        } else if (key.startsWith('on') && typeof val === 'function') {
            node.addEventListener(key.slice(2).toLowerCase(), val);
        } else if (key === 'data' && typeof val === 'object') {
            for (const [dk, dv] of Object.entries(val)) {
                node.dataset[dk] = dv;
            }
        } else {
            node.setAttribute(key, val);
        }
    }
    for (const child of children) {
        if (typeof child === 'string') {
            node.appendChild(document.createTextNode(child));
        } else if (child) {
            node.appendChild(child);
        }
    }
    return node;
}

export function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
}

export function setHtml(id, html) {
    const node = document.getElementById(id);
    if (node) node.innerHTML = html;
}

export function show(element) {
    if (typeof element === 'string') element = document.getElementById(element);
    if (element) element.classList.remove('hidden');
}

export function hide(element) {
    if (typeof element === 'string') element = document.getElementById(element);
    if (element) element.classList.add('hidden');
}

export function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

export function debounce(fn, ms) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), ms);
    };
}
