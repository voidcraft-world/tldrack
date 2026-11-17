/**
 * Renames a rack surface has sent whose effect the rack has not settled on yet.
 *
 * ── WHY FOCUS NEEDS THIS ────────────────────────────────────────────────────
 *
 * The bridge pushes its post-rename snapshot over SSE BEFORE it answers the
 * POST (`tldrack/main.py::_handle_rename_session` calls
 * `notify_snapshot()`, then `_send_json`). So every rack learns that the
 * focused session's old name is gone one round trip before it learns why, and
 * `adopt_focus` reads a vanished focus as "that session died" and falls to the
 * first live row. Untracked, renaming the agent you are in moves the live pane
 * to the top agent every time, and by the time the 200 lands `adopt_rename`
 * finds focus no longer on the old name and has nothing to carry.
 *
 * The reverse race is real too: a poll that left before the rename can land
 * AFTER the answer, still listing the old name and not the new one. So an
 * ANSWERED rename stays tracked until a snapshot reports the new name live, or
 * `RENAME_SETTLE_SNAPSHOTS` snapshots pass without it (the renamed session died
 * straight after — nothing left to hold focus for).
 *
 * One tracker per focus owner, read through `adopt_focus`'s `renames`.
 */

/** Snapshots an answered rename survives without its new name showing up. */
export const RENAME_SETTLE_SNAPSHOTS = 3;

interface PendingRename {
    to: string;
    /** The POST answered ok; now only waiting for a snapshot to agree. */
    answered: boolean;
    /** Snapshots seen since the answer that did not list `to`. */
    misses: number;
}

/** What `adopt_focus` reads. */
export interface RenameFollower {
    /**
     * Where focus on `current` belongs, given that `current` is NOT in `live`,
     * or null when no tracked rename explains its absence.
     */
    follow(current: string, live: readonly string[]): string | null;
}

export interface RenameTracker extends RenameFollower {
    /** Before the POST goes out. */
    begin(from: string, to: string): void;
    /** The POST answered. A refusal drops it at once — `from` still exists. */
    answer(from: string, ok: boolean): void;
    /** Every snapshot, AFTER focus was adopted against it. */
    observe(live: readonly string[]): void;
    /** Tracked renames, for tests and diagnostics. */
    size(): number;
}

export function create_rename_tracker(): RenameTracker {
    const pending = new Map<string, PendingRename>();

    return {
        begin(from, to) {
            if (!from || !to || from === to) return;
            pending.set(from, { to, answered: false, misses: 0 });
        },

        answer(from, ok) {
            const entry = pending.get(from);
            if (!entry) return;
            if (!ok) pending.delete(from);
            else entry.answered = true;
        },

        observe(live) {
            for (const [from, entry] of pending) {
                // An unanswered rename is bounded by the POST's own timeout.
                if (!entry.answered) continue;
                if (live.includes(entry.to)) {
                    pending.delete(from);
                    continue;
                }
                entry.misses += 1;
                if (entry.misses >= RENAME_SETTLE_SNAPSHOTS) pending.delete(from);
            }
        },

        follow(current, live) {
            // Focus still on the OLD name: move with it once the new one is
            // live, and hold until then rather than guess.
            const outgoing = pending.get(current);
            if (outgoing) return live.includes(outgoing.to) ? outgoing.to : current;
            // Focus already on the NEW name and a stale snapshot does not list
            // it yet: hold.
            for (const entry of pending.values()) {
                if (entry.to === current) return current;
            }
            return null;
        },

        size: () => pending.size,
    };
}
