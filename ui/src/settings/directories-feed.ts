/**
 * The DIRECTORIES tab's feed: the rack's own launch-directory client, with no rack under it.
 *
 * The read, the save, when the list is read again (a return to the page included) and the rule
 * that a read started before a save can never overwrite it are not restated: owned by
 * `agent-rack/launch-directories-client.ts` — see the note there. That client overlays a rack
 * transport, so this file hands it the smallest one there is: a rack with no sessions whose
 * bridge has just arrived, which is the client's cue to read once.
 *
 * RETRY IS AN ARRIVAL. The client reads when its bridge goes from unreachable to reachable, at
 * once and unpaced, because the bridge that came back may be another one. `refresh` replays that
 * edge, so a RETRY asks now and the pacing of the client's own repeat reads is left alone.
 *
 * `report` hears what each request says about the bridge as a whole: a failure when no answer
 * came, null when one did. The thrown error is reworded on the way through, so the form's own
 * line names the route and the cause, not the browser's bare "Failed to fetch".
 */
import { bridge_origin } from '../config/bridge-origin.ts';
import { BRIDGE_DOWN_HINT, is_local_access_blocked, local_fetch } from '../engine/terminal/local-network-gate.ts';
import {
    with_launch_directories,
    type LaunchDirectoriesEnv,
    type LaunchDirectoriesView,
} from '../agent-rack/launch-directories-client.ts';
import type { RackPayload, RackTransport } from '../agent-rack/rack-transport.ts';
import type { FetchImpl } from '../agent-rack/worktree-client.ts';
import { bridge_unreachable_message } from '../laya-review/bridge-error.ts';
import type { BridgeFailure } from './bridge-call.ts';

export interface DirectoriesFeed {
    /** Returns the unsubscribe function. */
    subscribe(listener: (view: LaunchDirectoriesView) => void): () => void;
    /** `value` is the JSON `LaunchDirectorySettings.vue` emits on SAVE. */
    save(value: string): void;
    /** Read again now. A read or a save already out is left to finish. */
    refresh(): void;
}

/** The client's own request timeout, for the sentence a timed-out read shows. */
const REQUEST_TIMEOUT_MS = 5_000;

const NO_RACK: RackPayload = { sessions: [], focused: null, max_sessions: 0, summary: '', reachable: true };

export function create_directories_feed(
    report: (down: BridgeFailure | null) => void,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
    env: LaunchDirectoriesEnv = {},
): DirectoriesFeed {
    let to_client: ((payload: RackPayload) => void) | null = null;
    const base: RackTransport = {
        send: () => {},
        subscribe: (listener) => {
            to_client = listener;
            listener(NO_RACK);
            return () => {
                to_client = null;
            };
        },
    };
    const watched: FetchImpl = async (input, init) => {
        try {
            const response = await fetch_impl(input, init);
            report(null);
            return response;
        } catch (error) {
            const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
            const gated = is_local_access_blocked(error);
            const message = gated ? error.message : bridge_unreachable_message(error, url, REQUEST_TIMEOUT_MS);
            report({ ok: false, kind: 'unreachable', status: 0, error: message, ...(gated ? {} : { hint: BRIDGE_DOWN_HINT }), body: null });
            throw new Error(message, { cause: error });
        }
    };
    const transport = with_launch_directories(base, (path) => `${bridge_origin()}${path}`, watched, env);
    return {
        subscribe: (listener) =>
            transport.subscribe((payload) => {
                if (payload.launch_directories) listener(payload.launch_directories);
            }),
        save: (value) => transport.send('directories', undefined, value),
        refresh: () => {
            to_client?.({ ...NO_RACK, reachable: false });
            to_client?.(NO_RACK);
        },
    };
}
