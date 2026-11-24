/**
 * What THIS browser draws: the rack's per-device switches for the parts that cost a GPU or a
 * CPU core. The settings page (`settings/`) writes them; the rack reads them and follows a change
 * made in another tab (`watch_rack_prefs`).
 *
 * Browser storage on purpose. What a page renders is a fact about the device showing it: a
 * laptop on battery and a desk machine open the same bridge and want different answers. The
 * switches for what the BRIDGE runs are the bridge's own, and every device sees those
 * (`bridge/tldrack/features.py`, `GET /features`).
 *
 *   office          the 3D office in the rack's canvas pane. Off: three.js is never fetched and
 *                   nothing renders per frame.
 *   office_quality  `sharp` supersamples (`agent-office/office-render-quality.ts`); `light` draws
 *                   at one device pixel per pixel and at half the frame rate.
 *   motion          the FUI transitions (`fui-motion-flags.ts` `enabled`). The WORK scanner keeps
 *                   running either way — see the note there.
 *
 * Storage can be missing or throw (a private window, blocked site data): every read and write is
 * guarded, and the page then draws the defaults, which are the rack as it was before this file.
 */
export const OFFICE_QUALITIES = ['sharp', 'light'] as const;
export type OfficeQuality = (typeof OFFICE_QUALITIES)[number];

export interface RackPrefs {
    office: boolean;
    office_quality: OfficeQuality;
    motion: boolean;
}

export const DEFAULT_RACK_PREFS: Readonly<RackPrefs> = { office: true, office_quality: 'sharp', motion: true };

/** The LITE preset's half for this browser. The bridge names its own half (`lite` on a feature). */
export const LITE_RACK_PREFS: Readonly<RackPrefs> = { office: false, office_quality: 'light', motion: false };

export const RACK_PREFS_KEY = 'tldrack:rack-prefs-v1';

/** The part of `Storage` this module touches — a real one fits, and so does a test's object. */
export interface RackPrefsStorage {
    getItem(key: string): string | null;
    setItem(key: string, value: string): void;
}

/** Whatever was stored, read field by field: an unknown or broken value keeps that field's default. */
export function parse_rack_prefs(raw: string | null): RackPrefs {
    let found: unknown = null;
    try {
        found = raw === null ? null : JSON.parse(raw);
    } catch {
        found = null;
    }
    const obj = typeof found === 'object' && found !== null && !Array.isArray(found) ? (found as Record<string, unknown>) : {};
    return {
        office: typeof obj.office === 'boolean' ? obj.office : DEFAULT_RACK_PREFS.office,
        office_quality: (OFFICE_QUALITIES as readonly unknown[]).includes(obj.office_quality)
            ? (obj.office_quality as OfficeQuality)
            : DEFAULT_RACK_PREFS.office_quality,
        motion: typeof obj.motion === 'boolean' ? obj.motion : DEFAULT_RACK_PREFS.motion,
    };
}

function page_storage(): RackPrefsStorage | null {
    try {
        return typeof localStorage === 'undefined' ? null : localStorage;
    } catch {
        return null; // some private modes throw on the property access itself
    }
}

export function load_rack_prefs(storage: RackPrefsStorage | null = page_storage()): RackPrefs {
    try {
        return parse_rack_prefs(storage?.getItem(RACK_PREFS_KEY) ?? null);
    } catch {
        return { ...DEFAULT_RACK_PREFS };
    }
}

/** False when nothing was stored, so a page can say the switch holds for this visit only. */
export function save_rack_prefs(prefs: RackPrefs, storage: RackPrefsStorage | null = page_storage()): boolean {
    if (!storage) return false;
    try {
        storage.setItem(RACK_PREFS_KEY, JSON.stringify(parse_rack_prefs(JSON.stringify(prefs))));
        return true;
    } catch {
        return false;
    }
}

export function same_rack_prefs(a: RackPrefs, b: RackPrefs): boolean {
    return a.office === b.office && a.office_quality === b.office_quality && a.motion === b.motion;
}

/** The slice of `window` the watcher needs. */
export interface StorageEventTarget {
    addEventListener(type: 'storage', listener: (event: { key: string | null; newValue: string | null }) => void): void;
    removeEventListener(type: 'storage', listener: (event: { key: string | null; newValue: string | null }) => void): void;
}

/**
 * Follow a change made in ANOTHER tab of this origin (the browser sends `storage` to every
 * document but the writer). `key === null` is a whole-storage clear, which is a change too.
 * Returns the stop function.
 */
export function watch_rack_prefs(on_change: (prefs: RackPrefs) => void, target: StorageEventTarget | null = page_target()): () => void {
    if (!target) return () => {};
    const listener = (event: { key: string | null; newValue: string | null }): void => {
        if (event.key === RACK_PREFS_KEY) on_change(parse_rack_prefs(event.newValue));
        else if (event.key === null) on_change({ ...DEFAULT_RACK_PREFS });
    };
    target.addEventListener('storage', listener);
    return () => target.removeEventListener('storage', listener);
}

function page_target(): StorageEventTarget | null {
    return typeof window === 'undefined' ? null : window;
}
