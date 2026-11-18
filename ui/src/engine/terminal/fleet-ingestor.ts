/**
 * One bridge port's ingestion loop: `GET /snapshot` (with the legacy `/status`
 * fallback), the SSE accelerator, the per-session tracker ladder, and the
 * activity ring buffer. Everything that TALKS to a tldrack and nothing
 * that talks to a browser.
 *
 * ITS ENVIRONMENT IS INJECTED: the base URL, the "is the operator looking"
 * test and the flags all arrive through `FleetIngestorDeps`. That is what lets
 * one loop serve the HTTP transport (`agent-rack/rack-http-transport.ts`) and a
 * host's own payload owner alike, and what lets it run under `node --test`
 * against fakes.
 *
 * WHAT STAYS OUT, and why:
 *
 * - **Notifications.** Rendering one reaches `Notification`, `document.hidden`
 *   and `AudioContext`. The ingestor hands the caller the outstanding SET
 *   (`on_notice`) and lets a document decide whether it is the one that
 *   renders it.
 * - **Subscriber fan-out.** Who wants which session is the caller's business;
 *   this emits per-session state and lets the caller route it.
 *
 * ONE PORT PER INGESTOR, each with its own timer. One shared interval across
 * every port would make `tick_in_flight` global, and a slow bridge would stall
 * every other one. Ports are independent; so are these.
 */

import {
    advance_tracker,
    derive_effective_state,
    display_state,
    is_live,
    // Aliased: the ingestor exposes its own `mark_seen(session)`, and the pure
    // one takes a tracker. Same concept, different altitude.
    mark_seen as clear_done_pending,
    new_tracker,
    parse_launch_pick,
    parse_profiles,
    parse_session_row,
    parse_session_rows,
    parse_transition_entry,
    parse_usage_limits,
    type BridgeIdentity,
    type LaunchPick,
    type ProfileInfo,
    type PortSnapshot,
    type PortSnapshotRow,
    type SessionStatus,
    type SessionTracker,
    type TerminalDisplayState,
    type TransitionEntry,
    type UsageLimits,
} from './terminal-status-logic.ts';
import { local_event_source, local_fetch } from './local-network-gate.ts';
import { next_step_mode_schema, type NextStepMode } from '@tldrack/schemas/next-step.ts';

const FETCH_TIMEOUT_MS = 2500;

/** `/snapshot`'s envelope as it arrives — one shape for the poll and the SSE frame. */
interface SnapshotBody {
    sessions?: unknown;
    bridge?: unknown;
    usage_limits?: unknown;
    profiles?: unknown;
    launch_pick?: unknown;
    closed?: unknown;
    restorable?: unknown;
    next_step?: unknown;
}

/** Every top-level key beside `sessions`, parsed once. Null is "absent or malformed": each
 *  is additive on the bridge, and its absence is a capability signal, never a zero. */
interface SnapshotExtras {
    bridge: BridgeIdentity | null;
    usage: UsageLimits | null;
    profiles: ProfileInfo[] | null;
    launch_pick: LaunchPick | null;
    /** How many recent closes `POST /reopen` can bring back (`agent-rack/closed-client.ts`). */
    closed: number | null;
    /** How many sessions from before a reboot `POST /restore` can bring back (`agent-rack/closed-client.ts`). */
    restorable: number | null;
    /** The suggester's mode while it is on (`tldrack/next_step.py`). */
    next_step_mode: NextStepMode | null;
}

const NO_EXTRAS: SnapshotExtras = {
    bridge: null, usage: null, profiles: null, launch_pick: null, closed: null, restorable: null, next_step_mode: null,
};

/** An additive count key: a positive integer, else null (absent at 0 on the bridge). */
function positive_count(value: unknown): number | null {
    return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null;
}

