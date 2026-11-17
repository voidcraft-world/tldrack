/**
 * Open (or focus) the Laya REVIEW deck in its own popup. ONE owner of the popup's address: the
 * rack's per-session LABEL button and any host surface that offers REVIEW read it from here.
 *
 * Synchronous on purpose: `window.open` must run inside the click that asked for it, or the
 * browser blocks the popup. One fixed window name, so a second press finds the window already
 * open, and the URL doubles as a bookmark. Pure apart from `open_laya_review`, so a view model
 * imports it under bare `node --test`.
 */
import type { ReviewRecentOrder } from '@tldrack/schemas/laya-review.ts';
import { REVIEW_PAGE } from '../config/pages.ts';

export const LAYA_REVIEW_PATH = `/${REVIEW_PAGE}`;
export const LAYA_REVIEW_TARGET = 'tldrack-review';
// Wide enough for the deck's two columns (`LayaReviewDeck.vue` `.lr`, 1280px).
export const LAYA_REVIEW_FEATURES = 'popup,width=1320,height=940';

/** The page's two views: the sampled DECK, one card at a time, and the RECENT list of live verdicts. */
export type LayaReviewView = 'deck' | 'recent';

/**
 * One rack session the RECENT list is narrowed to, and its NAME card drawn for: the rack's per-session LABEL button
 * opens it. `since` is when tmux created the session (epoch seconds), so a reused `vc-N` name never brings an older
 * session's turns. `aliases` are the names it had before, which the bridge knows (`topic_labels.py`) and the URL never
 * carries: Laya recorded each verdict under the name of the moment.
 */
export interface ReviewSession {
    name: string;
    since: number | null;
    aliases?: readonly string[];
}

/** Where in a view the page stands: the deck's DEAL filter (`review-deck-model.ts` `DEAL_STRATA`), or the RECENT list's
 *  order and session. Each is kept only on its own view, and a default is never written. */
export interface LayaReviewPlace {
    stratum?: string | null;
    order?: ReviewRecentOrder;
    session?: ReviewSession | null;
}

/** The bridge's session-name shape (`SESSION_NAME_RE`) and a tmux `session_created`. */
const SESSION_NAME = /^[A-Za-z0-9_-]{1,128}$/;
const SINCE = /^[1-9][0-9]{9}$/;

/** No surface: the deck opens on the one with the most to review (the plugin decides). The deck view is the default
 *  and names no `view`, so every bookmark made before RECENT existed still opens what it did. */
export function laya_review_href(surface?: string, view: LayaReviewView = 'deck', place: LayaReviewPlace = {}): string {
    const query = new URLSearchParams();
    if (surface) query.set('surface', surface);
    if (view !== 'deck') {
        query.set('view', view);
        if (place.order === 'edge') query.set('order', place.order);
        if (place.session && SESSION_NAME.test(place.session.name)) {
            query.set('session', place.session.name);
            if (place.session.since !== null && SINCE.test(String(place.session.since))) query.set('since', String(place.session.since));
        }
    } else if (place.stratum) query.set('stratum', place.stratum);
    const search = query.toString();
    return search ? `${LAYA_REVIEW_PATH}?${search}` : LAYA_REVIEW_PATH;
}

/** `?view=` off the page's own URL; anything but `recent` is the deck. */
export function view_from_search(search: string): LayaReviewView {
    return new URLSearchParams(search).get('view') === 'recent' ? 'recent' : 'deck';
}

/** `?session=` (+ `since`) off the page's own URL, or null: the whole list. Only the RECENT view reads it. */
export function session_from_search(search: string): ReviewSession | null {
    const query = new URLSearchParams(search);
    const name = query.get('session');
    if (!name || !SESSION_NAME.test(name)) return null;
    const since = query.get('since');
    return { name, since: since && SINCE.test(since) ? Number(since) : null };
}

/** `?order=` off the page's own URL; anything but `edge` is the newest first. */
export function order_from_search(search: string): ReviewRecentOrder {
    return new URLSearchParams(search).get('order') === 'edge' ? 'edge' : 'newest';
}

/** What an open REVIEW window takes to move to another address without reloading (`LayaReviewDeck.vue` listens). */
export const LAYA_REVIEW_GO_MESSAGE = 'tldrack-review-go';

/**
 * Open REVIEW at this place, or bring an open one forward and move it there. An empty URL finds the named window
 * without navigating it, so a page already up keeps its state and only switches (a message, not a reload); a fresh
 * popup comes back `about:blank` and is pointed at the address.
 */
export function open_laya_review(surface?: string, view: LayaReviewView = 'deck', place: LayaReviewPlace = {}): 'opened' | 'focused' | 'blocked' {
    const href = new URL(laya_review_href(surface, view, place), window.location.href).href;
    const win = window.open('', LAYA_REVIEW_TARGET, LAYA_REVIEW_FEATURES);
    if (!win) return 'blocked';
    if (shows_review(win)) {
        win.postMessage({ type: LAYA_REVIEW_GO_MESSAGE, href }, window.location.origin);
        win.focus();
        return 'focused';
    }
    win.location.href = href;
    win.focus();
    return 'opened';
}

/** The window already shows REVIEW on this origin. Another origin throws on the read, and is navigated instead. */
function shows_review(win: Window): boolean {
    try {
        return win.location.origin === window.location.origin && win.location.pathname === LAYA_REVIEW_PATH;
    } catch {
        return false;
    }
}
