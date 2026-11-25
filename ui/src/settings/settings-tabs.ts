/**
 * The settings page's tabs, the URL hash that names one, and the links to the pages beside it.
 * Pure, so the tab strip's rules run under bare `node --test`.
 *
 * THE HASH IS THE DEEP LINK. `settings.html#features` opens on FEATURES, which is how the rack
 * (or a README) points at one tab. An unknown or empty hash is the first tab, never an error.
 */
import { DIAGRAM_PAGE, EVALS_PAGE, RACK_PAGE, REVIEW_PAGE } from '../config/pages.ts';

export const SETTINGS_TABS = [
    { id: 'setup', label: 'SETUP', title: 'Updates, what this machine has, and the parts still to install' },
    { id: 'features', label: 'FEATURES', title: 'Switch off the parts of the rack that cost a CPU or a GPU' },
    { id: 'appearance', label: 'APPEARANCE', title: 'The terminal’s font, renderer and theme' },
    { id: 'directories', label: 'DIRECTORIES', title: 'Where a new agent starts' },
] as const;

export type SettingsTabId = (typeof SETTINGS_TABS)[number]['id'];

export const DEFAULT_TAB: SettingsTabId = SETTINGS_TABS[0].id;

/** `location.hash` (with or without its `#`) → the tab it names, else the first tab. */
export function tab_from_hash(hash: string): SettingsTabId {
    const name = hash.replace(/^#/, '').toLowerCase();
    return SETTINGS_TABS.find((tab) => tab.id === name)?.id ?? DEFAULT_TAB;
}

export function tab_hash(id: SettingsTabId): string {
    return `#${id}`;
}

/**
 * The tab a key moves focus to, or null when the key is not the strip's: the arrow keys wrap,
 * Home and End jump. (A tab strip takes ONE tab stop, and the arrows move inside it.)
 */
export function tab_for_key(current: SettingsTabId, key: string): SettingsTabId | null {
    const index = SETTINGS_TABS.findIndex((tab) => tab.id === current);
    const count = SETTINGS_TABS.length;
    if (key === 'ArrowRight') return SETTINGS_TABS[(index + 1) % count]!.id;
    if (key === 'ArrowLeft') return SETTINGS_TABS[(index - 1 + count) % count]!.id;
    if (key === 'Home') return SETTINGS_TABS[0].id;
    if (key === 'End') return SETTINGS_TABS[count - 1]!.id;
    return null;
}

export interface PageLink {
    label: string;
    title: string;
    href: string;
}

/**
 * The pages beside this one, each resolved against this page's own address so they land on
 * whatever origin and path served it. The names: owned by `config/pages.ts` — see the note there.
 * An address that is not a URL yields no links.
 */
export function page_links(page_href: string): PageLink[] {
    const pages: ReadonlyArray<Omit<PageLink, 'href'> & { page: string }> = [
        { label: '← RACK', title: 'Back to the agent rack', page: RACK_PAGE },
        { label: 'REVIEW', title: 'Laya review: grade the local model’s verdicts', page: REVIEW_PAGE },
        { label: 'DIAGRAM', title: 'How the rack works, one diagram per tab', page: DIAGRAM_PAGE },
        { label: 'EVALS', title: 'The eval results', page: EVALS_PAGE },
    ];
    try {
        return pages.map(({ page, ...link }) => ({ ...link, href: new URL(page, page_href).href }));
    } catch {
        return [];
    }
}
