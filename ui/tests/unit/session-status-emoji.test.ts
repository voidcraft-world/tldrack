import { test } from 'node:test';
import assert from 'node:assert/strict';
import { session_status_emoji } from '../../src/engine/terminal/session-status-emoji.ts';

test('rack status precedence preserves dead, cut-off, fresh, service and answer marks', () => {
    const failed = { error: 'rate_limit', limit: null, resets_at: 200, message: null };
    const done = { p: 0.1, answer: true as const };
    assert.equal(session_status_emoji({ state: 'free', dim: true, fresh: true, stop_failure: failed, work_done: done }, 100), '💀');
    assert.equal(session_status_emoji({ state: 'free', fresh: true, stop_failure: failed, work_done: done }, 100), '🪫');
    assert.equal(session_status_emoji({ state: 'free', fresh: true, work_done: done }, 100), '🆕');
    assert.equal(session_status_emoji({ state: 'free', exited: { code: 0 }, work_done: done }, 100), '🖥️');
    assert.equal(session_status_emoji({ session: 'svc-test', state: 'unknown' }, 100), '🖥️');
    assert.equal(session_status_emoji({ state: 'free', work_done: done }, 100), '📖');
    assert.equal(session_status_emoji({ state: 'busy', fresh: true, stop_failure: failed, work_done: done }, 100), '🤔');
    assert.equal(session_status_emoji({ state: 'attention', stop_failure: failed }, 100), '🙋');
    assert.equal(session_status_emoji({ state: 'free', stop_failure: failed }, 201), '🔋');
});
