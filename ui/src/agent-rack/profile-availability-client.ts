import { z } from 'zod';

const profile_availability_schema = z.object({ paused: z.array(z.string().regex(/^[a-z0-9][a-z0-9-]{0,31}$/)).max(256), error: z.string().nullable() });
type ProfileAvailability = z.infer<typeof profile_availability_schema>;
import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import type { RackPayload, RackTransport } from './rack-transport.ts';
import type { BaseUrl, FetchImpl } from './worktree-client.ts';

export interface ProfileAvailabilityView {
    state: ProfileAvailability | null;
    saving: boolean;
    error: string | null;
    saved: boolean;
}

export function with_profile_availability(
    base: RackTransport,
    base_url: BaseUrl,
    fetch_impl: FetchImpl = (...args) => local_fetch(...args),
): RackTransport {
    const listeners = new Set<(payload: RackPayload) => void>();
    let last: RackPayload | null = null;
    let view: ProfileAvailabilityView = { state: null, saving: false, error: null, saved: false };
    let reading = false;
    let checked = -Infinity;
    let generation = 0;
    let unsubscribe: (() => void) | null = null;

    function emit(): void {
        if (!last) return;
        for (const listener of listeners) listener({ ...last, profile_availability: view });
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
            const response = await fetch_impl(base_url('/profile-availability'), {
                ...(saving ? { method: 'POST', body: value, headers: { 'Content-Type': 'application/json' } } : {}),
                signal: AbortSignal.timeout(5000),
            });
            if (!response.ok) {
                if (response.status === 404) throw new Error('Restart the bridge to pause launch profiles');
                const body = await response.json().catch(() => null);
                throw new Error(typeof body?.error === 'string' ? body.error : 'Could not load launch profiles');
            }
            const parsed = profile_availability_schema.safeParse(await response.json());
            if (!parsed.success) throw new Error('The bridge returned invalid profile availability');
            if (version === generation) view = { state: parsed.data, saving: false, error: null, saved: saving || view.saved };
        } catch (error) {
            if (version === generation) view = { ...view, saving: false, saved: false,
                error: error instanceof Error ? error.message.slice(0, 240) : 'Launch profiles are unavailable' };
        } finally {
            if (!saving) reading = false;
            emit();
        }
    }

    return {
        send(action, session, value) {
            if (action === 'profile_pause') {
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
            else if (last) listener({ ...last, profile_availability: view });
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
