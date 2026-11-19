/**
 * "May this page talk to 127.0.0.1 yet?": the ONE owner of that question.
 *
 * ── WHY THIS EXISTS ──────────────────────────────────────────────────────────
 * Chrome 142 promoted the old, silent Private Network Access preflight into a user-facing
 * **Local Network Access** permission. Any request from a public origin down to loopback (a
 * `fetch`, a subresource, a subframe navigation) raises a modal the visitor has to answer:
 *
 *     <site> wants to access other apps and services on this device
 *
 * A host application that serves these modules from a public origin, and reaches for the
 * bridge as it boots, shows that prompt to every visitor before they have asked for anything,
 * including the ones with no bridge at all.
 *
 * The rule this module enforces: **no loopback request happens until the visitor touches a
 * surface that needs one.** Until then a surface renders its non-local chrome and offers a
 * CONNECT control.
 *
 * A page the bridge itself serves is the other case, and the simple one: it is already on
 * loopback, so nothing is gated (`page_is_loopback`).
 *
 * ── DELIBERATELY IMPORT-FREE ─────────────────────────────────────────────────
 * `rack-ingestor.ts` imports this and runs under bare `node --test`. Every browser global is
 * `typeof`-guarded for the same reason.
 *
 * ── THE PERMISSION IS NOT THE SAME QUESTION AS THE OPT-IN ────────────────────
 * Two independent facts, and the UI has to be able to tell them apart:
 *
 *  - **The opt-in** is ours, per device, in `localStorage`. It means "the user
 *    asked for local tools on this machine". Per device, like the terminal render
 *    options: the bridge is a property of the MACHINE.
 *  - **The permission** is Chrome's, per origin. `granted` means a request will
 *    not prompt; `denied` means it will fail no matter what we do, and only the
 *    user can undo it from Chrome's own settings.
 *
 * A `granted` permission opens the gate on its own: there is no prompt left to
 * defer, so a returning user pays exactly what they paid before this change.
 */

/** Chrome's permission name. Unknown to every other browser — hence the guard. */
const PERMISSION_NAME = 'local-network-access';

/** Per-device opt-in. `-v1` so a future meaning change can invalidate cleanly. */
const STORAGE_KEY = 'tldrack:local-network-v1';

export type LocalNetworkPermission = 'granted' | 'denied' | 'prompt' | 'unknown';

/**
 * `open`   — a loopback request may go out now.
 * `locked` — the user has not asked for local tools on this device yet.
 * `denied` — Chrome will refuse; only the user can undo it, in browser settings.
 */
export type LocalAccessState = 'open' | 'locked' | 'denied';

/**
 * The three ways the GATE (never the bridge) can be the reason a loopback
 * surface is empty. See `classify_access_notice` for which is which.
 *
 * Deliberately not a reworded `LocalAccessState`: `open` is not a notice, and
 * `prompting` is not a state — it is `open` crossed with a browser verdict.
 */
export type AccessNotice = 'locked' | 'denied' | 'prompting';

/** The shell command that starts the bridge: what a "not running" surface offers to copy. */
export const BRIDGE_START_COMMAND = 'tldrack up';

/** The copy for "the bridge is not answering", owned here so no surface spells its own. */
export const BRIDGE_DOWN_HINT = `Start it: ${BRIDGE_START_COMMAND}`;

/** Shown when the gate is shut: nothing is wrong, nothing has been asked yet. */
export const LOCAL_ACCESS_LOCKED_HINT = 'Local tools are off — connect to reach this machine’s bridge';

/**
 * Shown when Chrome has denied the permission. A page cannot re-prompt after a
 * block, so the only honest next step is the settings path — spelled out,
 * because "check your browser settings" is not an instruction anyone can follow.
 */
export const LOCAL_ACCESS_DENIED_HINT =
    'Chrome is blocking local network access — Settings › Privacy and security › ' +
    'Site settings › Additional permissions › Local network access';

/**
 * Shown while the browser's own prompt is outstanding.
 *
 * Worth its own string because the fix is neither ours nor a settings trip: the
 * dialog is on screen right now and wants an answer. Retrying is what makes it
 * reappear after a dismissal, so the surface pairs this with a retry control.
 */
export const LOCAL_ACCESS_PROMPTING_HINT = 'Waiting on the browser’s local network prompt — allow it to continue';

/** Thrown by `local_fetch` when the gate is shut. Never reaches the network. */
export class LocalAccessBlocked extends Error {
    public readonly state: Exclude<LocalAccessState, 'open'>;

    constructor(state: Exclude<LocalAccessState, 'open'>) {
        super(state === 'denied' ? LOCAL_ACCESS_DENIED_HINT : LOCAL_ACCESS_LOCKED_HINT);
        this.name = 'LocalAccessBlocked';
        this.state = state;
    }
}

/** Did this failure come from the gate rather than from a real network attempt? */
export function is_local_access_blocked(error: unknown): error is LocalAccessBlocked {
    return error instanceof LocalAccessBlocked;
}

// ── loopback detection ───────────────────────────────────────────────────────

