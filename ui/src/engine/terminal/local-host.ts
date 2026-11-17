/**
 * The name this machine goes by among the bridge's hosts, and how a host's name is shown.
 *
 * Import-free, so `device-readout.ts` and any host surface that lists machines read one label
 * rule. The name itself is the bridge core's `LOCAL` (its `local_llm` host
 * list): every host list it serves calls this machine that.
 */

/** The name the plugin gives this machine among its hosts. */
export const LOCAL_HOST = 'local';

export function host_label(name: string): string {
    return name === LOCAL_HOST ? 'LOCAL' : name.toUpperCase();
}
