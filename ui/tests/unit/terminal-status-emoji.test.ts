import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
    DEAD_SESSION_EMOJI,
    FRESH_SESSION_EMOJI,
    display_state_emoji,
    fresh_session_shown,
    session_state_emoji,
} from '../../src/config/terminal-status-flags.ts';
import { build_rack_rows, type RackSession, type RackState } from '../../src/engine/terminal/multi-terminal-layout.ts';

const STATES: RackState[] = ['attention', 'done', 'free', 'background', 'busy', 'unknown'];

test('every state has its own glyph, and gone is none of them', () => {
    const glyphs = STATES.map(display_state_emoji);
    assert.equal(new Set(glyphs).size, STATES.length);
    assert.ok(!glyphs.includes(DEAD_SESSION_EMOJI));
});

test('a word from a newer bridge gets the unknown glyph, never a blank', () => {
    assert.equal(display_state_emoji('thinking' as RackState), display_state_emoji('unknown'));
});

test('the glyph column reads top to bottom as how much each agent wants you', () => {
    // Named against the ladder, so a name sort cannot pass this by accident.
    const sessions: RackSession[] = [
        { session: 'a-working', state: 'busy', live: true },
        { session: 'b-jobs', state: 'background', live: true },
        { session: 'c-idle', state: 'free', live: true },
        { session: 'd-finished', state: 'done', live: true },
        { session: 'e-asking', state: 'attention', live: true },
    ];
    const column = build_rack_rows({ sessions, focused: null, max_sessions: 8 })
        .filter((row) => row.kind === 'session')
        .map((row) => display_state_emoji(row.state ?? 'unknown'))
        .join('');
    assert.equal(column, '🙋💬○⏳🤔');
});

// ── a turn the session limit cut off ──────────────────────────────────────────

import {
    STOP_FAILURE_EMOJI,
    stop_failure_phase,
    stop_failure_shown,
    stop_failure_title,
} from '../../src/config/terminal-status-flags.ts';
import type { StopFailure } from '../../src/engine/terminal/terminal-status-logic.ts';

const NOW = 1_700_000_000;
const LIMITED: StopFailure = { error: 'rate_limit', limit: 'five_hour', resets_at: NOW + 3600, message: null };

test('a cut-off mark is none of the state glyphs, and the three differ', () => {
    const marks = Object.values(STOP_FAILURE_EMOJI);
    assert.equal(new Set(marks).size, 3);
    for (const mark of marks) {
        assert.ok(!STATES.map(display_state_emoji).includes(mark));
        assert.notEqual(mark, DEAD_SESSION_EMOJI);
    }
});

test('it replaces only a finished-looking mark: never working, blocked or dead', () => {
    for (const state of ['done', 'free', 'background'] as const) assert.ok(stop_failure_shown(state, LIMITED, false));
    for (const state of ['busy', 'attention', 'unknown'] as const) assert.ok(!stop_failure_shown(state, LIMITED, false));
    assert.ok(!stop_failure_shown('free', LIMITED, true));
    assert.ok(!stop_failure_shown('free', null, false));
});

test('🪫 holds until the exact reset, then 🔋; any other API error is 🛑', () => {
    assert.equal(stop_failure_phase(LIMITED, NOW), 'limited');
    assert.equal(stop_failure_phase(LIMITED, NOW + 3600), 'recharged');
    assert.equal(stop_failure_phase({ ...LIMITED, resets_at: null }, NOW + 10 ** 7), 'limited');
    assert.equal(stop_failure_phase({ ...LIMITED, error: 'overloaded', limit: null, resets_at: null }, NOW), 'error');
});

test('the title says which limit and when it lifts, and what to do after', () => {
    assert.match(stop_failure_title(LIMITED, NOW), /^session limit · resets \S/);
    assert.match(stop_failure_title(LIMITED, NOW + 7200), /^session limit reset at .+ — resume it$/);
    // A weekly reset is days out, so it names the day.
    const weekly: StopFailure = { ...LIMITED, limit: 'seven_day', resets_at: NOW + 3 * 86_400 };
    const day = new Date(weekly.resets_at! * 1000).toLocaleDateString([], { weekday: 'short' });
    assert.ok(stop_failure_title(weekly, NOW).startsWith(`weekly limit · resets ${day} `));
    // No reset known: Claude Code's own notice, verbatim.
    assert.equal(stop_failure_title({ ...LIMITED, resets_at: null, message: 'You have hit your limit' }, NOW),
        'You have hit your limit');
    assert.equal(stop_failure_title({ error: 'authentication_failed', limit: null, resets_at: null, message: null }, NOW),
        'turn cut off: authentication failed — resume it');
});

test('the mark rides the row from the snapshot to the rack', () => {
    const rows = build_rack_rows({
        sessions: [{ session: 'vc-1', state: 'free', live: true, stop_failure: LIMITED }],
        focused: null,
        max_sessions: 8,
    });
    assert.deepEqual(rows[0].stop_failure, LIMITED);
});

test('a fresh session reads NEW, never the idle glyph a read reply wears', () => {
    // The bug: a just-opened agent and a finished one both drew ○.
    assert.notEqual(FRESH_SESSION_EMOJI, display_state_emoji('free'));
    assert.ok(!STATES.map(display_state_emoji).includes(FRESH_SESSION_EMOJI));
    assert.equal(session_state_emoji('free', true, false), FRESH_SESSION_EMOJI);
    assert.equal(session_state_emoji('free', undefined, false), display_state_emoji('free'));
});

test('fresh shows only on a live idle row', () => {
    assert.equal(fresh_session_shown('free', true, false), true);
    assert.equal(fresh_session_shown('free', true, true), false); // gone wins
    for (const state of STATES.filter((s) => s !== 'free')) {
        assert.equal(fresh_session_shown(state, true, false), false, state);
    }
});

test('the rack row carries fresh from its session, and only when set', () => {
    const sessions: RackSession[] = [
        { session: 'new-one', state: 'free', live: true, fresh: true },
        { session: 'read-one', state: 'free', live: true },
    ];
    const rows = build_rack_rows({ sessions, focused: null, max_sessions: 8 }).filter((r) => r.kind === 'session');
    assert.equal(rows.find((r) => r.session === 'new-one')?.fresh, true);
    assert.equal('fresh' in (rows.find((r) => r.session === 'read-one') ?? {}), false);
});