/**
 * Is this URL a loopback destination — i.e. one Chrome's LNA permission gates?
 *
 * PURE and exported so the tests can hit it without a document.
 *
 * Scope is deliberately **loopback only**, not the whole private-address space
 * the permission covers. The one private-but-not-loopback destination a page
 * reaches is the tailnet host behind `?remote=`, and that URL is typed by the
 * operator into their own address bar: the request IS their action, so there is
 * nothing left to defer. `rack-remote.ts` keeps its own independent host check
 * for that path: it is a security pin (which hosts may be framed as "your
 * terminal"), not a permission gate, and two checks are better than one shared one.
 */
export function is_loopback_url(url: string): boolean {
    let host: string;
    try {
        host = new URL(url, typeof location === 'undefined' ? 'https://void.invalid' : location.href).hostname;
    } catch {
        return false;
    }
    return host === 'localhost' || host === '127.0.0.1' || host === '[::1]' || host === '::1';
}

// ── the decision ─────────────────────────────────────────────────────────────

/**
 * The gate's whole rule, as a pure function of the two facts.
 *
 * Split out and exported for the tests: the interesting behaviour is the
 * precedence between a stored opt-in and a browser verdict, and asserting that
 * against real `localStorage` would test the wrong thing.
 *
 * `denied` wins over an opt-in because a request would fail either way, and the
 * surfaces read this to decide WHICH explanation to print. `granted` wins over
 * a missing opt-in because there is no prompt left to defer.
 */
export function decide_local_access(opted_in: boolean, permission: LocalNetworkPermission): LocalAccessState {
    if (permission === 'denied') return 'denied';
    if (permission === 'granted') return 'open';
    return opted_in ? 'open' : 'locked';
}

/**
 * Why a loopback surface has nothing to show, when the reason is NOT the bridge.
 *
 * `null` means the gate is not in the way, so the caller's own bridge-down copy
 * stands. Anything else is a DIFFERENT problem with a DIFFERENT fix, and the
 * whole point of returning them separately is that a surface must not print
 * "not running" at a bridge that is running perfectly well.
 *
 * Pure, and exported so the tests can assert the truth table without a browser.
 */
export function classify_access_notice(
    state: LocalAccessState,
    permission: LocalNetworkPermission,
): AccessNotice | null {
    if (state === 'locked') return 'locked';
    if (state === 'denied') return 'denied';
    // Open but unanswered: the opt-in is stored, so `local_fetch` lets the
    // request out — and it then HANGS behind Chrome's modal until the caller's
    // own timeout fires. Every surface reads that as an unreachable bridge while
    // the permission dialog is still on screen, which is the single most
    // misleading state this module can produce.
    return permission === 'prompt' ? 'prompting' : null;
}

// ── module state ─────────────────────────────────────────────────────────────

let opted_in = read_stored_optin();
let permission: LocalNetworkPermission = 'unknown';
/** Coalesced: every caller shares one `permissions.query`, which never prompts. */
let permission_read: Promise<LocalNetworkPermission> | null = null;
const listeners = new Set<(state: LocalAccessState) => void>();

function read_stored_optin(): boolean {
    try {
        return typeof localStorage !== 'undefined' && localStorage.getItem(STORAGE_KEY) === 'on';
    } catch {
        // Private windows and "block site data" both throw on ACCESS, not just
        // on write. Treat an unreadable store as "never opted in".
        return false;
    }
}

function notify(): void {
    const state = local_access_state();
    for (const fn of listeners) {
        try {
            fn(state);
        } catch {
            // A listener that throws must not stop the others from restarting.
        }
    }
}

/**
 * Is this document itself served from loopback? The browser's permission covers a request from
 * a more public address space down to a more private one. A page the bridge serves on
 * `127.0.0.1` asks loopback for loopback: there is no prompt to defer and nothing to opt in to.
 */
function page_is_loopback(): boolean {
    return typeof location !== 'undefined' && is_loopback_url(location.href);
}

/** Current state, synchronously. Reads the last permission answer, never waits. */
export function local_access_state(): LocalAccessState {
    if (page_is_loopback()) return 'open';
    return decide_local_access(opted_in, permission);
}

/** True when a loopback request may go out right now. */
export function is_local_access_open(): boolean {
    return local_access_state() === 'open';
}

/** The last permission answer. `unknown` until `refresh_local_permission` runs. */
export function local_network_permission(): LocalNetworkPermission {
    return permission;
}

/**
 * Ask the browser where the permission stands. **Never prompts** — `query` is a
 * read — so this is safe to call at boot, and it is what lets a user who has
 * already granted the permission keep today's behaviour exactly.
 *
 * Resolves to `unknown` on every browser that does not know the name (Firefox,
 * Safari, pre-142 Chrome), which the decision treats as "not granted". The cost
 * of that conservatism is one CONNECT press on a browser that would never have
 * prompted; the cost of guessing the other way is the prompt we are removing.
 */
