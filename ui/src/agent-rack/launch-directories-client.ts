/** Directory Settings travel with the rack surface, including framed and bus mounts. Launches
 * still go to their existing pane owner; only the selected cwd is added to the `new` argument. */
import { launch_directories_schema, type LaunchDirectories } from '@tldrack/schemas/launch-directories.ts';
import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import { watch_page_return, type PageReturn } from './page-return.ts';
import type { RackPayload, RackTransport } from './rack-transport.ts';
import type { BaseUrl, FetchImpl } from './worktree-client.ts';

export interface LaunchDirectoriesView {
    state: LaunchDirectories | null;
    saving: boolean;
    error: string | null;
    saved: boolean;
}

/** The least time between two reads from a trigger that can repeat with no news behind it. */
const REPEAT_READ_MS = 5000;

/** The page this overlay runs on, as a test hands it in. */
export interface LaunchDirectoriesEnv {
    watch_return?: (on_return: (how: PageReturn) => void) => () => void;
    now?: () => number;
}

/**
 * WHEN THE LIST IS READ. It changes when somebody saves it, which is rare, so there is no timer:
 * the read used to ride every payload (one `GET` each 5 s for as long as a rack page was open,
 * tray closed and tab hidden included) to fetch a list that had not moved. It is read
 *
 *   - once when the bridge ARRIVES: the first reachable payload, and each return from unreachable,
 *     because the bridge that came back may be a restarted one with another list;
 *   - by a save, whose own answer IS the saved list, so nothing is read after it;
 *   - when the operator comes back to the page (`page-return.ts`), which is what shows a list
 *     saved on the settings page in another tab, or on another device. A `visible` return always
 *     reads; a `focus` one at most each `REPEAT_READ_MS`, since a click out of the terminal is one;
 *   - again, at that same pace, while a read is OWED: the last one failed, so the tray holds an
 *     error or nothing. One dropped request must not leave it that way until the next return.
 */
export function with_launch_directories(
    base: RackTransport,
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
    env: LaunchDirectoriesEnv = {},
): RackTransport {
    const watch_return = env.watch_return ?? ((on_return) => watch_page_return(on_return));
    const now = env.now ?? (() => Date.now());
    const listeners = new Set<(payload: RackPayload) => void>();
    let last: RackPayload | null = null;
    let view: LaunchDirectoriesView = { state: null, saving: false, error: null, saved: false };
    let reading = false;
    let checked = -Infinity;
    /** A read failed and none has landed since. */
    let owed = false;
    let generation = 0;
    let unsubscribe: (() => void) | null = null;

    function emit(): void {
        if (!last) return;
        for (const listener of listeners) listener({ ...last, launch_directories: view });
    }

    /** A save when `value` is given, else a read; `repeatable` marks a read whose trigger can
     *  fire again with no news (the note above owns which). */
    async function request(value?: string, repeatable = false): Promise<void> {
        const saving = value !== undefined;
        if (saving && view.saving) return;
        if (!saving && (reading || view.saving || (repeatable && now() - checked < REPEAT_READ_MS))) return;
        const version = ++generation;
        if (saving) view = { ...view, saving: true, error: null, saved: false };
        else reading = true;
        checked = now();
        emit();
        try {
            const response = await fetch_impl(base_url('/launch-directories'), {
                ...(saving ? { method: 'POST', body: value, headers: { 'Content-Type': 'application/json' } } : {}),
                signal: AbortSignal.timeout(5000),
            });
            if (!response.ok) {
                if (response.status === 404) throw new Error('Restart the bridge to configure launch directories');
                const body = await response.json().catch(() => null);
                throw new Error(typeof body?.error === 'string' ? body.error : 'Could not load launch directories');
            }
            const parsed = launch_directories_schema.safeParse(await response.json());
            if (!parsed.success) throw new Error('The bridge returned invalid launch directories');
            if (version === generation) {
                view = { state: parsed.data, saving: false, error: null, saved: saving || view.saved };
                owed = false;
            }
        } catch (error) {
            if (!saving) owed = true;
            if (version === generation) view = { ...view, saving: false, saved: false,
                error: error instanceof Error ? error.message.slice(0, 240) : 'Launch directories are unavailable' };
        } finally {
            if (!saving) reading = false;
            emit();
        }
    }

    return {
        send(action, session, value) {
            if (action === 'new' && value?.startsWith('{') && !last?.launch_cwd) {
                view = { ...view, error: 'Reload the parent app to launch in a selected directory' };
                emit();
                return;
            }
            if (action === 'directories') {
                if (value !== undefined) void request(value);
            } else base.send(action, session, value);
        },
        subscribe(listener) {
            listeners.add(listener);
            if (!unsubscribe) {
                const stop_payloads = base.subscribe((payload) => {
                    const arrived = payload.reachable && !last?.reachable;
                    last = payload;
                    emit();
                    if (arrived) void request();
                    else if (payload.reachable && owed) void request(undefined, true);
                });
                const stop_return = watch_return((how) => {
                    if (last?.reachable) void request(undefined, how === 'focus');
                });
                unsubscribe = () => {
                    stop_payloads();
                    stop_return();
                };
            } else if (last) listener({ ...last, launch_directories: view });
            return () => {
                listeners.delete(listener);
                if (!listeners.size) {
                    unsubscribe?.();
                    unsubscribe = null;
                    generation += 1;
                    owed = true; // the bump may have thrown a read away; the next subscriber gets one
                }
            };
        },
    };
}
