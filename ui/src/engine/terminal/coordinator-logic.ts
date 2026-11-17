/**
 * Pure logic for the coordinator panel: tolerant parsing of the tldrack
 * coordinator plugin's `CoordinatorState` envelope, the `/resume` sidecar the
 * parser cannot carry, plus the ranking and the two label helpers the panel
 * renders with. Deliberately dependency-free (no three, no vue, no DOM), so it
 * runs under `node --test` (tests/unit/coordinator-logic.test.ts).
 *
 * Contract mirror: the wire shape is defined in plugins/coordinator/README.md.
 * Parsing is tolerant, with one rule that matters more here than elsewhere: a
 * finding whose `kind` this build has never heard of is KEPT at the bottom
 * rung, never dropped. The plugin ships independently of the pages, so a newer
 * plugin teaching itself a sixth signal must not blank an older panel.
 */

import { as_record, as_string } from './wire-parse.ts';

// ── wire shapes (the plugin's CoordinatorState envelope) ─────────────────────

/** The signals the plugin can raise, most urgent first. */
export type CoordinatorKind =
    | 'blocked'
    | 'rejected-plan'
    | 'dismissed-question'
    | 'stated-next-step'
    | 'open-offer'
    | 'unactioned-recommendation';

/** Set only by the deep (`claude -p`) pass — is the named next step real work? */
export type CoordinatorWorkClass = 'open-work' | 'routine';

/** One parked session worth walking back to. */
export interface CoordinatorFinding {
    /** `<8-char session prefix>:<kind>` — stable across runs; the dismissal key. */
    key: string;
    session_id: string;
    label: string;
    cwd: string | null;
    project: string | null;
    branch: string | null;
    /** Last record that actually carried a timestamp — NEVER the file mtime. */
    last_active: string | null;
    age_hours: number | null;
    /** A `CoordinatorKind`, or an unrecognised string from a newer plugin build. */
    kind: CoordinatorKind | string;
    /** 0 = most urgent, 4 = least. See the severity ladder in the plugin README. */
    severity: number;
    headline: string;
    detail: string;
    /** tmux session name while the session is still running. */
    live_session: string | null;
    work_class: CoordinatorWorkClass | null;
}

/** What the panel renders from — always complete, even offline. */
export interface CoordinatorView {
    status: 'idle' | 'scanning';
    scanned_at: string | null;
    scanned: number;
    ranked: boolean;
    /** Which pass sorted the list: Laya on this machine, or the `claude -p` fallback (`laya_rank.py`). */
    ranked_by: 'laya' | 'claude' | null;
    dismissed: number;
    error: string | null;
    findings: CoordinatorFinding[];
    /** Page-side only: false when the bridge/plugin did not answer. */
    reachable: boolean;
}

export function empty_coordinator_view(): CoordinatorView {
    return {
        status: 'idle',
        scanned_at: null,
        scanned: 0,
        ranked: false,
        ranked_by: null,
        dismissed: 0,
        error: null,
        findings: [],
        reachable: false,
    };
}

/** The ladder the plugin ranks by, restated here as the fallback when the wire
 *  omits `severity`. `stated-next-step` is the one kind whose rung moves — it is
 *  only real work once the deep pass says so. */
const KIND_SEVERITY: Record<CoordinatorKind, number> = {
    blocked: 0,
    'rejected-plan': 1,
    'dismissed-question': 1,
    'stated-next-step': 4,
    'open-offer': 3,
    'unactioned-recommendation': 4,
};

/** Rung labels, indexed by severity. */
const SEVERITY_LABELS = ['BLOCKED', 'REFUSED', 'OPEN', 'OFFER', 'ROUTINE'];

const MAX_HEADLINE_CHARS = 160;
const MAX_DETAIL_CHARS = 600;
const HOURS_PER_DAY = 24;
const HOURS_PER_WEEK = 24 * 7;

// ── parsing ──────────────────────────────────────────────────────────────────
// `as_record` / `as_string`: owned by `wire-parse.ts` — see the note there.

