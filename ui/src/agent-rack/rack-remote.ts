/**
 * Which bridge a rack page talks to, and the allowlist that decides whether it may.
 *
 * A rack page needs two ORIGINS: one serving ttyd, one serving the bridge. There are two ways to
 * get them, and `rack-http-transport.ts` owns what is then done with them.
 *
 * 1. THE PAGE'S OWN ORIGIN (`same_origin_config`). The bridge serves the page, so the page talks
 *    to where it came from. Nothing is named in a URL, so nothing needs a pin.
 * 2. `?remote=<host>` (`resolve_remote_config`): another machine, typically a phone pointed at a
 *    desktop over a tailnet. This is the path the allowlist guards.
 *
 * WHY THE ALLOWLIST IS NOT OPTIONAL
 *
 * A rack page has no login: the bridge's own guards are the gate. So when the page is hosted
 * somewhere public, an origin taken from the URL comes from an attacker as easily as from the
 * operator. An unvalidated `?remote=` would make the hosted page a phishing kit:
 *
 *     https://rack.example/?remote=https://evil.example
 *
 * renders attacker-controlled content inside a frame captioned "your terminal", on a real URL of
 * the host, and posts this page's KILL and RENAME actions at the attacker's server. Nothing
 * about that needs the victim to run a bridge at all.
 *
 * So the rule is loopback, widened by exactly the machines the build has named: an EXACT-HOST
 * PIN.
 *
 * WHY A PIN, AND NOT A `*.ts.net` SUFFIX
 *
 * A suffix rule assumes the suffix means "somewhere on your own tailnet". It does not:
 * `tailscale funnel` publishes `<machine>.<tailnet>.ts.net` to the public internet, so an
 * attacker with a funneled node owns a real `.ts.net` name that resolves for everybody. The pin
 * closes that. It restricts the DESTINATION, not the client: any device on the tailnet can
 * still load the page and drive a pinned host.
 *
 * An empty list means `?remote=` reaches loopback only. Failing closed is the point.
 *
 * No `import.meta` here: `node --test` runs this file directly. The host list is PASSED IN by
 * the caller (which reads it from `global/environment.ts`), and storage access is confined to
 * the thin wrappers at the bottom.
 */
import { parse_json_object } from '../utils/parse-json-object.ts';

/** Device-local, like the window rect: which machine this device is pointed at is a
 *  property of the device. */
export const REMOTE_STORAGE_KEY = 'tldrack:agent-rack-remote';

export const REMOTE_PARAM = 'remote';
export const BRIDGE_PARAM = 'bridge';

/** Where `tailscale serve` is documented to put the bridge's status listener.
 *  Only a DEFAULT — `?bridge=` overrides it, and it is validated identically. */
export const DEFAULT_REMOTE_BRIDGE_PORT = 8443;

/**
 * Split the configured host list (`VITE_ALLOWED_REMOTE_HOSTS`) into hostnames.
 *
 * Tolerant of commas, whitespace and empty entries because it parses a hand-edited
 * `.env` line. Entries are lowercased to match `URL.hostname`, and a full URL is
 * accepted and reduced to its host so `https://mymac.tailXXXX.ts.net` in the env
 * file does the obvious thing rather than silently matching nothing.
 */
export function parse_host_list(raw: string | null | undefined): string[] {
    if (typeof raw !== 'string') return [];
    const hosts: string[] = [];
    for (const part of raw.split(',')) {
        const token = part.trim().toLowerCase();
        if (token === '') continue;
        if (token.includes('://')) {
            try {
                hosts.push(new URL(token).hostname);
                continue;
            } catch {
                continue; // unparseable entry — drop it rather than match loosely
            }
        }
        // Strip a stray port or trailing slash; the pin is on the HOST alone, so
        // that `mymac.ts.net:8443` in the env file still matches `mymac.ts.net`.
        hosts.push(token.replace(/:\d+$/, '').replace(/\/+$/, ''));
    }
    return hosts;
}

