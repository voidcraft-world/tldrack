import {
    ANSWER_READ_EMOJI, ANSWER_TO_READ_EMOJI, DEAD_SESSION_EMOJI, FRESH_SESSION_EMOJI, STOP_FAILURE_EMOJI, WORK_DONE_EMOJI,
    answer_read_shown, answer_to_read_shown, display_state_emoji, fresh_session_shown, stop_failure_phase, stop_failure_shown, work_done_shown,
} from '../../config/terminal-status-flags.ts';
import { SERVICE_SESSION_EMOJI, shows_service_mark } from './service-sessions.ts';
import type { RackRow } from './multi-terminal-layout.ts';

/** One precedence for the rack and 3D bots: dead, cut off, fresh, service, unread answer,
 *  completed work, then ordinary state. The office relay resolves this before projecting
 *  away the verdict metadata, so a done turn is not mistaken for completed work. */
export function session_status_emoji(
    row: Partial<Pick<RackRow, 'session' | 'state' | 'dim' | 'stop_failure' | 'fresh' | 'exited' | 'work_done'>>,
    now_s: number,
): string {
    const dead = row.dim === true;
    if (dead) return DEAD_SESSION_EMOJI;
    if (row.stop_failure && stop_failure_shown(row.state, row.stop_failure, dead)) {
        return STOP_FAILURE_EMOJI[stop_failure_phase(row.stop_failure, now_s)];
    }
    if (fresh_session_shown(row.state, row.fresh, dead)) return FRESH_SESSION_EMOJI;
    if (row.exited || shows_service_mark(row.session, row.state)) return SERVICE_SESSION_EMOJI;
    if (answer_to_read_shown(row.state, row.work_done, dead)) return ANSWER_TO_READ_EMOJI;
    if (answer_read_shown(row.state, row.work_done, dead)) return ANSWER_READ_EMOJI[row.state];
    if (work_done_shown(row.state, row.work_done ? true : null, dead)) return WORK_DONE_EMOJI[row.state];
    return display_state_emoji(row.state ?? 'unknown');
}

const STATUS_EMOJIS = new Set([
    ...(['attention', 'done', 'free', 'background', 'busy', 'unknown'] as const).map(display_state_emoji),
    DEAD_SESSION_EMOJI, FRESH_SESSION_EMOJI, SERVICE_SESSION_EMOJI, ANSWER_TO_READ_EMOJI,
    ...Object.values(STOP_FAILURE_EMOJI), ...Object.values(WORK_DONE_EMOJI), ...Object.values(ANSWER_READ_EMOJI),
]);

/** Only status glyphs, never arbitrary relayed label text. */
export function parse_status_emoji(value: unknown): string | undefined {
    return typeof value === 'string' && STATUS_EMOJIS.has(value) ? value : undefined;
}
