/**
 * Pure row/column logic for a RACK BOARD: a column readout of the rack that
 * answers "which agent needs me, and how long has it been waiting" without
 * opening a terminal. A host draws the board on a surface of its own; the rack
 * and the office take the state words from here (`state_label`).
 *
 * THE COUPLING THAT MATTERS: this takes `build_rack_rows` OUTPUT, not its
 * input. The board does not sort, group, or truncate: it renders the row list
 * the rack already built, in that order. So row N on the board is row N in the
 * rack by construction, not by two call sites agreeing to call the same sort.
 * Attention ordering, cohort grouping and the `+N MORE` rule are inherited for
 * free, and a change to any of them moves both surfaces together.
 *
 * Free of three and of the DOM, so `node --test` runs it directly (strip-only
 * mode: no enums, no parameter properties, no namespaces).
 */
import type { RackRow, RackState } from './multi-terminal-layout.ts';

/** Per-session detail the board renders in columns. Everything here already
 *  exists on the bridge's snapshot rows; most of it was surfaced nowhere. */
export interface FleetBoardSession {
    state: RackState;
    /** Short model label, already formatted by the caller. */
    model: string | null;
    /** `activity.zone`: which part of the machine the agent is working in. */
    zone: string | null;
    /**
     * Seconds since the bridge last heard from this session.
     *
     * For an `attention` row this IS the wait time, because the hook that
     * reports attention fires the moment the agent blocks — which is exactly
     * the number that makes this a triage tool instead of a status list.
     * "blocked" and "blocked for 11 minutes" are different emergencies.
     *
     * For a `busy` row it reads as staleness: a working agent fires hooks
     * steadily, so a large age means stuck or gone, not busy.
     */
    age_seconds: number | null;
    /** A tmux client is already attached (a native terminal, or a rack page). */
    attached: boolean;
    /** Why the session wants you — rendered on a continuation line. */
    message: string | null;
}

export interface FleetBoardInput {
    /** Straight from `build_rack_rows`. */
    rows: RackRow[];
    sessions: Map<string, FleetBoardSession>;
    /** Maximum LINES the board can draw, including continuations and overflow. */
    max_lines: number;
}

export type FleetBoardLineKind = 'cohort' | 'session' | 'detail' | 'overflow' | 'empty';

export interface FleetBoardLine {
    kind: FleetBoardLineKind;
    session?: string;
    /** Status colour key; absent on non-session lines. */
    state?: RackState;
    /** Rendered cell text, left to right. Empty for cohort/overflow/empty lines,
     *  which carry `text` instead. */
    cells: string[];
    text?: string;
    /** Detail/cohort lines render dimmed. */
    dim: boolean;
    /** A tmux client is already on this session — drawn as a trailing marker. */
    attached: boolean;
}

export interface FleetBoardView {
    lines: FleetBoardLine[];
    /** Live sessions the board is showing. */
    shown: number;
    /** Live sessions in the rack overall. */
    total: number;
    /** How many needed attention, across the WHOLE rack — including any the
     *  line budget pushed off the board. */
    blocked: number;
    /** Banner above the rows — bridge-down copy, and the command that fixes it.
     *  Clicking the board while this is set copies `notice.copy`. */
    /**
     * The whole-surface message. `action` says what a click on it DOES —
     * `copy` puts `copy` on the clipboard (the bridge start command), `connect`
     * asks for local network access. Optional, and `copy` when absent.
     */
    notice?: { text: string; copy: string; action?: 'copy' | 'connect' } | null;
}

/**
 * The board's state when the bridge is not answering.
 *
 * A machine with no bridge running otherwise shows empty instruments and no
 * clue why. The fix has to live on the surface the operator is already looking at.
 */
