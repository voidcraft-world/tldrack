/**
 * The menu a right-click on an office robot opens: what its rack row offers, in the
 * rack's own words.
 *
 * NOT A SECOND SET OF BUTTONS. The worktree entry IS the row's PR / CLEAN button
 * (`rack-view.ts::worktree_button_view` owns its label, its title and what a press
 * does), read off the same fields the rack relays (`agent-office-link.ts`), so the
 * two surfaces cannot disagree about whether a branch wants a PR or a clean-up.
 * Whatever writes arms first and fires on the second press, as it does on the row
 * (`engine/terminal/arm-logic.ts` owns the rule). Opening a PR on GitHub and showing
 * a session in the rack write nothing, so they take one press.
 *
 * Pure: the view supplies the arm and decides where a press goes.
 */
import type { OfficeAgent } from '../engine/terminal/agent-office-logic.ts';
import type { OfficeAction } from '../engine/terminal/agent-office-link.ts';
import { worktree_button_view, type WorktreeButtonRow } from '../engine/terminal/rack-view.ts';

/** An arm on the menu lasts as long as one on the rack's row. */
export const OFFICE_MENU_ARM_MS = 4000;

export type OfficeMenuItemId = 'select' | OfficeAction;

export interface OfficeMenuItem {
    id: OfficeMenuItemId;
    label: string;
    /** The second line: what a press does. */
    detail: string;
    /** `send` acts on one press, `arm` on the second, `open` opens `url`; `none` is drawn but dead. */
    press: 'send' | 'arm' | 'open' | 'none';
    url: string | null;
    tone: 'open' | 'error' | 'danger' | null;
    armed: boolean;
}

/** The arm's key: one session's one entry, so an arm never follows the menu to another robot. */
export function office_menu_key(session: string, id: OfficeMenuItemId): string {
    return `${id}\n${session}`;
}

/**
 * The entries for `agent`. `armed` is the entry whose arm is live on this agent, or
 * null. `usable` is false while the rack cannot act (stale, unreachable, detached):
 * every entry that would reach it is then dead, and only a link to GitHub still opens.
 */
export function office_menu(
    agent: OfficeAgent,
    worktree: WorktreeButtonRow | undefined,
    armed: OfficeMenuItemId | null,
    usable: boolean,
): OfficeMenuItem[] {
    const items: OfficeMenuItem[] = [];
    // The focused session is already in the live pane: the rack's row drops its ▶ too.
    if (!agent.focused) {
        items.push({ id: 'select', label: '▶ OPEN', detail: 'Show in the rack’s live terminal',
            press: usable ? 'send' : 'none', url: null, tone: null, armed: false });
    }
    if (worktree && agent.kind === 'agent') {
        const view = worktree_button_view(worktree, armed === 'pr' || armed === 'cleanup' ? armed : null);
        items.push({ id: view.kind, label: view.label, detail: view.title,
            press: view.press === 'open' || usable ? view.press : 'none',
            url: view.url, tone: view.tone, armed: armed === view.kind && view.press === 'arm' });
    }
    const closing = armed === 'kill';
    items.push({ id: 'kill', label: closing ? 'CLOSE?' : '✕ CLOSE',
        detail: closing ? `Click again: kills '${agent.session}'` : 'Kill this tmux session · asks twice',
        press: usable ? 'arm' : 'none', url: null, tone: 'danger', armed: closing });
    return items;
}