function parse_snapshot_extras(body: SnapshotBody): SnapshotExtras {
    const next_step = next_step_mode_schema.safeParse(body.next_step);
    return {
        bridge: parse_bridge(body.bridge),
        usage: parse_usage_limits(body.usage_limits),
        profiles: parse_profiles(body.profiles),
        launch_pick: parse_launch_pick(body.launch_pick),
        closed: positive_count(body.closed),
        restorable: positive_count(body.restorable),
        next_step_mode: next_step.success ? next_step.data.mode : null,
    };
}
/** Client-side ticker cache size — also the /activity fetch limit. */
const ACTIVITY_LOG_CAP = 50;
/**
 * Consecutive answers that must omit a session before its tracker is dropped.
 *
 * Not tuning — it is what stops a single flaky `tmux ls` on the bridge from
 * resetting the whole rack's state. See the long note in `process`.
 */
const TRACKER_ABSENCE_GRACE = 3;

/** A command pushed FROM the bridge TO this app (bridge: POST /focus). */
export type BridgeCommandTarget = 'world' | 'rack';
export interface BridgeCommand {
    action: 'focus_session';
    session: string;
    /** `world` = a host's framed rack, `rack` = the rack page. The words are the bridge's. */
    target: BridgeCommandTarget;
}

/** The outstanding rack after one tick, in the shape a caller that renders
 *  banners needs, so this module never learns that notifications exist. Waits only: a finished
 *  turn is never a ping (`tldrack/notify.py` property 6 owns why). */
export interface FleetNoticeSets {
    attention: { session: string; message: string | null }[];
}

/**
 * Only the flags this loop actually reads. A narrow structural type rather than
 * the whole `TERMINAL_STATUS_FLAGS` blob: `TERMINAL_STATUS_FLAGS` satisfies it,
 * and a caller pushing flags across a boundary has a short list to carry.
 */
export interface IngestorFlags {
    poll_interval_ms: number;
    debounce_polls: number;
    hide_dead_sessions: boolean;
    snapshot_reprobe_ticks: number;
    sse_enabled: boolean;
    busy_stale_after_s: number;
    title_hint_enabled: boolean;
    fake_cwds: Record<string, string>;
    fake_background: Record<string, number | null>;
}

export interface FleetIngestorDeps {
    /** `path` → absolute URL. Loopback beside the bridge, the remote origin for a rack pointed at another machine. */
    base_url: (path: string) => string;
    /**
     * A GETTER, never a captured value. `TERMINAL_STATUS_FLAGS` is a live object:
     * a page, a host or a seeder changes it, and none of that would reach a
     * snapshot taken at construction.
     */
    flags: () => IngestorFlags;
    /**
     * Is the operator looking at this session right now? Decides done-vs-seen, so a
     * session that finishes under your eyes never latches a `done` highlight.
     * Per-DOCUMENT truth, which is exactly why it is injected.
     */
    is_watching: (session: string) => boolean;
    /**
     * Sessions to keep advancing even when the snapshot omits them: the
     * caller's per-session subscribers. They advance toward `unknown` and do
     * not freeze at their last state. Also the probe list for the legacy
     * ladder. Return nothing when there are none.
     */
    tracked_sessions: () => Iterable<string>;
    /**
     * May we fall back to the pre-snapshot `GET /status` route?
     *
     * A POLICY, not a shape difference. The remote path answers **false** on
     * purpose: a bridge old enough to lack `/snapshot` also predates the Origin
     * guard that makes remote access safe at all, so talking to one over a
     * network is the thing to refuse, not the thing to support. Do not
     * "simplify" this to a constant.
     */
    allow_legacy_fallback: boolean;
    /**
     * Last chance to add rows the bridge has not caught up with yet — i.e. the
     * `+ NEW` optimistic row.
     *
     * `?arg=<name>` under ttyd's `-a` creates the tmux session, but only once the
     * iframe has loaded and its socket has connected, so for at least one poll
     * the snapshot has no such row. Without this the focus re-adopt would yank
     * the pane back to some other session and the new one would never be created
     * at all. Applied BEFORE filtering and tracking, so the pending name also
     * reaches `known_sessions()` and cannot be handed out twice.
     */
    augment_sessions?: (sessions: SessionStatus[]) => SessionStatus[];
    on_snapshot: (snapshot: PortSnapshot) => void;
    /** Per-session state, for surfaces that subscribe to one terminal. */
    on_session?: (session: string, display: TerminalDisplayState, status: SessionStatus | null) => void;
    on_activity?: (entries: TransitionEntry[]) => void;
    on_command?: (command: BridgeCommand) => void;
    /** The whole outstanding set, once per tick. Omit to raise nothing — which
     *  is what every surface that is not the elected notifier should do. */
    on_notice?: (sets: FleetNoticeSets) => void;
    fetch_impl?: typeof fetch;
    event_source?: (url: string) => EventSource | null;
    set_interval?: (fn: () => void, ms: number) => ReturnType<typeof setInterval>;
    clear_interval?: (handle: ReturnType<typeof setInterval>) => void;
}

