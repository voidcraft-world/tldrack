import test from 'node:test';
import assert from 'node:assert/strict';

import { set_bridge_origin } from '../../src/config/bridge-origin.ts';
import { fetch_launch_settings, save_launch_settings } from '../../src/settings/profile-launch-client.ts';

const state = {
    profiles: [{ id: 'codex', label: 'CODEX', kind: 'codex', profile_model_label: 'old-model', model: 'example-model-v2', reasoning_effort: 'medium' }],
    reasoning_efforts: ['low', 'medium', 'high'],
    error: null,
};

test('reads validated defaults and saves only the selected profile’s choices', async (t) => {
    set_bridge_origin('https://machine.example');
    const calls: { url: string; body: unknown; method: string | undefined }[] = [];
    t.mock.method(globalThis, 'fetch', async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : null, method: init?.method });
        return Response.json(state);
    });
    assert.deepEqual(await fetch_launch_settings(), { ok: true, data: state });
    assert.deepEqual(await save_launch_settings('codex', 'example-model-v2', 'medium'), { ok: true, data: state });
    assert.equal(calls[0].url, 'https://machine.example/profile-launch-settings');
    assert.deepEqual(calls[1], {
        url: calls[0].url, method: 'POST', body: { profile: 'codex', model: 'example-model-v2', reasoning_effort: 'medium' },
    });
    await save_launch_settings('codex', null, null);
    assert.deepEqual(calls[2].body, { profile: 'codex', model: null, reasoning_effort: null });
});

test('malformed success, older bridge and persistence failures are never shown as saved', async (t) => {
    let response = Response.json({ profiles: [{ ...state.profiles[0], model: 42 }] });
    t.mock.method(globalThis, 'fetch', async () => response);
    const invalid = await fetch_launch_settings();
    assert.equal(!invalid.ok && invalid.kind, 'invalid');
    response = Response.json({ error: 'Not found' }, { status: 404 });
    const missing = await fetch_launch_settings();
    assert.equal(!missing.ok && missing.kind, 'missing');
    response = Response.json({ error: 'Could not save launch defaults to disk' }, { status: 500 });
    const failed = await save_launch_settings('codex', 'custom-model', 'high');
    assert.equal(!failed.ok && failed.error, 'Could not save launch defaults to disk');
});
