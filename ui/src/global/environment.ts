/**
 * The one build-time setting the pages read.
 *
 * `VITE_ALLOWED_REMOTE_HOSTS`: comma-separated machines a rack page may be pointed at with
 * `?remote=<host>`, for example `mymac.tail1234.ts.net`. Parsed by
 * `agent-rack/rack-remote.ts` `parse_host_list`.
 *
 * It is an exact-host pin, and empty means `?remote=` reaches loopback only. A suffix rule such
 * as "any tailnet name" is wrong: a tailnet can publish a machine to the public internet under
 * the same suffix, so an attacker's machine could be named in a `?remote=` link and framed as the
 * operator's own terminal. An unset value must never fall back to "any host".
 *
 * A page the bridge serves needs none of this: it talks to the origin it came from
 * (`rack-remote.ts` `same_origin_config`).
 */
export const ALLOWED_REMOTE_HOSTS: string = import.meta.env?.VITE_ALLOWED_REMOTE_HOSTS ?? '';
