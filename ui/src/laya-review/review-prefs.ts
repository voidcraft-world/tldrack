/**
 * The REVIEW page's two per-device switches: LIGHT (the page's theme) and EXPLAIN (every mark's sentence version,
 * drawn inline). Browser storage on purpose: they are one viewer's conveniences on one device. The review page and
 * a deck mounted inside a host application's page on the same origin share them, so one press sets both.
 *
 * Storage can be missing or throw (a private window, blocked site data, a thumbnail capture): every read and write
 * is guarded, and the page then simply draws the defaults. Why EXPLAIN defaults on: owned by `review-deck-model.ts`
 * § EXPLAIN — see the note there.
 */
export type ReviewTheme = 'dark' | 'light';

export interface ReviewPrefs {
    theme: ReviewTheme;
    explain: boolean;
}

export const REVIEW_PREFS_DEFAULT: Readonly<ReviewPrefs> = { theme: 'dark', explain: true };

export const REVIEW_PREFS_KEY = 'tldrack:laya-review-prefs';

/** The part of `Storage` this module touches — a real one fits, and so does a test's object. */
export interface PrefsStorage {
    getItem(key: string): string | null;
    setItem(key: string, value: string): void;
}

/** Whatever was stored, read field by field: an unknown or broken value falls back to that field's default. */
export function parse_review_prefs(raw: string | null): ReviewPrefs {
    let found: unknown = null;
    try {
        found = raw === null ? null : JSON.parse(raw);
    } catch {
        found = null;
    }
    const obj = typeof found === 'object' && found !== null ? (found as Record<string, unknown>) : {};
    return {
        theme: obj.theme === 'light' || obj.theme === 'dark' ? obj.theme : REVIEW_PREFS_DEFAULT.theme,
        explain: typeof obj.explain === 'boolean' ? obj.explain : REVIEW_PREFS_DEFAULT.explain,
    };
}

function page_storage(): PrefsStorage | null {
    try {
        return window.localStorage;
    } catch {
        return null;
    }
}

export function load_review_prefs(storage: PrefsStorage | null = page_storage()): ReviewPrefs {
    try {
        return parse_review_prefs(storage?.getItem(REVIEW_PREFS_KEY) ?? null);
    } catch {
        return { ...REVIEW_PREFS_DEFAULT };
    }
}

export function save_review_prefs(prefs: ReviewPrefs, storage: PrefsStorage | null = page_storage()): void {
    try {
        storage?.setItem(REVIEW_PREFS_KEY, JSON.stringify({ theme: prefs.theme, explain: prefs.explain }));
    } catch {
        // Not stored: this page still shows the choice; the next load draws the defaults.
    }
}