export interface RemoteConfig {
    /** Origin serving ttyd — the terminal iframe's base. */
    ttyd_origin: string;
    /** Origin serving the bridge status listener — snapshot / events / actions. */
    bridge_origin: string;
}

/**
 * Is this exactly one of the machines the build named?
 *
 * Exact match only — no suffixes, no ranges, no wildcards. The whole point of the
 * pin is that "looks like it belongs to a tailnet" is not a property anyone can
 * verify from a hostname, so the only safe question is "is it on the list".
 *
 * A raw `100.x` tailnet address is not blanket-allowed either: if you want to
 * address a machine that way, put that address on the list.
 */
function is_pinned_host(hostname: string, allowed_hosts: readonly string[]): boolean {
    return allowed_hosts.includes(hostname);
}

/** `[::1]` keeps its brackets in `URL.hostname`; accept both spellings.
 *  Exported: a host page that frames a loopback terminal asks the same question. */
export function is_loopback_host(hostname: string): boolean {
    return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]' || hostname === '::1';
}

/**
 * Add an implicit `https://` to a bare hostname.
 *
 * Purely an ergonomic affordance: this string gets typed once, by hand, on a
 * phone keyboard. It cannot weaken anything — the result still goes through the
 * full check below, and `evil.example` fails there exactly as `https://evil.example`
 * does. Anything already carrying a scheme, or a protocol-relative `//host`, is
 * left alone so the parser sees what the user actually wrote.
 */
function with_implicit_scheme(raw: string): string {
    if (raw.startsWith('//')) return raw;
    if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) return raw;
    return `https://${raw}`;
}

/**
 * Validate an origin and return it in canonical form, or `null` if it is not
 * somewhere this window may talk to.
 *
 * Returning the normalized `url.origin` rather than a boolean is deliberate: it
 * means callers cannot accidentally use the raw input string, which is the
 * shape of bug where a check passes on one value and a fetch uses another.
 */
export function normalize_remote_origin(candidate: unknown, allowed_hosts: readonly string[]): string | null {
    if (typeof candidate !== 'string') return null;
    const trimmed = candidate.trim();
    if (trimmed === '') return null;

    let url: URL;
    try {
        url = new URL(with_implicit_scheme(trimmed));
    } catch {
        return null;
    }

    // `https://mac.ts.net@evil.example` parses with hostname `evil.example`, so
    // checking `hostname` already defeats it — but userinfo has no legitimate
    // use here at all, and a URL wearing a disguise is worth refusing outright
    // rather than merely surviving.
    if (url.username !== '' || url.password !== '') return null;

    // An ORIGIN, not a URL. A path, query or fragment means someone is dressing
    // up a hostile host to survive a sloppy `startsWith` check somewhere
    // downstream; there is no reason for a legitimate caller to send one.
    if ((url.pathname !== '/' && url.pathname !== '') || url.search !== '' || url.hash !== '') return null;

    const host = url.hostname.toLowerCase();

    if (url.protocol === 'https:') {
        // A pinned machine (the documented path — `tailscale serve` issues a real
        // cert for its MagicDNS name), or loopback for desktop testing.
        if (is_pinned_host(host, allowed_hosts) || is_loopback_host(host)) return url.origin;
        return null;
    }

    if (url.protocol === 'http:') {
        // Cleartext is permitted ONLY where it cannot cross a network the user
        // does not control: their own machine. A pinned host still has to be
        // https — `tailscale serve` provides a real certificate, so there is no
        // reason to accept cleartext to it, and this also refuses exposure on a LAN.
        if (is_loopback_host(host)) return url.origin;
        return null;
    }

    return null;
}

