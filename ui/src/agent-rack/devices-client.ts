/**
 * The rack's read of `GET /plugin/devices/state`: the DEVICES section's CPU and memory per machine, and this
 * machine's resource lease (`device-readout.ts` `DeviceLease`).
 *
 * Polled only while the section is OPEN and the rack is on screen (each owner's `wanted`), because
 * the plugin samples only while it is being read: a closed section is what lets this machine's sampler
 * and the compute hosts' ssh streams wind down (`plugins/devices/devices_plugin/sampler.py`).
 *
 * Free of the DOM, like `dev-services-client.ts`: every rack payload owner loads it.
 */
import { devices_signature, parse_devices, type DevicesState } from '../engine/terminal/device-readout.ts';
import { create_plugin_poller, fetch_plugin_state, type PluginPoller } from './plugin-poller.ts';
import type { BaseUrl, FetchImpl, PollerIoDeps } from './worktree-client.ts';

/** A little over the plugin's 2 s sample, so most polls carry a fresh one. */
export const DEVICES_POLL_MS = 3000;

export function fetch_devices(base_url: BaseUrl, fetch_impl?: FetchImpl): Promise<DevicesState | null> {
    return fetch_plugin_state(base_url, '/plugin/devices/state', parse_devices, fetch_impl);
}

export interface DevicesPollerDeps extends PollerIoDeps {
    base_url: BaseUrl;
    on_change: () => void;
    /** Section open AND the rack on screen; read every tick. */
    wanted: () => boolean;
    poll_interval_ms?: number;
}

export type DevicesPoller = PluginPoller<DevicesState>;

export function create_devices_poller(deps: DevicesPollerDeps): DevicesPoller {
    return create_plugin_poller({
        ...deps,
        fetch: (do_fetch) => fetch_devices(deps.base_url, do_fetch),
        signature: devices_signature,
        poll_interval_ms: deps.poll_interval_ms ?? DEVICES_POLL_MS,
    });
}

// ── open / closed, per device ───────────────────────────────────────────────

const OPEN_KEY = 'tldrack:rack-devices-open';

/** Open unless this device closed it. Storage can throw (private window, blocked site data). */
export function read_devices_open(storage: Pick<Storage, 'getItem'> | null = default_storage()): boolean {
    try {
        return storage?.getItem(OPEN_KEY) !== '0';
    } catch {
        return true;
    }
}

export function write_devices_open(open: boolean, storage: Pick<Storage, 'setItem'> | null = default_storage()): void {
    try {
        storage?.setItem(OPEN_KEY, open ? '1' : '0');
    } catch {
        // The toggle still works for this page's lifetime; it just will not be remembered.
    }
}

function default_storage(): Storage | null {
    try {
        return typeof localStorage === 'undefined' ? null : localStorage;
    } catch {
        return null;
    }
}
