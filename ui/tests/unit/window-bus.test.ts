import test from 'node:test';
import assert from 'node:assert/strict';
import { CHANNEL_NAME, LEGACY_CHANNEL_NAME, accept_envelope, type BusEnvelope } from '../../src/composables/useWindowBus.ts';

const envelope = (extra: Partial<BusEnvelope> = {}): BusEnvelope => ({ type: 'companion:hello', sender_id: 'other', sender_role: 'companion', ...extra });

test('the bus is tldrack-instance, and its legacy twin the old name', () => {
    assert.equal(CHANNEL_NAME, 'tldrack-instance');
    // Legacy (step 6.8): the channel's name before 0.1.0.
    assert.equal(LEGACY_CHANNEL_NAME, 'voidcraft-instance');
});

test('a window hears each post once, from a window of either release', () => {
    // A window of this release posts on both channels with bus: 2; the copy on the legacy channel is dropped.
    assert.ok(accept_envelope(envelope({ bus: 2 }), false, 'me'));
    assert.ok(!accept_envelope(envelope({ bus: 2 }), true, 'me'));
    // Legacy (step 6.8): a window one release behind posts on the legacy channel only, with no marker.
    assert.ok(accept_envelope(envelope(), true, 'me'));
    assert.ok(!accept_envelope(envelope({ sender_id: 'me', bus: 2 }), false, 'me'), 'never its own post');
    assert.ok(!accept_envelope(null, false, 'me'));
    assert.ok(!accept_envelope({ sender_id: 'x' } as BusEnvelope, false, 'me'));
});
