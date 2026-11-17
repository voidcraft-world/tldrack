/**
 * The rack's read of `GET /plugin/dev-services/state` — what puts `● VITE :5173`
 * on a `svc-vite` row instead of "model unknown".
 *
 * Read-only on purpose: the rack's actions on a service row are the ordinary
 * row actions (open, kill). START / RESTART / ADOPT belong to whatever surface the
 * supervisor plugin's host gives them; a copy of those buttons here would be one
 * more face to keep in step.
 *
 * Free of the DOM for the same reason as `worktree-client.ts`: every rack payload
 * owner loads it, and it runs under bare `node --test`.
 */
import { parse_dev_services, service_readout, type DevServiceRow } from '../engine/terminal/service-sessions.ts';
import { create_plugin_poller, fetch_plugin_state, type PluginPoller } from './plugin-poller.ts';
import type { BaseUrl, FetchImpl, PollerIoDeps } from './worktree-client.ts';

/**
 * Idle cadence. A dev server changes state when someone restarts it, so the
 * rack's 5 s worktree cadence is already generous; there is no fast tier.
 */
export const DEV_SERVICES_POLL_MS = 5000;

/** The service rows, or null when the plugin is not mounted or the bridge did not answer. */
export function fetch_dev_services(base_url: BaseUrl, fetch_impl?: FetchImpl): Promise<DevServiceRow[] | null> {
    return fetch_plugin_state(base_url, '/plugin/dev-services/state', parse_dev_services, fetch_impl);
}

export interface DevServicesPollerDeps extends PollerIoDeps {
    base_url: BaseUrl;
    on_change: () => void;
    /**
     * Is there a row to decorate? Read on every tick: a rack with no `svc-*`
     * session costs this poller nothing, so a machine without the plugin never
     * sends it a single request (and never logs a 404 per poll).
     */
    wanted: () => boolean;
    poll_interval_ms?: number;
}

export type DevServicesPoller = PluginPoller<DevServiceRow[]>;

/**
 * What the rack can SEE of the answer. A running Vite's `log_tail` moves with
 * every HMR line, so signing the whole document would re-render the rack each
 * poll for text no row draws; the readouts are the whole visible surface.
 */
function rack_signature(rows: DevServiceRow[] | null): string {
    return rows ? JSON.stringify(rows.map((row) => [row.session, service_readout(row)])) : '';
}

export function create_dev_services_poller(deps: DevServicesPollerDeps): DevServicesPoller {
    return create_plugin_poller({
        ...deps,
        fetch: (do_fetch) => fetch_dev_services(deps.base_url, do_fetch),
        signature: rack_signature,
        poll_interval_ms: deps.poll_interval_ms ?? DEV_SERVICES_POLL_MS,
    });
}
