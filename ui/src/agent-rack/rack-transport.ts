/**
 * The seam that lets ONE `AgentRackComponent` render in very different places:
 *
 *   • inside a frame of a host application, talking to the host over
 *     `window.parent.postMessage` (`create_iframe_transport`, below),
 *   • inside a satellite window, talking to a host's main window over the window bus
 *     (`rack-bus-transport.ts`), and
 *   • on its own page, talking to a bridge over HTTP (`rack-http-transport.ts`).
 *
 * Everything the component needs is one `RackPayload` in and one action out, so the difference
 * between those worlds is exactly two functions.
 *
 * DELIBERATELY FREE of the window bus and of any fetch. A framed rack repaints inside its
 * host's scene, and the host pays for that bundle on every frame, so each heavier transport
 * lives in its own file and is imported only by the mount that uses it.
 */
import type { RackPayload } from '../engine/terminal/rack-view.ts';
import { message_is } from '../events/messages.ts';

/** host → rack frame: the current rack and which session is in the live pane. A host that embeds
 *  the rack in a frame posts this; both strings are part of that contract (the rack reads each under
 *  its legacy name too: `events/messages.ts` `message_is`). */
export const MESSAGE_AGENT_RACK_DATA = 'tldrack-agent-rack-data';
/** rack frame → host: `{ action, session?, value? }`, in the vocabulary of `RackAction` below. */
export const MESSAGE_AGENT_RACK_ACTION = 'tldrack-agent-rack-action';

/** `RackPayload` moved to `engine/terminal/rack-view.ts`, beside the one
 *  function that builds it — the same placement `RackSession` already has in
 *  `multi-terminal-layout.ts`. Re-exported so no importer moved, and so this
 *  file stays what its header says it is: the transport SEAM, not the data. */
export type { RackPayload };

/** NOTE this is NOT `RackActionId` (multi-terminal-layout.ts), which is the set
 *  of per-ROW actions (`kill` last). These are whole-rack intents, dispatched by
 *  whoever holds the other end of the transport.
 *  `render` is session-less, like `new`. */
export type RackAction =
    | 'metric'
    /** Save Settings' launch directories; handled by launch-directories-client.ts. */
    | 'directories'
    | 'select' | 'kill' | 'rename' | 'render'
    /** Start a session. `value` is a profile id or JSON `{profile, cwd}` when the
     *  payload advertises `launch_cwd`. `POST /spawn` on a bridge with `profiles`,
     *  legacy attach-or-create otherwise (never for an explicit directory).
     *  Session-less: the host picks the name (`spawn-client.ts::launch_new`). */
    | 'new'
    /** Create a git-worktree-backed session. `value` is a JSON
     *  `{branch, prompt?, profile?}` — see `worktree-client.ts::parse_create_request` for
     *  why a two-field form rides as one string. Session-less, like `new`. */
    | 'worktree'
    /** Commit, push and open a PR for the worktree behind `session`. */
    | 'pr'
    /** CLEAN: the worktree behind `session` has landed — kill its session(s),
     *  remove the tree, delete the branch (`worktree-client.ts::clean_up_worktree`). */
    | 'cleanup'
    | 'group_control'
    /**
     * Open this device's local-network gate. Session-less, like `new`.
     *
     * It MUST cross the transport rather than being handled where the button
     * lives: `local-network-gate.ts` keeps the opt-in in module state and
     * registers no `storage` listener, so `unlock_local_access()` called inside
     * a framed rack or a satellite window would open a gate no fetch ever
     * consults. The document that owns the ingestor is the one that must unlock.
     */
    | 'connect'
    /** ✗ NOT OPEN: clear Laya's ⚠️ on `session` until its next turn end, and grade it
     *  (`POST /open-work/dismiss`, `open-work-client.ts`). */
    | 'dismiss_open_work'
    /** ✓ STILL OPEN: grade Laya's ⚠️ on `session` as right; it stays (`POST /open-work/confirm`). */
    | 'confirm_open_work'
    /** REPORT what the operator did about `session`'s suggested next step (shown, dismissed, or a
     *  control that fired). `value` is a JSON `NextStepEvent`. Never an act: the control already
     *  fired through its own handler (`next-step-client.ts`). */
    | 'next_step'
    /** ⏸ NOT NOW: park the ⚠️'s loop — seen, nothing posted (`POST /open-work/snooze`). */
    | 'snooze_open_work'
    /** Open or close the DEVICES section. Session-less, and it crosses the transport
     *  because the document that owns the poller is the one that must stop polling. */
    | 'devices'
    /** One mission write. `value` is a JSON `MissionRequest` (`missions-client.ts`). Session-less,
     *  and every member hears it through its hooks, so nothing is typed into a pane. */
    | 'mission'
    /** Open or close the ↺ CLOSED list. Session-less; the host owns it (`closed-client.ts`). */
    | 'closed'
    /** Bring one closed session back. `value` is the close's id from `GET /closed`; the session
     *  name is the BRIDGE's pick (`POST /reopen`), so it rides no `session`. */
    | 'reopen'
    /** Bring back sessions a reboot took down (`POST /restore`). `value` is one session name, or
     *  absent for every row that can come back. Session-less: the name rides `value`, because the
     *  session does not exist yet. */
    | 'restore'
    /** Stop offering every remaining restore (`POST /restore/forget`). Session-less. */
    | 'restore_forget'
    /** LAUNCH › LEAD: start a lead session. `value` is a JSON `{profile, resume?}` (`"auto"` or a
     *  Claude profile id). Session-less; the bridge names the session and the owner focuses it once
     *  the snapshot lists it (`lead-client.ts::launch_lead`). */
    | 'lead'
    /** One operator verb on a lead's decision. `value` is a JSON `DecisionRequest`
     *  (`lead-client.ts::parse_decision_request`); the payload owner adds the decision secret. */
    | 'lead_decision'
    /** Star `session`, or clear its star. `value` is the state asked for, `'on'` or `'off'`, never a
     *  toggle (`star-client.ts::star_value`); the payload owner posts it (`POST /session-star`). */
    | 'star'
    /** The search box's text, on every keystroke (`''` when cleared). Session-less; the payload
     *  owner asks which sessions SAID it and carries the answer back as `chat_search`
     *  (`chat-search.ts`). */
    | 'search';

