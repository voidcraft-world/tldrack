import test from 'node:test';
import assert from 'node:assert/strict';
import { create_pane_settle, type SettleTimers } from '../../src/agent-rack/pane-settle.ts';

/** A manual clock: `advance(ms)` fires every timer that has come due. */
function fake_timers(): SettleTimers & { advance: (ms: number) => void; pending: () => number } {
    let now = 0;
    let next_id = 0;
    const live = new Map<number, { at: number; fn: () => void }>();
    return {
        set: (fn, ms) => {
            const id = ++next_id;
            live.set(id, { at: now + ms, fn });
            return id;
        },
        clear: (handle) => void live.delete(handle as number),
        advance: (ms) => {
            now += ms;
            for (const [id, timer] of [...live].sort((a, b) => a[1].at - b[1].at)) {
                if (timer.at > now || !live.has(id)) continue;
                live.delete(id);
                timer.fn();
            }
        },
        pending: () => live.size,
    };
}

function harness(grace = 800, cap = 8000) {
    const timers = fake_timers();
    let changes = 0;
    const settle = create_pane_settle({
        on_change: () => void changes++,
        grace_ms: () => grace,
        cap_ms: () => cap,
        timers,
    });
    return { settle, timers, changes: () => changes };
}

test('navigating marks the session loading and notifies once', () => {
    const { settle, changes } = harness();
    settle.navigated('vc-1');
    assert.equal(settle.loading(), 'vc-1');
    assert.equal(changes(), 1);
});

test('load starts the grace window; the row unlocks only after it', () => {
    const { settle, timers, changes } = harness();
    settle.navigated('vc-1');
    timers.advance(1500);
    settle.loaded();
    timers.advance(799);
    assert.equal(settle.loading(), 'vc-1');
    timers.advance(1);
    assert.equal(settle.loading(), null);
    assert.equal(changes(), 2);
    assert.equal(timers.pending(), 0);
});

test('the cap unlocks a pane whose load never arrives', () => {
    const { settle, timers } = harness();
    settle.navigated('vc-1');
    timers.advance(7999);
    assert.equal(settle.loading(), 'vc-1');
    timers.advance(1);
    assert.equal(settle.loading(), null);
});

test('a second navigation replaces the first and restarts both timers', () => {
    const { settle, timers } = harness();
    settle.navigated('vc-1');
    settle.loaded();
    timers.advance(500);
    settle.navigated('vc-2');
    timers.advance(500); // vc-1's grace would have fired here
    assert.equal(settle.loading(), 'vc-2');
    settle.loaded();
    timers.advance(800);
    assert.equal(settle.loading(), null);
});

test('a repeated load inside the grace window does not extend it', () => {
    const { settle, timers } = harness();
    settle.navigated('vc-1');
    settle.loaded();
    timers.advance(600);
    settle.loaded();
    timers.advance(200);
    assert.equal(settle.loading(), null);
});

test('blanking the pane clears at once; a load for about:blank is ignored', () => {
    const { settle, timers, changes } = harness();
    settle.navigated('vc-1');
    settle.navigated(null);
    assert.equal(settle.loading(), null);
    assert.equal(changes(), 2);
    settle.loaded();
    assert.equal(timers.pending(), 0);
});

test('dispose cancels every timer without notifying', () => {
    const { settle, timers, changes } = harness();
    settle.navigated('vc-1');
    settle.loaded();
    settle.dispose();
    assert.equal(timers.pending(), 0);
    assert.equal(settle.loading(), null);
    assert.equal(changes(), 1);
});
