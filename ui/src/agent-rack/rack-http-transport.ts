/**
 * The HTTP rack transport: the page talks to a bridge directly and assembles `RackPayload`
 * itself. It is what `index.html` runs on, against the origin that served it, and what a phone
 * runs on against another machine (`rack-remote.ts` owns which bridge).
 *
 * `create_iframe_transport` talks to a host over `window.parent`, and `create_bus_transport`
 * to a host's main window over a `BroadcastChannel`. Both assume something else polls the
 * rack. Here nothing else does. `AgentRackComponent` is unchanged and unaware, which is the
 * whole point of the seam.
 *
 * ONE NOTIFICATION WRITER PER ORIGIN PER BRIDGE PORT
 *
 * A notification tag is scoped by bridge port, not by window. Two documents on one origin that
 * both raised rack notices would overwrite and retract each other's banners forever. So this
 * transport raises none: it shares the ingestion core (`rack-ingestor.ts`, `rack-view.ts`),
 * and the ingestor raises a notice only when it is handed an `on_notice`. This file hands it
 * none, and must not start to. A page that wants banners is a different feature: it would need
 * one owner per origin, chosen on purpose.
 */
import { next_session_name } from '../engine/terminal/multi-terminal-layout.ts';
import { create_rack_metric_delivery } from './metrics-delivery.ts';
import { create_next_step_delivery } from './next-step-client.ts';
import { create_rack_ingestor } from '../engine/terminal/rack-ingestor.ts';
import {
    adopt_focus,
    attach_only_sessions,
    build_rack_payload,
    cleaning_sessions,
    live_sessions,
    worktree_for_session,
    type WorktreeRow,
} from '../engine/terminal/rack-view.ts';
import { create_rename_tracker } from '../engine/terminal/rename-tracker.ts';
import { MULTI_TERMINAL_FLAGS } from '../config/multi-terminal-flags.ts';
import { TERMINAL_STATUS_FLAGS } from '../config/terminal-status-flags.ts';
import { augment_rack_sessions } from './rack-ingest.ts';
import { create_launch_notice, launch_new } from './spawn-client.ts';
import {
    clean_up_worktree,
    cleanup_notice,
    cleanup_request,
    create_island_focus,
    create_notice,
    control_task_group,
    create_worktree,
    create_worktree_poller,
    open_pr,
    parse_create_request,
} from './worktree-client.ts';
import { create_dev_services_poller } from './dev-services-client.ts';
import { create_devices_poller, read_devices_open, write_devices_open } from './devices-client.ts';
import { has_unread_answer, open_work_answer, read_answer } from './open-work-client.ts';
import { parse_star_value, set_star } from './star-client.ts';
import { create_missions_poller, parse_mission_request, post_mission } from './missions-client.ts';
import { create_closed_list } from './closed-client.ts';
import { create_chat_search } from './chat-search.ts';
import { create_lead_poller, handle_lead_action, type LeadActionDeps } from './lead-client.ts';
import { is_service_session } from '../engine/terminal/service-sessions.ts';
import type { RemoteConfig } from './rack-remote.ts';
import type { RackPayload, RackTransport } from './rack-transport.ts';
import {
    classify_access_notice,
    is_loopback_url,
    local_access_state,
    local_fetch,
    local_network_permission,
    unlock_local_access,
} from '../engine/terminal/local-network-gate.ts';

/** A phone on cellular is slower than loopback, but a timeout longer than the poll interval
 *  just queues requests. */
const FETCH_TIMEOUT_MS = 2500;

export interface RemoteRackTransport extends RackTransport {
    /** Stop every timer and close the SSE stream. The window calls this on
     *  unmount; `subscribe`'s unsubscribe only detaches one listener. */
    dispose: () => void;
    /** Force a refresh — used after an action so the UI does not wait a poll. */
    refresh: () => void;
}

/**
 * @param config Validated origins. **Must** have come from
 *   `rack-remote.ts::normalize_remote_origin` — nothing here re-checks them,
 *   and this module both fetches from and frames whatever it is handed.
 */
