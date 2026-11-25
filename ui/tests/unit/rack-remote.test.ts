import test from 'node:test';
import assert from 'node:assert/strict';
import {
    page_bridge_origin,
    resolved_ttyd_origin,
    same_origin_config,
    BRIDGE_PARAM,
    DEFAULT_REMOTE_BRIDGE_PORT,
    REMOTE_PARAM,
    derive_bridge_origin,
    is_allowed_remote,
    normalize_remote_origin,
    parse_host_list,
    parse_remote_config,
    parse_stored_config,
} from '../../src/agent-rack/rack-remote.ts';

/** The pinned machine, as a deployment would configure it. */
const PINNED_HOST = 'mbp.tail1234.ts.net';
const PINNED = [PINNED_HOST];
const TAILNET = `https://${PINNED_HOST}`;
/** A deployment with the feature switched off — the default everywhere until
 *  VITE_ALLOWED_REMOTE_HOSTS is set. */
const NO_HOSTS: string[] = [];

// ── parse_host_list ─────────────────────────────────────────────────────────

test('parse_host_list splits, trims and lowercases', () => {
    assert.deepEqual(parse_host_list('a.ts.net, B.TS.NET ,,  c.ts.net '), ['a.ts.net', 'b.ts.net', 'c.ts.net']);
});

test('parse_host_list accepts a full URL or a host:port and keeps only the host', () => {
    assert.deepEqual(parse_host_list('https://mbp.tail1234.ts.net'), [PINNED_HOST]);
    assert.deepEqual(parse_host_list('mbp.tail1234.ts.net:8443'), [PINNED_HOST]);
    assert.deepEqual(parse_host_list('mbp.tail1234.ts.net/'), [PINNED_HOST]);
});

test('parse_host_list returns empty for absent/blank input — the feature-off default', () => {
    assert.deepEqual(parse_host_list(''), []);
    assert.deepEqual(parse_host_list('   '), []);
    assert.deepEqual(parse_host_list(null), []);
    assert.deepEqual(parse_host_list(undefined), []);
});

// ── what is ALLOWED ─────────────────────────────────────────────────────────

test('accepts the pinned host over https', () => {
    assert.equal(normalize_remote_origin(TAILNET, PINNED), TAILNET);
    assert.equal(normalize_remote_origin(`${TAILNET}:8443`, PINNED), `${TAILNET}:8443`);
});

test('accepts loopback regardless of the pin, so desktop dev is unaffected', () => {
    assert.equal(normalize_remote_origin('http://localhost:7681', NO_HOSTS), 'http://localhost:7681');
    assert.equal(normalize_remote_origin('http://127.0.0.1:7682', NO_HOSTS), 'http://127.0.0.1:7682');
    assert.equal(normalize_remote_origin('http://[::1]:7681', NO_HOSTS), 'http://[::1]:7681');
});

test('a bare hostname gets an implicit https, since it is typed on a phone', () => {
    assert.equal(normalize_remote_origin(PINNED_HOST, PINNED), TAILNET);
    assert.equal(normalize_remote_origin('evil.example', PINNED), null);
});

test('input is trimmed and the host is case-folded', () => {
    assert.equal(normalize_remote_origin('  https://MBP.Tail1234.TS.NET  ', PINNED), TAILNET);
});

// ── what is REFUSED ─────────────────────────────────────────────────────────
//
// This is the half that matters. A hosted copy of the rack page is public and
// auth-free, so every case below is reachable by handing someone a link.

test('REGRESSION: a funneled *.ts.net host is refused', () => {
    // `tailscale funnel` publishes <machine>.<tailnet>.ts.net to the PUBLIC
    // internet, so an attacker owns a genuine .ts.net name that resolves for
    // everybody. The original `.ts.net` suffix rule accepted exactly this.
    assert.equal(normalize_remote_origin('https://attacker.tail9999.ts.net', PINNED), null);
    assert.equal(normalize_remote_origin('https://evil.ts.net', PINNED), null);
    // …including one whose label merely contains the pinned name.
    assert.equal(normalize_remote_origin('https://mbp.tail1234.ts.net.evil.ts.net', PINNED), null);
});

