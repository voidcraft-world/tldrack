import test from 'node:test';
import assert from 'node:assert/strict';
import { LONG_PRESS_MS, office_pointer_gesture } from '../../src/agent-office/office-pointer.ts';

function pointer(id = 1, x = 100, y = 100, pointer_type = 'touch', button = 0, extra: { ctrlKey?: boolean; timeStamp?: number } = {}) {
    return { pointerId: id, clientX: x, clientY: y, pointerType: pointer_type, button, ...extra };
}

test('a single tap selects, including small finger jitter', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer());
    assert.equal(gesture.move(pointer(1, 102, 102)), 0);
    assert.equal(gesture.up(pointer(1, 102, 102)), 'select');
    assert.equal(gesture.pressed(), false);
});

test('one-finger swipes scroll the embedded column, never select on release', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer());
    assert.equal(gesture.move(pointer(1, 100, 80)), 20);
    assert.equal(gesture.move(pointer(1, 100, 90)), -10);
    assert.equal(gesture.up(pointer()), null);
});

test('pinches neither scroll the strip nor select an agent, even after one finger lifts', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer());
    gesture.down(pointer(2, 200));
    assert.equal(gesture.move(pointer(1, 80, 80)), 0);
    assert.equal(gesture.move(pointer(2, 220, 120)), 0);
    assert.equal(gesture.up(pointer(2, 200)), null);
    assert.equal(gesture.move(pointer(1, 100, 60)), 0);
    assert.equal(gesture.up(pointer()), null);
    gesture.down(pointer());
    assert.equal(gesture.up(pointer()), 'select');
});

test('cancelling either finger prevents a leftover pointer becoming a tap', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer());
    gesture.down(pointer(2, 200));
    gesture.cancel(2);
    assert.equal(gesture.up(pointer()), null);
    gesture.down(pointer());
    gesture.cancel();
    assert.equal(gesture.up(pointer()), null);
});

test('mouse drags that return to their starting point still do not select', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer(1, 100, 100, 'mouse'));
    assert.equal(gesture.move(pointer(1, 150, 140, 'mouse')), 0);
    assert.equal(gesture.up(pointer(1, 100, 100, 'mouse')), null);
});

test('standalone touch stays with orbit controls and the middle button does nothing', () => {
    const gesture = office_pointer_gesture(false);
    gesture.down(pointer());
    assert.equal(gesture.move(pointer(1, 100, 80)), 0);
    assert.equal(gesture.up(pointer()), null);
    gesture.down(pointer(1, 100, 100, 'mouse', 1));
    assert.equal(gesture.up(pointer(1, 100, 100, 'mouse', 1)), null);
});

test('a still second-button click or ctrl-click opens the menu; the same button dragged pans instead', () => {
    const gesture = office_pointer_gesture(false);
    gesture.down(pointer(1, 100, 100, 'mouse', 2));
    assert.equal(gesture.up(pointer(1, 101, 101, 'mouse', 2)), 'menu');
    gesture.down(pointer(1, 100, 100, 'mouse', 0, { ctrlKey: true }));
    assert.equal(gesture.up(pointer(1, 100, 100, 'mouse', 0)), 'menu');
    gesture.down(pointer(1, 100, 100, 'mouse', 2));
    assert.equal(gesture.move(pointer(1, 160, 100, 'mouse', 2)), 0);
    assert.equal(gesture.up(pointer(1, 160, 100, 'mouse', 2)), null);
});

test('a finger held still opens the menu; a quick tap, a held mouse and a held swipe do not', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer(1, 100, 100, 'touch', 0, { timeStamp: 1000 }));
    assert.equal(gesture.up(pointer(1, 101, 100, 'touch', 0, { timeStamp: 1000 + LONG_PRESS_MS })), 'menu');
    gesture.down(pointer(1, 100, 100, 'touch', 0, { timeStamp: 3000 }));
    assert.equal(gesture.up(pointer(1, 100, 100, 'touch', 0, { timeStamp: 3000 + LONG_PRESS_MS - 1 })), 'select');
    gesture.down(pointer(1, 100, 100, 'mouse', 0, { timeStamp: 5000 }));
    assert.equal(gesture.up(pointer(1, 100, 100, 'mouse', 0, { timeStamp: 9000 })), 'select');
    gesture.down(pointer(1, 100, 100, 'touch', 0, { timeStamp: 10_000 }));
    gesture.move(pointer(1, 100, 60, 'touch', 0, { timeStamp: 10_400 }));
    assert.equal(gesture.up(pointer(1, 100, 60, 'touch', 0, { timeStamp: 11_000 })), null);
});