export function bridge_down_view(start_command: string): FleetBoardView {
    return {
        lines: [
            {
                kind: 'empty',
                cells: [],
                text: 'BRIDGE NOT RUNNING — CLICK TO COPY THE START COMMAND',
                dim: false,
                attached: false,
            },
            { kind: 'detail', cells: [], text: start_command, dim: true, attached: false },
        ],
        shown: 0,
        total: 0,
        blocked: 0,
        notice: { text: 'BRIDGE OFFLINE', copy: start_command },
    };
}

/**
 * The board's state when the page has not been allowed to reach loopback.
 *
 * A SEPARATE view from `bridge_down_view`, not a reworded one, because the two
 * are different facts with different fixes: "the bridge is not running" wants a
 * shell command, "the browser has not been asked yet" wants a click. Printing
 * the start command here would send the user to a terminal to fix a problem
 * that is not on the machine. `engine/terminal/local-network-gate.ts` owns why
 * the gate exists at all.
 */
export function local_access_view(denied: boolean): FleetBoardView {
    const head = denied ? 'LOCAL ACCESS BLOCKED BY THE BROWSER' : 'LOCAL TOOLS OFF — CLICK TO CONNECT';
    const detail = denied
        ? 'Chrome ▸ Site settings ▸ Additional permissions ▸ Local network access'
        : 'This wall reads a bridge on THIS machine; the browser asks first';
    return {
        lines: [
            { kind: 'empty', cells: [], text: head, dim: false, attached: false },
            { kind: 'detail', cells: [], text: detail, dim: true, attached: false },
        ],
        shown: 0,
        total: 0,
        blocked: 0,
        // A denied permission cannot be re-prompted from script, so the click
        // has nothing to do — the copy above is the only actionable thing left.
        notice: denied ? { text: 'ACCESS BLOCKED', copy: detail } : { text: 'NOT CONNECTED', copy: '', action: 'connect' },
    };
}

/** Column labels, in render order. */
export const FLEET_BOARD_COLUMNS = ['STATE', 'SESSION', 'MODEL', 'WORK', 'AGE'];

/** Human state words. Deliberately not the internal vocabulary: "attention" is
 *  what the hook calls it, "BLOCKED" is what it means to the person reading. */
const STATE_LABEL: Record<RackState, string> = {
    attention: 'BLOCKED',
    busy: 'WORKING',
    background: 'BACKGROUND',
    done: 'REPLY READY',
    free: 'IDLE',
    unknown: '—',
};

/** Zone key → short column word. Short because this column is scanned, not read. */
const ZONE_LABEL: Record<string, string> = {
    computer: 'bash',
    codebase: 'files',
    aws: 'aws',
    web: 'web',
    tests: 'tests',
    thinking: 'think',
    desk: '—',
};

export function state_label(state: RackState): string {
    return STATE_LABEL[state] ?? '—';
}

export function zone_label(zone: string | null | undefined): string {
    if (!zone) return '—';
    return ZONE_LABEL[zone] ?? zone;
}

/**
 * Compact duration for the AGE column: `42s`, `4m12s`, `1h04m`, `3d`.
 *
 * Seconds are dropped past an hour on purpose — at that range the precision is
 * noise, and the column has a fixed width the layout budgeted for.
 */
export function format_age(seconds: number | null | undefined): string {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '—';
    const total = Math.floor(seconds);
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) {
        const rest = total % 60;
        return rest === 0 ? `${minutes}m` : `${minutes}m${String(rest).padStart(2, '0')}s`;
    }
    const hours = Math.floor(minutes / 60);
    if (hours < 24) {
        const rest = minutes % 60;
        return rest === 0 ? `${hours}h` : `${hours}h${String(rest).padStart(2, '0')}m`;
    }
    return `${Math.floor(hours / 24)}d`;
}

/** Truncate with an ellipsis, never a hard cut — a clipped word reads as data. */
export function clip(text: string, max: number): string {
    if (max <= 0) return '';
    if (text.length <= max) return text;
    if (max === 1) return '…';
    return text.slice(0, max - 1) + '…';
}