function as_number(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function as_count(value: unknown): number {
    const n = as_number(value);
    return n === null || n < 0 ? 0 : Math.floor(n);
}

/** Trim to a hard character budget. The plugin already caps these; re-capping
 *  here is the cheap redundant check, not a second opinion on wording. */
function clamp_text(value: unknown, max_chars: number): string {
    const text = as_string(value);
    if (text === null) return '';
    const trimmed = text.trim();
    return trimmed.length <= max_chars ? trimmed : `${trimmed.slice(0, max_chars - 1)}…`;
}

function is_known_kind(kind: string): kind is CoordinatorKind {
    return Object.prototype.hasOwnProperty.call(KIND_SEVERITY, kind);
}

/**
 * Resolve a finding's rung.
 *
 * An unknown kind is pinned to the bottom rung no matter what the wire claims:
 * this build cannot judge a signal it has never seen, and letting it assert
 * severity 0 would let a newer plugin push an unrenderable row to the top of the
 * panel. Known kinds keep the server's number (it owns work_class), falling back
 * to the ladder when the field is missing or junk.
 */
function resolve_severity(kind: string, raw: unknown, work_class: CoordinatorWorkClass | null): number {
    if (!is_known_kind(kind)) return 4;
    const wire = as_number(raw);
    if (wire !== null) return Math.min(4, Math.max(0, Math.round(wire)));
    if (kind === 'stated-next-step') return work_class === 'open-work' ? 2 : 4;
    return KIND_SEVERITY[kind];
}

/** One wire finding → a row, or null when it carries no usable identity. */
function parse_finding(raw: unknown): CoordinatorFinding | null {
    const row = as_record(raw);
    if (row === null) return null;
    const session_id = as_string(row.session_id) ?? '';
    const wire_key = as_string(row.key);
    // Without a key AND without a session there is nothing to dismiss and nothing
    // to name — a synthesized key would collide with every other such row and one
    // dismissal would suppress them all.
    if (wire_key === null && session_id === '') return null;
    const kind = as_string(row.kind) ?? 'unknown';
    const work_class = row.work_class === 'open-work' || row.work_class === 'routine' ? row.work_class : null;
    const short_id = session_id.slice(0, 8);
    const key = wire_key ?? `${short_id}:${kind}`;
    return {
        key,
        session_id,
        label: as_string(row.label) ?? (short_id || key),
        cwd: as_string(row.cwd),
        project: as_string(row.project),
        branch: as_string(row.branch),
        last_active: as_string(row.last_active),
        age_hours: as_number(row.age_hours),
        kind,
        severity: resolve_severity(kind, row.severity, work_class),
        // A headline is one line by contract; a stray newline renders as a box
        // glyph on a canvas-text surface, so flatten it and do not trust the sender.
        headline: clamp_text(row.headline, MAX_HEADLINE_CHARS).replace(/\s+/g, ' '),
        detail: clamp_text(row.detail, MAX_DETAIL_CHARS),
        live_session: as_string(row.live_session),
        work_class,
    };
}

/**
 * The whole `CoordinatorState` body → a view. Total: any malformed, missing,
 * null or wrong-typed field degrades to a safe default and never throws.
 * A body that isn't an object at all yields the empty, UNREACHABLE view, so a
 * garbage 200 reads as "no data" and not as a real, empty audit.
 */
export function parse_coordinator_state(raw: unknown): CoordinatorView {
    const view = empty_coordinator_view();
    const root = as_record(raw);
    if (root === null) return view;
    view.status = root.status === 'scanning' ? 'scanning' : 'idle';
    view.scanned_at = as_string(root.scanned_at);
    view.scanned = as_count(root.scanned);
    view.ranked = root.ranked === true;
    view.ranked_by = root.ranked_by === 'laya' || root.ranked_by === 'claude' ? root.ranked_by : null;
    view.dismissed = as_count(root.dismissed);
    view.error = as_string(root.error);
    if (Array.isArray(root.findings)) {
        view.findings = root.findings
            .map((row) => parse_finding(row))
            .filter((row): row is CoordinatorFinding => row !== null);
    }
    view.reachable = true;
    return view;
}

// ── the /resume sidecar ──────────────────────────────────────────────────────

/** What `/resume` hangs off the side of its `CoordinatorState` envelope. */
export interface CoordinatorResumed {
    /** The tmux session now running `claude --resume <id>`; null when refused. */
    session: string | null;
    /** False when that session already existed — RESUME pressed twice. */
    created: boolean;
}

/**
 * Read `/resume`'s `resumed` sidecar off the RAW body.
 *
 * **This exists because `parse_coordinator_state` above is deliberately TOTAL
 * and keeps only the fields it knows about** — it drops `resumed` silently, so
 * reading it back off the parsed view would compile, run, and always be
 * undefined. It lives here, beside the parser that drops it and under the tests
 * that pin both, and not privately in whatever client posts `/resume`: a later
 * pass that folds that post into a shared helper would otherwise take this with
 * it, and every RESUME would report "no session named" while the tmux session
 * was in fact up, with no test going red.
 */
export function read_resumed(body: unknown): CoordinatorResumed {
    const resumed = as_record(as_record(body)?.resumed);
    const session = as_string(resumed?.session);
    return { session, created: resumed?.created === true };
}

/**
 * A refused resume → one line the panel can show.
 *
 * Prefers the plugin's own `error` (it knows which of the refusals fired) and
 * falls back to the status, so a future refusal code is still reported rather
 * than swallowed.
 */
export function resume_refusal(status: number, body: unknown): string {
    const message = as_string(as_record(body)?.error);
    if (message !== null) return message;
    if (status === 400) return 'that finding is no longer known to the coordinator — re-run AUDIT';
    if (status === 503) return 'the bridge host has no tmux, or no claude binary on its PATH';
    // Same meaning 404 carries on every other bridge route (see the rename
    // flow): the host built its route table at startup, so an unknown path is a
    // bridge that predates the route rather than a missing finding.
    if (status === 404) return 'this tldrack has no /resume route — restart it to pick the plugin up';
    return `resume failed (HTTP ${status})`;
}

// ── ranking ──────────────────────────────────────────────────────────────────

/** ISO timestamp → epoch ms, with anything unparseable sorting oldest. */
function activity_ms(finding: CoordinatorFinding): number {
    if (finding.last_active === null) return Number.NEGATIVE_INFINITY;
    const ms = Date.parse(finding.last_active);
    return Number.isNaN(ms) ? Number.NEGATIVE_INFINITY : ms;
}

/**
 * Sort by severity ascending, then by last activity descending, and cap.
 *
 * The plugin already sends findings sorted this way; re-sorting here is what lets
 * the panel filter (hide routine) or merge without having to preserve wire order.
 * A non-positive `max` means "no cap".
 * Never mutates the input: callers hold a cached view.
 */
export function rank_findings(findings: CoordinatorFinding[], max: number): CoordinatorFinding[] {
    const sorted = [...findings].sort((a, b) => {
        if (a.severity !== b.severity) return a.severity - b.severity;
        return activity_ms(b) - activity_ms(a);
    });
    return max > 0 ? sorted.slice(0, max) : sorted;
}

// ── display helpers ──────────────────────────────────────────────────────────

/** Rung → the badge the panel prints. Out-of-range clamps rather than blanks. */
export function severity_label(severity: number): string {
    if (!Number.isFinite(severity)) return SEVERITY_LABELS[SEVERITY_LABELS.length - 1];
    const index = Math.min(SEVERITY_LABELS.length - 1, Math.max(0, Math.round(severity)));
    return SEVERITY_LABELS[index];
}

/**
 * Age in hours → a two-or-three character chip: `3h`, `2d`, `3w`, or `—`.
 *
 * Floors at every step rather than rounding, so the number can never disagree
 * with its own bucket (a rounded 23.9 h reads `24h`, which is a day). Negative
 * input is clock skew between the bridge host and the browser, not data — clamp
 * it to zero instead of printing a future.
 */
export function age_label(hours: number | null): string {
    if (hours === null || !Number.isFinite(hours)) return '—';
    const safe = Math.max(0, hours);
    if (safe < HOURS_PER_DAY) return `${Math.floor(safe)}h`;
    if (safe < HOURS_PER_WEEK) return `${Math.floor(safe / HOURS_PER_DAY)}d`;
    return `${Math.floor(safe / HOURS_PER_WEEK)}w`;
}