test('an empty host list means the remote rack is OFF, not wide open', () => {
    assert.equal(normalize_remote_origin(TAILNET, NO_HOSTS), null);
    assert.equal(normalize_remote_origin('https://anything.ts.net', NO_HOSTS), null);
});

test('refuses an arbitrary https origin', () => {
    assert.equal(normalize_remote_origin('https://evil.example', PINNED), null);
    assert.equal(normalize_remote_origin('https://rack.example', PINNED), null);
});

test('refuses a userinfo-embedded lookalike', () => {
    // Parses with hostname `evil.example` — the classic "looks like the real host
    // to a human, resolves to the attacker" URL.
    assert.equal(normalize_remote_origin(`https://${PINNED_HOST}@evil.example`, PINNED), null);
    assert.equal(normalize_remote_origin(`https://user:pw@${PINNED_HOST}`, PINNED), null);
});

test('refuses anything carrying a path, query or fragment', () => {
    assert.equal(normalize_remote_origin(`${TAILNET}/hijack`, PINNED), null);
    assert.equal(normalize_remote_origin(`${TAILNET}/?x=1`, PINNED), null);
    assert.equal(normalize_remote_origin(`${TAILNET}/#frag`, PINNED), null);
});

test('refuses cleartext to a pinned host — tailscale serve gives it a real cert', () => {
    assert.equal(normalize_remote_origin(`http://${PINNED_HOST}`, PINNED), null);
});

test('refuses raw tailnet IPs unless they are themselves pinned', () => {
    // No blanket 100.64.0.0/10: "is on some tailnet" is not a property anyone can
    // verify from an address. Pin the address if you want to use one.
    assert.equal(normalize_remote_origin('http://100.64.0.1', PINNED), null);
    assert.equal(normalize_remote_origin('https://100.64.0.77', PINNED), null);
    assert.equal(normalize_remote_origin('https://100.64.0.77', ['100.64.0.77']), 'https://100.64.0.77');
});

test('refuses LAN addresses', () => {
    assert.equal(normalize_remote_origin('http://192.168.1.42:7681', PINNED), null);
    assert.equal(normalize_remote_origin('http://10.0.0.5:7681', PINNED), null);
});

test('refuses non-http schemes and unparseable input', () => {
    assert.equal(normalize_remote_origin('javascript:alert(1)', PINNED), null);
    assert.equal(normalize_remote_origin('file:///etc/passwd', PINNED), null);
    assert.equal(normalize_remote_origin(`ws://${PINNED_HOST}`, PINNED), null);
    assert.equal(normalize_remote_origin('//evil.example', PINNED), null, 'protocol-relative gets no implicit scheme');
    assert.equal(normalize_remote_origin('', PINNED), null);
    assert.equal(normalize_remote_origin('   ', PINNED), null);
});

test('refuses non-string input rather than coercing it', () => {
    assert.equal(normalize_remote_origin(null, PINNED), null);
    assert.equal(normalize_remote_origin(undefined, PINNED), null);
    assert.equal(normalize_remote_origin(42, PINNED), null);
    assert.equal(normalize_remote_origin({ toString: () => TAILNET }, PINNED), null);
});

test('is_allowed_remote agrees with normalize_remote_origin', () => {
    assert.ok(is_allowed_remote(TAILNET, PINNED));
    assert.ok(!is_allowed_remote(TAILNET, NO_HOSTS));
    assert.ok(!is_allowed_remote('https://evil.example', PINNED));
});

// ── derive_bridge_origin ────────────────────────────────────────────────────

test('derive_bridge_origin keeps the host and swaps in the bridge port', () => {
    assert.equal(derive_bridge_origin(TAILNET), `${TAILNET}:${DEFAULT_REMOTE_BRIDGE_PORT}`);
    assert.equal(derive_bridge_origin(`${TAILNET}:443`), `${TAILNET}:${DEFAULT_REMOTE_BRIDGE_PORT}`);
    assert.equal(derive_bridge_origin('not a url'), null);
});

// ── parse_remote_config ─────────────────────────────────────────────────────

