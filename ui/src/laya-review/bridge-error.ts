/**
 * How a plugin client reads a bridge plugin's failure body, and what it says when the
 * bridge gave none. ONE OWNER, for `bridge-get.ts`, `bridge-post.ts` and the Laya
 * client. Import-free on purpose: nothing here needs more than a `Response`.
 */

/**
 * Prefer the plugin's own `{error, hint}` over an HTTP status line — it is written for a human
 * and usually names the exact command that fixes the situation.
 */
export async function read_bridge_error(response: Response): Promise<{ error: string; hint?: string }> {
    try {
        const body = await response.json();
        if (body && typeof body.error === 'string') {
            const hint = typeof body.hint === 'string' ? body.hint : unmounted_plugin_hint(response.status, body.error);
            return { error: body.error, hint };
        }
    } catch {
        /* fall through to the status line */
    }
    return { error: `bridge responded ${response.status}` };
}

/** The bridge host's own 404 for a plugin it never mounted (the bridge core's `dispatch_plugin`). */
const UNMOUNTED_PLUGIN = /^unknown plugin: ([a-z0-9][a-z0-9-]*)$/;

/**
 * The remedy for a plugin the running bridge never loaded. The plugin cannot say it — it is not running —
 * and every plugin is enabled the same way: one entry in the operator's `plugins.json`.
 */
export function unmounted_plugin_hint(status: number, error: string): string | undefined {
    const plugin = status === 404 ? UNMOUNTED_PLUGIN.exec(error)?.[1] : undefined;
    return plugin ? enable_plugin_hint(plugin) : undefined;
}

/** How any plugin is enabled. Also what a page says for one it knows is missing. */
export function enable_plugin_hint(plugin: string): string {
    return `Add plugins/${plugin} to ~/.config/tldrack/plugins.json, then restart the bridge`;
}

/**
 * What a panel says when a bridge request got no answer: the route it tried, and why — a timeout (the
 * bridge is up but slow or wedged) or the thrown error's own words. The cause is kept because a bare
 * "not reachable" shows a page bug (a throw before `fetch` ever ran) as a dead bridge.
 * A shut local-access gate is the caller's check, reported as itself before this.
 */
export function bridge_unreachable_message(error: unknown, url: string, timeout_ms: number): string {
    const where = url.replace(/^https?:\/\//, '').replace(/\?.*$/, '');
    if (error instanceof DOMException && error.name === 'TimeoutError') {
        return `The bridge did not answer ${where} within ${Math.round(timeout_ms / 1000)}s`;
    }
    const detail = error instanceof Error && error.message ? ` (${error.message})` : '';
    return `Local bridge is not reachable on ${where}${detail}`;
}
