import test from 'node:test';
import assert from 'node:assert/strict';
import { parse_session_row, parse_stuck, type PortSnapshotRow } from '../../src/engine/terminal/terminal-status-logic.ts';
import { to_rack_session } from '../../src/engine/terminal/rack-view.ts';
import { build_rack_rows } from '../../src/engine/terminal/multi-terminal-layout.ts';
import { has_unread_answer } from '../../src/agent-rack/open-work-client.ts';
import {
    ANSWER_READ_EMOJI,
    ANSWER_TO_READ_EMOJI,
    OPEN_WORK_BADGE,
    OPEN_WORK_WAITING_BADGE,
    STUCK_BADGE,
    TERMINAL_STATUS_FLAGS,
    WORK_DONE_EMOJI,
    OPEN_WORK_WAITING_TITLE,
    answer_read_shown,
    answer_read_title,
    answer_to_read_shown,
    answer_to_read_title,
    open_work_badge_shown,
    open_work_children_title,
    open_work_mark_kind,
    stuck_shown,
    stuck_title,
    open_work_badge_title,
    open_work_waiting_shown,
    work_done_shown,
    work_done_title,
} from '../../src/config/terminal-status-flags.ts';

// Laya's optional unfinished-work warning (tldrack/open_work.py owns the verdict).