/**
 * Build the board's rendered lines.
 *
 * Overflow is an explicit `+N MORE` line, NEVER a silent slice — the same rule
 * the rack lives by, and for the same reason: a board that quietly drops the
 * agent asking for you is worse than no board.
 */
export function build_fleet_board(input: FleetBoardInput): FleetBoardView {
    const { rows, sessions, max_lines } = input;

    let total = 0;
    let blocked = 0;
    for (const detail of sessions.values()) {
        total++;
        if (detail.state === 'attention') blocked++;
    }

    if (rows.every((r) => r.kind === 'empty')) {
        return {
            lines: [{ kind: 'empty', cells: [], text: 'NO TMUX SESSIONS', dim: true, attached: false }],
            shown: 0,
            total,
            blocked,
        };
    }

    const lines: FleetBoardLine[] = [];
    let shown = 0;
    let truncated_from: number | null = null;

    // One line is always reserved for a possible `+N MORE`, so the board can
    // never fill up in a way that leaves it unable to admit what it dropped.
    const budget = Math.max(1, max_lines - 1);

    for (let i = 0; i < rows.length; i++) {
        const row = rows[i];
        // Note lines are the rack's business — the board is a column grid.
        if (row.kind === 'note' || row.kind === 'empty') continue;

        if (row.kind === 'cohort') {
            if (lines.length >= budget) {
                truncated_from = i;
                break;
            }
            lines.push({ kind: 'cohort', cells: [], text: row.text, dim: true, attached: false });
            continue;
        }
        if (row.kind === 'overflow') {
            lines.push({ kind: 'overflow', cells: [], text: row.text, dim: true, attached: false });
            continue;
        }

        const session = row.session ?? '';
        const detail = sessions.get(session);
        const state = detail?.state ?? row.state ?? 'unknown';
        const message = detail?.message ?? null;
        // An attention row spends a second line explaining itself; anything else
        // is one line. Reserve both up front so a blocked agent is never
        // rendered with its reason cut off.
        const needed = state === 'attention' && message ? 2 : 1;
        if (lines.length + needed > budget) {
            truncated_from = i;
            break;
        }

        lines.push({
            kind: 'session',
            session,
            state,
            cells: [
                state_label(state),
                session,
                detail?.model ?? '—',
                zone_label(detail?.zone),
                format_age(detail?.age_seconds),
            ],
            dim: !row.state && !detail,
            attached: detail?.attached ?? false,
        });
        shown++;

        if (needed === 2) {
            lines.push({
                kind: 'detail',
                session,
                state,
                cells: [],
                text: message ?? '',
                dim: true,
                attached: false,
            });
        }
    }

    // Anything the budget cut off gets counted and stated.
    if (truncated_from !== null) {
        let hidden = 0;
        let hidden_blocked = 0;
        for (let i = truncated_from; i < rows.length; i++) {
            if (rows[i].kind !== 'session') continue;
            hidden++;
            const detail = rows[i].session ? sessions.get(rows[i].session as string) : undefined;
            if ((detail?.state ?? rows[i].state) === 'attention') hidden_blocked++;
        }
        if (hidden > 0) {
            const suffix = hidden_blocked > 0 ? ` · ${hidden_blocked} BLOCKED` : '';
            lines.push({
                kind: 'overflow',
                cells: [],
                text: `+${hidden} MORE${suffix}`,
                dim: true,
                attached: false,
            });
        }
    }

    return { lines, shown, total, blocked };
}

/** Header text: `RACK   3 BLOCKED · 8 SESSIONS`, or just the count when calm. */
export function fleet_board_header(view: FleetBoardView): string {
    if (view.total === 0) return 'RACK';
    const parts: string[] = [];
    if (view.blocked > 0) parts.push(`${view.blocked} BLOCKED`);
    parts.push(`${view.total} SESSION${view.total === 1 ? '' : 'S'}`);
    return `RACK   ${parts.join(' · ')}`;
}
