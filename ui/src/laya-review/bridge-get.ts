/**
 * GET one bridge plugin's JSON: the read every plugin client of the review page makes.
 *
 * ONE OWNER of the sequence fetch → status → error body → unreachable. Never throws; each client maps
 * the result into its own view.
 *
 * A shut local-access gate is reported as itself, not as a dead bridge, so the remedy shown is the
 * right one (`local-network-gate.ts` owns the gate).
 */
import { bridge_unreachable_message, read_bridge_error } from './bridge-error.ts';
import { BRIDGE_DOWN_HINT, is_local_access_blocked, local_fetch } from '../engine/terminal/local-network-gate.ts';

/**
 * `status` is the HTTP status, 0 when no answer came (unreachable, or the local-access gate) — the GET
 * twin of `BridgePostResult.status`, so a caller can tell a route the running plugin lacks (404) from a
 * refusal. `body` is a failure's parsed JSON (null when it had none), the twin of `BridgePostResult.body`,
 * for a caller that keys on a field beyond `{error, hint}` — a refusal `code`. Most callers ignore both.
 */
export type BridgeGet = { ok: true; body: any } | { ok: false; error: string; hint?: string; status: number; body: unknown };

export async function get_bridge_json(url: string, timeout_ms: number): Promise<BridgeGet> {
    try {
        const response = await local_fetch(url, { signal: AbortSignal.timeout(timeout_ms) });
        if (!response.ok) {
            // A clone, so `read_bridge_error` still reads the body it owns.
            const body: unknown = await response
                .clone()
                .json()
                .catch(() => null);
            return { ok: false, status: response.status, body, ...(await read_bridge_error(response)) };
        }
        return { ok: true, body: await response.json() };
    } catch (error) {
        if (is_local_access_blocked(error)) return { ok: false, error: error.message, status: 0, body: null };
        return { ok: false, error: bridge_unreachable_message(error, url, timeout_ms), hint: BRIDGE_DOWN_HINT, status: 0, body: null };
    }
}
