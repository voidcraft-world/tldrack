/**
 * The rack's drawing of a suggested next step: the row chip's words, the card's [Do it] state, and
 * the ONE dispatcher [Do it] presses through. Pure, so `node --test` covers it.
 *
 * [Do it] is not a second door to an action. It presses the row's own control handler — `select`
 * (▶), the ✕'s `on_kill_click`, the PR/CLEAN button's `on_worktree_click` — so the control's arm
 * (KILL?, PR?, CLEAN?) and its refusals run exactly as for a hand on the row. The first [Do it] on an
 * armed control ARMS it; only a second press fires. Nothing here runs on a timer, a payload or a
 * render: `press_next_step` is reached from a click handler and nowhere else. What a suggestion is: owned by
 * `bridge/tldrack/next_step.py` — see the note there.
 */
import { NEXT_STEP_ACTIONS, type NextStep, type NextStepAction } from '@tldrack/schemas/next-step.ts';
import type { WorktreeButtonView } from '../engine/terminal/rack-view.ts';

/** `→ Close · tests pass, branch merged`, or null for a `none` (its reason shows on the opened
 *  row only). */
export function next_step_chip(step: NextStep | undefined | null): string | null {
    if (!step || step.kind !== 'suggest' || !step.label) return null;
    return `→ ${step.label} · ${step.reason}`;
}

/** Is this a control the suggester's vocabulary names — the presses the rack reports? */
export function is_next_step_control(action: string): action is NextStepAction {
    return (NEXT_STEP_ACTIONS as readonly string[]).includes(action);
}

/** What the row's control looks like right now, as the card needs it. */
export interface RowControlState {
    frozen: boolean;
    /** The row's ✕ shows `KILL?`. */
    kill_armed: boolean;
    /** The row's PR/CLEAN button, or undefined when the row has none. */
    worktree?: WorktreeButtonView;
    /** That button is armed AS the kind it shows. */
    worktree_armed: boolean;
}

/** The card's [Do it]: its label mirrors the control's arm, so the second press reads `KILL?`. */
export interface CardPress {
    label: string;
    title: string;
    disabled: boolean;
    armed: boolean;
}

const WORKTREE_WORDS: Record<'pr' | 'cleanup', { button: string; does: string }> = {
    pr: { button: 'PR', does: 'commits, pushes and opens the pull request' },
    cleanup: { button: 'CLEAN', does: 'removes the worktree, its branch and this session' },
};

/** The [Do it] for a suggestion, or null when there is nothing to press (`none`). */
export function card_press(step: NextStep | undefined | null, row: RowControlState): CardPress | null {
    if (!step || step.kind !== 'suggest' || !step.action) return null;
    const ready = (title: string): CardPress => ({ label: 'DO IT', title, disabled: row.frozen, armed: false });
    switch (step.action) {
        case 'select':
            return ready('Open this session in the live terminal — the row’s ▶');
        case 'kill':
            return row.kill_armed
                ? { label: 'KILL?', title: 'Confirm: kills this session — the row’s ✕, pressed twice', disabled: row.frozen, armed: true }
                : ready('Press the row’s ✕ — it asks again (KILL?) before it kills');
        case 'pr':
        case 'cleanup': {
            const words = WORKTREE_WORDS[step.action];
            const view = row.worktree;
            if (!view || view.kind !== step.action || view.press !== 'arm') {
                return { label: 'DO IT', title: `This row has no ${words.button} to press right now`, disabled: true, armed: false };
            }
            return row.worktree_armed
                ? { label: view.label, title: view.title, disabled: row.frozen, armed: true }
                : ready(`Press the row’s ${words.button} — it asks again before it ${words.does}`);
        }
    }
}

/** The row's own control handlers, exactly what a hand on the row calls. */
export interface RowControls {
    select: () => void;
    kill: () => void;
    worktree: () => void;
}

/**
 * Press the suggested control. Returns the action pressed, or null when nothing was (a `none`, a
 * disabled press, a control the row no longer shows). Calls AT MOST ONE handler.
 */
export function press_next_step(
    step: NextStep | undefined | null,
    press: CardPress | null,
    controls: RowControls,
): NextStepAction | null {
    if (!step || step.kind !== 'suggest' || !step.action || !press || press.disabled) return null;
    if (step.action === 'select') controls.select();
    else if (step.action === 'kill') controls.kill();
    else controls.worktree();
    return step.action;
}

/** A shown suggestion's identity, for the once-per-hash `shown` report. */
export function shown_key(session: string, step: NextStep): string {
    return `${session}\u0000${step.hash}`;
}
