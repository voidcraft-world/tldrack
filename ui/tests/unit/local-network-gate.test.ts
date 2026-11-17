/**
 * The gate in front of every loopback request.
 *
 * Two behaviours are worth pinning, and both are failures that would be silent:
 * a URL classified as remote when it is loopback (the prompt comes back), and a
 * URL classified as loopback when it is not (the tailnet phone path dies with no
 * error anyone can read).
 */
import { strict as assert } from 'node:assert';
import { afterEach, describe, it } from 'node:test';

import {
    classify_access_notice,
    decide_local_access,
    is_loopback_url,
    is_local_access_blocked,
    local_access_state,
    local_fetch,
    on_local_access_change,
    relock_local_access,
    unlock_local_access,
    LocalAccessBlocked,
} from '../../src/engine/terminal/local-network-gate.ts';

const real_fetch = globalThis.fetch;
afterEach(() => {
    globalThis.fetch = real_fetch;
    relock_local_access();
});

function serve(): string[] {
    const seen: string[] = [];
    globalThis.fetch = (async (input: unknown) => {
        seen.push(String(input));
        return { ok: true } as Response;
    }) as unknown as typeof fetch;
    return seen;
}

describe('is_loopback_url', () => {
    it('matches every spelling of the local machine', () => {
        for (const url of [
            'http://127.0.0.1:7682/snapshot',
            'http://localhost:7681/?arg=voidcraft',
            'http://[::1]:7682/state',
        ]) {
            assert.equal(is_loopback_url(url), true, url);
        }
    });

    it('does NOT match the tailnet host the phone path uses', () => {
        // The `?remote=` path is a public https origin reaching a 100.64/10 name.
        // Gating it would break the phone rack for a request the user made by
        // typing the URL — see the note on `is_loopback_url`.
        assert.equal(is_loopback_url('https://mac.tailnet.ts.net/snapshot'), false);
        assert.equal(is_loopback_url('https://example.com/api'), false);
    });

    it('treats an unparseable URL as not-loopback rather than throwing', () => {
        assert.equal(is_loopback_url('%%%'), false);
    });
});

describe('decide_local_access', () => {
    it('lets a granted permission open the gate with no opt-in', () => {
        // The no-regression case: a user who already answered Allow must not be
        // asked to press CONNECT before anything works again.
        assert.equal(decide_local_access(false, 'granted'), 'open');
    });

    it('lets the opt-in open the gate while the browser has not been asked', () => {
        assert.equal(decide_local_access(true, 'prompt'), 'open');
        assert.equal(decide_local_access(true, 'unknown'), 'open');
    });

    it('stays locked with neither', () => {
        assert.equal(decide_local_access(false, 'prompt'), 'locked');
        assert.equal(decide_local_access(false, 'unknown'), 'locked');
    });

    it('reports denied over an opt-in — the request would fail either way', () => {
        assert.equal(decide_local_access(true, 'denied'), 'denied');
    });
});

describe('classify_access_notice', () => {
    it('says nothing once the gate is open AND the browser has agreed', () => {
        // The whole point of returning null: a surface must keep its own
        // bridge-down copy for the one case where the bridge really is down.
        assert.equal(classify_access_notice('open', 'granted'), null);
    });

    it('reports prompting when the opt-in is stored but the browser has not answered', () => {
        // The failure this exists for: the fetch is allowed OUT, hangs behind
        // Chrome's modal, and the caller's timeout fires — which every surface
        // used to render as "bridge not running" with the dialog still on screen.
        assert.equal(classify_access_notice('open', 'prompt'), 'prompting');
    });

    it('does not report prompting on a browser that has no such permission', () => {
        // Firefox, Safari and pre-142 Chrome answer `unknown`, and they never
        // raise a dialog — telling that user to wait for one would be a lie.
        assert.equal(classify_access_notice('open', 'unknown'), null);
    });

    it('reports the gate itself ahead of any browser verdict', () => {
        for (const permission of ['granted', 'denied', 'prompt', 'unknown'] as const) {
            assert.equal(classify_access_notice('locked', permission), 'locked');
            assert.equal(classify_access_notice('denied', permission), 'denied');
        }
    });
});

describe('local_fetch', () => {
    it('never touches the network for a loopback URL while shut', async () => {
        const seen = serve();
        await assert.rejects(() => local_fetch('http://127.0.0.1:7682/snapshot'), LocalAccessBlocked);
        assert.deepEqual(seen, []);
    });

    it('tags its rejection so a caller can tell it from a dead bridge', async () => {
        try {
            await local_fetch('http://127.0.0.1:7682/snapshot');
            assert.fail('expected a rejection');
        } catch (error) {
            assert.equal(is_local_access_blocked(error), true);
            assert.equal((error as LocalAccessBlocked).state, 'locked');
        }
    });

    it('passes a non-loopback URL straight through, shut or not', async () => {
        const seen = serve();
        await local_fetch('https://example.com/api/x');
        assert.deepEqual(seen, ['https://example.com/api/x']);
    });

    it('reaches the network once the user has connected', async () => {
        const seen = serve();
        unlock_local_access();
        assert.equal(local_access_state(), 'open');
        await local_fetch('http://127.0.0.1:7682/snapshot');
        assert.deepEqual(seen, ['http://127.0.0.1:7682/snapshot']);
    });
});

describe('on_local_access_change', () => {
    it('fires on connect, and stops after unsubscribe', () => {
        const states: string[] = [];
        const off = on_local_access_change((state) => states.push(state));
        unlock_local_access();
        off();
        relock_local_access();
        assert.deepEqual(states, ['open']);
    });

    it('survives a listener that throws', () => {
        // One bad subscriber must not stop the others re-arming their pollers.
        const states: string[] = [];
        const off_bad = on_local_access_change(() => {
            throw new Error('boom');
        });
        const off_good = on_local_access_change((state) => states.push(state));
        unlock_local_access();
        off_bad();
        off_good();
        assert.deepEqual(states, ['open']);
    });
});
