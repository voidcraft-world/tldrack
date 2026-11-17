import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import { create_metric_delivery } from './rack-metrics.ts';

/** Runs in the transport OWNER, so the iframe uses the owner's local-network permission. */
export function create_rack_metric_delivery(base_url: (path: string) => string) {
    return create_metric_delivery(async event => {
        const response = await local_fetch(base_url('/rack-metrics/event'), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(event), signal: AbortSignal.timeout(2500), keepalive: true,
        });
        // Permanent rejection / old bridge: discard rather than block every subsequent event.
        return response.ok || (response.status >= 400 && response.status < 500);
    });
}