test('no ?remote= means this is an ordinary desktop pop-out', () => {
    assert.equal(parse_remote_config('', PINNED), null);
    assert.equal(parse_remote_config('?other=1', PINNED), null);
});

test('?remote= alone derives the bridge origin', () => {
    assert.deepEqual(parse_remote_config(`?${REMOTE_PARAM}=${encodeURIComponent(TAILNET)}`, PINNED), {
        ttyd_origin: TAILNET,
        bridge_origin: `${TAILNET}:${DEFAULT_REMOTE_BRIDGE_PORT}`,
    });
});

test('?bridge= overrides the derived origin and is pinned too', () => {
    const search =
        `?${REMOTE_PARAM}=${encodeURIComponent(TAILNET)}` +
        `&${BRIDGE_PARAM}=${encodeURIComponent(`${TAILNET}:9999`)}`;
    assert.deepEqual(parse_remote_config(search, PINNED), {
        ttyd_origin: TAILNET,
        bridge_origin: `${TAILNET}:9999`,
    });

    // A hostile bridge origin fails the WHOLE config — the terminal must never
    // come from one machine while the actions go to another.
    const split_brain =
        `?${REMOTE_PARAM}=${encodeURIComponent(TAILNET)}` +
        `&${BRIDGE_PARAM}=${encodeURIComponent('https://attacker.tail9999.ts.net')}`;
    assert.equal(parse_remote_config(split_brain, PINNED), null);
});

test('a refused ttyd origin yields no config at all', () => {
    assert.equal(parse_remote_config(`?${REMOTE_PARAM}=${encodeURIComponent('https://evil.example')}`, PINNED), null);
    assert.equal(parse_remote_config(`?${REMOTE_PARAM}=`, PINNED), null);
});

// ── parse_stored_config ─────────────────────────────────────────────────────

test('parse_stored_config re-validates rather than trusting storage', () => {
    const good = JSON.stringify({ ttyd_origin: TAILNET, bridge_origin: `${TAILNET}:8443` });
    assert.deepEqual(parse_stored_config(good, PINNED), {
        ttyd_origin: TAILNET,
        bridge_origin: `${TAILNET}:8443`,
    });

    const poisoned = JSON.stringify({ ttyd_origin: 'https://evil.example', bridge_origin: 'https://evil.example' });
    assert.equal(parse_stored_config(poisoned, PINNED), null);

    assert.equal(parse_stored_config(null, PINNED), null);
    assert.equal(parse_stored_config('not json', PINNED), null);
    assert.equal(parse_stored_config('{}', PINNED), null);
    assert.equal(parse_stored_config(JSON.stringify({ ttyd_origin: TAILNET }), PINNED), null, 'bridge origin missing');
});

test('a stored origin is retired when the pin list stops including it', () => {
    // The realistic case: the host list changes between deploys. A previously
    // valid bookmark must stop working rather than be grandfathered in.
    const stored = JSON.stringify({ ttyd_origin: TAILNET, bridge_origin: `${TAILNET}:8443` });
    assert.equal(parse_stored_config(stored, ['someone-else.tail1234.ts.net']), null);
    assert.equal(parse_stored_config(stored, NO_HOSTS), null);
});

test('same_origin_config: a page the bridge serves talks to its own origin, ttyd one port below', () => {
    const page = (href: string) => {
        const url = new URL(href);
        return { protocol: url.protocol, hostname: url.hostname, port: url.port, origin: url.origin };
    };
    assert.deepEqual(same_origin_config(page('http://127.0.0.1:7682/')), {
        ttyd_origin: 'http://127.0.0.1:7681',
        bridge_origin: 'http://127.0.0.1:7682',
    });
    assert.deepEqual(same_origin_config(page('http://localhost:7791/review.html')), {
        ttyd_origin: 'http://localhost:7790',
        bridge_origin: 'http://localhost:7791',
    });
    // Behind `tailscale serve`: the bridge on 8443, ttyd on the host's default port.
    assert.deepEqual(same_origin_config(page('https://mymac.tail1234.ts.net:8443/')), {
        ttyd_origin: 'https://mymac.tail1234.ts.net',
        bridge_origin: 'https://mymac.tail1234.ts.net:8443',
    });
});

