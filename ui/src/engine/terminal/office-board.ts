/**
 * What the office's WALL MONITOR says: the rack in a dozen lines, for a glance from across
 * the floor. How many agents work, wait and idle; each account's usage, or that it is out;
 * each machine's CPU and memory.
 *
 * NOTHING HERE IS NEW DATA. The counts are the layout's own bands
 * (`agent-office-logic.ts`); the accounts are the LAUNCH chips' (`ProfileInfo`); the machines
 * are the DEVICES rows (`device-readout.ts`). The rack relays a projection of the last two
 * (`agent-office-link.ts`), because the office polls nothing.
 *
 * Pure: no three, no DOM, no clock. The scene draws the lines it is handed
 * (`agent-office-scene.ts` `OfficeBoard`). Tested by `tests/unit/office-board.test.ts`.
 */
import { display_state_color_css } from '../../config/terminal-status-flags.ts';
import { office_band_counts, type OfficeLayout } from './agent-office-logic.ts';
import type { DeviceRow, DeviceTone } from './device-readout.ts';
import { standing_reason, type ProfileInfo } from './terminal-status-logic.ts';

/** One line of the monitor: monospace text in one colour. */
export interface BoardLine {
    text: string;
    color: string;
}

/** An account, as the monitor reads it. */
export interface OfficeAccount {
    label: string;
    /** Used %, 0–100, or null where the account reported no such window. */
    five_hour: number | null;
    week: number | null;
    /** Why no turn runs there (`standing_reason`), or null on an account in good standing. */
    out: string | null;
}

/** A machine, as the monitor reads it. */
export interface OfficeMachine {
    label: string;
    tone: DeviceTone;
    /** `OFFLINE`, `CONNECTING`, `STALE`, or null while the meters speak for it. */
    status: string | null;
    cpu: number | null;
    ram: number | null;
    /** The resource lease's one line (who holds the GPU), where the machine reports one. */
    lease: string | null;
}

export interface OfficeBoardData {
    accounts: OfficeAccount[];
    machines: OfficeMachine[];
}

const MAX_ROWS = 16;
const LABEL_MAX = 24;
const LEASE_MAX = 64;
const TONES: readonly DeviceTone[] = ['ok', 'wait', 'bad'];

function pct(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? Math.min(100, Math.max(0, Math.round(value))) : null;
}

/** The accounts worth a line: every profile that reported usage or is out. A shell, and an
 *  agent CLI whose quota never reaches the bridge, have nothing to say. */
export function office_accounts(profiles: readonly ProfileInfo[]): OfficeAccount[] {
    return profiles
        .filter((profile) => profile.usage !== null || profile.standing !== null)
        .slice(0, MAX_ROWS)
        .map((profile) => ({
            label: profile.label,
            five_hour: pct(profile.usage?.five_hour?.used_percentage),
            week: pct(profile.usage?.seven_day?.used_percentage),
            out: profile.standing ? standing_reason(profile.standing) : null,
        }));
}

export function office_machines(rows: readonly DeviceRow[]): OfficeMachine[] {
    return rows.slice(0, MAX_ROWS).map((row) => ({
        label: row.label,
        tone: row.tone,
        status: row.status,
        cpu: pct(row.meters.find((meter) => meter.label === 'CPU')?.pct),
        ram: pct(row.meters.find((meter) => meter.label === 'RAM')?.pct),
        lease: row.lease ? row.lease.summary.slice(0, LEASE_MAX) : null,
    }));
}

function text(value: unknown, max: number): string | null {
    return typeof value === 'string' && value.trim() !== '' ? value.trim().slice(0, max) : null;
}

function rows(value: unknown): Record<string, unknown>[] {
    if (!Array.isArray(value)) return [];
    return value.slice(0, MAX_ROWS).filter((row): row is Record<string, unknown> => typeof row === 'object' && row !== null);
}

/** A relayed board, re-validated: same origin is not the same as well-formed. A row with no
 *  label is dropped; nothing left is no board. */
