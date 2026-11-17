// The pure half of the satellite-window watchdog (useCompanionLifecycle.ts).
// Zero imports, no clock, no DOM — the caller passes `now` and the window's
// observable state, so every branch below is unit-testable.
//
// WHY THIS EXISTS AS ITS OWN DECISION
//
// The watchdog's job is to clean up a satellite whose main window CRASHED. A
// bare test, "no heartbeat for 10 seconds", conflates three very different
// causes of silence:
//
//   1. the main window died           → close, correct
//   2. the main window is THROTTLED   → close, WRONG
//   3. the main window is FROZEN or discarded by the browser → close, WRONG
//
// (2) is not an edge case, it is the normal state of the machine while the
// operator works in the satellite. A hidden page's `setInterval` is throttled
// hard: Chrome drops timers in a page that has been hidden and quiet for ~5
// minutes to about ONE WAKE-UP PER MINUTE, and on macOS a window fully covered
// by another (by this very satellite) already counts as hidden. A 3s heartbeat
// against a 10s threshold cannot survive that, so a rack on the bare timer
// closes itself mid-keystroke after a few minutes' work.
//
// So silence is a question and not a verdict, answered two ways:
//
//   • PROBE. `MAIN_PING` is an EVENT, and event dispatch is not on the timer
//     budget that silenced the main in the first place: a throttled-but-alive
//     main answers immediately, which distinguishes (2) from (1) outright.
//   • NEVER YANK A WINDOW IN USE. A frozen or discarded main (3) cannot answer
//     anything, so probing alone cannot separate it from a crash — but the user
//     staring at this window can. A satellite the user can SEE is never closed
//     on a guess; it reports `orphaned` and keeps probing, so a main that comes
//     back (a discarded tab reloads the moment it is clicked) simply reconnects.
//     A hidden satellite has nothing to protect and is closed as before.
//
// The graceful paths are untouched and remain the primary mechanism: an ordinary
// quit or sign-out sends MAIN_SHUTDOWN and closes satellites at once. This is
// only the crash fallback, and a fallback must not be trigger-happy.

/** No beat for this long → start asking. Three missed 3s beats plus slack. */
export const SILENCE_THRESHOLD_MS = 10000;

/**
 * Unanswered `MAIN_PING`s before the main is presumed dead.
 *
 * Each costs one watchdog tick (~3s), so the full budget from the last beat is
 * ~10s of silence plus ~12s of asking. Deliberately generous: the cost of
 * waiting is a stale window for another few seconds, and the cost of being
 * wrong is destroying whatever the user was typing into it.
 */
export const REQUIRED_UNANSWERED_PROBES = 3;

export type LifecycleAction =
    /** The main is beating. Nothing to do. */
    | 'idle'
    /** Silent — send a `MAIN_PING` and wait. Also the recovery path out of `orphaned`. */
    | 'probe'
    /** Presumed dead, but the user can see this window: report it, keep probing, do NOT close. */
    | 'orphaned'
    /** Presumed dead and nobody is looking. Clean up. */
    | 'close';

export interface LifecycleInput {
    now: number;
    /** Timestamp of the last MAIN_ALIVE / MAIN_READY / ping answer. */
    last_main_seen: number;
    /** Consecutive `MAIN_PING`s sent with no answer since the last beat. */
    unanswered_probes: number;
    /**
     * The user can see this window right now — visible OR focused.
     *
     * Both halves are load-bearing. A satellite parked on a second monitor has
     * `hidden === false` forever and is never focused, yet closing it is exactly
     * as destructive as closing the focused one; and a focused window can
     * briefly report hidden across a display/space switch.
     */
    in_use: boolean;
}

export function decide_lifecycle_action(input: LifecycleInput): LifecycleAction {
    if (input.now - input.last_main_seen <= SILENCE_THRESHOLD_MS) {
        return 'idle';
    }
    if (input.unanswered_probes < REQUIRED_UNANSWERED_PROBES) {
        return 'probe';
    }
    return input.in_use ? 'orphaned' : 'close';
}
