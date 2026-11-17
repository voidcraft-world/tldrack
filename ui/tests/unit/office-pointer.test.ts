import test from 'node:test';
import assert from 'node:assert/strict';
import { office_pointer_gesture } from '../../src/agent-office/office-pointer.ts';

function pointer(id = 1, x = 100, y = 100, pointer_type = 'touch', button = 0) {
    return { pointerId: id, clientX: x, clientY: y, pointerType: pointer_type, button };
}

test('a single tap selects, including small finger jitter', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer());
    assert.equal(gesture.move(pointer(1, 102, 102)), 0);
    assert.equal(gesture.up(pointer(1, 102, 102)), true);
    assert.equal(gesture.pressed(), false);
});

test('one-finger swipes scroll the embedded column, never select on release', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer());
    assert.equal(gesture.move(pointer(1, 100, 80)), 20);
    assert.equal(gesture.move(pointer(1, 100, 90)), -10);
    assert.equal(gesture.up(pointer()), false);
});

test('pinches neither scroll the strip nor select an agent, even after one finger lifts', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer());
    gesture.down(pointer(2, 200));
    assert.equal(gesture.move(pointer(1, 80, 80)), 0);
    assert.equal(gesture.move(pointer(2, 220, 120)), 0);
    assert.equal(gesture.up(pointer(2, 200)), false);
    assert.equal(gesture.move(pointer(1, 100, 60)), 0);
    assert.equal(gesture.up(pointer()), false);
    gesture.down(pointer());
    assert.equal(gesture.up(pointer()), true);
});

test('cancelling either finger prevents a leftover pointer becoming a tap', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer());
    gesture.down(pointer(2, 200));
    gesture.cancel(2);
    assert.equal(gesture.up(pointer()), false);
    gesture.down(pointer());
    gesture.cancel();
    assert.equal(gesture.up(pointer()), false);
});

test('mouse drags that return to their starting point still do not select', () => {
    const gesture = office_pointer_gesture(true);
    gesture.down(pointer(1, 100, 100, 'mouse'));
    assert.equal(gesture.move(pointer(1, 150, 140, 'mouse')), 0);
    assert.equal(gesture.up(pointer(1, 100, 100, 'mouse')), false);
});

test('standalone touch stays with orbit controls and secondary clicks never select', () => {
    const gesture = office_pointer_gesture(false);
    gesture.down(pointer());
    assert.equal(gesture.move(pointer(1, 100, 80)), 0);
    assert.equal(gesture.up(pointer()), false);
    gesture.down(pointer(1, 100, 100, 'mouse', 2));
    assert.equal(gesture.up(pointer(1, 100, 100, 'mouse', 2)), false);
});
