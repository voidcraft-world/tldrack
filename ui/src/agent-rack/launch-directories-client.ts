/** Directory Settings travel with the rack surface, including framed and bus mounts. Launches
 * still go to their existing pane owner; only the selected cwd is added to the `new` argument. */
import { launch_directories_schema, type LaunchDirectories } from '@tldrack/schemas/launch-directories.ts';
import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import type { RackPayload, RackTransport } from './rack-transport.ts';
import type { BaseUrl, FetchImpl } from './worktree-client.ts';

export interface LaunchDirectoriesView {
    state: LaunchDirectories | null;
    saving: boolean;
    error: string | null;
    saved: boolean;
}

export function with_launch_directories(
    base: RackTransport,
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): RackTransport {
    const listeners = new Set<(payload: RackPayload) => void>();
    let last: RackPayload | null = null;
    let view: LaunchDirectoriesView = { state: null, saving: false, error: null, saved: false };
    let reading = false;
    let checked = -Infinity;
    let generation = 0;
    let unsubscribe: (() => void) | null = null;

    function emit(): void {
        if (!last) return;
        for (const listener of listeners) listener({ ...last, launch_directories: view });
    }

    async function request(value?: string): Promise<void> {
        const saving = value !== undefined;
        if (saving && view.saving) return;
        if (!saving && (reading || view.saving || Date.now() - checked < 5000)) return;
        const version = ++generation;
        if (saving) view = { ...view, saving: true, error: null, saved: false };
        else reading = true;
        checked = Date.now();
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
            if (version === generation) view = { state: parsed.data, saving: false, error: null, saved: saving || view.saved };
        } catch (error) {
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
            if (!unsubscribe) unsubscribe = base.subscribe((payload) => {
                last = payload;
                emit();
                if (payload.reachable) void request();
            });
            else if (last) listener({ ...last, launch_directories: view });
            return () => {
                listeners.delete(listener);
                if (!listeners.size) {
                    unsubscribe?.();
                    unsubscribe = null;
                    generation += 1;
                }
            };
        },
    };
}
