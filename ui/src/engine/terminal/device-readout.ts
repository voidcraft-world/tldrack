/**
 * The rack's DEVICES section: each machine's CPU and memory, from `GET /plugin/devices/state`.
 *
 * The bridge sends raw numbers (cores busy, bytes); every percentage and every string on screen
 * is made here, once, so two rack surfaces cannot format them two ways. What the numbers
 * MEAN, and why sampling them costs nothing while nobody looks: `plugins/devices/`.
 *
 * Pure (no DOM): `rack-view.ts` builds the rows into the payload and runs under bare
 * `node --test`. This machine's row also carries the resource lease (`device_lease`): who holds
 * the GPU's GB budget, the subscription login and the CPU, beside the hosts' JOBS.
 */
import { host_label } from './local-host.ts';
import { LIVE_QUIET_S, LeaseStatusSchema, type LeaseSlot, type LeaseStatus } from '@tldrack/schemas/resource-lease.ts';

export type DeviceState = 'connecting' | 'up' | 'down';

export interface DeviceSample {
    name: string;
    local: boolean;
    state: DeviceState;
    os: string | null;
    cores: number | null;
    /** Cores busy over the last sample interval; null until there are two samples. */
    cpu_busy: number | null;
    mem_used: number | null;
    mem_total: number | null;
    /** Seconds since that sample. */
    age_s: number | null;
    error: string | null;
    /** This machine only, and null until its first read: `plugins/devices/devices_plugin/sessions.py`. */
    session_memory: SessionMemory | null;
    /** A compute host only, and null until its first sample: what it runs, as named jobs. */
    jobs: HostJobs | null;
    /** This machine only, null until its first read: who holds each slot of the resource lease (`schemas/src/resource-lease.ts`). */
    lease: LeaseStatus | null;
}

export type JobState = 'running' | 'held' | 'idle';

/** One job on a compute host. Its shape, and what running / held / idle mean: `plugins/devices/README.md`. */
export interface HostJob {
    key: string;
    label: string;
    state: JobState;
    procs: number;
    /** Cores used over the sample window (a delta, not a lifetime average). */
    cores: number;
    rss: number;
    age_s: number | null;
    /** Its processes, folded by a safe name (never a command line). */
    groups: { name: string; count: number; state: JobState; cores: number; rss: number }[];
    groups_more: number;
}

export interface HostJobs {
    window_s: number | null;
    rows: HostJob[];
    /** Why the last read failed, while nothing newer has come (the devices plugin's `error`). */
    error?: string;
}

/** RSS per tmux session, largest first. A ranking, not an accounting: `sessions.py` says why. */
export interface SessionMemory {
    sessions: { session: string; rss: number; procs: number; cpu_pct?: number | null }[];
    age_s?: number;
    /** Sessions past the plugin's cap. */
    more: number;
    orphans: { rss: number; procs: number };
}

export interface DevicesState {
    sample_s: number;
    devices: DeviceSample[];
}

/** A meter turns amber here: a box this busy or this full is the one to stop sending work to. */
export const DEVICE_WARN_PCT = 85;

/** A sample older than this many intervals is not "now" any more (a host asleep before ssh notices). */
export const DEVICE_STALE_INTERVALS = 3;

const DEVICE_STATES: readonly DeviceState[] = ['connecting', 'up', 'down'];
const GIB = 1024 ** 3;
const MIB = 1024 ** 2;

/** Rows the section draws; the rest are counted (`DeviceMemory.more`). */
export const MEMORY_ROWS = 5;

/** Jobs an open JOBS list draws; the rest are counted (`DeviceJobs.more`). */
export const JOB_ROWS = 8;
/** Jobs the closed line names: the busy ones (running or held), busiest first. */
export const JOB_SUMMARY_ROWS = 3;
/** The key of the job that holds every process no named job claimed. */
export const OTHER_JOB = 'other';
const JOB_STATES: readonly JobState[] = ['running', 'held', 'idle'];

