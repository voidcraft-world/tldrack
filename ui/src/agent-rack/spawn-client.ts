/**
 * The ONE owner of `POST /spawn` — `+ NEW` with a launch profile — and of the
 * order every rack owner must run it in.
 *
 * More than one surface can own a `+ NEW`: `rack-http-transport.ts`, and a host
 * application's own payload owner.
 * They differ in where focus and the pending row live, and in nothing else, so
 * the SEQUENCE lives here once and each owner hands in its own state as hooks.
 *
 * ── Why the order is the whole job ──────────────────────────────────────────
 *
 * Focus must NOT move until the bridge answers 201. The rack page's iframe URL
 * recomputes whenever `focused` changes, and a bare `?arg=<name>` is ttyd's
 * attach-or-CREATE: point the pane at the new name first and ttyd builds a bare
 * login shell under it before `/spawn` lands — which then answers 409 for a
 * session it never made, and the operator gets a shell instead of their agent. So:
 *
 *   1. pick a name nothing has claimed (`next_session_name` over every name the
 *      bridge knows, dead rows included) and raise the optimistic row — this
 *      reserves the name and shows the click landed — with focus untouched;
 *   2. await `/spawn`;
 *   3. 201 → now focus, emit, navigate; the pending row stays until a snapshot
 *      lists the session, or a stale in-flight poll would yank focus away;
 *   4. 409 → somebody took the name between the pick and the spawn: once more,
 *      with that name excluded;
 *   5. 404 → the bridge predates `/spawn`: the legacy path, unchanged;
 *   6. anything else → drop the pending row and say why.
 *
 * DELIBERATELY FREE of the DOM, so it runs under bare `node --test`, the same
 * split `worktree-client.ts` uses.
 */

import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import { next_session_name } from '../engine/terminal/multi-terminal-layout.ts';
import { AUTO_PROFILE_ID, type LaunchPick, type ProfileInfo } from '../engine/terminal/terminal-status-logic.ts';
import type { BaseUrl, FetchImpl } from './worktree-client.ts';

/** tmux `new-session -d` returns as soon as the pane forks, so this is a
 *  latency budget for loopback (or a remote origin), not for the agent's boot. */
export const SPAWN_TIMEOUT_MS = 8000;

export type SpawnResult =
    | 'ok'
    /** 409 — the name is taken. */
    | 'taken'
    /** 400 — the bridge refused the name or the profile id. */
    | 'invalid'
    /** 503 — the profile's CLI is not installed where the bridge looks. */
    | 'cli_missing'
    /** 403 — Origin guard. */
    | 'forbidden'
    /** 404 — the bridge predates `/spawn`; restart it. */
    | 'unsupported'
    | 'error'
    /** No answer at all (transport failure / timeout). */
    | 'unreachable';

/** Map a `/spawn` status. 404 keeps its one meaning on this bridge — "this bridge
 *  predates the route" — exactly as `classify_rename_status` does. */
export function classify_spawn_status(status: number): SpawnResult {
    if (status === 201 || status === 200) return 'ok';
    if (status === 409) return 'taken';
    if (status === 400) return 'invalid';
    if (status === 503) return 'cli_missing';
    if (status === 403) return 'forbidden';
    if (status === 404) return 'unsupported';
    return 'error';
}

export interface SpawnOutcome {
    result: SpawnResult;
    /** The bridge's own `error` line, when it sent one. */
    error?: string;
}

