/**
 * The resource lease: who may use the GPU's share of memory, the subscription Claude login and the CPU of one
 * machine, and in what order. This file is the wire owner (a ticket, the capacity file, the readout) and the pure
 * arithmetic every reader must agree on (which tickets hold, whether the night window is open). It imports zod and
 * nothing else, so a page can read it. Every taker, in any language, holds to it: a port names this file as its
 * owner.
 *
 * - **Where:** `<lock root>/lease/<slot>/<ms>.<pid>.json`, one ticket a taker. nq's design: no daemon, `ls` is the
 *   status.
 * - **Order:** by name. A ticket is named under the lease's mutex with a millisecond past every ticket already in its
 *   slot, so a new ticket always sorts last and a ticket that holds can never be overtaken.
 * - **Fit:** a slot has a capacity in its own unit (GB, holders, cores). A ticket holds while the units of every ticket
 *   ahead of it, held or waiting, plus its own fit (`slot_holds`), so a small job never jumps a big one.
 * - **Legacy locks** are plain lock files an older taker drops in the lock root, each claiming its slot whole while
 *   its pid runs, or while it names no pid. A host that still has such takers lists their file names in its own
 *   taker. A current ticket may mirror itself into one, so an older taker that reads only the lock file still waits;
 *   a lock naming a live ticket's pid is then that ticket's mirror, never a claim of its own (`legacy_is_claim`).
 *
 * The environment names are `TLDRACK_*`. The lease's variables were first named `VOIDCRAFT_*`, and every taker on a
 * machine reads these, so a reader asks `env_value`: the new name first, then the old one. Neither side has to move
 * first. The Python half: `plugins/laya/laya_bridge/env_names.py`.
 */
import { z } from 'zod';

export const LEASE_VERSION = 1;
/** The lease's folder in the lock root. */
export const LEASE_DIRNAME = 'lease';
/** Moves the whole lock root (tests, a second checkout); unset, every taker uses the machine's default lock root. */
export const LOCK_DIR_ENV = 'TLDRACK_LOCK_DIR';
/** Set by a scheduler on the work it starts: a take inherits `scheduled` from it. */
export const LEASE_SCHEDULED_ENV = 'TLDRACK_LEASE_SCHEDULED';
export const ENV_PREFIX = 'TLDRACK_';
export const LEGACY_ENV_PREFIX = 'VOIDCRAFT_';

/** `TLDRACK_X` → `VOIDCRAFT_X`; any other name has no older spelling. */
export function legacy_env_name(name: string): string | null {
  return name.startsWith(ENV_PREFIX) ? LEGACY_ENV_PREFIX + name.slice(ENV_PREFIX.length) : null;
}

/** The value of `name`, else of its older spelling. Empty counts as unset. */
export function env_value(env: Readonly<Record<string, string | undefined>>, name: string): string | undefined {
  const value = env[name];
  if (value) return value;
  const old = legacy_env_name(name);
  return (old && env[old]) || undefined;
}

export const LEASE_SLOTS = ['gpu', 'subscription_claude', 'cpu'] as const;
export type LeaseSlot = (typeof LEASE_SLOTS)[number];
export const SLOT_UNITS: Readonly<Record<LeaseSlot, string>> = { gpu: 'gb', subscription_claude: 'holder', cpu: 'core' };

/** What the default capacity is worked out from: the machine's memory (shared with the GPU) and its cores. */
export interface LeaseHost {
  readonly memory_gb: number;
  readonly cores: number;
}
/** Memory the gpu slot leaves to the desktop, `laya-serve` (~2 GB) and the bridge's next-step model. */
export const GPU_RESERVE_GB = 10;
/** The gpu slot never shrinks below this share of memory, however small the machine. */
export const GPU_MIN_SHARE = 0.25;
/** Cores the cpu slot leaves to the operator and the rack's agents. */
export const CPU_RESERVE_CORES = 4;

/**
 * The capacity when `capacity.json` is absent, worked out from the machine. gpu: memory less GPU_RESERVE_GB (a quarter
 * of it at least): a budget is the only thing that keeps a Laya fine-tune from landing beside a 20–26B local model.
 * subscription_claude: one unattended Claude job at a time. cpu: the cores less CPU_RESERVE_CORES, at least one.
 * A 32 GB machine with 10 cores gets 22 GB, 1 and 6.
 */
