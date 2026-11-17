/** The rack samples LAST activity, not tool lifecycle events. Never invent completions,
 * durations, parallel calls or hook outcomes from it. Counts are observations since opening
 * this view; a repeated snapshot is not another call. TLDRack's renderer owns drawing (`activity-map-renderer.ts`). */
import type { RackSession } from '../engine/terminal/multi-terminal-layout.ts';
import { is_service_session } from '../engine/terminal/service-sessions.ts';
import { classify, clean, setupFrom } from './lot-map/map.ts';
import type { BoardSize } from './activity-map-layout.ts';
import { draw_activity_map } from './activity-map-renderer.ts';
import type { Board, StationId } from './lot-map/types.ts';

const SETUP = setupFrom({ coreName: 'AGENT', theme: 'dark', memoryServers: 'memory', colors: 'accent=#93ffd4' });
export const MAP_SAMPLE_LIMIT = 40;

export interface ActivityMap {
    session: string;
    name: string;
    state: RackSession['state'];
    active: StationId | null;
    signature: string;
    board: Board;
    label: string;
}

export function map_station(row: RackSession): StationId | null {
    if (!row.live || is_service_session(row.session) || row.state === 'unknown') return null;
    if (row.state === 'attention' || row.state === 'done') return 'you';
    if (row.state !== 'busy') return null;
    const activity = row.activity;
    if (!activity) return 'model';
    const tool = activity.tool ?? '';
    if (/browser|playwright|cua/i.test(tool)) return 'browser';
    if (/spawn_agent|send_message|wait_agent|followup_task|^Task$/.test(tool)) return 'agents';
    if (/request_user_input/.test(tool)) return 'you';
    if (/apply_patch/.test(tool)) return 'files';
    if (activity.zone === 'codebase') return 'files';
    if (activity.zone === 'web' || activity.zone === 'aws') return 'web';
    return classify(tool, {}, SETUP).station;
}

export function observe_activity(previous: ActivityMap | null, row: RackSession, now: number): ActivityMap {
    const prior = previous?.session === row.session ? previous : null;
    const station = map_station(row);
    const label = !row.live ? 'Session ended'
        : row.state === 'attention' ? 'Waiting for you'
        : row.state === 'done' ? 'Reply ready'
        : row.state === 'free' ? 'Idle'
        : row.state === 'background' ? 'Background work'
        : row.state === 'unknown' ? 'Activity unavailable'
        : clean(row.activity?.tool ?? 'Working', 80);
    const signature = JSON.stringify([row.state, row.live, row.activity ?? null]);
    const board: Board = {
        entries: prior?.board.entries ?? [],
        counts: prior?.board.counts ?? {},
        ...(row.model_label ? { model: clean(row.model_label, 60) } : {}),
        ...(typeof row.context_left === 'number' && Number.isFinite(row.context_left)
            ? { context: 100 - Math.max(0, Math.min(100, row.context_left)) } : {}),
    };
    if (station && prior?.signature !== signature) {
        board.entries = [...board.entries, {
            id: `${now}:${board.entries.length}`, station, label, from: 'core' as const,
            startedAt: now, status: 'ok' as const,
        }].slice(-MAP_SAMPLE_LIMIT);
        board.counts = { ...board.counts, [station]: (board.counts[station] ?? 0) + 1 };
    }
    return { session: row.session, name: clean(row.auto_name || row.session, 80), state: row.state, active: station, signature, board, label };
}

/** Render as an image document, never executable inline markup. SMIL is removed while stale,
 * idle or reduced-motion; no background animation timer is needed in either surface. */
export function activity_map_image(map: ActivityMap, _now: number, animate: boolean, size?: BoardSize): string {
    const svg = draw_activity_map(map, animate, size);
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
