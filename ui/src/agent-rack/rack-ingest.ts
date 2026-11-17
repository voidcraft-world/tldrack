/**
 * The rack ingestors' shared middle.
 *
 * `rack-http-transport.ts` and a host application's own payload owner each
 * own a `create_fleet_ingestor`, and each keeps its own focus,
 * its own `pending_new` and its own idea of where the bridge is. What they must
 * never disagree about is what an optimistic row looks like and when a pending
 * `+ NEW` name stops being pending — those live here, once.
 */

import { is_live, type SessionStatus } from '../engine/terminal/terminal-status-logic.ts';
import { creating_sessions, type WorktreeState } from './worktree-client.ts';

/** A believable row for a session that exists only because we just asked for
 *  it. `unknown` + `live` is exactly what the bridge will report for it on the
 *  next snapshot anyway.
 *
 *  `profile` rides along for a `/spawn` launch, which is what puts the row in
 *  `attach_only` (`rack-view.ts::attach_only_with_profiled`) BEFORE the bridge
 *  reports it — so the pane's first navigation already uses `attach:`, and the
 *  snapshot catching up does not change the URL and reload the pane. */
function optimistic_row(session: string, profile: string | null = null): SessionStatus {
    const row: SessionStatus = {
        session,
        state: 'unknown',
        age_seconds: null,
        message: null,
        live: true,
        title: null,
        attached: false,
    };
    if (profile) row.profile = profile;
    return row;
}

/**
 * Fold the rows the bridge has not caught up with into a snapshot — both racks'
 * `augment_sessions`.
 *
 * Two kinds of row: the optimistic `+ NEW` one, kept until the bridge reports
 * it (or the list visibly loses the row the operator just created and gains it back
 * a poll later), and one per session a worktree create is still building —
 * owned by `worktree-client.ts::creating_sessions`, see the note there.
 *
 * Returns the next `pending_new` rather than taking a reference to it: clearing
 * it is part of the same decision, and the caller stores it back onto its own
 * module/closure state.
 */
export function augment_rack_sessions(
    sessions: SessionStatus[],
    pending_new: string | null,
    worktree_state: WorktreeState | null,
    pending_profile: string | null = null,
): { sessions: SessionStatus[]; pending_new: string | null } {
    const live = sessions.filter(is_live).map((s) => s.session);
    // The bridge has caught up — the optimistic row is now the real one.
    const pending = pending_new && live.includes(pending_new) ? null : pending_new;
    let next = pending && !sessions.some((s) => s.session === pending)
        ? [...sessions, optimistic_row(pending, pending_profile)]
        : sessions;
    for (const session of creating_sessions(worktree_state)) {
        if (next.some((s) => s.session === session)) continue;
        if (next === sessions) next = [...sessions];
        next.push(optimistic_row(session));
    }
    return { sessions: next, pending_new: pending };
}
