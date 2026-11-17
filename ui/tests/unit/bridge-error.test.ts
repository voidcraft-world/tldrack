import test from 'node:test';
import assert from 'node:assert/strict';
import { enable_plugin_hint, read_bridge_error, unmounted_plugin_hint } from '../../src/laya-review/bridge-error.ts';

const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

test('a plugin the bridge never mounted names how to enable it', async () => {
    const hint = enable_plugin_hint('docs-sync');
    assert.deepEqual(await read_bridge_error(reply(404, { error: 'unknown plugin: docs-sync' })), {
        error: 'unknown plugin: docs-sync',
        hint,
    });
    // The remedy names the plugin and the one file that enables it.
    assert.match(hint, /\bdocs-sync\b/);
    assert.match(hint, /~\/\.config\/tldrack\/plugins\.json/);
    assert.notEqual(enable_plugin_hint('nightly-report'), hint);
});

test("a plugin's own hint wins, and other failures get none invented", async () => {
    assert.equal((await read_bridge_error(reply(404, { error: 'unknown plugin: x', hint: 'mine' }))).hint, 'mine');
    assert.equal(unmounted_plugin_hint(404, 'Unknown eval route'), undefined);
    assert.equal(unmounted_plugin_hint(503, 'unknown plugin: docs-sync'), undefined);
    assert.equal(unmounted_plugin_hint(404, 'unknown plugin: ../../etc'), undefined);
    assert.deepEqual(await read_bridge_error(new Response('not json', { status: 502 })), { error: 'bridge responded 502' });
});