export function default_capacity(host: LeaseHost): Readonly<Record<LeaseSlot, number>> {
  const memory = Number.isFinite(host.memory_gb) && host.memory_gb > 0 ? host.memory_gb : 0;
  const cores = Number.isFinite(host.cores) && host.cores > 0 ? Math.floor(host.cores) : 1;
  const gpu = Math.round(Math.max(memory * GPU_MIN_SHARE, memory - GPU_RESERVE_GB) * 10) / 10;
  return {
    gpu: Math.min(MAX_CAPACITY.gpu, Math.max(0.1, gpu)),
    subscription_claude: 1,
    cpu: Math.min(MAX_CAPACITY.cpu, Math.max(1, cores - CPU_RESERVE_CORES)),
  };
}
export const MAX_CAPACITY: Readonly<Record<LeaseSlot, number>> = { gpu: 512, subscription_claude: 4, cpu: 256 };
/** A Laya router target loaded on MPS: the checkpoint's own size. */
export const ROUTER_TARGET_GB = 1.6;
/** An Ollama model counts its listed size (`/api/tags`, what `ollama list` prints) times this: its KV cache and runner. */
export const OLLAMA_RESIDENT_FACTOR = 1.15;

/** What the units are spent on; each slot takes its own kinds. */
export const LEASE_KINDS = ['ollama', 'mps', 'claude', 'cpu'] as const;
export const SLOT_KINDS: Readonly<Record<LeaseSlot, readonly string[]>> = { gpu: ['ollama', 'mps'], subscription_claude: ['claude'], cpu: ['cpu'] };

/** A holder is an id (a branch, a command), never prose: it is drawn on the rack and read over the tailnet. */
export const LEASE_HOLDER_RE = /^[A-Za-z0-9][A-Za-z0-9 ._:/@+-]{0,79}$/;
export const LEASE_MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,79}$/;
/** `<ms>.<pid>.json`: thirteen digits of epoch milliseconds (fixed width until 2286), so a name sort is the order. */
export const TICKET_NAME_RE = /^(\d{13})\.([1-9]\d{0,9})\.json$/;
export const LEASE_SIGNATURE_MAX = 64;

/** The lease's mutex (`.mutex` in the lease folder): held only while a ticket is named and written. */
export const MUTEX_NAME = '.mutex';
/** A mutex older than this, or whose pid is gone, was left by a crash: the critical section is one listing and one write. */
export const MUTEX_STALE_MS = 10_000;
export const MUTEX_WAIT_MS = 3_000;

// ── When scheduled work may run ───────────────────────────────────────────────────────────────────────────────

/**
 * Scheduled GPU work runs inside this window only (`SLOT_YIELDS`), on the machine's own wall clock (`zone_clock`); a
 * hand-started take ignores it. 03:00–07:00 since 2026-11-08 (it was 00:30–07:30), so overnight agent sessions are
 * not slowed.
 */
export const GPU_NIGHT_WINDOW = { start: '03:00', end: '07:00' } as const;
/** Live work leaves a mark and never takes a ticket: Laya's rack verdicts and the bridge's next-step chip. */
export const LIVE_NAMES = ['laya-decide', 'next-step'] as const;
export const LIVE_DIRNAME = 'live';
/** Scheduled work steps aside while a live mark is younger than this. */
export const LIVE_QUIET_S = 10;
/** …and while the 1-minute load is past this many per core: the machine is someone else's. */
export const LOAD_PER_CORE_MAX = 1;

/** The yields a SCHEDULED take keeps on each slot, in the order they are checked (labelling is not heavy on the machine). */
export const SLOT_YIELDS: Readonly<Record<LeaseSlot, readonly ('outside_window' | 'live' | 'load')[]>> = {
  gpu: ['outside_window', 'live', 'load'],
  subscription_claude: [],
  cpu: ['load'],
};

/** Why a take did not hold, as a code. `busy` and the yields are waits, never failures. */
export const TAKE_REFUSALS = ['busy', 'too_big', 'live', 'load', 'outside_window', 'io'] as const;
export type TakeRefusal = (typeof TAKE_REFUSALS)[number];

const CLOCK_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Is a window open at local wall-clock `clock` (`HH:MM`)? It may wrap midnight; `end` is exclusive. */
export function window_open(window: { readonly start: string; readonly end: string }, clock: string): boolean {
  if (!CLOCK_RE.test(clock) || !CLOCK_RE.test(window.start) || !CLOCK_RE.test(window.end)) return false;
  if (window.start === window.end) return false;
  return window.start < window.end ? window.start <= clock && clock < window.end : clock >= window.start || clock < window.end;
}

function minutes(clock: string): number {
  return Number(clock.slice(0, 2)) * 60 + Number(clock.slice(3, 5));
}

/**
 * Does `inner` lie inside `outer` (both may wrap midnight)? A scheduler that books GPU work checks its own window lies
 * inside GPU_NIGHT_WINDOW, so it never books hours the lease refuses.
 */
