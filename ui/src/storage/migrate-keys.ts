/**
 * A one-time copy of this app's `localStorage` keys from the names they had before the pages
 * were their own project. Without it, a browser that already used the rack on some origin
 * would lose its font size, its split, its remembered machine and its panel states on the day
 * that origin starts serving these pages.
 *
 * IMPORT THIS FIRST in a page entry (`agent-rack/mount.ts` and each `main.ts` do). Modules run
 * in import order, and some read storage as they load (`engine/terminal/local-network-gate.ts`
 * reads its opt-in at module scope), so the copy has to land before they do.
 *
 * It copies and never deletes: the old page, if it is still served somewhere on the origin,
 * keeps working. It never overwrites: a key the new name already holds is the newer answer.
 * `MIGRATED_KEY` makes it run once per origin, so a setting cleared later is not resurrected.
 *
 * The list is history, not a registry: these are the keys that existed at the move. A new key
 * never had an old name and does not belong here.
 */
export const MIGRATED_KEY = 'tldrack:storage-migrated-v1';

/** `[old name, new name]`. */
export const KEY_MOVES: ReadonlyArray<readonly [string, string]> = [
    ...[
        'activity-map',
        'agent-canvas-layout',
        'agent-canvas-size',
        'agent-office',
        'agent-rack-remote',
        'agent-rack-split-px',
        'agent-rack-window-rect',
        'local-network-v1',
        'rack-complete-open',
        'rack-devices-open',
        'rack-jobs-expanded',
        'rack-jobs-open',
        'rack-memory-open',
        'terminal-render-options-v2',
        'terminal-render-options-v3',
    ].map((name) => [`voidcraft:${name}`, `tldrack:${name}`] as const),
    ['vc:laya-review:prefs', 'tldrack:laya-review-prefs'],
];

/** The slice of `Storage` the copy needs, so a test hands it a plain map. */
export interface KeyStore {
    getItem: (key: string) => string | null;
    setItem: (key: string, value: string) => void;
}

/** Returns how many keys were copied. Pure apart from `store`. */
export function migrate_keys(store: KeyStore): number {
    if (store.getItem(MIGRATED_KEY) !== null) return 0;
    let copied = 0;
    for (const [old_key, new_key] of KEY_MOVES) {
        const value = store.getItem(old_key);
        if (value === null || store.getItem(new_key) !== null) continue;
        store.setItem(new_key, value);
        copied += 1;
    }
    store.setItem(MIGRATED_KEY, String(copied));
    return copied;
}

try {
    if (typeof localStorage !== 'undefined') migrate_keys(localStorage);
} catch {
    // Site data blocked, or the quota is full: every reader falls back to its default.
}