export async function spawn_session(
    base_url: BaseUrl,
    request: { name: string; profile: string },
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<SpawnOutcome> {
    try {
        const response = await fetch_impl(base_url('/spawn'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(request),
            signal: AbortSignal.timeout(SPAWN_TIMEOUT_MS),
        });
        const result = classify_spawn_status(response.status);
        if (result === 'ok') return { result };
        let error: string | undefined;
        try {
            const body: unknown = await response.json();
            const raw = body && typeof body === 'object' ? (body as Record<string, unknown>).error : undefined;
            if (typeof raw === 'string' && raw.trim() !== '') error = raw.trim().slice(0, 200);
        } catch {
            // Not JSON — the status alone says enough.
        }
        return error === undefined ? { result } : { result, error };
    } catch {
        return { result: 'unreachable' };
    }
}

/** The one line the rack shows for a launch that did not happen. */
export function spawn_failure_message(label: string, outcome: SpawnOutcome): string {
    switch (outcome.result) {
        case 'cli_missing':
            return `${label}: CLI not found on this machine`;
        case 'forbidden':
            return `${label}: the bridge refused this origin`;
        case 'unreachable':
            return `${label}: the bridge did not answer`;
        case 'taken':
            return `${label}: no free session name`;
        default:
            return outcome.error ? `${label}: ${outcome.error}` : `${label}: launch failed`;
    }
}

/** What a rack owner hands `launch_new`: its own state, as callbacks. */
export interface LaunchNewHooks {
    base_url: BaseUrl;
    /** Every name the bridge knows, dead rows included. */
    known_sessions: () => readonly string[];
    /** The bridge's profiles, or undefined on a pre-`/spawn` bridge. */
    profiles: () => readonly ProfileInfo[] | undefined;
    /** The bridge's `launch_pick`, or undefined on a bridge that cannot
     *  resolve `"auto"` — then an AUTO request falls back like an unknown id. */
    launch_pick?: () => LaunchPick | undefined;
    /** Raise (or with null, drop) the optimistic row, and emit. MUST NOT move
     *  focus — that is the whole reason this sequence exists. */
    set_pending: (name: string | null, profile: string | null) => void;
    /** 201: focus the session, emit, then navigate the pane — in that order. */
    on_spawned: (name: string) => void;
    /** The pre-`/spawn` `+ NEW`, unchanged. */
    legacy_new: () => void;
    /** A launch that did not happen, as one line. */
    on_failure: (message: string) => void;
    fetch_impl?: FetchImpl;
}

/** What `launch_new` sends, the profile its optimistic row wears meanwhile,
 *  and the label a failure names. */
interface LaunchChoice {
    request: string;
    shown: string;
    label: string;
}

/** AUTO when asked for and the bridge can pick, else `profile` when it is
 *  launchable, else the default, else the first launchable one. */
function choose_launch(
    profile: string | null,
    profiles: readonly ProfileInfo[],
    pick: LaunchPick | undefined,
): LaunchChoice | null {
    if (profile === AUTO_PROFILE_ID && pick?.id) {
        // The bridge re-picks on arrival; the current pick is only the row's
        // best guess until the snapshot names the real profile.
        return { request: AUTO_PROFILE_ID, shown: pick.id, label: 'AUTO' };
    }
    const chosen =
        profiles.find((p) => p.id === profile && p.cli_found) ??
        profiles.find((p) => p.default && p.cli_found) ??
        profiles.find((p) => p.cli_found);
    return chosen ? { request: chosen.id, shown: chosen.id, label: chosen.label } : null;
}

/**
 * `+ NEW` for `profile`. Falls back to `legacy_new` only when the bridge sent
 * no profiles or `/spawn` 404s. A null or unknown profile id resolves to the
 * BRIDGE'S DEFAULT profile, never to a bare shell: the rack offered a launch,
 * and silently downgrading it to a shell is the "which profile will this
 * open?" ambiguity this feature removes. `"auto"` goes to the bridge as is —
 * it resolves the freest account at click time (`tldrack/launch_pick.py`).
 */
export async function launch_new(profile: string | null, hooks: LaunchNewHooks): Promise<void> {
    const profiles = hooks.profiles();
    if (profiles === undefined || profiles.length === 0) {
        hooks.legacy_new();
        return;
    }
    const chosen = choose_launch(profile, profiles, hooks.launch_pick?.());
    if (!chosen) {
        hooks.on_failure('no launch profile has its CLI installed');
        return;
    }

    let name = next_session_name(hooks.known_sessions());
    hooks.set_pending(name, chosen.shown);
    let outcome = await spawn_session(hooks.base_url, { name, profile: chosen.request }, hooks.fetch_impl);

    if (outcome.result === 'taken') {
        name = next_session_name([...hooks.known_sessions(), name]);
        hooks.set_pending(name, chosen.shown);
        outcome = await spawn_session(hooks.base_url, { name, profile: chosen.request }, hooks.fetch_impl);
    }

    if (outcome.result === 'ok') {
        hooks.on_spawned(name);
        return;
    }
    hooks.set_pending(null, null);
    if (outcome.result === 'unsupported') {
        hooks.legacy_new();
        return;
    }
    // AUTO's refusal names no single CLI, so the bridge's own line says it best.
    const auto_reason = chosen.request === AUTO_PROFILE_ID && outcome.result === 'cli_missing' ? outcome.error : undefined;
    hooks.on_failure(auto_reason ? `AUTO: ${auto_reason}` : spawn_failure_message(chosen.label, outcome));
}

/** How long a launch notice stays in the rack head. Long enough to read a
 *  sentence after glancing back from the terminal; short enough that a stale
 *  failure does not sit there describing a launch the operator has moved past. */
export const LAUNCH_NOTICE_MS = 10_000;

export interface LaunchNotice {
    show: (text: string) => void;
    clear: () => void;
    text: () => string | null;
}

/** One expiring line per rack owner. `on_change` re-emits the payload. */
export function create_launch_notice(on_change: () => void, ttl_ms: number = LAUNCH_NOTICE_MS): LaunchNotice {
    let text: string | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const stop = (): void => {
        if (timer !== null) clearTimeout(timer);
        timer = null;
    };
    return {
        show: (next) => {
            stop();
            text = next;
            timer = setTimeout(() => {
                timer = null;
                text = null;
                on_change();
            }, ttl_ms);
            on_change();
        },
        clear: () => {
            stop();
            if (text === null) return;
            text = null;
            on_change();
        },
        text: () => text,
    };
}