export function create_http_transport(config: RemoteConfig): RemoteRackTransport {
    const listeners = new Set<(payload: RackPayload) => void>();

    /** Which session the live pane shows. Owned HERE: this document is the pane's owner. */
    let focused: string | null = null;
    /** Sessions whose `POST /open-work/read` is in flight, so a burst of snapshots posts once. */
    const reading = new Set<string>();
    /**
     * A `+ NEW` name the bridge has not caught up with yet.
     *
     * `?arg=<name>` under ttyd's `-a` creates the tmux session, but only once the
     * iframe has loaded and its socket has connected — so for at least one poll
     * the snapshot has no such row. Without this, the `on_snapshot` re-adopt
     * ("focus a live session") would immediately yank focus back to some other
     * session and the new one would never be created at all.
     *
     * Fed to the ingestor as `augment_sessions`, so the pending name reaches
     * `known_sessions()` too and `+ NEW` cannot hand it out twice.
     *
     * On loopback the race is mostly won; over a tailnet it would be lost, and
     * `create()` ticks straight away besides.
     */
    let pending_new: string | null = null;
    /** The launch profile behind `pending_new`, so its optimistic row is already attach-only
     *  (`rack-ingest.ts` `optimistic_row` owns why). */
    let pending_profile: string | null = null;
    /** The name `POST /spawn` is still building. A row click on it is IGNORED: the pane would
     *  navigate to a bare `?arg=`, and ttyd's attach-or-create would win the race for the name
     *  (`spawn-client.ts` owns the ordering note). */
    let spawning: string | null = null;
    let disposed = false;
    const launch_notice = create_launch_notice(() => emit());
    /** Keeps focus on a session renamed from here — owned by `rename-tracker.ts`. */
    const renames = create_rename_tracker();

    function url(path: string): string {
        return `${config.bridge_origin}${path}`;
    }
    const record_metric = create_rack_metric_delivery(url);
    const report_next_step = create_next_step_delivery(url);

    /** Worktree state. Absent plugin → `null` state → no `worktrees` key on the
     *  payload → no worktree UI at all. */
    const worktrees = create_worktree_poller({
        base_url: url,
        poll_interval_ms: MULTI_TERMINAL_FLAGS.worktree_poll_ms,
        on_change: () => {
            land_island_focus();
            emit();
        },
    });

    /** A 🏝 launch waiting for its session — `worktree-client.ts::create_island_focus`. */
    const island_focus = create_island_focus();

    function land_island_focus(): void {
        const live = live_sessions(ingestor.last_snapshot()?.sessions ?? []);
        const session = island_focus.take(worktrees.state(), live, Date.now());
        if (session !== null) focused = session;
    }

    /** dev-services state for the `svc-*` readout; idle while no such row. */
    const dev_services = create_dev_services_poller({
        base_url: url,
        on_change: () => emit(),
        wanted: () => (ingestor.last_snapshot()?.sessions ?? []).some((row) => is_service_session(row.session)),
    });

    /** The DEVICES section; open/closed is this device's own (`devices-client.ts`). */
    let devices_open = read_devices_open();
    const devices = create_devices_poller({
        base_url: url,
        on_change: () => emit(),
        wanted: () => devices_open && !document.hidden,
    });
    const missions = create_missions_poller({
        base_url: url,
        on_change: () => emit(),
        wanted: () => !document.hidden,
    });
    /** Every LEAD run (`lead-client.ts`). */
    const lead = create_lead_poller({
        base_url: url,
        on_change: () => emit(),
        wanted: () => !document.hidden,
    });
    /** The search box's chat half: asked here, debounced, carried back on the payload (`chat-search.ts`). */
    const chat_search = create_chat_search({ base_url: url, on_change: () => emit() });
    /** The ↺ list: closes and reboot restores. Read only when opened or when a snapshot count moves (`closed-client.ts`). */
    const closed_list = create_closed_list({
        base_url: url,
        on_change: () => emit(),
        // `POST /reopen` answers after tmux does, so this is `+ NEW`'s `on_spawned` with the pending
        // row set first: the pane's first URL is then already the profiled `attach:` form, not one
        // the next poll would change and reload.
        on_reopened: (session, profile) => {
            island_focus.cancel();
            pending_new = session;
            pending_profile = profile;
            focused = session;
            emit();
            void ingestor.tick();
        },
        on_failure: (message) => launch_notice.show(message),
        // RESTORE ALL: no one session to focus; the next poll lists them all.
        on_restored: () => void ingestor.tick(),
    });
    /** Each takes its own session-less actions and declines the rest (`handle_action` → false). */
    const session_less_owners = [closed_list, chat_search];

    function build_payload(): RackPayload {
        const rows = ingestor.last_snapshot()?.sessions ?? [];
        const worktree_state = worktrees.state();
        return build_rack_payload({
            rows,
            focused,
            reachable: ingestor.last_snapshot()?.reachable ?? false,
            max_sessions: MULTI_TERMINAL_FLAGS.scroll_max_sessions,
            // The port the bridge says its ttyd holds. The page frames `config.ttyd_origin`, and on
            // loopback corrects its port from this (`rack-remote.ts` `resolved_ttyd_origin`).
            ttyd_port: ingestor.last_snapshot()?.bridge?.port,
            //
            // `attached` is NEVER false here. It means "something is registered
            // to service actions", which for a bus satellite is a statement about the
            // main window. On this path the BRIDGE services every action directly, so the
            // rack is never frozen — and `AgentRackComponent.send()` hard-returns
            // while frozen, which would silently disable every button.
            attached: true,
            // Undefined when no plugin answered, which is what keeps the
            // worktree UI off every machine that has not registered it.
            worktrees: worktree_state?.worktrees,
            task_groups: worktree_state?.task_groups,
            missions: missions.state() ?? undefined,
            // Resolved HERE, since nothing else polls: `rack-view.ts` `attach_only_sessions`.
            attach_only: worktree_state?.worktree_paths
                ? [...attach_only_sessions(rows, worktree_state.worktree_paths)]
                : undefined,
            // Every non-main tree, joined onto each row as its island.
            islands: worktree_state?.worktree_trees,
            // Only when the peer IS loopback. A `?remote=<tailnet host>` fetch
            // never consults the gate (`local_fetch` passes non-loopback
            // straight through), so classifying it would tell a phone user to
            // turn on local tools when the real problem is that the machine is off.
            access: is_loopback_url(url('/snapshot'))
                ? (classify_access_notice(local_access_state(), local_network_permission()) ?? undefined)
                : undefined,
            // Undefined on a bridge without `/spawn` — the capability signal.
            profiles: ingestor.last_snapshot()?.profiles,
            launch_pick: ingestor.last_snapshot()?.launch_pick,
            next_step_mode: ingestor.last_snapshot()?.next_step_mode,
            services: dev_services.state() ?? undefined,
            launch_notice: launch_notice.text() ?? undefined,
            pending_profiled: pending_profile ? pending_new : null,
            spawning,
            devices: devices.state() ?? undefined,
            devices_open,
            closed: closed_list.view(),
            chat_search: chat_search.view(),
            lead: lead.state() ?? undefined,
        });
    }

    function emit(): void {
        if (disposed) return;
        const payload = build_payload();
        for (const listener of listeners) listener(payload);
    }

    /**
     * The poll, the SSE stream and the tracker ladder are `rack-ingestor.ts`'s, shared with
     * any other payload owner. What is left here is this transport's own.
     *
     * `allow_legacy_fallback: false` is the one real POLICY difference, and it is
     * a security decision rather than a shape one: a bridge old enough to lack
     * `/snapshot` also predates the Origin guard that makes remote access safe at
     * all, so degrading to `GET /status` over a tailnet would be the wrong favour.
     *
     * It never raises a notification (no `on_notice`): the file header owns why.
     */
    const ingestor = create_rack_ingestor({
        base_url: url,
        flags: () => TERMINAL_STATUS_FLAGS,
        // Only the session actually on screen here counts as watched.
        is_watching: (session) => !document.hidden && session === focused,
        tracked_sessions: () => [],
        allow_legacy_fallback: false,
        // The `+ NEW` row, the building worktree rows and the `pending_new`
        // hand-back: owned by `rack-ingest.ts` — see the note there.
        augment_sessions: (sessions) => {
            const augmented = augment_rack_sessions(sessions, pending_new, worktrees.state(), pending_profile);
            pending_new = augmented.pending_new;
            if (pending_new === null) pending_profile = null;
            return augmented.sessions;
        },
        on_snapshot: (snapshot) => {
            if (disposed) return;
            closed_list.observe_count(snapshot.closed, snapshot.restorable);
            chat_search.observe_sessions(live_sessions(snapshot.sessions));
            // Adopt a focus on first data, and re-adopt if the focused session
            // died. Must run against the LIVE set: focusing a dead name would
            // point ttyd at `?arg=<name>`, which under `-a` silently CREATES it.
            // `pending_new` is the deliberate exception — that one is *supposed*
            // to be created.
            const live = live_sessions(snapshot.sessions);
            land_island_focus();
            // A session a CLEAN is about to kill is no candidate — `cleaning_sessions` owns why.
            const leaving = cleaning_sessions(snapshot.sessions, worktrees.state()?.worktrees);
            focused = adopt_focus(focused, live.filter((session) => !leaving.has(session)), pending_new, renames);
            renames.observe(live);
            // An answer that lands on the pane already on screen is being read as it arrives.
            if (!document.hidden) mark_answer_read(focused);
            emit();
        },
    });

    /** The operator has `session`'s pane open: read its answer, if its row holds one
     *  (`has_unread_answer`). The bridge clears the mark and repaints every client. */
    function mark_answer_read(session: string | null): void {
        if (session === null || reading.has(session)) return;
        if (!has_unread_answer(ingestor.last_snapshot()?.sessions ?? [], session)) return;
        reading.add(session);
        void read_answer(url, session).then((ok) => {
            reading.delete(session);
            if (ok && !disposed) void ingestor.tick();
        });
    }

    // ── actions ────────────────────────────────────────────────────────────

    async function post(path: string, body?: unknown): Promise<Response | null> {
        try {
            return await local_fetch(url(path), {
                method: 'POST',
                headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
                body: body === undefined ? undefined : JSON.stringify(body),
                signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
            });
        } catch {
            return null;
        }
    }

    async function kill(session: string): Promise<void> {
        if (pending_new === session) pending_new = null;
        // Blank the pane BEFORE the kill, never after.
        //
        // `ttyd_src` is computed from `focused`, and ttyd's client reconnects the
        // moment its child exits — ~30 ms, measured — while a bare `?arg=<name>`
        // is `tmux new -A`, i.e. attach-or-CREATE. Leave the pane pointed at the
        // session being killed and it re-creates it long before this `tick()`
        // returns, so `ingest`'s re-adopt then sees a LIVE row and keeps focus
        // there: the kill looks like it silently failed, forever.
        //
        // Deliberately unconditional on the kill's outcome. Re-pointing at a session that turned out to still be
        // alive costs one reconnect and `tick()` re-adopts it on the next tick;
        // the reverse mistake is unrecoverable from here.
        const was_focused = focused === session;
        if (was_focused) {
            focused = null;
            emit();
        }
        await post(`/kill-session?session=${encodeURIComponent(session)}`);
        // Let the refresh decide the new focus rather than guessing here: if the
        // kill failed, the session is still live and still the right focus.
        await ingestor.tick();
    }

    async function rename(session: string, name: string): Promise<void> {
        renames.begin(session, name);
        const res = await post('/rename-session', { session, name });
        renames.answer(session, res?.ok === true);
        // Nothing local moves until the bridge says yes — a refused rename must
        // leave the rack pointing at the name that still exists, or its ✕ and ▶
        // both miss.
        if (res?.ok && focused === session) focused = name;
        await ingestor.tick();
    }

    /**
     * `+ NEW`, with the LAUNCH chip's profile in `value`. The sequence — and why
     * focus must not move before `/spawn` answers — is owned by
     * `spawn-client.ts::launch_new`; this only hands it this document's own state.
     */
    function create(profile: string | null): void {
        if (spawning !== null) return; // one launch in flight at a time
        launch_notice.clear();
        void launch_new(profile, {
            base_url: url,
            known_sessions: () => ingestor.known_sessions(),
            profiles: () => ingestor.last_snapshot()?.profiles,
            launch_pick: () => ingestor.last_snapshot()?.launch_pick,
            set_pending: (name, next_profile) => {
                pending_new = name;
                pending_profile = next_profile;
                spawning = name;
                emit();
                void ingestor.tick();
            },
            on_spawned: (name) => {
                spawning = null;
                focused = name;
                emit();
                void ingestor.tick();
            },
            legacy_new,
            on_failure: (message) => {
                spawning = null;
                launch_notice.show(message);
            },
        });
    }

    /**
     * The pre-`/spawn` `+ NEW`: ttyd runs with `-a`, so pointing the pane at an
     * unused `?arg=<name>` IS the creation. Only on a bridge that sent no
     * `profiles`, or answered `/spawn` with 404. The whole job is picking a
     * name nothing has claimed — see `RackIngestor.known_sessions`.
     */
    function legacy_new(): void {
        spawning = null;
        pending_profile = null;
        const name = next_session_name(ingestor.known_sessions());
        focused = name;
        pending_new = name;
        // Optimistic row so the list does not visibly lag the pane by a poll.
        emit();
        void ingestor.tick();
    }

    /** `+ WORKTREE`: a plugin route, because a worktree session is spawned with
     *  an explicit `-c <path>` in its own tree — `+ NEW` always lands in the
     *  bridge's one global `--start-dir`. */
    async function make_worktree(value: string | undefined): Promise<void> {
        const request = parse_create_request(value);
        if (!request) return;
        const result = await create_worktree(url, request);
        const notice = create_notice(result);
        if (notice) launch_notice.show(notice);
        // The worktree tick comes FIRST and is not optional. `/create` answers 202 with the work
        // still ahead of it, and the plugin drops its `/state` cache on the way out, so this one
        // round-trip is what puts the `creating` row (and, through `augment_sessions`, its session
        // row) on screen at once. Nothing local mirrors it: the plugin is the single owner of
        // create state, and a second copy here could only disagree with it.
        await worktrees.tick();
        // 202 means a worker is still building this branch, so there is no session to attach to
        // yet: arm the hand-off that focuses it the poll its session appears
        // (`create_island_focus`). 200 is the idempotent "it was already there" and focuses now.
        if (result.ok && result.session && result.status !== 202) {
            island_focus.cancel();
            focused = result.session;
        } else if (result.ok && result.session) {
            island_focus.arm(result.session, Date.now());
        }
        await ingestor.tick();
    }

    /** LAUNCH › LEAD and the decision verbs — the sequence is `lead-client.ts::handle_lead_action`'s. */
    const lead_deps: LeadActionDeps = {
        base_url: url,
        island_focus,
        launch_notice,
        tick_rack: () => void ingestor.tick(),
        tick_lead: () => void lead.tick(),
    };

    async function make_pr(branch: string): Promise<void> {
        await open_pr(url, branch);
        // The plugin answers 202 and finishes on a background thread; this tick
        // just picks up `pr_status: "opening"` so the button can show it.
        await worktrees.tick();
    }

    /** CLEAN on the rack row `session`, whose tree is `row`. The worker kills BOTH the pressed
     *  session and the tree's derived one (they differ when the row joined by cwd), so the pane
     *  lets go of both names before the kill, as `kill` above does. */
    async function make_cleanup(row: WorktreeRow, session: string): Promise<void> {
        if (focused === session || focused === row.session) {
            focused = null;
            emit();
        }
        const notice = cleanup_notice(await clean_up_worktree(url, cleanup_request(row, session)));
        if (notice) launch_notice.show(notice);
        await worktrees.tick();
    }

    const transport: RemoteRackTransport = {
        send: (action, session, value) => {
            if (action === 'metric') { record_metric(value); return; }
            // A REPORT of what the operator did about a suggestion, never an act (`next-step-client.ts`).
            if (action === 'next_step') { report_next_step(session, value); return; }
            if (action === 'new') {
                island_focus.cancel();
                create(typeof value === 'string' ? value : null);
                return;
            }
            if (action === 'devices') {
                devices_open = !devices_open;
                write_devices_open(devices_open);
                emit();
                if (devices_open) void devices.tick();
                return;
            }
            // Session-less actions with their own owner (↺ CLOSED, the search box).
            if (session_less_owners.some((owner) => owner.handle_action(action, value))) return;
            if (action === 'connect') {
                // This document owns the ingestor here — there is no main
                // window to route to — so the unlock lands locally.
                unlock_local_access();
                emit();
                void ingestor.tick();
                return;
            }
            if (action === 'mission') {
                const request = parse_mission_request(value);
                if (request) {
                    void post_mission(url, request).then((result) => {
                        if (!result.ok) launch_notice.show(result.error ?? 'Mission update failed');
                        void missions.tick();
                    });
                }
                return;
            }
            if (action === 'group_control' && session && value) {
                void control_task_group(url, session, value).then(result => {
                    if (!result.ok) launch_notice.show(result.error ?? 'Could not update task group');
                    void worktrees.tick();
                });
                return;
            }
            // LAUNCH › LEAD and the DECISIONS card's verbs, session-less — `lead-client.ts`.
            if (handle_lead_action(action, value, lead_deps)) return;
            if (action === 'worktree') {
                void make_worktree(value);
                return;
            }
            if ((action === 'pr' || action === 'cleanup') && session) {
                // `session` is the rack row's session name; the plugin keys on
                // the BRANCH, so translate through the state we already hold
                // rather than making the caller know both — by name, then by the
                // session's cwd, the join the button was drawn from
                // (`rack-view.ts::worktree_for_session`).
                const snapshot_row = ingestor.last_snapshot()?.sessions.find((r) => r.session === session);
                const row = worktree_for_session(
                    { session, cwd: snapshot_row?.cwd },
                    worktrees.state()?.worktrees,
                );
                if (!row) return;
                if (action === 'pr') void make_pr(row.branch);
                else void make_cleanup(row, session);
                return;
            }
            if (action === 'select' && session) {
                // Any row the user picks outranks a 🏝 launch still on its way.
                island_focus.cancel();
                // A row `/spawn` is still building is not selectable yet — the
                // ordering note in `spawn-client.ts::launch_new` says why.
                if (spawning === session) return;
                focused = session;
                // Moving away from a just-created LEGACY session abandons it —
                // the pane never stayed on it long enough for tmux to create it,
                // so the optimistic row would otherwise linger as a phantom
                // forever. A `/spawn` in flight is built regardless.
                if (pending_new && pending_new !== session && spawning !== pending_new) pending_new = null;
                // Looking at a ✅ is what clears it. This transport keeps its OWN tracker (its
                // ingestor), so the tick re-derives `display` from the cleared tracker now.
                ingestor.mark_seen(session);
                mark_answer_read(session);
                void ingestor.tick();
                emit();
                return;
            }
            if (action === 'kill' && session) {
                void kill(session);
                return;
            }
            const answer = open_work_answer(action);
            if (answer && session) {
                void answer(url, session).then((ok) => {
                    if (ok) void ingestor.tick();
                });
                return;
            }
            const starred = action === 'star' ? parse_star_value(value) : null;
            if (starred !== null && session) {
                void set_star(url, session, starred).then((ok) => {
                    if (ok && !disposed) void ingestor.tick();
                });
                return;
            }
            // Never on a name `/spawn` is still building: tmux has no such session until it answers.
            // The rack locks the box (`RackPayload.spawning`); this is the redundant half.
            if (action === 'rename' && session && typeof value === 'string' && session !== spawning) {
                void rename(session, value);
                return;
            }
            // `render` is handled by the window's own prop/emit path, so it has
            // no meaning here. Dropped silently rather than half-serviced.
        },
        subscribe: (on_payload) => {
            listeners.add(on_payload);
            // Replay immediately so a late subscriber is not blank until the
            // next poll. Gated on having data: an empty replay would
            // move the window off `about:blank` before there is a session to
            // show, which is the ONE-LIVE-TERMINAL rule's trigger condition.
            if (ingestor.last_snapshot()) on_payload(build_payload());
            return () => listeners.delete(on_payload);
        },
        refresh: () => void ingestor.tick(),
        dispose: () => {
            disposed = true;
            listeners.clear();
            launch_notice.clear();
            ingestor.stop();
            worktrees.stop();
            dev_services.stop();
            devices.stop();
            missions.stop();
            lead.stop();
            chat_search.dispose();
        },
    };

    // Kick off. The first poll is immediate so the window paints real rows
    // rather than an empty rack for one interval.
    ingestor.start_polling();
    worktrees.start();
    dev_services.start();
    devices.start();
    missions.start();
    lead.start();

    return transport;
}
