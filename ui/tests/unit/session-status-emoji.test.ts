import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parse_status_emoji, session_status_emoji } from '../../src/engine/terminal/session-status-emoji.ts';

test('rack status precedence preserves dead, cut-off, fresh, service and answer marks', () => {
    const failed = { error: 'rate_limit', limit: null, resets_at: 200, message: null };
    const done = { p: 0.1, answer: true as const };
    assert.equal(session_status_emoji({ state: 'free', dim: true, fresh: true, stop_failure: failed, work_done: done }, 100), '💀');
    assert.equal(session_status_emoji({ state: 'free', fresh: true, stop_failure: failed, work_done: done }, 100), '🪫');
    assert.equal(session_status_emoji({ state: 'free', fresh: true, work_done: done }, 100), '🆕');
    assert.equal(session_status_emoji({ state: 'free', exited: { code: 0 }, work_done: done }, 100), '🖥️');
    assert.equal(session_status_emoji({ session: 'svc-test', state: 'unknown' }, 100), '🖥️');
    assert.equal(session_status_emoji({ state: 'free', work_done: done }, 100), '📖');
    // Read, the 📖 stays beside the check its state draws (bridge/tldrack/open_work.py § An answer to read).
    const read = { p: 0.1, read: true as const };
    assert.equal(session_status_emoji({ state: 'free', work_done: read }, 100), '☑️📖');
    assert.equal(session_status_emoji({ state: 'done', work_done: read }, 100), '✅📖');
    assert.equal(session_status_emoji({ state: 'free', work_done: { p: 0.1 } }, 100), '☑️', 'never an answer: the plain check');
    assert.equal(session_status_emoji({ state: 'busy', work_done: read }, 100), '🤔');
    assert.equal(session_status_emoji({ state: 'free', dim: true, work_done: read }, 100), '💀');
    assert.equal(session_status_emoji({ state: 'busy', fresh: true, stop_failure: failed, work_done: done }, 100), '🤔');
    assert.equal(session_status_emoji({ state: 'attention', stop_failure: failed }, 100), '🙋');
    assert.equal(session_status_emoji({ state: 'free', stop_failure: failed }, 201), '🔋');
});

test('the office relay carries a read answer\'s mark like any other status glyph', () => {
    assert.equal(parse_status_emoji('☑️📖'), '☑️📖');
    assert.equal(parse_status_emoji('✅📖'), '✅📖');
    assert.equal(parse_status_emoji('📖☑️'), undefined);
});