test('same_origin_config: no port to derive from, cleartext off loopback, or a file page is null', () => {
    assert.equal(same_origin_config({ protocol: 'https:', hostname: 'rack.example', port: '', origin: 'https://rack.example' }), null);
    assert.equal(same_origin_config({ protocol: 'http:', hostname: '192.168.0.2', port: '7682', origin: 'http://192.168.0.2:7682' }), null);
    assert.equal(same_origin_config({ protocol: 'file:', hostname: '', port: '', origin: 'null' }), null);
});

test('resolved_ttyd_origin: on loopback the bridge names its ttyd port; elsewhere the configured origin stands', () => {
    const dev = { ttyd_origin: 'http://127.0.0.1:7790', bridge_origin: 'http://127.0.0.1:7791' };
    assert.equal(resolved_ttyd_origin(dev, 7781), 'http://127.0.0.1:7781');
    assert.equal(resolved_ttyd_origin(dev, undefined), 'http://127.0.0.1:7790');
    for (const bad of [0, -1, 70000, 1.5, Number.NaN]) assert.equal(resolved_ttyd_origin(dev, bad), 'http://127.0.0.1:7790');
    // Behind a proxy the bridge's local port is not the public one.
    const tailnet = { ttyd_origin: 'https://mymac.tail1234.ts.net', bridge_origin: 'https://mymac.tail1234.ts.net:8443' };
    assert.equal(resolved_ttyd_origin(tailnet, 7681), 'https://mymac.tail1234.ts.net');
    // Two different hosts: nothing says the bridge's port means anything on the other one.
    const split = { ttyd_origin: 'http://localhost:7681', bridge_origin: 'http://127.0.0.1:7682' };
    assert.equal(resolved_ttyd_origin(split, 9000), 'http://localhost:7681');
});

test('page_bridge_origin: the page\'s own origin, unless an allowed ?bridge= names another', () => {
    const page = (href: string) => {
        const url = new URL(href);
        return { protocol: url.protocol, origin: url.origin, search: url.search };
    };
    assert.equal(page_bridge_origin(page('http://127.0.0.1:7791/review.html?view=recent'), []), 'http://127.0.0.1:7791');
    // A dev server serves the page but is not a bridge.
    assert.equal(page_bridge_origin(page('http://localhost:5173/review.html?bridge=http://127.0.0.1:7682'), []), 'http://127.0.0.1:7682');
    // Named, and not allowed: refused, never a silent fall back to the page's origin.
    assert.equal(page_bridge_origin(page('http://localhost:5173/review.html?bridge=https://evil.example'), []), null);
    assert.equal(page_bridge_origin({ protocol: 'file:', origin: 'null', search: '' }, []), null);
});

test('page_bridge_origin: a page opened by bare name talks to the bridge the rack remembers', () => {
    const page = (href: string) => {
        const url = new URL(href);
        return { protocol: url.protocol, origin: url.origin, search: url.search };
    };
    const remembered = { ttyd_origin: 'http://127.0.0.1:7681', bridge_origin: 'http://127.0.0.1:7682' };
    // The rack on a dev server opens `settings.html#setup`: no query, and the dev server is no bridge.
    assert.equal(page_bridge_origin(page('http://localhost:5174/settings.html#setup'), [], remembered), 'http://127.0.0.1:7682');
    // A named bridge still wins, and a refused one is still refused rather than swapped for the remembered one.
    assert.equal(page_bridge_origin(page('http://localhost:5174/review.html?bridge=http://127.0.0.1:7791'), [], remembered), 'http://127.0.0.1:7791');
    assert.equal(page_bridge_origin(page('http://localhost:5174/review.html?bridge=https://evil.example'), [], remembered), null);
    // Nothing remembered: the page's own origin, as a bridge-served page has always had it.
    assert.equal(page_bridge_origin(page('http://127.0.0.1:7682/settings.html'), [], null), 'http://127.0.0.1:7682');
});