export function refresh_local_permission(): Promise<LocalNetworkPermission> {
    if (permission_read) return permission_read;
    permission_read = (async (): Promise<LocalNetworkPermission> => {
        try {
            if (typeof navigator === 'undefined' || navigator.permissions?.query === undefined) return 'unknown';
            // The name is not in TypeScript's PermissionName union — it is a
            // Chrome-only addition — and an unknown name REJECTS rather than
            // returning a state, which is exactly how we detect support.
            const status = await navigator.permissions.query({
                name: PERMISSION_NAME as PermissionName,
            });
            apply_permission(status.state as LocalNetworkPermission);
            // Chrome fires this when the user flips the switch in site settings,
            // so a recovery from `denied` needs no reload.
            status.onchange = () => apply_permission(status.state as LocalNetworkPermission);
            return permission;
        } catch {
            return 'unknown';
        } finally {
            // Cleared so a later call re-asks: `query` is cheap, and a stale
            // `unknown` from a browser that has since been upgraded is worse.
            permission_read = null;
        }
    })();
    return permission_read;
}

function apply_permission(next: LocalNetworkPermission): void {
    if (next !== 'granted' && next !== 'denied' && next !== 'prompt') return;
    if (next === permission) return;
    permission = next;
    notify();
}

/**
 * Resolve the permission read, then answer "may we go".
 *
 * A boot path has to await this rather than reading `is_local_access_open()`
 * directly, or someone who granted the permission long
 * ago would be told the gate is shut for the first few hundred milliseconds of
 * every session and never reconnect.
 */
export async function local_access_ready(): Promise<boolean> {
    await refresh_local_permission();
    return is_local_access_open();
}

// ── the opt-in ───────────────────────────────────────────────────────────────

/**
 * The user asked for local tools on this device.
 *
 * Called from a real user action, such as a CONNECT press. It is what makes the very
 * next loopback request legitimate, and therefore what makes Chrome's prompt
 * appear at a moment the user can connect to something they just did.
 *
 * Idempotent, and it notifies even when the stored flag was already set: a
 * second press on a `prompt`-state permission is the user retrying a modal they
 * dismissed, and every service should re-attempt.
 */
export function unlock_local_access(): void {
    if (!opted_in) {
        opted_in = true;
        try {
            if (typeof localStorage !== 'undefined') localStorage.setItem(STORAGE_KEY, 'on');
        } catch {
            // Unwritable store — the opt-in still holds for this session.
        }
    }
    void refresh_local_permission();
    notify();
}

/** Forget the opt-in: the off switch of any control that offers one. */
export function relock_local_access(): void {
    opted_in = false;
    try {
        if (typeof localStorage !== 'undefined') localStorage.removeItem(STORAGE_KEY);
    } catch {
        // Nothing to clean up if the store never took it.
    }
    notify();
}

/** Fires whenever the gate opens, shuts, or learns a new permission answer. */
export function on_local_access_change(fn: (state: LocalAccessState) => void): () => void {
    listeners.add(fn);
    return () => listeners.delete(fn);
}

// ── the chokepoint ───────────────────────────────────────────────────────────

/**
 * `fetch`, with the gate in front of it.
 *
 * **Every loopback call site in the app goes through this.** A non-loopback URL
 * passes straight through untouched, which is what makes it safe to swap in
 * mechanically at sites that build their URL from an injected base (the HTTP
 * transport's remote origin, a dev server's own port).
 *
 * A shut gate REJECTS rather than resolving to a synthetic error response, so
 * the dozens of existing `catch { … unreachable }` blocks degrade to their
 * bridge-down state with no edit. The tag on the error is what lets the few
 * surfaces that care print "not connected" instead of "not running".
 */
export function local_fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    if (!is_loopback_url(url_of(input))) return fetch(input, init);
    const state = local_access_state();
    if (state !== 'open') return Promise.reject(new LocalAccessBlocked(state));
    return fetch(input, init);
}

/**
 * `fetch`'s first argument in every form it takes.
 *
 * Widened from a bare string so this drops straight into the `fetch_impl`
 * defaults the services share (`(...args: Parameters<typeof fetch>) => …`)
 * without a cast at each site — a cast per call site is exactly how one of
 * them ends up silently un-gated.
 */
function url_of(input: RequestInfo | URL): string {
    if (typeof input === 'string') return input;
    if (typeof URL !== 'undefined' && input instanceof URL) return input.href;
    return (input as Request).url;
}

/**
 * `new EventSource`, with the same gate.
 *
 * Returns `null` while shut — the ingestor already treats a null stream as "no
 * SSE, fall back to the poll", so the closed state needs no new branch there.
 */
export function local_event_source(url: string): EventSource | null {
    if (typeof EventSource === 'undefined') return null;
    if (is_loopback_url(url) && !is_local_access_open()) return null;
    return new EventSource(url);
}

/**
 * DEV TOOL: inspect and drive the gate from a console. A page or a host exposes
 * this object where it likes.
 *
 * `connect()` is the same call the on-screen controls make, so the whole path
 * can be exercised without hunting for a button.
 */
export const LOCAL_NETWORK_DEV = {
    get state(): LocalAccessState {
        return local_access_state();
    },
    get permission(): LocalNetworkPermission {
        return permission;
    },
    connect: unlock_local_access,
    disconnect: relock_local_access,
    refresh: refresh_local_permission,
};