test('parse_session_row validates open_work and its probability', () => {
    assert.equal(parse_session_row({ session: 'a', state: 'free', open_work: true, open_work_p: 0.83 })?.open_work, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', open_work: 'yes' })?.open_work, null);
    assert.equal(parse_session_row({ session: 'a', state: 'free', open_work_p: 1.5 })?.open_work_p, null);
    assert.equal(parse_session_row({ session: 'a', state: 'free' })?.open_work, undefined);
});

test('the badge rides only a finished turn: done and free, never busy, attention or dead', () => {
    assert.equal(open_work_badge_shown('done', true, false), true);
    assert.equal(open_work_badge_shown('free', true, false), true);
    for (const state of ['busy', 'attention', 'background', 'unknown'] as const) {
        assert.equal(open_work_badge_shown(state, true, false), false, state);
    }
    assert.equal(open_work_badge_shown('done', true, true), false);
    assert.equal(open_work_badge_shown('done', false, false), false);
    assert.equal(open_work_badge_shown('done', null, false), false);
});

test('a background row keeps its ⏳ when work is open: the ⚠️ waits until the row is idle', () => {
    assert.equal(open_work_waiting_shown('background', true, false), true);
    assert.equal(open_work_badge_shown('background', true, false), false);
    for (const state of ['done', 'free', 'busy', 'attention', 'unknown'] as const) {
        assert.equal(open_work_waiting_shown(state, true, false), false, state);
    }
    assert.equal(open_work_waiting_shown('background', true, true), false, 'never on a dead row');
    assert.equal(open_work_waiting_shown('background', false, false), false);
    assert.match(OPEN_WORK_WAITING_TITLE, /not waiting on you/);
    const was = TERMINAL_STATUS_FLAGS.open_work_badge;
    TERMINAL_STATUS_FLAGS.open_work_badge = false;
    try {
        assert.equal(open_work_waiting_shown('background', true, false), false, 'one flag for both marks');
    } finally {
        TERMINAL_STATUS_FLAGS.open_work_badge = was;
    }
});

test('the flag turns the badge off', () => {
    const was = TERMINAL_STATUS_FLAGS.open_work_badge;
    TERMINAL_STATUS_FLAGS.open_work_badge = false;
    try {
        assert.equal(open_work_badge_shown('done', true, false), false);
    } finally {
        TERMINAL_STATUS_FLAGS.open_work_badge = was;
    }
});

test('the title says the probability when there is one', () => {
    assert.equal(open_work_badge_title(0.834), 'planned work may remain (83%) — ⏸ not now · ✗ not open · ✓ still open');
    assert.equal(open_work_badge_title(null), 'planned work may remain — ⏸ not now · ✗ not open · ✓ still open');
    assert.equal(open_work_badge_title(0.834, true), 'planned work may remain (83%) · graded — ⏸ not now · ✗ not open');
});

function snapshot_row(overrides: Partial<PortSnapshotRow> & { session: string }): PortSnapshotRow {
    return { state: 'free', display: 'free', age_seconds: 1, message: null, title: null, attached: false, live: true, ...overrides };
}

test('an open verdict reaches the rack row; a clean one adds nothing', () => {
    const open = to_rack_session(snapshot_row({ session: 'a', display: 'done', open_work: true, open_work_p: 0.9 }));
    assert.deepEqual(open.open_work, { p: 0.9 });
    const clean = to_rack_session(snapshot_row({ session: 'b', display: 'done', open_work: false, open_work_p: 0.1 }));
    assert.equal('open_work' in clean, false);

    const rows = build_rack_rows({ sessions: [open, clean], focused: null, max_sessions: 8 });
    assert.deepEqual(rows.find((r) => r.session === 'a')?.open_work, { p: 0.9 });
    assert.equal(rows.find((r) => r.session === 'b')?.open_work, undefined);
});

test('parse_session_row validates work_done', () => {
    assert.equal(parse_session_row({ session: 'a', state: 'free', work_done: true })?.work_done, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', work_done: 1 })?.work_done, null);
    assert.equal(parse_session_row({ session: 'a', state: 'free' })?.work_done, undefined);
});

test('a done verdict reaches the rack row as ✅ data; never beside an open one', () => {
    const done = to_rack_session(snapshot_row({ session: 'a', display: 'done', open_work: false, work_done: true, open_work_p: 0.12 }));
    assert.deepEqual(done.work_done, { p: 0.12 });
    const unsure = to_rack_session(snapshot_row({ session: 'b', display: 'done', open_work: false, work_done: false, open_work_p: 0.5 }));
    assert.equal('work_done' in unsure, false);
    const both = to_rack_session(snapshot_row({ session: 'c', display: 'done', open_work: true, work_done: true, open_work_p: 0.9 }));
    assert.equal('work_done' in both, false, 'a malformed row that claims both draws the warning, never the check');

    const rows = build_rack_rows({ sessions: [done, unsure], focused: null, max_sessions: 8 });
    assert.deepEqual(rows.find((r) => r.session === 'a')?.work_done, { p: 0.12 });
    assert.equal(rows.find((r) => r.session === 'b')?.work_done, undefined);
});

// ── An answer to read (bridge/tldrack/open_work.py § An answer to read) ──────────

test('parse_session_row validates answer_to_read', () => {
    assert.equal(parse_session_row({ session: 'a', state: 'free', answer_to_read: true })?.answer_to_read, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', answer_to_read: 'yes' })?.answer_to_read, null);
    assert.equal(parse_session_row({ session: 'a', state: 'free' })?.answer_to_read, undefined, 'an older bridge sends none');
});

test('the answer to read rides the ✅ data to the rack row, and only the ✅', () => {
    const asked = to_rack_session(snapshot_row({
        session: 'a', display: 'done', open_work: false, work_done: true, open_work_p: 0.05, answer_to_read: true }));
    assert.deepEqual(asked.work_done, { p: 0.05, answer: true });
    const read = to_rack_session(snapshot_row({
        session: 'b', display: 'done', open_work: false, work_done: true, open_work_p: 0.05, answer_to_read: false }));
    assert.deepEqual(read.work_done, { p: 0.05 }, 'read: the ✅ alone');
    const unsure = to_rack_session(snapshot_row({
        session: 'c', display: 'done', open_work: false, work_done: false, open_work_p: 0.5, answer_to_read: true }));
    assert.equal('work_done' in unsure, false, 'no ✅, nothing for the mark to ride');

    const rows = build_rack_rows({ sessions: [asked, read], focused: null, max_sessions: 8 });
    assert.deepEqual(rows.find((r) => r.session === 'a')?.work_done, { p: 0.05, answer: true });
    assert.deepEqual(rows.find((r) => r.session === 'b')?.work_done, { p: 0.05 });
});

test('opening a pane reads its answer only when its row holds one', () => {
    const rows = [{ session: 'a', answer_to_read: true }, { session: 'b', answer_to_read: false }, { session: 'c' }];
    assert.equal(has_unread_answer(rows, 'a'), true);
    for (const session of ['b', 'c', 'gone', null]) assert.equal(has_unread_answer(rows, session), false, String(session));
});

test('✅ replaces only a reply-ready or idle live row, behind its flag', () => {
    assert.equal(work_done_shown('done', true, false), true);
    assert.equal(work_done_shown('free', true, false), true);
    for (const state of ['busy', 'attention', 'background', 'unknown'] as const) {
        assert.equal(work_done_shown(state, true, false), false, state);
    }
    assert.equal(work_done_shown('done', true, true), false);
    assert.equal(work_done_shown('done', null, false), false);
    const was = TERMINAL_STATUS_FLAGS.work_done_mark;
    TERMINAL_STATUS_FLAGS.work_done_mark = false;
    try {
        assert.equal(work_done_shown('done', true, false), false);
    } finally {
        TERMINAL_STATUS_FLAGS.work_done_mark = was;
    }
    assert.deepEqual(WORK_DONE_EMOJI, { done: '✅', free: '☑️' });
    assert.equal(work_done_title('done', 0.123), 'reply ready · work looks done (12% open)');
    assert.equal(work_done_title('free', null), 'idle · work looks done');
});

test('📖 takes the ✅ place while its answer is unread, wherever the ✅ would draw', () => {
    assert.equal(answer_to_read_shown('done', { answer: true }, false), true);
    assert.equal(answer_to_read_shown('free', { answer: true }, false), true, 'a host that marks it seen without reading it');
    assert.equal(answer_to_read_shown('done', {}, false), false, 'read, or never a question: the plain ✅');
    assert.equal(answer_to_read_shown('done', null, false), false);
    assert.equal(answer_to_read_shown('done', { answer: true }, true), false, 'a gone row draws 💀');
    for (const state of ['busy', 'attention', 'background', 'unknown'] as const) {
        assert.equal(answer_to_read_shown(state, { answer: true }, false), false, state);
    }
    const marks = [ANSWER_TO_READ_EMOJI, ...Object.values(ANSWER_READ_EMOJI), ...Object.values(WORK_DONE_EMOJI),
        OPEN_WORK_BADGE, OPEN_WORK_WAITING_BADGE, STUCK_BADGE];
    assert.equal(new Set(marks).size, marks.length, 'one mark, one meaning');
    assert.equal(answer_to_read_title(0.05), 'answer to read · work looks done (5% open)');
    assert.equal(answer_to_read_title(null), 'answer to read · work looks done');
});

test('a read answer keeps its 📖 beside the check, wherever the ✅ would draw', () => {
    assert.equal(answer_read_shown('free', { read: true }, false), true);
    assert.equal(answer_read_shown('done', { read: true }, false), true);
    assert.equal(answer_read_shown('free', {}, false), false, 'never an answer: the plain ☑️');
    assert.equal(answer_read_shown('free', { read: true }, true), false, 'a gone row draws 💀');
    for (const state of ['busy', 'attention', 'background', 'unknown'] as const) {
        assert.equal(answer_read_shown(state, { read: true }, false), false, state);
    }
    assert.deepEqual(ANSWER_READ_EMOJI, { done: '✅📖', free: '☑️📖' });
    assert.equal(parse_session_row({ session: 'a', state: 'free', answer_read: true })?.answer_read, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', answer_read: 'yes' })?.answer_read, null);
    assert.deepEqual(to_rack_session(snapshot_row({ session: 'R', display: 'free', open_work: false, work_done: true,
        open_work_p: 0.05, answer_read: true })).work_done, { p: 0.05, read: true });
    assert.equal(answer_read_title('free', 0.05), 'idle · answer read · work looks done (5% open)');
    assert.equal(answer_read_title('done', 0.05, true), 'reply ready · answer read · you marked it done');
});

// tldrack/attention.py: ⏳ while children run, nothing once parked; stuck.py's 🧱.

test('a parent whose children still work draws ⏳, and the ⚠️ comes back when they stop', () => {
    assert.equal(open_work_mark_kind('free', true, false, { children: 2 }), 'waiting');
    assert.equal(open_work_mark_kind('done', true, false, { children: 1 }), 'waiting');
    assert.equal(open_work_mark_kind('free', true, false, { children: 0 }), 'warning');
    assert.equal(open_work_mark_kind('background', true, false), 'waiting');
    assert.equal(open_work_badge_shown('free', true, false, { children: 2 }), false);
    assert.match(open_work_children_title(1), /waiting on 1 child session, not on you/);
    assert.match(open_work_children_title(3), /3 child sessions/);
});

test('a parked ⚠️ draws nothing, even over ⏳', () => {
    assert.equal(open_work_mark_kind('done', true, false, { seen: true }), 'seen');
    assert.equal(open_work_mark_kind('background', true, false, { seen: true, children: 2 }), 'seen');
    assert.equal(open_work_badge_shown('done', true, false, { seen: true }), false);
    assert.equal(open_work_mark_kind('busy', true, false, { seen: true }), null);
    assert.equal(open_work_mark_kind('done', true, true, { seen: true }), null);
});

test('the attention fields reach the rack row', () => {
    const row = parse_session_row({ session: 'NOTES', state: 'free', open_work: true, open_work_p: 0.9,
        children_running: 3, open_work_seen: true, open_work_repeat: true })!;
    assert.equal(row.children_running, 3);
    assert.equal(row.open_work_seen, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', children_running: -1 })?.children_running, undefined);
    const session = to_rack_session(snapshot_row({ session: 'NOTES', display: 'free', open_work: true, open_work_p: 0.9,
        children_running: 3, open_work_seen: true }));
    assert.deepEqual(session.open_work, { p: 0.9, seen: true, children: 3 });
    // ✓ STILL OPEN was pressed: the row says so, and only a literal true counts.
    assert.equal(parse_session_row({ session: 'a', state: 'free', open_work: true, open_work_graded: true })?.open_work_graded, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', open_work: true, open_work_graded: 'yes' })?.open_work_graded, false);
    assert.deepEqual(to_rack_session(snapshot_row({ session: 'G', display: 'done', open_work: true, open_work_p: 0.8,
        open_work_graded: true })).open_work, { p: 0.8, graded: true });
});

test('a mark the operator labelled on LAYA REVIEW says it is theirs, never Laya\'s p', () => {
    // bridge/tldrack/open_work.py § The operator's label: only a literal true counts.
    assert.equal(parse_session_row({ session: 'a', state: 'free', open_work_labelled: true })?.open_work_labelled, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', open_work_labelled: 1 })?.open_work_labelled, false);
    assert.deepEqual(to_rack_session(snapshot_row({ session: 'L', display: 'done', open_work: true, open_work_p: 0.05,
        open_work_graded: true, open_work_labelled: true })).open_work, { p: 0.05, graded: true, labelled: true });
    assert.deepEqual(to_rack_session(snapshot_row({ session: 'D', display: 'free', open_work: false, work_done: true,
        open_work_p: 0.95, answer_to_read: true, open_work_labelled: true })).work_done, { p: 0.95, answer: true, labelled: true });
    assert.equal(open_work_badge_title(0.05, true, true), 'you marked work still open · graded — ⏸ not now · ✗ not open');
    assert.equal(work_done_title('free', 0.95, true), 'idle · you marked it done');
    assert.equal(answer_to_read_title(0.95, true), 'answer to read · you marked it done');
});

test('stuck is validated, drawn on working rows only, and says why', () => {
    assert.deepEqual(parse_stuck({ kind: 'repeat_failure', count: 4 }), { kind: 'repeat_failure', count: 4, seconds: null });
    assert.equal(parse_stuck({ kind: 'bored' }), null);
    assert.equal(parse_stuck('stuck'), null);
    const stuck = { kind: 'no_progress' as const, count: null, seconds: 1260 };
    assert.equal(stuck_shown('busy', stuck, false), true);
    assert.equal(stuck_shown('free', stuck, false), false);
    assert.equal(stuck_shown('busy', stuck, true), false);
    assert.equal(stuck_title(stuck), 'stuck? no progress for 21 min');
    assert.equal(stuck_title({ kind: 'repeat_failure', count: 5, seconds: null }), 'stuck? the same command failed 5× this turn');
    const rows = build_rack_rows({ sessions: [to_rack_session(snapshot_row({ session: 'a', display: 'busy',
        stuck: { kind: 'repeat_failure', count: 4, seconds: null } }))], focused: null, max_sessions: 8 });
    assert.deepEqual(rows.find((r) => r.session === 'a')?.stuck, { kind: 'repeat_failure', count: 4, seconds: null });
});

test('the session start reaches the rack row, for the LABEL button’s `since`; a bad one is dropped', () => {
    assert.equal(parse_session_row({ session: 'a', state: 'free', created_at: 1_700_000_000 })?.created_at, 1_700_000_000);
    assert.equal(parse_session_row({ session: 'a', state: 'free', created_at: '1700000000' })?.created_at, undefined);
    assert.equal(parse_session_row({ session: 'a', state: 'free', created_at: 1.5 })?.created_at, undefined);
    const started = to_rack_session(snapshot_row({ session: 'a', created_at: 1_700_000_000 }));
    const unknown = to_rack_session(snapshot_row({ session: 'b' }));
    const rows = build_rack_rows({ sessions: [started, unknown], focused: null, max_sessions: 8 });
    assert.equal(rows.find((r) => r.session === 'a')?.created_at, 1_700_000_000);
    assert.equal(rows.find((r) => r.session === 'b' && r.kind === 'session')?.created_at, undefined);
});
