import test from 'node:test';
import assert from 'node:assert/strict';
import { highlight_parts, read_conversation, search_history } from '../../src/agent-rack/history-client.ts';
import { history_conversation_schema } from '@tldrack/schemas/agent-history.ts';

test('literal highlighting preserves markup, regex punctuation, casing and unmatched text', () => {
    const source = '<script>[x].* BAD</script> [X].*';
    const parts = highlight_parts(source, '[x].*');
    assert.equal(parts.map((part) => part.text).join(''), source);
    assert.deepEqual(parts.filter((part) => part.match).map((part) => part.text), ['[x].*', '[X].*']);
    assert.deepEqual(highlight_parts(source, '  '), [{ text: source, match: false }]);
    assert.deepEqual(highlight_parts('a $b \\ c', '$b \\'), [
        { text: 'a ', match: false }, { text: '$b \\', match: true }, { text: ' c', match: false },
    ]);
    const many = highlight_parts('x'.repeat(20_000), 'x');
    assert.equal(many.map((part) => part.text).join(''), 'x'.repeat(20_000));
    assert.ok(many.length <= 401);
});

test('history requests encode query text and use the supplied local or remote bridge', async (t) => {
    const calls: { url: string; init: RequestInit }[] = [];
    t.mock.method(globalThis, 'fetch', async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return new Response(JSON.stringify({ hits: [], next_offset: null, indexed_at: 123 }));
    });
    const controller = new AbortController();
    await search_history('https://mac.tail.ts.net:8443/', 'a&b=%_"', 30, 7, controller.signal);
    const url = new URL(calls[0].url);
    assert.equal(url.origin, 'https://mac.tail.ts.net:8443');
    assert.equal(url.searchParams.get('q'), 'a&b=%_"');
    assert.equal(url.searchParams.get('file_id'), '7');
    assert.equal(url.searchParams.get('offset'), '30');
    assert.equal(calls[0].init.cache, 'no-store');
    controller.abort();
    assert.equal(calls[0].init.signal?.aborted, true);
});

test('malformed responses and unavailable bridges surface an actionable failure', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => new Response('{"messages": "wrong"}'));
    await assert.rejects(read_conversation('http://127.0.0.1:7682', 1, null, new AbortController().signal), /unreadable/);
    t.mock.method(globalThis, 'fetch', async () => new Response('{"error":"No index yet"}', { status: 503 }));
    await assert.rejects(search_history('http://127.0.0.1:7682', 'test', 0, null, new AbortController().signal), /No index yet/);
    assert.equal(history_conversation_schema.safeParse({ session: { file_id: -1 }, messages: [] }).success, false);
});
