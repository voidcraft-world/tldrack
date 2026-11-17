import { test } from 'node:test';
import assert from 'node:assert/strict';
import { place_office_labels, type LabelAnchor } from '../../src/agent-office/office-labels.ts';
const anchor = (session: string, overrides: Partial<LabelAnchor> = {}): LabelAnchor => ({
    session, left: 160, top: 140, width: 90, height: 18, depth: 20, focused: false, lines: 2, ...overrides,
});
test('empty space permits modest growth, still tied to the 3D anchor', () => {
    const [label] = place_office_labels([anchor('a')], 640, 480);
    assert.equal(label.scale, 1.5);
    assert.equal(label.dx, 0);
    assert.equal(label.dy, 0);
    assert.equal(label.opacity, 1);
});
test('a filled model footprint forbids growth or relocation into furniture', () => {
    const [label] = place_office_labels([anchor('a')], 640, 480, [{ left: 0, top: 0, width: 640, height: 480 }]);
    assert.equal(label.scale, 1);
    assert.equal(label.dx, 0);
    assert.equal(label.dy, 0);
});
test('a local gap allows a small sidestep that clears the projected model', () => {
    const model = { left: 250, top: 130, width: 120, height: 100 };
    const [label] = place_office_labels([anchor('a')], 640, 480, [model]);
    assert.ok(label.dx < 0 && Math.abs(label.dx) <= 40);
    assert.ok(label.left + label.width < model.left);
    assert.ok(label.scale <= 1.5);
});
test('overlap fades the farther name, independent of input order or selected status', () => {
    const near = anchor('near', { depth: 10 });
    const far = anchor('far', { depth: 40, focused: true });
    const labels = place_office_labels([far, near], 640, 480, [{ left: 0, top: 0, width: 640, height: 480 }]);
    assert.equal(labels[0].session, 'near');
    assert.equal(labels[0].opacity, 1);
    assert.ok(labels[1].opacity >= 0.27 && labels[1].opacity < 0.4);
});
test('distant names keep perspective scaling instead of becoming fixed-size cards', () => {
    const [near] = place_office_labels([anchor('near')], 640, 480);
    const [far] = place_office_labels([anchor('far', { width: 45, height: 9, depth: 40 })], 640, 480);
    assert.equal(far.height, near.height / 2);
    assert.equal(far.width, near.width / 2);
});
test('close-up names are capped and crowded views never drop sessions', () => {
    const [close] = place_office_labels([anchor('close', { width: 700, height: 140, depth: 2 })], 640, 480);
    assert.ok(close.height <= 44);
    assert.ok(close.width <= 640 * 0.45);
    const rows = Array.from({ length: 100 }, (_, i) => anchor(String(i), { depth: i + 1 }));
    assert.equal(place_office_labels(rows, 640, 480).length, 100);
    assert.deepEqual(place_office_labels([], 640, 480), []);
    assert.deepEqual(place_office_labels(rows, 0, 0), []);
});
