import test from 'node:test';
import assert from 'node:assert/strict';
import { RENAME_SETTLE_SNAPSHOTS, create_rename_tracker } from '../../src/engine/terminal/rename-tracker.ts';
import { adopt_focus } from '../../src/engine/terminal/rack-view.ts';

// The bridge pushes its post-rename snapshot BEFORE answering the POST, so the
// first rack a rack sees after a rename lists `beta` and not `alpha` while the
// rename is still unanswered. Without the tracker, focus on `alpha` fell to the
// first live row.

test('the SSE snapshot that beats the answer moves focus WITH the rename', () => {
    const renames = create_rename_tracker();
    renames.begin('alpha', 'beta');
    assert.equal(adopt_focus('alpha', ['aaa-top', 'beta'], null, renames), 'beta');
});

test('without the tracker the same snapshot drops focus to the top row', () => {
    // The bug, pinned so the fix cannot be "simplified" away unnoticed.
    assert.equal(adopt_focus('alpha', ['aaa-top', 'beta']), 'aaa-top');
});

test('a rename in flight holds focus while neither name is live', () => {
    const renames = create_rename_tracker();
    renames.begin('alpha', 'beta');
    assert.equal(adopt_focus('alpha', ['aaa-top'], null, renames), 'alpha');
});

test('a stale snapshot after the answer does not drop focus on the new name', () => {
    // A poll that left before the rename lands after it: old name listed, new not.
    const renames = create_rename_tracker();
    renames.begin('alpha', 'beta');
    renames.answer('alpha', true);
    assert.equal(adopt_focus('beta', ['aaa-top', 'alpha'], null, renames), 'beta');
});

test('a refused rename stops protecting either name', () => {
    const renames = create_rename_tracker();
    renames.begin('alpha', 'beta');
    renames.answer('alpha', false);
    assert.equal(renames.size(), 0);
    // `alpha` still live: kept by the ordinary rule.
    assert.equal(adopt_focus('alpha', ['aaa-top', 'alpha'], null, renames), 'alpha');
    // `alpha` gone for real: the ordinary fallback.
    assert.equal(adopt_focus('alpha', ['aaa-top'], null, renames), 'aaa-top');
});

test('an answered rename settles once a snapshot lists the new name', () => {
    const renames = create_rename_tracker();
    renames.begin('alpha', 'beta');
    renames.answer('alpha', true);
    renames.observe(['aaa-top', 'beta']);
    assert.equal(renames.size(), 0);
});

test('an unanswered rename is never settled by snapshots', () => {
    // Bounded by the POST's own timeout, not by how many snapshots stream in.
    const renames = create_rename_tracker();
    renames.begin('alpha', 'beta');
    for (let i = 0; i < RENAME_SETTLE_SNAPSHOTS + 2; i += 1) renames.observe(['beta']);
    assert.equal(renames.size(), 1);
});

test('an answered rename whose session died is dropped after the settle window', () => {
    const renames = create_rename_tracker();
    renames.begin('alpha', 'beta');
    renames.answer('alpha', true);
    for (let i = 0; i < RENAME_SETTLE_SNAPSHOTS - 1; i += 1) renames.observe(['aaa-top']);
    assert.equal(adopt_focus('beta', ['aaa-top'], null, renames), 'beta');
    renames.observe(['aaa-top']);
    assert.equal(renames.size(), 0);
    assert.equal(adopt_focus('beta', ['aaa-top'], null, renames), 'aaa-top');
});

test('a rename does not protect an unrelated dead focus', () => {
    const renames = create_rename_tracker();
    renames.begin('alpha', 'beta');
    assert.equal(adopt_focus('ghost', ['aaa-top', 'beta'], null, renames), 'aaa-top');
});

test('a no-op rename is not tracked', () => {
    const renames = create_rename_tracker();
    renames.begin('alpha', 'alpha');
    renames.begin('', 'beta');
    assert.equal(renames.size(), 0);
});
