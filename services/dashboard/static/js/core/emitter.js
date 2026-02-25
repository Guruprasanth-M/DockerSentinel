const listeners = new Map();

export function on(event, callback) {
    if (!listeners.has(event)) {
        listeners.set(event, new Set());
    }
    listeners.get(event).add(callback);
    return () => off(event, callback);
}

export function off(event, callback) {
    const set = listeners.get(event);
    if (set) set.delete(callback);
}

export function emit(event, data) {
    const set = listeners.get(event);
    if (set) {
        for (const cb of set) {
            try {
                cb(data);
            } catch (err) {
                console.error('[emitter] Error in listener for ' + event + ':', err);
            }
        }
    }
}

export function once(event, callback) {
    const wrapper = (data) => {
        off(event, wrapper);
        callback(data);
    };
    on(event, wrapper);
}