export function window_within(inner: { readonly start: string; readonly end: string }, outer: { readonly start: string; readonly end: string }): boolean {
  if (![inner.start, inner.end, outer.start, outer.end].every((clock) => CLOCK_RE.test(clock))) return false;
  if (inner.start === inner.end || outer.start === outer.end) return false;
  const at = (clock: string) => (minutes(clock) - minutes(outer.start) + 1440) % 1440;
  const span = at(outer.end) || 1440;
  const start = at(inner.start);
  const end = at(inner.end) || 1440;
  return start < end && end <= span;
}

/** `HH:MM` at `now` in `zone` (an IANA name), or on the machine's own clock without one. */
export function zone_clock(now: Date, zone?: string): string {
  return new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(now);
}

/** Whole minutes from `clock` (`HH:MM`) to `window`'s end while the window is open there, or null while it is closed. */
export function window_minutes_left(window: { readonly start: string; readonly end: string }, clock: string): number | null {
  if (!window_open(window, clock)) return null;
  return (minutes(window.end) - minutes(clock) + 1440) % 1440;
}

/**
 * A scheduled run's deadline in epoch ms: its scheduler sets it to the run's start plus its budget, the moment it
 * stops the engine. An engine that reads it finishes its unit of work (a batch, the score) before then, instead of
 * losing it to the stop.
 */
export const EVAL_DEADLINE_ENV = 'TLDRACK_EVAL_DEADLINE';
const DEADLINE_RE = /^[1-9]\d{0,15}$/;

/**
 * Minutes this run may still work at `now`, whichever bound comes first: the GPU night window's end when its scheduler
 * started it (`LEASE_SCHEDULED_ENV`; 0 once the window is closed), and the run's deadline when it has one
 * (`EVAL_DEADLINE_ENV`; never below 0). Null when neither bounds it: a run by hand. Every scheduled engine reads its
 * stop from here, less its own margin for the unit in flight. `zone` as `zone_clock`'s.
 */
export function run_minutes_left(env: Readonly<Record<string, string | undefined>>, now: Date, zone?: string): number | null {
  const left: number[] = [];
  if (env_value(env, LEASE_SCHEDULED_ENV) === '1') left.push(window_minutes_left(GPU_NIGHT_WINDOW, zone_clock(now, zone)) ?? 0);
  const deadline = env_value(env, EVAL_DEADLINE_ENV) ?? '';
  if (DEADLINE_RE.test(deadline)) left.push(Math.max(0, (Number(deadline) - now.getTime()) / 60_000));
  return left.length === 0 ? null : Math.min(...left);
}

/** Any holder string made safe for a ticket: disallowed characters become `-`, then it is cut to 80. */
export function lease_holder_text(raw: string): string {
  const text = raw.replace(/[^A-Za-z0-9 ._:/@+-]/g, '-').replace(/^[^A-Za-z0-9]+/, '').slice(0, 80).trimEnd();
  return LEASE_HOLDER_RE.test(text) ? text : 'unnamed';
}

// ── A ticket ──────────────────────────────────────────────────────────────────────────────────────────────────

export const TICKET_RULES = ['slot_kind', 'holder_units', 'capacity'] as const;
export type TicketRule = (typeof TICKET_RULES)[number];

const TicketShape = z.strictObject({
  v: z.literal(LEASE_VERSION),
  slot: z.enum(LEASE_SLOTS),
  holder: z.string().regex(LEASE_HOLDER_RE),
  kind: z.enum(LEASE_KINDS),
  /** GB on gpu, 1 on subscription_claude, cores on cpu. */
  units: z.number().finite().positive(),
  models: z.array(z.string().regex(LEASE_MODEL_RE)).max(8),
  /** Started by a scheduler: it keeps to the window and yields to live work and load. */
  scheduled: z.boolean(),
  pid: z.int().min(1).max(2 ** 31 - 1),
  /** `ps -p <pid> -o lstart=`: a reused pid is a different process. */
  signature: z.string().min(1).max(LEASE_SIGNATURE_MAX).nullable(),
  /** The writer holds an exclusive flock on the ticket for its life (Python takers); Node takers cannot. */
  flock: z.boolean(),
  at: z.iso.datetime(),
});
export type LeaseTicket = z.infer<typeof TicketShape>;