function num_or_null(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function parse_session_memory(raw: unknown): SessionMemory | null {
    if (!raw || typeof raw !== 'object') return null;
    const doc = raw as Record<string, unknown>;
    if (!Array.isArray(doc.sessions)) return null;
    const orphans = (doc.orphans ?? {}) as Record<string, unknown>;
    return {
        ...(num_or_null(doc.age_s) !== null ? { age_s: num_or_null(doc.age_s)! } : {}),
        sessions: doc.sessions.flatMap((row: unknown) => {
            const entry = (row ?? {}) as Record<string, unknown>;
            const rss = num_or_null(entry.rss);
            return typeof entry.session === 'string' && rss !== null
                ? [{ session: entry.session, rss, procs: num_or_null(entry.procs) ?? 0, ...(num_or_null(entry.cpu_pct) !== null ? { cpu_pct: num_or_null(entry.cpu_pct) } : {}) }]
                : [];
        }),
        more: num_or_null(doc.more) ?? 0,
        orphans: { rss: num_or_null(orphans.rss) ?? 0, procs: num_or_null(orphans.procs) ?? 0 },
    };
}

function job_state(value: unknown): JobState {
    return JOB_STATES.find((s) => s === value) ?? 'idle';
}

function parse_jobs(raw: unknown): HostJobs | null {
    if (!raw || typeof raw !== 'object') return null;
    const doc = raw as Record<string, unknown>;
    if (!Array.isArray(doc.rows)) return null;
    return {
        ...(typeof doc.error === 'string' && doc.error ? { error: doc.error } : {}),
        window_s: num_or_null(doc.window_s),
        rows: doc.rows.flatMap((value: unknown) => {
            const row = (value ?? {}) as Record<string, unknown>;
            if (typeof row.key !== 'string' || typeof row.label !== 'string') return [];
            const groups = Array.isArray(row.groups) ? row.groups : [];
            return [{
                key: row.key,
                label: row.label,
                state: job_state(row.state),
                procs: num_or_null(row.procs) ?? 0,
                cores: num_or_null(row.cores) ?? 0,
                rss: num_or_null(row.rss) ?? 0,
                age_s: num_or_null(row.age_s),
                groups: groups.flatMap((g: unknown) => {
                    const group = (g ?? {}) as Record<string, unknown>;
                    return typeof group.name === 'string'
                        ? [{ name: group.name, count: num_or_null(group.count) ?? 0, state: job_state(group.state),
                             cores: num_or_null(group.cores) ?? 0, rss: num_or_null(group.rss) ?? 0 }]
                        : [];
                }),
                groups_more: num_or_null(row.groups_more) ?? 0,
            }];
        }),
    };
}

function parse_device(raw: unknown): DeviceSample | null {
    if (!raw || typeof raw !== 'object') return null;
    const row = raw as Record<string, unknown>;
    if (typeof row.name !== 'string' || row.name === '') return null;
    const state = DEVICE_STATES.find((s) => s === row.state) ?? 'down';
    return {
        name: row.name,
        local: row.local === true,
        state,
        os: typeof row.os === 'string' ? row.os : null,
        cores: num_or_null(row.cores),
        cpu_busy: num_or_null(row.cpu_busy),
        mem_used: num_or_null(row.mem_used),
        mem_total: num_or_null(row.mem_total),
        age_s: num_or_null(row.age_s),
        error: typeof row.error === 'string' ? row.error : null,
        session_memory: parse_session_memory(row.session_memory),
        jobs: parse_jobs(row.jobs),
        lease: parse_lease(row.lease),
    };
}

/** The lease's readout, or null: absent, not read yet, or not a shape this page knows (it only ever gains keys). */
function parse_lease(raw: unknown): LeaseStatus | null {
    if (raw === null || raw === undefined) return null;
    const parsed = LeaseStatusSchema.safeParse(raw);
    return parsed.success ? parsed.data : null;
}

/** `/state`'s body, or null when it is not a devices document (the plugin is not mounted). */
export function parse_devices(body: unknown): DevicesState | null {
    if (!body || typeof body !== 'object') return null;
    const doc = body as Record<string, unknown>;
    if (!Array.isArray(doc.devices)) return null;
    return {
        sample_s: num_or_null(doc.sample_s) ?? 2,
        devices: doc.devices.map(parse_device).filter((row): row is DeviceSample => row !== null),
    };
}

// ── the readout ─────────────────────────────────────────────────────────────

export type DeviceTone = 'ok' | 'wait' | 'bad';

export interface DeviceMeter {
    label: 'CPU' | 'RAM';
    /** `2.2/10` or `21.4/32 GB`; `—` stands for a value not sampled yet. */
    text: string;
    pct: number | null;
    warn: boolean;
}

export interface DeviceRow {
    key: string;
    label: string;
    tone: DeviceTone;
    /** Replaces the meters' meaning when set: `OFFLINE`, `CONNECTING`, `STALE`. */
    status: string | null;
    /** Empty when the device has nothing to show (down, or never sampled). */
    meters: DeviceMeter[];
    /** The sentence version, for `title` and `aria-label`. */
    title: string;
    /** Which agent holds it. Absent on a device that does not report it. */
    memory?: DeviceMemory;
    /** What a compute host runs. Absent on a device that does not report it. */
    jobs?: DeviceJobs;
    /** Who holds this machine's resource lease. Absent on a device that does not report it. */
    lease?: DeviceLease;
}

export interface DeviceLease {
    /** The one line: `GPU 17.2/22 · laya-lab golden 3.2 · nightly-build 14 — CLAUDE · label-open-work`, or `free`. */
    summary: string;
    /** The sentence version, window and live work included. */
    title: string;
    /** A budget slot is full (`DEVICE_WARN_PCT`), or any slot has work queued behind it. */
    warn: boolean;
}

export type JobTone = 'run' | 'held' | 'idle' | 'other';

export interface DeviceJobRow {
    /** `<device>/<job>`: what an open job is remembered by. */
    key: string;
    status: string;
    label: string;
    /** `7.9 cores · 2.1 GB` */
    text: string;
    title: string;
    tone: JobTone;
    groups: { key: string; name: string; text: string; title: string; held: boolean }[];
}

export interface DeviceJobs {
    /** The closed line: `RUNNING NIGHTLY BUILD · PAUSED BENCH`, or `no active tasks`. */
    summary: string;
    title: string;
    rows: DeviceJobRow[];
    /** Jobs past `JOB_ROWS`. */
    more: number;
    /** The read failed and nothing newer has come: no rows, and the summary says so. */
    failed?: boolean;
}

export interface DeviceMemory {
    rows: { key: string; label: string; text: string; title: string }[];
    /** Sessions past `MEMORY_ROWS`, or past the plugin's own cap. */
    more: number;
    /** Agent processes no rack row owns; null when there are none. */
    orphans: { text: string; title: string } | null;
}

function pct_of(part: number | null, whole: number | null): number | null {
    if (part === null || whole === null || whole <= 0) return null;
    return Math.min(100, Math.max(0, Math.round((part / whole) * 100)));
}

/** GB with one decimal; a total drops a `.0` (`36`, `15.5`), a used value never does. */
function gb(bytes: number, trim: boolean): string {
    const text = (bytes / GIB).toFixed(1);
    return trim && text.endsWith('.0') ? text.slice(0, -2) : text;
}

/** `640 MB` under a gigabyte, `1.2 GB` from there. */
function memory_text(bytes: number): string {
    return bytes >= GIB ? `${(bytes / GIB).toFixed(1)} GB` : `${Math.round(bytes / MIB)} MB`;
}

function device_memory(memory: SessionMemory, labels: ReadonlyMap<string, string> | undefined): DeviceMemory {
    const shown = memory.sessions.slice(0, MEMORY_ROWS);
    return {
        rows: shown.map((row) => {
            const label = labels?.get(row.session) ?? row.session;
            const text = memory_text(row.rss);
            return { key: row.session, label, text, title: `${label}: ${text} resident across ${row.procs} processes` };
        }),
        more: memory.sessions.length - shown.length + memory.more,
        orphans: memory.orphans.procs === 0 ? null : {
            text: memory_text(memory.orphans.rss),
            title: `${memory.orphans.procs} agent or MCP processes that no rack session owns`,
        },
    };
}

/** `45s`, `45m`, `3h`, `2d`: an age as a short readout. */
export function span_text(seconds: number): string {
    const s = Math.max(0, seconds);
    if (s < 90) return `${Math.floor(s)}s`;
    if (Math.round(s / 60) < 60) return `${Math.round(s / 60)}m`;
    if (Math.round(s / 3600) < 36) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86_400)}d`;
}

const JOB_STATUS: Readonly<Record<JobState, string>> = { running: 'RUNNING', held: 'PAUSED', idle: 'IDLE' };
const JOB_TONES: Readonly<Record<JobState, JobTone>> = { running: 'run', held: 'held', idle: 'idle' };
const STATE_WORDS: Readonly<Record<JobState, string>> = {
    running: 'running: using CPU in the latest sample',
    held: 'paused: its worker processes are suspended',
    idle: 'idle: processes are present with little or no CPU activity',
};

function usage_text(cores: number, rss: number): string {
    return `${cores.toFixed(1)} cores · ${memory_text(rss)}`;
}

function device_jobs(device: string, jobs: HostJobs): DeviceJobs {
    if (jobs.error && !jobs.rows.length) {
        return { summary: 'unreadable', title: `Could not read what this host runs: ${jobs.error}. Retrying.`, rows: [], more: 0, failed: true };
    }
    const busy = jobs.rows.filter((job) => job.key !== OTHER_JOB && job.state !== 'idle');
    const named = busy.slice(0, JOB_SUMMARY_ROWS).map((job) => `${JOB_STATUS[job.state]} ${job.label}`);
    const summary = named.length ? named.join(' · ') + (busy.length > named.length ? ` +${busy.length - named.length}` : '')
        : jobs.rows.length ? 'no active tasks' : 'no processes reported';
    const shown = jobs.rows.slice(0, JOB_ROWS);
    const window = jobs.window_s === null ? '' : ` over the last ${Math.round(jobs.window_s)} s`;
    return {
        summary,
        title: `What this host runs${window}: ${busy.length ? busy.map((job) => `${job.label} ${job.state}`).join(', ') : 'nothing busy'}`,
        rows: shown.map((job) => {
            const other = job.key === OTHER_JOB;
            const age = job.age_s === null || other ? '' : `, up ${span_text(job.age_s)}`;
            const text = usage_text(job.cores, job.rss);
            return {
                key: `${device}/${job.key}`,
                status: other ? '' : JOB_STATUS[job.state],
                label: job.label,
                text,
                title: `${job.label}: ${other ? 'everything no job claims' : STATE_WORDS[job.state]} · ${job.cores.toFixed(1)} cores, `
                    + `${memory_text(job.rss)} across ${job.procs} process${job.procs === 1 ? '' : 'es'}${age}`,
                tone: other ? 'other' : JOB_TONES[job.state],
                groups: [
                    ...job.groups.map((group) => ({
                        key: group.name,
                        name: `${group.count}× ${group.name}`,
                        text: usage_text(group.cores, group.rss),
                        title: `${group.count} ${group.name} process${group.count === 1 ? '' : 'es'}: ${STATE_WORDS[group.state]} · `
                            + `${group.cores.toFixed(1)} cores, ${memory_text(group.rss)}`,
                        held: group.state === 'held',
                    })),
                    ...(job.groups_more ? [{ key: '+more', name: `+${job.groups_more} more`, text: '', title: `${job.groups_more} smaller groups`, held: false }] : []),
                ],
            };
        }),
        more: Math.max(jobs.rows.length - shown.length, 0),
    };
}

/** A slot's short name on the line: the slot names are the lease's ids, never shown raw. */
const SLOT_WORDS: Readonly<Record<LeaseSlot, string>> = { gpu: 'GPU', subscription_claude: 'CLAUDE', cpu: 'CPU' };
const UNIT_WORDS: Readonly<Record<string, string>> = { gb: 'GB', core: 'cores', holder: 'holder' };

/** One decimal, a whole number without its `.0`: `3.2`, `14`. */
function units_text(units: number): string {
    const text = units.toFixed(1);
    return text.endsWith('.0') ? text.slice(0, -2) : text;
}

function device_lease(lease: LeaseStatus): DeviceLease {
    const busy = lease.slots.filter((slot) => slot.holders.length > 0);
    const counted = (slot: LeaseStatus['slots'][number]) => slot.slot !== 'subscription_claude';
    const parts = busy.map((slot) => {
        const word = SLOT_WORDS[slot.slot];
        const head = counted(slot) ? `${word} ${units_text(slot.used)}/${units_text(slot.capacity)}` : word;
        const holders = slot.holders.filter((h) => h.holds).map((h) => (counted(slot) ? `${h.holder} ${units_text(h.units)}` : h.holder));
        return [head, ...holders, ...(slot.waiting ? [`${slot.waiting} waiting`] : [])].join(' · ');
    });
    const sentences = lease.slots.map((slot) => {
        const unit = UNIT_WORDS[slot.unit] ?? slot.unit;
        const held = slot.holders.map((h) =>
            `${h.holder} (${h.legacy ? 'an older checkout\'s Ollama lock' : `${units_text(h.units)} ${unit}, ${h.kind}${h.scheduled ? ', scheduled' : ''}`}`
            + `${h.age_s === null ? '' : `, ${span_text(h.age_s)}`}${h.holds ? '' : ', waiting'})`);
        return `${SLOT_WORDS[slot.slot]} ${units_text(slot.used)} of ${units_text(slot.capacity)} ${unit}`
            + (held.length ? ` held by ${held.join(', ')}` : ' free');
    });
    // Only a fresh mark is news (scheduled work is stepping aside for it); its age would re-render every poll.
    const live = lease.live.filter((mark) => mark.age_s < LIVE_QUIET_S).map((mark) => `${mark.name} live now: scheduled work steps aside`);
    const title = [
        `Resource lease: ${sentences.join('; ')}`,
        `night window ${lease.window.start}–${lease.window.end} ${lease.window.open ? 'open' : 'closed'}`,
        ...live,
    ].join(' · ');
    // One holder is what the subscription slot is for, so only a budget (GB, cores) can read as full.
    const warn = lease.slots.some((slot) => slot.waiting > 0 || (counted(slot) && (pct_of(slot.used, slot.capacity) ?? 0) >= DEVICE_WARN_PCT));
    return { summary: parts.length ? parts.join(' — ') : 'free', title, warn };
}

function cpu_meter(device: DeviceSample): DeviceMeter | null {
    if (device.cores === null && device.cpu_busy === null) return null;
    const busy = device.cpu_busy === null ? '—' : device.cpu_busy.toFixed(1);
    const pct = pct_of(device.cpu_busy, device.cores);
    return {
        label: 'CPU',
        text: device.cores === null ? busy : `${busy}/${device.cores}`,
        pct,
        warn: pct !== null && pct >= DEVICE_WARN_PCT,
    };
}

function ram_meter(device: DeviceSample): DeviceMeter | null {
    if (device.mem_total === null) return null;
    const used = device.mem_used === null ? '—' : gb(device.mem_used, false);
    const pct = pct_of(device.mem_used, device.mem_total);
    return { label: 'RAM', text: `${used}/${gb(device.mem_total, true)} GB`, pct, warn: pct !== null && pct >= DEVICE_WARN_PCT };
}

function sentence(label: string, device: DeviceSample, meters: DeviceMeter[], status: string | null): string {
    const parts = [label];
    if (device.os) parts.push(device.os);
    for (const meter of meters) {
        const pct = meter.pct === null ? '' : ` (${meter.pct}%)`;
        parts.push(meter.label === 'CPU' ? `${meter.text} cores busy${pct}` : `${meter.text} memory used${pct}`);
    }
    if (status) parts.push(status.toLowerCase());
    if (device.error) parts.push(device.error);
    return parts.join(' · ');
}

/** One row per device, in the plugin's order (this machine first). `labels` turns a session
 *  name into what its rack row calls it. */
export function device_rows(state: DevicesState, labels?: ReadonlyMap<string, string>): DeviceRow[] {
    const stale_after = state.sample_s * DEVICE_STALE_INTERVALS;
    return state.devices.map((device) => {
        const label = host_label(device.name);
        const meters = device.state === 'down' ? [] : [cpu_meter(device), ram_meter(device)].filter((m): m is DeviceMeter => m !== null);
        let tone: DeviceTone = 'ok';
        let status: string | null = null;
        if (device.state === 'down') {
            tone = 'bad';
            status = 'OFFLINE';
        } else if (meters.length === 0) {
            tone = 'wait';
            status = 'CONNECTING';
        } else if (device.state === 'connecting') {
            tone = 'wait';
        } else if (device.age_s !== null && device.age_s > stale_after) {
            tone = 'wait';
            status = 'STALE';
        }
        const row: DeviceRow = { key: device.name, label, tone, status, meters, title: sentence(label, device, meters, status) };
        if (device.session_memory && device.state !== 'down') row.memory = device_memory(device.session_memory, labels);
        if (device.jobs && device.state !== 'down') row.jobs = device_jobs(device.name, device.jobs);
        if (device.lease && device.state !== 'down') row.lease = device_lease(device.lease);
        return row;
    });
}

/** Everything the section draws; equal signatures re-render nothing. */
export function devices_signature(state: DevicesState | null): string {
    return state ? JSON.stringify([device_rows(state), state.devices.filter(row => row.local).map(row => ({
        sessions: row.session_memory?.sessions, stale: (row.session_memory?.age_s ?? 0) > 30,
    }))]) : '';
}