export function parse_office_board(raw: unknown): OfficeBoardData | undefined {
    if (typeof raw !== 'object' || raw === null) return undefined;
    const data = raw as Record<string, unknown>;
    const accounts = rows(data.accounts).flatMap((row): OfficeAccount[] => {
        const label = text(row.label, LABEL_MAX);
        return label ? [{ label, five_hour: pct(row.five_hour), week: pct(row.week), out: text(row.out, LABEL_MAX) }] : [];
    });
    const machines = rows(data.machines).flatMap((row): OfficeMachine[] => {
        const label = text(row.label, LABEL_MAX);
        return label ? [{
            label,
            tone: TONES.find((tone) => tone === row.tone) ?? 'wait',
            status: text(row.status, LABEL_MAX),
            cpu: pct(row.cpu),
            ram: pct(row.ram),
            lease: text(row.lease, LEASE_MAX),
        }] : [];
    });
    return accounts.length || machines.length ? { accounts, machines } : undefined;
}

/** The monitor's own palette: the rack's mint for headings, its amber and red for a level
 *  that wants a look (`.rack__chip-week--warn`, `.rack__chip-week--out`). */
export const BOARD_COLORS = { head: '#8ccdaa', text: '#c9d3d8', dim: '#7d898f', warn: '#f8c146', bad: '#ff7b72' } as const;

/** Past this a window is close to its cap: the LAUNCH chip's own threshold. */
export const BOARD_WARN_PCT = 80;

function level_color(...levels: (number | null)[]): string {
    return levels.some((level) => level !== null && level >= BOARD_WARN_PCT) ? BOARD_COLORS.warn : BOARD_COLORS.text;
}

function cell(label: string, value: number | null): string {
    return value === null ? ' '.repeat(label.length + 5) : `${label} ${String(value).padStart(3)}%`;
}

function plural(count: number, one: string, many: string): string {
    return `${count} ${count === 1 ? one : many}`;
}

/**
 * The monitor's lines, top to bottom. The three counts always show, zeros included: a line
 * that comes and goes would move every line under it. `stale` (the rack stopped relaying)
 * greys the accounts and machines and says so; the counts are the layout's and are as fresh
 * as the floor they stand behind.
 */
export function board_lines(layout: OfficeLayout, board: OfficeBoardData | undefined, stale = false): BoardLine[] {
    const counts = office_band_counts(layout);
    const lines: BoardLine[] = [
        { text: `RACK · ${plural(layout.agents, 'AGENT', 'AGENTS')}${layout.services ? ` · ${plural(layout.services, 'SERVICE', 'SERVICES')}` : ''}`,
            color: BOARD_COLORS.head },
        { text: `${String(counts.working).padStart(3)} WORKING`, color: counts.working ? display_state_color_css('busy') : BOARD_COLORS.dim },
        { text: `${String(counts.waiting).padStart(3)} ${counts.waiting === 1 ? 'NEEDS YOU' : 'NEED YOU'}`,
            color: counts.waiting ? display_state_color_css('attention') : BOARD_COLORS.dim },
        { text: `${String(counts.idle).padStart(3)} IDLE`, color: BOARD_COLORS.dim },
    ];
    const aged = (color: string): string => (stale ? BOARD_COLORS.dim : color);
    if (board?.accounts.length) {
        const wide = Math.max(...board.accounts.map((account) => account.label.length));
        lines.push({ text: `ACCOUNTS${stale ? ' · STALE' : ''}`, color: BOARD_COLORS.head });
        for (const account of board.accounts) {
            const name = account.label.padEnd(wide);
            lines.push(account.out
                ? { text: `${name}  OUT · ${account.out.toUpperCase()}`, color: aged(BOARD_COLORS.bad) }
                : { text: `${name}  ${cell('5H', account.five_hour)}  ${cell('WK', account.week)}`.trimEnd(),
                    color: aged(level_color(account.five_hour, account.week)) });
        }
    }
    if (board?.machines.length) {
        const wide = Math.max(...board.machines.map((machine) => machine.label.length));
        lines.push({ text: `MACHINES${stale ? ' · STALE' : ''}`, color: BOARD_COLORS.head });
        for (const machine of board.machines) {
            const name = machine.label.padEnd(wide);
            const quiet = machine.tone === 'bad' ? BOARD_COLORS.bad : BOARD_COLORS.dim;
            lines.push(machine.status && machine.cpu === null && machine.ram === null
                ? { text: `${name}  ${machine.status}`, color: aged(quiet) }
                : { text: `${name}  ${cell('CPU', machine.cpu)}  ${cell('RAM', machine.ram)}${machine.status ? `  ${machine.status}` : ''}`.trimEnd(),
                    color: aged(machine.status ? quiet : level_color(machine.cpu, machine.ram)) });
            if (machine.lease) lines.push({ text: `${' '.repeat(wide)}  ${machine.lease}`, color: BOARD_COLORS.dim });
        }
    }
    return lines;
}
