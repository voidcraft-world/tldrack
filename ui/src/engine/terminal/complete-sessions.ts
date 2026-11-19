/**
 * The COMPLETE band: sessions whose work is finished AND has nowhere left to go.
 *
 * WHY A BAND. Sorted by urgency alone, a finished agent lands among the working
 * ones, and higher than most of them (`done` and `free` outrank `busy` on the
 * urgency ladder, because a turn-over row is a prompt waiting). For a run whose
 * last turn closed the loop, that puts a session with nothing left to do near
 * the top of the rack. The rule since 2026-10-31: finished work gets a section of its own,
 * and a session that finished but still has a worktree is not finished. So these
 * rows leave their project cohort for ONE trailing band, and they are the first rows to
 * go behind `+N MORE` (`multi-terminal-layout.ts` `COMPLETE_RANK`).
 *
 * THE RULE: all five must hold.
 *
 *  1. **The turn is over and Laya judged it DONE.** That is exactly where ✅/☑️
 *     draws (`work_done_shown`), so the band and the mark always agree. The
 *     reply mark alone never certifies completion, so with no Laya verdict a
 *     session never lands here. Planned work still open (⚠️) or a turn an API
 *     error cut off (🪫🔋🛑) is not done.
 *  2. **The operator has SEEN it** (☑️, state `free`), never an unread ✅
 *     (`done`). HIDE-FIRST, AND ONLY AFTER A LOOK: Laya's ✅ alone never moves a
 *     row out of sight. An unread ✅ stays in its project at its urgency rung
 *     until it is opened once, and so does an answer nobody has read (📖).
 *     Why: people miss silent defects on exactly the
 *     outputs they trust most (in one 2026 study, 79% of the missed defects
 *     had been rated high-confidence — Dissanayake et al.), and an agent's own
 *     account of its work is wrong often enough (22.58% of misalignment
 *     episodes over 20,574 sessions were inaccurate self-reports — Tang et
 *     al.) that a classifier reading that account cannot be the last reader.
 *  3. **No island.** A session standing in a non-main worktree still owes
 *     something (a PR, a merge, a cleanup of the tree), whatever its last turn
 *     said. Once the worktree is removed, the island goes, and so does the reason.
 *  4. **Alive.** A gone session is a different fact (`— session gone —`).
 *  5. **Not a service row.** Machinery is never finished (`service-sessions.ts`).
 *
 * A FAMILY is complete only when every member is: a finished parent with a
 * working child stays with the working (`build_rack_rows`).
 *
 * Dependency-free at runtime, so `multi-terminal-layout.ts` still loads under
 * bare `node --test` (the flags module has type-only imports).
 */
import { work_done_shown } from '../../config/terminal-status-flags.ts';
import { is_service_session } from './service-sessions.ts';
// Type-only, so the cycle with the layout module is erased at runtime.
import type { RackSession } from './multi-terminal-layout.ts';

/** The band's header. Uppercase mono, like every head on these pages. */
export const COMPLETE_COHORT_LABEL = 'COMPLETE';

/** Does this session belong in the COMPLETE band? The five rules above. */
export function is_complete_session(entry: RackSession): boolean {
    if (!entry.live || is_service_session(entry.session)) return false;
    if (entry.island || entry.open_work || entry.stop_failure) return false;
    // An unread answer is not seen, whatever the seen-bit says (rule 2): a host that never posts the read keeps it here.
    if (entry.work_done?.answer) return false;
    return work_done_shown(entry.state, entry.work_done ? true : null, false) && entry.state === 'free';
}