export interface FleetIngestor {
    /**
     * Open the SSE stream WITHOUT starting the poll.
     *
     * These are two lifecycles on purpose. A bridge `command` (`POST /focus`)
     * needs only the stream. It is push-only by design, because replaying a
     * missed command later would move the operator long after they asked, and
     * a host may hold that subscription for its whole lifetime. Fused with the
     * poll, one visit to a rack would leave a 3 s `/snapshot` poll running for
     * as long as the host lives.
     */
    start_stream: () => void;
    /** Begin polling (and ensure the stream). Safe to call repeatedly. */
    start_polling: () => void;
    /** Stop polling but KEEP the stream — the state a port is in when only a
     *  command subscriber is left. */
    stop_polling: () => void;
    /** Tear down both. */
    stop: () => void;
    is_polling: () => boolean;
    /** One pass, out of band. `start()` calls it so the first data is immediate. */
    tick: () => Promise<void>;
    /** Null until this port's first poll lands. Synchronous by design — two
     *  callers pull it rather than subscribe. */
    last_snapshot: () => PortSnapshot | null;
    /**
     * Every session name the bridge reported, **dead rows included and
     * unfiltered** — the `+ NEW` collision set.
     *
     * Deliberately NOT `last_snapshot().sessions`: `hide_dead_sessions` (on by
     * default) drops `live: false` rows at ingestion, so the snapshot is
     * live-only. A collision set taken from there can hand `+ NEW` a name the
     * bridge still has a store row for. Harmless today, because `tmux new -A`
     * attaches only to a session that is alive, but the margin is free and the
     * failure it guards (silently landing inside a running agent) is the worst
     * one this surface has.
     */
    known_sessions: () => string[];
    /** Clear a session's done highlight: the operator demonstrably looked at it. */
    mark_seen: (session: string) => void;
    /** Live tracker view, for a caller deriving its own rollups. */
    trackers: () => ReadonlyMap<string, SessionTracker>;
    is_sse_connected: () => boolean;
}

/** Additive `bridge` key of /snapshot — older bridges don't send it, and a
 *  malformed object is treated as absent rather than trusted. */
function parse_bridge(value: unknown): BridgeIdentity | null {
    if (typeof value !== 'object' || value === null) return null;
    const bridge = value as BridgeIdentity;
    if (typeof bridge.pid !== 'number') return null;
    if (typeof bridge.launched_by !== 'object' || bridge.launched_by === null) return null;
    if (typeof bridge.launched_by.kind !== 'string') return null;
    return bridge;
}

/** `fake_background` is empty unless a seeder set it; see the flag. An
 *  own-key check, for the reason `fake_cwds` is typeof-guarded below — a
 *  session named `constructor` must not read Object.prototype as a seed. */
function seeded_background(status: SessionStatus | null, fakes: Record<string, number | null>): SessionStatus | null {
    if (!status || !Object.hasOwn(fakes, status.session)) return status;
    return { ...status, state: 'background', background_jobs: fakes[status.session] };
}