/**
 * The config of a page the bridge itself serves: the bridge is the page's origin.
 *
 * ttyd is one port below the bridge by default, because `tldrack` puts the bridge on ttyd's
 * port + 1. That is only the first guess: `resolved_ttyd_origin` corrects it from the bridge's
 * own answer. Behind `tailscale serve` the bridge is on `DEFAULT_REMOTE_BRIDGE_PORT` and ttyd on the host's
 * default port: the inverse of `derive_bridge_origin`. `null` when neither rule applies (a
 * reverse proxy on a default port, a `file:` page), and the page then shows its connect card.
 *
 * No pin: the origin was not named by a URL parameter, so there is nothing an attacker could
 * have chosen. Cleartext is still loopback only, the same rule `normalize_remote_origin` keeps.
 */
export function same_origin_config(
    location: { protocol: string; hostname: string; port: string; origin: string },
): RemoteConfig | null {
    if (location.protocol !== 'https:' && !(location.protocol === 'http:' && is_loopback_host(location.hostname))) {
        return null;
    }
    const port = Number(location.port);
    if (location.port === '' || !Number.isInteger(port) || port < 2) return null;
    let ttyd: URL;
    try {
        ttyd = new URL(location.origin);
    } catch {
        return null;
    }
    ttyd.port = port === DEFAULT_REMOTE_BRIDGE_PORT ? '' : String(port - 1);
    return { ttyd_origin: ttyd.origin, bridge_origin: location.origin };
}

/**
 * The bridge a page OTHER than the rack talks to (Laya review): `?bridge=<origin>` when it is
 * given and allowed, else the origin that served the page. `null` for a page with no usable
 * origin (`file:`), and the caller then keeps the default (`config/bridge-origin.ts`).
 *
 * `?bridge=` exists for a dev server, which serves the page but is not a bridge. It goes
 * through the same check as `?remote=`, so it can name loopback or a pinned host and nothing
 * else.
 */
export function page_bridge_origin(
    location: { protocol: string; origin: string; search: string },
    allowed_hosts: readonly string[],
): string | null {
    let named: string | null = null;
    try {
        named = new URLSearchParams(location.search).get(BRIDGE_PARAM);
    } catch {
        named = null;
    }
    if (named !== null) return normalize_remote_origin(named, allowed_hosts);
    return location.protocol === 'http:' || location.protocol === 'https:' ? location.origin : null;
}

/**
 * The ttyd origin to frame, once the bridge has said which port its ttyd holds
 * (`/snapshot` `bridge.port`).
 *
 * On loopback the bridge's word wins over the configured guess: a bridge started with its own
 * `--port` and `--status-port` need not keep them adjacent. Only the PORT moves; the host stays
 * the validated loopback one. Off loopback the configured origin stands, because behind a proxy
 * the bridge's local port is not the public one.
 */
export function resolved_ttyd_origin(config: RemoteConfig, ttyd_port: number | undefined): string {
    if (ttyd_port === undefined || !Number.isInteger(ttyd_port) || ttyd_port < 1 || ttyd_port > 65535) {
        return config.ttyd_origin;
    }
    let bridge: URL;
    let ttyd: URL;
    try {
        bridge = new URL(config.bridge_origin);
        ttyd = new URL(config.ttyd_origin);
    } catch {
        return config.ttyd_origin;
    }
    if (!is_loopback_host(bridge.hostname) || bridge.hostname !== ttyd.hostname) return config.ttyd_origin;
    ttyd.port = String(ttyd_port);
    return ttyd.origin;
}

/** Convenience predicate. `normalize_remote_origin` is the real check. */
export function is_allowed_remote(candidate: unknown, allowed_hosts: readonly string[]): boolean {
    return normalize_remote_origin(candidate, allowed_hosts) !== null;
}

/**
 * The bridge's origin when the caller gave only the ttyd one.
 *
 * Same host, `DEFAULT_REMOTE_BRIDGE_PORT`. Returns `null` rather than throwing
 * for an unparseable input so the caller's "no valid config" path stays single.
 */
