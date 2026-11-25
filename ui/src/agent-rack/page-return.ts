/**
 * "The operator came back to this page": the one signal a poller that rests while nobody is
 * looking wakes on.
 *
 * The rack's pollers already ask `document.hidden` on each tick and send nothing while it is
 * true (`rack-http-transport.ts` `wanted`). That alone leaves a returning operator looking at
 * the state from before they left until the next tick, which for a poller that has no timer at
 * all is never. So resting has a second half, and this is it: whoever rests also asks here to be
 * told the moment the page is looked at again, and reads once.
 *
 *   visible  the tab was hidden and is shown again (a tab switch, a restored window). Rare and
 *            always meaningful.
 *   focus    the window took focus while already visible: another window, or a change made on
 *            another device, and the operator is back. NOISY: clicking from the terminal's frame
 *            back onto the rack is a `focus` too, so a caller rate-limits what it does on one.
 *            And INCOMPLETE: while the terminal's frame holds the keyboard, leaving and coming
 *            back reaches no listener on this document. It is an extra chance, never the only one.
 *
 * Free of any global at import, so the modules that take it run under bare `node --test`: with
 * no page there is nothing to come back to, and the watcher is a no-op.
 */
export type PageReturn = 'visible' | 'focus';

/** The slice of a page the watcher needs. */
export interface PageReturnEnv {
    document: {
        readonly hidden: boolean;
        addEventListener(type: 'visibilitychange', listener: () => void): void;
        removeEventListener(type: 'visibilitychange', listener: () => void): void;
    };
    window: {
        addEventListener(type: 'focus', listener: () => void): void;
        removeEventListener(type: 'focus', listener: () => void): void;
    };
}

function page_env(): PageReturnEnv | null {
    return typeof document === 'undefined' || typeof window === 'undefined' ? null : { document, window };
}

/** Call `on_return` each time the operator comes back. Returns the stop function. */
export function watch_page_return(on_return: (how: PageReturn) => void, env: PageReturnEnv | null = page_env()): () => void {
    if (!env) return () => {};
    const on_visibility = (): void => {
        if (!env.document.hidden) on_return('visible');
    };
    const on_focus = (): void => on_return('focus');
    env.document.addEventListener('visibilitychange', on_visibility);
    env.window.addEventListener('focus', on_focus);
    return () => {
        env.document.removeEventListener('visibilitychange', on_visibility);
        env.window.removeEventListener('focus', on_focus);
    };
}