export interface RackTransport {
    /** `value` is the action's argument: `rename`'s new session name, `new`'s
     *  LAUNCH profile id (absent → the bridge's default profile; ignored by a
     *  bridge without `/spawn`), `worktree`'s JSON request, `render`'s options.
     *  Kept as one optional string rather than a per-action payload type:
     *  it crosses a postMessage boundary AND a BroadcastChannel, and every hop
     *  re-validates `typeof === 'string'`, which a union would only complicate. */
    send: (action: RackAction, session?: string, value?: string) => void;
    /** Returns an unsubscribe function. */
    subscribe: (on_payload: (payload: RackPayload) => void) => () => void;
}

/**
 * The frame transport: a framed rack and its host, over `window.parent`.
 *
 * `'*'` as the target origin: the page is same-origin with its host and the
 * payload carries nothing secret.
 */
export function create_iframe_transport(): RackTransport {
    return {
        send: (action, session, value) => {
            window.parent.postMessage({ type: MESSAGE_AGENT_RACK_ACTION, action, session, value }, '*');
        },
        subscribe: (on_payload) => {
            const listener = (event: MessageEvent): void => {
                if (!message_is(event.data, MESSAGE_AGENT_RACK_DATA)) return;
                on_payload(event.data.payload as RackPayload);
            };
            window.addEventListener('message', listener);
            return () => window.removeEventListener('message', listener);
        },
    };
}

/** A transport whose payloads carry a local overlay, re-delivered on demand. */
export interface OverlaidRackTransport extends RackTransport {
    /** The overlay changed: re-deliver the last payload to every subscriber. */
    refresh_overlay: () => void;
}

/**
 * `base`, with `overlay()` spread onto every payload it delivers.
 *
 * For a field only the CONSUMING document knows — today `pane_loading`, which
 * the rack page's own terminal iframe decides (`agent-rack/pane-settle.ts`). A
 * framed rack needs none of this: its pane owner is the host that posts the
 * payload, and it overlays there. `overlay()` returns only the keys it sets, so
 * an absent field stays absent rather than becoming an explicit `undefined`.
 */
export function with_payload_overlay(
    base: RackTransport,
    overlay: () => Partial<RackPayload>,
): OverlaidRackTransport {
    const listeners = new Set<(payload: RackPayload) => void>();
    let last: RackPayload | null = null;
    return {
        send: (action, session, value) => base.send(action, session, value),
        subscribe: (on_payload) => {
            listeners.add(on_payload);
            const unsubscribe = base.subscribe((next) => {
                last = next;
                on_payload({ ...next, ...overlay() });
            });
            return () => {
                listeners.delete(on_payload);
                unsubscribe();
            };
        },
        refresh_overlay: () => {
            if (!last) return;
            const next = { ...last, ...overlay() };
            for (const listener of listeners) listener(next);
        },
    };
}