export function derive_bridge_origin(ttyd_origin: string): string | null {
    let url: URL;
    try {
        url = new URL(ttyd_origin);
    } catch {
        return null;
    }
    url.port = String(DEFAULT_REMOTE_BRIDGE_PORT);
    return url.origin;
}

/**
 * Read a remote config out of a query string.
 *
 * `null` means "not a remote window": the overwhelmingly common case, and the
 * one that must leave a rack beside its own bridge untouched.
 *
 * BOTH origins are validated, including the derived one: deriving from an
 * already-checked origin cannot introduce a new host, but a future edit to
 * `derive_bridge_origin` could, and this is not the place to rely on that
 * staying true.
 */
export function parse_remote_config(search: string, allowed_hosts: readonly string[]): RemoteConfig | null {
    let params: URLSearchParams;
    try {
        params = new URLSearchParams(search);
    } catch {
        return null;
    }

    const raw_ttyd = params.get(REMOTE_PARAM);
    if (raw_ttyd === null) return null;

    const ttyd_origin = normalize_remote_origin(raw_ttyd, allowed_hosts);
    if (ttyd_origin === null) return null;

    const raw_bridge = params.get(BRIDGE_PARAM);
    const bridge_origin = normalize_remote_origin(raw_bridge ?? derive_bridge_origin(ttyd_origin), allowed_hosts);
    if (bridge_origin === null) return null;

    return { ttyd_origin, bridge_origin };
}

/**
 * Re-validate a config that came back out of storage.
 *
 * Not paranoia for its own sake: anything that ever ran on this origin could have
 * written that key, and — more likely in practice — the pinned host list can
 * CHANGE between builds, which must retire a stored origin that is no longer
 * approved rather than grandfather it forever.
 */
export function parse_stored_config(raw: string | null, allowed_hosts: readonly string[]): RemoteConfig | null {
    const candidate = parse_json_object(raw);
    if (candidate === null) return null;
    const ttyd_origin = normalize_remote_origin(candidate.ttyd_origin, allowed_hosts);
    const bridge_origin = normalize_remote_origin(candidate.bridge_origin, allowed_hosts);
    if (ttyd_origin === null || bridge_origin === null) return null;
    return { ttyd_origin, bridge_origin };
}

// ── storage (the only impure functions in this module) ──────────────────────

export function load_remote_config(allowed_hosts: readonly string[]): RemoteConfig | null {
    try {
        return parse_stored_config(window.localStorage.getItem(REMOTE_STORAGE_KEY), allowed_hosts);
    } catch {
        return null;
    }
}

export function save_remote_config(config: RemoteConfig): void {
    try {
        window.localStorage.setItem(REMOTE_STORAGE_KEY, JSON.stringify(config));
    } catch {
        // Private-mode / quota. Remote mode still works for this page load; the
        // user just re-opens their bookmarked URL next time.
    }
}

export function clear_remote_config(): void {
    try {
        window.localStorage.removeItem(REMOTE_STORAGE_KEY);
    } catch {
        // Nothing to do — see save_remote_config.
    }
}

/**
 * The one call the window makes: query string wins, storage is the fallback.
 *
 * A VALID `?remote=` is persisted, which is what lets the phone's bookmark be a
 * bare `index.html`. An INVALID one is refused *and* clears storage
 * rather than silently falling through to the stored value — otherwise a user
 * who followed a hostile link would see a working rack and reasonably conclude
 * the link was fine.
 */
export function resolve_remote_config(search: string, allowed_hosts: readonly string[]): RemoteConfig | null {
    let has_param = false;
    try {
        has_param = new URLSearchParams(search).has(REMOTE_PARAM);
    } catch {
        has_param = false;
    }

    if (has_param) {
        const from_query = parse_remote_config(search, allowed_hosts);
        if (from_query) {
            save_remote_config(from_query);
            return from_query;
        }
        clear_remote_config();
        return null;
    }

    return load_remote_config(allowed_hosts);
}