export function create_fleet_ingestor(deps: FleetIngestorDeps): FleetIngestor {
    // The DEFAULTS carry the local-network gate; the injected forms do not, so
    // a test's fake fetch stays a fake fetch. Every real caller builds a
    // loopback URL from `deps.base_url`, and `local_fetch` passes a
    // non-loopback one straight through, so a remote origin is unaffected.
    // See `local-network-gate.ts`.
    const do_fetch = deps.fetch_impl ?? ((...args: Parameters<typeof fetch>) => local_fetch(args[0] as string, args[1]));
    const start_timer = deps.set_interval ?? ((fn, ms) => setInterval(fn, ms));
    const stop_timer = deps.clear_interval ?? ((handle) => clearInterval(handle));
    const open_event_source = deps.event_source ?? ((url: string) => local_event_source(url));

    const trackers = new Map<string, SessionTracker>();
    /** Consecutive answers that did not mention a tracked session — see the
     *  grace in `process`. Cleared the moment it shows up again. */
    const absences = new Map<string, number>();
    let raw_sessions: SessionStatus[] = [];
    let last_snapshot: PortSnapshot | null = null;
    /** /snapshot 404'd (older bridge) — use per-session /status until a re-probe succeeds. */
    let legacy = false;
    let ticks_since_probe = 0;
    /** Suppress notifications on the very first processed tick: entry must not
     *  replay the current rack state as a burst of alerts. */
    let first_tick_done = false;
    let activity_log: TransitionEntry[] = [];
    let sse: EventSource | null = null;
    let sse_connected = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    let tick_in_flight = false;
    let stopped = false;

    // ── SSE push (GET /events): an ACCELERATOR, never a replacement ─────────
    //
    // Snapshot frames are ingested the instant a hook fires instead of on the
    // next poll, but the poll keeps its full cadence regardless: the tracker's
    // downgrade debounce counts POLL OBSERVATIONS, and older bridges have no
    // /events at all.

    function open_sse(): void {
        if (sse || !deps.flags().sse_enabled) return;
        const stream = open_event_source(deps.base_url('/events'));
        if (!stream) return;
        sse = stream;
        stream.onopen = () => {
            sse_connected = true;
        };
        // EventSource reconnects on its own; while it's down (or the bridge
        // predates /events and keeps 404ing) the poll carries everything.
        // Deliberately NOT marked unreachable here: the poll is the authority on
        // that, and a single reconnect blip must not flash the rack to STALE.
        stream.onerror = () => {
            sse_connected = false;
        };
        stream.addEventListener('snapshot', (event) => {
            ingest_sse_snapshot((event as MessageEvent).data as string);
        });
        stream.addEventListener('activity', (event) => {
            ingest_sse_activity((event as MessageEvent).data as string);
        });
        stream.addEventListener('command', (event) => {
            ingest_sse_command((event as MessageEvent).data as string);
        });
    }

    function ingest_sse_snapshot(data: string): void {
        if (!deps.flags().sse_enabled) return; // live-tunable kill switch
        try {
            const body = JSON.parse(data) as SnapshotBody;
            const sessions = parse_session_rows(body.sessions);
            legacy = false;
            process(sessions, true, parse_snapshot_extras(body));
        } catch {
            // malformed frame — the next poll corrects
        }
    }

    function ingest_sse_activity(data: string): void {
        if (!deps.flags().sse_enabled) return;
        try {
            const entry = parse_transition_entry(JSON.parse(data));
            if (!entry) return;
            activity_log = [entry, ...activity_log].slice(0, ACTIVITY_LOG_CAP);
            deps.on_activity?.(activity_log);
        } catch {
            // malformed frame — ignore
        }
    }

    /** The one SSE frame that makes the app ACT rather than repaint, so an
     *  unknown action or a malformed session name is dropped, never guessed at. */
    function ingest_sse_command(data: string): void {
        if (!deps.flags().sse_enabled) return;
        try {
            const body = JSON.parse(data) as { action?: unknown; session?: unknown; target?: unknown };
            if (body.action !== 'focus_session') return;
            if (typeof body.session !== 'string' || !/^[A-Za-z0-9_-]+$/.test(body.session)) return;
            // ABSENT means `world` — that is every frame an older bridge sends.
            // An unknown value is dropped rather than guessed at.
            if (body.target !== undefined && body.target !== 'world' && body.target !== 'rack') return;
            deps.on_command?.({
                action: 'focus_session',
                session: body.session,
                target: body.target === 'rack' ? 'rack' : 'world',
            });
        } catch {
            // malformed frame — commands are fire-and-forget, nothing to correct
        }
    }

    /** Replace the ticker cache from GET /activity (the bridge's ring buffer is
     *  the source of truth). Seeds on subscribe, and is the poll-time refresh
     *  while the SSE stream is down. Older bridges 404 → ticker stays empty. */
    async function refresh_activity(): Promise<void> {
        try {
            const res = await do_fetch(deps.base_url(`/activity?limit=${ACTIVITY_LOG_CAP}`), {
                signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
            });
            if (!res.ok) return;
            const body = (await res.json()) as { entries?: unknown[] };
            const entries = (Array.isArray(body.entries) ? body.entries : [])
                .map(parse_transition_entry)
                .filter((entry): entry is TransitionEntry => entry !== null);
            const signature = `${entries.length}:${entries[0]?.ts ?? 0}`;
            const current = `${activity_log.length}:${activity_log[0]?.ts ?? 0}`;
            if (signature === current) return;
            activity_log = entries;
            deps.on_activity?.(activity_log);
        } catch {
            // unreachable — the ticker keeps its last entries
        }
    }

    // ── the poll ladder ────────────────────────────────────────────────────

    async function poll(): Promise<void> {
        const flags = deps.flags();
        let sessions: SessionStatus[] | null = null;
        let reachable = false;
        let extras: SnapshotExtras = NO_EXTRAS;

        const want_snapshot = !legacy || ticks_since_probe >= flags.snapshot_reprobe_ticks;
        if (want_snapshot) {
            ticks_since_probe = 0;
            try {
                const res = await do_fetch(deps.base_url('/snapshot'), {
                    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
                });
                if (res.ok) {
                    const body = (await res.json()) as SnapshotBody;
                    sessions = parse_session_rows(body.sessions);
                    extras = parse_snapshot_extras(body);
                    reachable = true;
                    legacy = false;
                } else if (res.status === 404 && deps.allow_legacy_fallback) {
                    legacy = true; // pre-snapshot bridge — fall through to /status
                }
            } catch {
                // unreachable — handled below
            }
        } else {
            ticks_since_probe += 1;
        }

        if (sessions === null && legacy && deps.allow_legacy_fallback) {
            const named = [...deps.tracked_sessions()];
            // With no session subscribers a legacy bridge would look permanently
            // unreachable — probe the default-session endpoint just for liveness.
            const probes: (string | null)[] = named.length > 0 ? named : [null];
            const rows = await Promise.all(
                probes.map(async (session): Promise<SessionStatus | null> => {
                    try {
                        const query = session === null ? '' : `?session=${encodeURIComponent(session)}`;
                        const res = await do_fetch(deps.base_url(`/status${query}`), {
                            signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
                        });
                        if (!res.ok) return null;
                        // Seeded with the name asked for; the body's own `session` wins.
                        const body = (await res.json()) as Record<string, unknown>;
                        const row = parse_session_row({ session: session ?? '', ...body });
                        return row ? { ...row, title: null, attached: false } : null;
                    } catch {
                        return null;
                    }
                }),
            );
            const hits = rows.filter((r): r is SessionStatus => r !== null);
            reachable = hits.length > 0;
            sessions = named.length > 0 ? hits : hits.filter((r) => r.state !== 'unknown');
        }

        process(sessions ?? [], reachable, extras);
        // Ticker fallback while the SSE stream is down — otherwise entries only
        // ever arrive by push.
        if (!sse_connected && deps.on_activity) await refresh_activity();
    }

    /**
     * Advance every tracker one observation and publish the result.
     *
     * `hide_dead_sessions` is applied HERE rather than at the fetch, so
     * `raw_sessions` keeps the unfiltered list that `known_sessions()` needs.
     */
    function process(incoming: SessionStatus[], reachable: boolean, extras: SnapshotExtras): void {
        const { bridge, usage, profiles, launch_pick, closed, restorable, next_step_mode } = extras;
        const flags = deps.flags();
        raw_sessions = deps.augment_sessions ? deps.augment_sessions(incoming) : incoming;
        const visible = flags.hide_dead_sessions ? raw_sessions.filter(is_live) : raw_sessions;

        const by_name = new Map<string, SessionStatus | null>(visible.map((s) => [s.session, s]));
        // Tracked sessions missing from the snapshot (or an unreachable bridge)
        // still advance their trackers — toward 'unknown'.
        for (const session of deps.tracked_sessions()) {
            if (!by_name.has(session)) by_name.set(session, null);
        }

        const suppress = !first_tick_done;
        const rows: PortSnapshotRow[] = [];
        // Collected across the loop and delivered ONCE below: N agents blocking
        // together must produce one summary, not N racing banners.
        const wants_you: { session: string; message: string | null }[] = [];

        for (const [session, listed] of by_name) {
            const status = seeded_background(listed, flags.fake_background);
            const effective =
                status && reachable
                    ? derive_effective_state(status, {
                          busy_stale_after_s: flags.busy_stale_after_s,
                          title_hint_enabled: flags.title_hint_enabled,
                      })
                    : 'unknown';
            const previous = trackers.get(session) ?? new_tracker();
            const { tracker } = advance_tracker(previous, effective, {
                debounce_polls: flags.debounce_polls,
                watching: deps.is_watching(session),
            });
            trackers.set(session, tracker);

            // The notifier wants the CURRENT outstanding set (so it can retract
            // once everything resolves), but membership must still respect the
            // debounce — `attention` commits INSTANTLY on the upgrade branch, so
            // testing `display === 'attention'` alone would alert on the very
            // first poll and reintroduce exactly the flicker `debounce_polls`
            // exists to absorb.
            if (!suppress) {
                const display = display_state(tracker);
                if (display === 'attention' && tracker.committed_polls >= flags.debounce_polls) {
                    wants_you.push({ session, message: status?.message ?? null });
                }
            }

            deps.on_session?.(session, display_state(tracker), status ?? null);
            if (status) {
                // fake_cwds is empty unless a seeder set it; see the flag.
                // typeof-guarded rather than `?? status.cwd`, because a session
                // legitimately named `constructor` (or `toString`) would otherwise
                // pull a FUNCTION off Object.prototype and blow up path splitting.
                const override = flags.fake_cwds[session];
                const cwd = typeof override === 'string' ? override : status.cwd;
                rows.push({ ...status, cwd, display: display_state(tracker) });
            }
        }

        // ── dropping trackers for sessions that vanished ────────────────────
        //
        // A tracker holds state that exists NOWHERE else — `done_pending` above
        // all, which is the only record that an agent finished while you were
        // away. Dropping one and rebuilding it from `new_tracker()` therefore
        // loses information rather than merely resetting a cache:
        //
        //   • `done_pending` is lost, so a `done` row silently reverts to `free`
        //     and the operator never learns that session finished.
        //   • `committed_polls` restarts, re-arming the attention notification's
        //     debounce so a blocked agent can re-alert.
        //   • the downgrade debounce restarts mid-transition.
        //
        // Two guards, because a session can appear to vanish two different ways:
        //
        // 1. **The bridge did not answer at all.** An unreachable tick calls
        //    `process([], false, …)`, and with `tracked_sessions()` empty that
        //    would wipe EVERY tracker on a single dropped poll. Silence is not
        //    evidence of absence: the same rule the satellite watchdog keeps in
        //    `companion/lifecycle-logic.ts`.
        //
        // 2. **The bridge answered, but the answer flickered.** `live` is
        //    computed as "this session is in `list_tmux_sessions()` right now"
        //    (`snapshot.py::merge_snapshot`), so one `tmux ls` that times out or
        //    loses a race reports the whole rack dead for one tick — and with
        //    `hide_dead_sessions` on by default those rows are filtered out
        //    upstream of here, so they simply are not in `by_name`. One flaky
        //    subprocess would reset every session's state. Hence the grace: an
        //    absence has to persist before it is believed.
        //
        // A tracker that outlives its session by a few ticks costs one map entry
        // and nothing else — `rows` is built from `by_name`, never from here.
        if (reachable) {
            for (const session of [...trackers.keys()]) {
                if (by_name.has(session)) {
                    absences.delete(session);
                    continue;
                }
                const missed = (absences.get(session) ?? 0) + 1;
                if (missed < TRACKER_ABSENCE_GRACE) {
                    absences.set(session, missed);
                    continue;
                }
                absences.delete(session);
                trackers.delete(session);
            }
        }

        // ONE delivery per tick, after every session has been advanced.
        if (!suppress) deps.on_notice?.({ attention: wants_you });

        first_tick_done = true;
        rows.sort((a, b) => a.session.localeCompare(b.session));
        last_snapshot = { reachable, sessions: rows, bridge, usage_limits: usage };
        // Absent, never `[]`, on a bridge without `/spawn` — see PortSnapshot.
        if (profiles !== null) last_snapshot.profiles = profiles;
        if (launch_pick !== null) last_snapshot.launch_pick = launch_pick;
        if (closed !== null) last_snapshot.closed = closed;
        if (restorable !== null) last_snapshot.restorable = restorable;
        if (next_step_mode !== null) last_snapshot.next_step_mode = next_step_mode;
        deps.on_snapshot(last_snapshot);
    }

    async function tick(): Promise<void> {
        if (stopped || tick_in_flight) return; // slow bridge — skip, don't stack
        tick_in_flight = true;
        try {
            await poll();
        } finally {
            tick_in_flight = false;
        }
    }

    return {
        start_stream: () => {
            stopped = false;
            open_sse();
        },
        start_polling: () => {
            stopped = false;
            open_sse();
            if (timer !== null) return;
            // setInterval, NOT the render loop: browsers throttle a hidden tab's
            // intervals to ~1/min, which only DELAYS alerts. A rAF-driven poll
            // would stop entirely.
            timer = start_timer(() => void tick(), deps.flags().poll_interval_ms);
            void tick(); // don't make the first subscriber wait a full interval
        },
        stop_polling: () => {
            if (timer === null) return;
            stop_timer(timer);
            timer = null;
        },
        stop: () => {
            stopped = true;
            if (timer !== null) {
                stop_timer(timer);
                timer = null;
            }
            sse?.close();
            sse = null;
            sse_connected = false;
        },
        is_polling: () => timer !== null,
        tick,
        last_snapshot: () => last_snapshot,
        known_sessions: () => raw_sessions.map((s) => s.session),
        mark_seen: (session) => {
            const tracker = trackers.get(session);
            if (!tracker?.done_pending) return;
            const cleared = clear_done_pending(tracker);
            trackers.set(session, cleared);
            deps.on_session?.(session, display_state(cleared), find_row(session));
        },
        trackers: () => trackers,
        is_sse_connected: () => sse_connected,
    };

    function find_row(session: string): SessionStatus | null {
        return last_snapshot?.sessions.find((s) => s.session === session) ?? null;
    }
}