export function ticket_rule_breaks(ticket: LeaseTicket): TicketRule[] {
  const breaks = new Set<TicketRule>();
  if (!SLOT_KINDS[ticket.slot].includes(ticket.kind)) breaks.add('slot_kind');
  if (ticket.slot === 'subscription_claude' && ticket.units !== 1) breaks.add('holder_units');
  if (ticket.units > MAX_CAPACITY[ticket.slot]) breaks.add('capacity');
  return TICKET_RULES.filter((rule) => breaks.has(rule));
}

export const LeaseTicketSchema = TicketShape.superRefine((ticket, ctx) => {
  for (const rule of ticket_rule_breaks(ticket)) ctx.addIssue({ code: 'custom', message: rule });
});

/** `capacity.json` in the lease folder: a machine-wide override of `default_capacity`, one reader for every taker. */
export const LeaseCapacitySchema = z.strictObject({
  v: z.literal(LEASE_VERSION),
  gpu: z.number().finite().positive().max(MAX_CAPACITY.gpu).optional(),
  subscription_claude: z.int().min(1).max(MAX_CAPACITY.subscription_claude).optional(),
  cpu: z.number().finite().positive().max(MAX_CAPACITY.cpu).optional(),
});
export type LeaseCapacity = z.infer<typeof LeaseCapacitySchema>;

export function slot_capacity(slot: LeaseSlot, config: LeaseCapacity | null, host: LeaseHost): number {
  return config?.[slot] ?? default_capacity(host)[slot];
}

// ── Which tickets hold ────────────────────────────────────────────────────────────────────────────────────────

/** One claim on a slot, in name order: a ticket, or a legacy lock (which holds whole, ahead of every ticket). */
export interface SlotClaim {
  readonly units: number;
  readonly legacy: boolean;
}

/** Whether a legacy lock naming `pid` is a claim of its own: not when it is the mirror of a live ticket in its slot (the header). */
export function legacy_is_claim(pid: number | null, ticket_pids: ReadonlySet<number>): boolean {
  return pid === null || !ticket_pids.has(pid);
}

/** Floating GB (1.6 + 1.6 + …) must not refuse a ticket that fits to the tenth. */
const FIT_EPSILON = 1e-9;

/**
 * Whether each claim holds, in the order given (legacy first, then tickets by name). A legacy lock always holds and
 * counts first; a ticket holds when everything ahead of it, held or waiting, plus its own units fit the capacity.
 */
export function slot_holds(claims: readonly SlotClaim[], capacity: number): boolean[] {
  let ahead = claims.filter((claim) => claim.legacy).reduce((sum, claim) => sum + claim.units, 0);
  return claims.map((claim) => {
    if (claim.legacy) return true;
    const holds = ahead + claim.units <= capacity + FIT_EPSILON;
    ahead += claim.units;
    return holds;
  });
}

// ── The readout (devices plugin → the rack's DEVICES section) ─────────────────────────────────────────────────

export const LeaseHolderSchema = z.strictObject({
  holder: z.string().regex(LEASE_HOLDER_RE),
  /** null for a legacy lock, which says nothing about what it runs. */
  kind: z.enum(LEASE_KINDS).nullable(),
  units: z.number().finite().nonnegative(),
  models: z.array(z.string().regex(LEASE_MODEL_RE)).max(8),
  scheduled: z.boolean().nullable(),
  holds: z.boolean(),
  legacy: z.boolean(),
  age_s: z.number().finite().nonnegative().nullable(),
});
export type LeaseHolder = z.infer<typeof LeaseHolderSchema>;

export const LeaseSlotStatusSchema = z.strictObject({
  slot: z.enum(LEASE_SLOTS),
  unit: z.string(),
  capacity: z.number().finite().positive(),
  /** Units of the claims that hold. */
  used: z.number().finite().nonnegative(),
  /** Claims queued behind them. */
  waiting: z.int().min(0),
  holders: z.array(LeaseHolderSchema).max(64),
});
export type LeaseSlotStatus = z.infer<typeof LeaseSlotStatusSchema>;

/** The lease's readout: what the devices plugin's `GET /plugin/devices/state` carries as `lease`. */
export const LeaseStatusSchema = z.strictObject({
  v: z.literal(LEASE_VERSION),
  slots: z.array(LeaseSlotStatusSchema).max(LEASE_SLOTS.length),
  window: z.strictObject({ start: z.string().regex(CLOCK_RE), end: z.string().regex(CLOCK_RE), open: z.boolean() }),
  live: z.array(z.strictObject({ name: z.enum(LIVE_NAMES), age_s: z.number().finite().nonnegative() })).max(LIVE_NAMES.length),
  /** Files in the lease that are not a ticket this version reads (left alone, never deleted). */
  unreadable: z.int().min(0),
});
export type LeaseStatus = z.infer<typeof LeaseStatusSchema>;
