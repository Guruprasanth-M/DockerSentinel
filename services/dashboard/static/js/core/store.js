const state = {};
const watchers = new Map();

export function get(key) {
    return state[key];
}

export function set(key, value) {
    const prev = state[key];
    state[key] = value;
    notifyWatchers(key, value, prev);
}

export function update(key, fn) {
    const prev = state[key];
    const next = fn(prev);
    state[key] = next;
    notifyWatchers(key, next, prev);
}

export function watch(key, callback) {
    if (!watchers.has(key)) {
        watchers.set(key, new Set());
    }
    watchers.get(key).add(callback);

    // Return unsubscribe function
    return () => {
        const set = watchers.get(key);
        if (set) set.delete(callback);
    };
}

function notifyWatchers(key, value, prev) {
    const set = watchers.get(key);
    if (set) {
        for (const cb of set) {
            try {
                cb(value, prev);
            } catch (err) {
                console.error('[store] Watcher error for key ' + key + ':', err);
            }
        }
    }
}

export function getAll() {
    return { ...state };
}

export function reset() {
    for (const key of Object.keys(state)) {
        delete state[key];
    }
}
