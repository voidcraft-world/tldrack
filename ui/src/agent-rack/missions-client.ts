/**
 * The rack's side of missions: agent sessions assigned to one task, sharing notes. Owner of what
 * a mission is and how an agent hears about one: `bridge/tldrack/missions.py`.
 *
 * A poll, not a snapshot key: a mission changes when a person or an agent acts, so the rack reads
 * `GET /missions` on the plugin seam, and a bridge that predates the route answers 404, which
 * grows no section at all.
 *
 * Free of the DOM, like `devices-client.ts`: every rack payload owner loads it.
 */
import { mission_schema, type Mission, type MissionMember, type MissionNote } from '@tldrack/schemas/missions.ts';
import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import { create_plugin_poller, fetch_plugin_state, type PluginPoller } from './plugin-poller.ts';
import type { BaseUrl, FetchImpl, PollerIoDeps } from './worktree-client.ts';

export type { Mission, MissionMember, MissionNote };

export const MISSIONS_POLL_MS = 4000;
const WRITE_TIMEOUT_MS = 5000;

export type MissionsState = Mission[];

/** `/missions`' body, or null when it is not a missions document. A bad row is dropped alone. */
export function parse_missions(body: unknown): MissionsState | null {
    const rows = (body as { missions?: unknown } | null)?.missions;
    if (!Array.isArray(rows)) return null;
    return rows.flatMap((row) => {
        const parsed = mission_schema.safeParse(row);
        return parsed.success ? [parsed.data] : [];
    });
}

export function missions_signature(state: MissionsState | null): string {
    return state ? JSON.stringify(state) : '';
}

export function fetch_missions(base_url: BaseUrl, fetch_impl?: FetchImpl): Promise<MissionsState | null> {
    return fetch_plugin_state(base_url, '/missions', parse_missions, fetch_impl);
}

export interface MissionsPollerDeps extends PollerIoDeps {
    base_url: BaseUrl;
    on_change: () => void;
    wanted: () => boolean;
    poll_interval_ms?: number;
}

export type MissionsPoller = PluginPoller<MissionsState>;

export function create_missions_poller(deps: MissionsPollerDeps): MissionsPoller {
    return create_plugin_poller({
        ...deps,
        fetch: (do_fetch) => fetch_missions(deps.base_url, do_fetch),
        signature: missions_signature,
        poll_interval_ms: deps.poll_interval_ms ?? MISSIONS_POLL_MS,
    });
}

// ── the rack's reads ────────────────────────────────────────────────────────

/** The mission a session is on. A session is on one at most. */
export function mission_for_session(missions: readonly Mission[] | undefined, session: string): Mission | undefined {
    return missions?.find((mission) => mission.members.some((member) => member.session === session));
}

/** Why a member reading `hears: false` is told nothing: `missions.py` § DELIVERED BY THE HOOK. */
export const CANNOT_HEAR =
    'Cannot hear this mission: its hooks discard the answer. Codex always does; a Claude account needs tldrack profiles --install-hooks';

/** A rack row's tooltip for a session on a mission, or null for one on none. */
export function mission_row_title(missions: readonly Mission[] | undefined, session: string | undefined): string | null {
    const mission = session ? mission_for_session(missions, session) : undefined;
    if (!mission) return null;
    const deaf = mission.members.some((member) => member.session === session && member.hears === false);
    return `On the mission: ${mission.title}${deaf ? `. ${CANNOT_HEAR}` : ''}`;
}

// ── writes ──────────────────────────────────────────────────────────────────

/** One write. It crosses the rack transport as a JSON string (`RackTransport.send` says why). */
export type MissionRequest =
    | { op: 'create'; title: string; sessions: string[] }
    | { op: 'assign'; session: string; mission: string | null }
    | { op: 'role'; session: string; role: string }
    | { op: 'note'; mission: string; text: string }
    | { op: 'close'; mission: string };

const OPS: readonly string[] = ['create', 'assign', 'role', 'note', 'close'];

/** Every hop re-validates what crossed a postMessage: null for anything that is not a request. */
export function parse_mission_request(value: unknown): MissionRequest | null {
    if (typeof value !== 'string') return null;
    try {
        const parsed: unknown = JSON.parse(value);
        const op = (parsed as { op?: unknown } | null)?.op;
        return typeof op === 'string' && OPS.includes(op) ? (parsed as MissionRequest) : null;
    } catch {
        return null;
    }
}

export interface MissionResult {
    ok: boolean;
    error?: string;
}

export async function post_mission(
    base_url: BaseUrl,
    request: MissionRequest,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): Promise<MissionResult> {
    try {
        const response = await fetch_impl(base_url('/missions'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(request),
            signal: AbortSignal.timeout(WRITE_TIMEOUT_MS),
        });
        if (response.ok) return { ok: true };
        // 404 has one meaning on this bridge: it predates the route.
        if (response.status === 404) return { ok: false, error: 'Restart the bridge to use missions' };
        const body = (await response.json().catch(() => null)) as { error?: unknown } | null;
        return { ok: false, error: typeof body?.error === 'string' ? body.error : 'Mission update failed' };
    } catch {
        return { ok: false, error: 'Bridge not reachable' };
    }
}
