import test from 'node:test';
import assert from 'node:assert/strict';
import { create_arm_latch } from '../../src/engine/terminal/arm-logic.ts';

/** A hand-cranked clock: `fire()` runs whatever is pending, `live()` counts it. */
function fake_timers() {
    const pending = new Map<number, () => void>();
    let next = 1;
    return {
        set_timeout: (fn: () => void, _ms: number): unknown => {
            const handle = next++;
            pending.set(handle, fn);
            return handle;
        },
        clear_timeout: (handle: unknown): void => {
            pending.delete(handle as number);
        },
        fire: (): void => {
            const fns = [...pending.values()];
            pending.clear();
            for (const fn of fns) fn();
        },
        live: (): number => pending.size,
    };
}

function latch(on_change?: (armed: string | null) => void) {
    const timers = fake_timers();
    const arm = create_arm_latch({
        timeout_ms: 4000,
        set_timeout: timers.set_timeout,
        clear_timeout: timers.clear_timeout,
        on_change,
    });
    return { arm, timers };
}

test('the first press arms, the second fires — and fired means disarmed', () => {
    const { arm, timers } = latch();
    assert.equal(arm.press('delete:x'), 'armed');
    assert.equal(arm.armed(), 'delete:x');
    assert.equal(timers.live(), 1, 'one auto-disarm timer is running');
    assert.equal(arm.press('delete:x'), 'fired');
    assert.equal(arm.armed(), null);
    assert.equal(timers.live(), 0, 'firing clears the timer');
});

test('the timeout disarms', () => {
    const { arm, timers } = latch();
    arm.press('prune');
    timers.fire();
    assert.equal(arm.armed(), null);
    assert.equal(arm.press('prune'), 'armed', 'a press after the window arms again, never fires');
});

test('pressing another key MOVES the arm — one amber button at a time', () => {
    const { arm, timers } = latch();
    arm.press('delete:a');
    assert.equal(arm.press('delete:b'), 'armed');
    assert.equal(arm.armed(), 'delete:b');
    assert.equal(timers.live(), 1, 'the first key’s timer was cleared, not stacked');
    assert.equal(arm.press('delete:a'), 'armed', 'the old key is cold again');
});

test('retain disarms a key that retired from the render set', () => {
    // An armed state must not outlive its button. The panel passes what it is
    // about to draw. Why: owned by `src/engine/terminal/arm-logic.ts` — see the note there.
    const { arm, timers } = latch();
    arm.press('delete:gone');
    arm.retain(['delete:kept', 'delete:other']);
    assert.equal(arm.armed(), null);
    assert.equal(timers.live(), 0);
});

test('retain keeps an arm whose key is still rendered', () => {
    const { arm } = latch();
    arm.press('delete:kept');
    arm.retain(new Set(['delete:kept']));
    assert.equal(arm.armed(), 'delete:kept');
});

test('disarm is explicit and idempotent', () => {
    const { arm, timers } = latch();
    arm.press('pr:x');
    arm.disarm();
    assert.equal(arm.armed(), null);
    assert.equal(timers.live(), 0);
    arm.disarm();
    assert.equal(arm.armed(), null);
});

test('on_change fires once per transition, and never for a no-op', () => {
    const seen: (string | null)[] = [];
    const { arm, timers } = latch((armed) => seen.push(armed));
    arm.retain([]);
    arm.disarm();
    assert.deepEqual(seen, [], 'nothing armed, nothing to say — retain runs every render');
    arm.press('a');
    arm.press('b');
    arm.press('b');
    arm.press('c');
    timers.fire();
    assert.deepEqual(seen, ['a', 'b', null, 'c', null]);
});
