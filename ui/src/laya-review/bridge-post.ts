/**
 * POST to one bridge plugin route: the write every plugin client of the review page makes.
 *
 * ONE OWNER, the twin of `bridge-get.ts`, of the sequence JSON body → the plugin's one-line `message`
 * → the restart hint on 426/404 → a clip → the local-access gate or a dead bridge. Never throws; each
 * client keeps its own routes, timeouts and clip length.
 *
 * A plugin answers `{ok, plugin, message[, code, error]}`, so a 200 whose body says `ok: false` is a
 * refusal, not a success.
 */
import { bridge_unreachable_message } from './bridge-error.ts';
import { is_local_access_blocked, local_fetch } from '../engine/terminal/local-network-gate.ts';

export interface BridgePostResult {
    ok: boolean;
    /** 0 when the request never got an answer. */
    status: number;
    /** One line for beside the button: the plugin's own `message` (else `error`), clipped. */
    message: string;
    /** The parsed JSON body, unvalidated (null when there was none, or it was not JSON) — for a client that `safeParse`s its route's own shape. */
    body: unknown;
}

/** What a 426 or 404 means here: the code on disk is newer than the bridge that is running. */
export const BRIDGE_PREDATES_MESSAGE = 'The running bridge predates this button. Restart the bridge.';

/** `headers` adds to the JSON content type — e.g. the decision verbs' `X-VC-Decision-Secret`. */
export async function post_bridge_json(
    url: string,
    body: unknown,
    timeout_ms: number,
    message_chars: number,
    headers: Readonly<Record<string, string>> = {},
): Promise<BridgePostResult> {
    try {
        const response = await local_fetch(url, {
            method: 'POST',
            headers: { ...headers, 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: AbortSignal.timeout(timeout_ms),
        });
        let message = '';
        let refused = false;
        let parsed: unknown = null;
        try {
            parsed = await response.json();
            const reply = parsed as { message?: unknown; error?: unknown; ok?: unknown } | null;
            const text = reply?.message ?? reply?.error;
            if (typeof text === 'string') message = text;
            refused = reply?.ok === false;
        } catch {
            /* an empty or non-JSON body: the status line below stands in */
        }
        // 426: the bridge HOST predates plugin request headers; 404: the PLUGIN predates this
        // route. Either way the code on disk is newer than what is running.
        if (response.status === 426 || response.status === 404) message = BRIDGE_PREDATES_MESSAGE;
        const ok = response.ok && !refused;
        return {
            ok,
            status: response.status,
            message: clip(message || (ok ? 'sent' : `bridge responded ${response.status}`), message_chars),
            body: parsed,
        };
    } catch (error) {
        const message = is_local_access_blocked(error) ? error.message : bridge_unreachable_message(error, url, timeout_ms);
        return { ok: false, status: 0, message, body: null };
    }
}

function clip(text: string, chars: number): string {
    return text.length > chars ? `${text.slice(0, chars - 1)}…` : text;
}
