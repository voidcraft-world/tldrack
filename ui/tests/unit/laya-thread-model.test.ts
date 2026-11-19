import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import type { HistoryThread, HistoryThreads, HistoryTurn } from '@tldrack/schemas/agent-history.ts';
import type { ReviewRecentRow } from '@tldrack/schemas/laya-review.ts';

import { visible_rows, with_answer } from '../../src/laya-review/recent-model.ts';
import {
    THREAD_HUES,
    UNLINKED_GAP_S,
    after_grade,
    after_grade_text,
    band_head,
    band_holding,
    band_title,
    card_source,
    clock_span,
    explain_bands,
    first_in_groups,
    fnv1a,
    group_head,
    left_after_grade,
    monogram,
    next_todo,
    next_todo_band,
    next_todo_from,
    opening_band,
    opening_row,
    pager_at,
    preview_text,
    thread_bands,
    thread_hue,
    thread_key,
    threads_ask_of,
    todo_count,
    turn_groups,
    turn_head,
} from '../../src/laya-review/thread-model.ts';

// RECENT's conversations: one per thread, shown one at a time (thread-model.ts owns the keying and why).

const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const A_RESUMED = '33333333-3333-4333-8333-333333333333';

let n = 0;
function row(over: Partial<ReviewRecentRow> = {}): ReviewRecentRow {
    n += 1;
    return {
        key: n.toString(16).padStart(16, '0'),
        decision_id: `20261107T120000-${n.toString(16).padStart(12, '0')}`,
        at: '2026-11-07T12:00:00Z',
        subject: 'vc-8',
        conversation: null,
        p: 0.2, threshold: 0.45, done_threshold: 0.4, mark: 'done', band: 'done',
        model: 'laya', checkpoint: null, text: 'Closing text.', text_chars: 13, stratum: null,
        reference_open: false, operator: null,
        ...over,
    };
}

const turn = (over: Partial<HistoryTurn> = {}): HistoryTurn => ({
    status: 'linked', thread: A, conversation: A, file_id: 7, closing_seq: 40, turn_idx: 3, human_ordinal: 4,
    woke_by: 'human', request: null, ask: null, closing_tail: 'The agent’s own closing.', closing_chars: 2412, ...over,
});
const thread = (over: Partial<HistoryThread> = {}): HistoryThread => ({
    thread: A, title: 'Drop the slow suite from regular deploys', first_user: { seq: 0, ts: 1, role: 'human', text: 'the slow tests are blocking every deploy' },
    started_at: Date.parse('2026-11-07T10:00:00Z') / 1000, last_ts: null, cwd: '/repo', branch: 'main', profile: null,
    source: 'claude', kind: 'main', human_turns: 4, resumed: 0, ...over,
});
const join = (over: Partial<HistoryThreads> = {}): HistoryThreads => ({ decisions: {}, keys: {}, conversations: {}, threads: {}, indexed_at: 1, ...over });
const at = (hm: string) => `2026-11-07T${hm}:00Z`;

describe('thread_key', () => {
    it('trusts the link, then the raw conversation through the join, then nothing', () => {
        const r = row({ conversation: A_RESUMED });
        assert.equal(thread_key(r, join({ decisions: { [r.decision_id]: turn({ thread: A }) } })), A);
        assert.equal(thread_key(r, join({ conversations: { [A_RESUMED]: A } })), A, 'a resume folds into its first file');
        assert.equal(thread_key(r, join({ conversations: { [A_RESUMED]: null } })), A_RESUMED, 'unindexed: its own id');
        assert.equal(thread_key(r, null), A_RESUMED);
        assert.equal(thread_key(row(), null), null);
    });
});

describe('thread_bands', () => {
    it('one conversation per thread, the newest conversation first, its turns oldest first', () => {
        const a1 = row({ conversation: A, at: at('10:00') });
        const b1 = row({ conversation: B, at: at('11:00'), subject: 'vc-3' });
        const a2 = row({ conversation: A, at: at('12:00') });
        const bands = thread_bands([a2, b1, a1], null, 'newest');
        assert.deepEqual(bands.map((b) => b.rows.map((r) => r.decision_id)), [[a1.decision_id, a2.decision_id], [b1.decision_id]]);
        assert.equal(new Set(bands.map((b) => b.key)).size, bands.length, 'one key per conversation');
    });

    it('names a conversation by the rack name its newest turn ran under', () => {
        const old = row({ conversation: A, at: at('10:00'), subject: 'vc-2' });
        const fresh = row({ conversation: A, at: at('12:00'), subject: 'vc-9' });
        assert.equal(thread_bands([fresh, old], null, 'newest')[0].subject, 'vc-9');
    });

    it('keeps a resumed conversation one band', () => {
        const old = row({ conversation: A, at: at('10:00') });
        const fresh = row({ conversation: A_RESUMED, at: at('12:00') });
        const bands = thread_bands([fresh, old], join({ conversations: { [A]: A, [A_RESUMED]: A }, threads: { [A]: thread({ resumed: 1 }) } }), 'newest');
        assert.equal(bands.length, 1);
        assert.equal(bands[0].new_in_rack, null, 'never "new conversation" for its own resume');
        assert.deepEqual(band_head(bands[0], { now: Date.parse(at('12:30')), join_ok: true }).tags.map((t) => t.text), ['RESUMED 1×']);
    });

    it('marks the newer conversation of a reused rack name, in either order', () => {
        const older = row({ conversation: A, at: at('10:00') });
        const newer = row({ conversation: B, at: at('11:00') });
        for (const order of ['newest', 'edge'] as const) {
            const bands = thread_bands([newer, older], null, order);
            const by = new Map(bands.map((b) => [b.rows[0].decision_id, b.new_in_rack]));
            assert.equal(by.get(newer.decision_id), 'vc-8', order);
            assert.equal(by.get(older.decision_id), null, order);
        }
        // The rack name prints beside the tag already: the tag says NEW, its tip names the rack.
        const fresh = thread_bands([newer, older], null, 'newest').find((b) => b.new_in_rack)!;
        const [mark] = band_head(fresh, { now: 0, join_ok: true }).tags;
        assert.equal(mark.text, 'NEW');
        assert.match(mark.title, /vc-8 held a different one earlier on this page/);
    });

    it('dates a conversation by its start when the ledger knows it, not by its oldest turn on the page', () => {
        // B started first, though only its later turn made the page.
        const a = row({ conversation: A, at: at('10:00') });
        const b = row({ conversation: B, at: at('11:00') });
        const j = join({ threads: { [A]: thread(), [B]: thread({ thread: B, started_at: Date.parse(at('08:00')) / 1000 }) } });
        const bands = thread_bands([b, a], j, 'newest');
        assert.equal(bands.find((x) => x.rows[0] === a)?.new_in_rack, 'vc-8');
        assert.equal(bands.find((x) => x.rows[0] === b)?.new_in_rack, null);
    });

    it('groups unlinked rows by rack name and a 30-minute gap, and labels them', () => {
        // 30 minutes apart is still one run; 50 is two.
        assert.equal(UNLINKED_GAP_S, 30 * 60);
        const r1 = row({ at: at('15:40') });
        const r2 = row({ at: at('15:10') });
        const r3 = row({ at: at('14:20') });
        const r4 = row({ at: at('15:20'), subject: 'vc-3' });
        const bands = thread_bands([r1, r4, r2, r3], join(), 'newest');
        assert.deepEqual(bands.map((b) => b.rows.length), [2, 1, 1]);
        assert.ok(bands.every((b) => b.kind === 'unlinked' && b.new_in_rack === null), 'nothing says two runs differ');
        const head = band_head(bands[0], { now: Date.parse(at('16:00')), join_ok: true });
        assert.equal(head.monogram, '··');
        assert.deepEqual(head.tags.map((t) => t.text), ['NOT LINKED']);
        assert.match(head.title, /^vc-8, /);
        assert.deepEqual(band_head(bands[0], { now: 0, join_ok: false }).tags, [], 'no join: no per-band wallpaper');
    });

    it('says INDEXING while the ledger has not caught up', () => {
        const fresh = row({ conversation: B });
        const loose = row({ subject: 'vc-11' });
        const j = join({ decisions: { [fresh.decision_id]: turn({ status: 'pending', thread: null }), [loose.decision_id]: turn({ status: 'missing', thread: null }) } });
        const bands = thread_bands([fresh, loose], j, 'newest');
        assert.ok(bands.every((b) => b.indexing));
        assert.deepEqual(band_head(bands[1], { now: 0, join_ok: true }).tags.map((t) => t.text), ['INDEXING'], 'not also NOT LINKED');
        assert.equal(thread_bands([fresh], null, 'newest')[0].indexing, false, 'no join yet: nothing to say');
    });

    it('in EDGE order ranks conversations by their turn nearest a cut (the server’s order), turns still oldest first', () => {
        const b1 = row({ conversation: B, at: at('12:00') });
        const a2 = row({ conversation: A, at: at('11:00') });
        const a1 = row({ conversation: A, at: at('10:00') });
        const bands = thread_bands([b1, a2, a1], null, 'edge');
        assert.deepEqual(bands.map((b) => b.rows), [[b1], [a1, a2]]);
    });
});

describe('marks', () => {
    it('are stable, two characters, and spread over the identity hues', () => {
        assert.equal(fnv1a('t:abc'), fnv1a('t:abc'));
        assert.notEqual(fnv1a('t:abc'), fnv1a('t:abd'));
        const ids = Array.from({ length: 64 }, (_, i) => `t:${i}`);
        for (const id of ids) {
            assert.match(monogram({ identity: id, kind: 'thread' }), /^[A-HJ-NP-Z][1-9]$/);
            const hue = thread_hue(id);
            assert.ok(Number.isInteger(hue) && hue >= 0 && hue < THREAD_HUES);
        }
        assert.equal(new Set(ids.map(thread_hue)).size, THREAD_HUES, 'every hue is used');
    });
});

describe('heads', () => {
    const now = Date.parse(at('12:12'));
    it('titles by the ledger, then the needle, then the first prompt, then the rack name', () => {
        const band = thread_bands([row({ conversation: A })], join({ threads: { [A]: thread() } }), 'newest')[0];
        assert.equal(band_title(band, null, now), 'Drop the slow suite from regular deploys');
        const bare = { ...band, thread: thread({ title: null }) };
        assert.equal(band_title(bare, { title: 'Found title', first_user: null }, now), 'Found title');
        assert.equal(band_title(bare, null, now), 'the slow tests are blocking every deploy');
        assert.equal(band_title({ ...band, thread: null }, null, now), 'vc-8');
        assert.equal(band_title({ ...band, thread: null, subject: null }, null, now), 'Conversation');
    });

    it('a band reads turns on the page, how many to grade, and when it started', () => {
        const graded = row({ conversation: A, operator: { answer_id: '20261107T120000-aaaaaaaa', key: '00000000000000aa', answer: 'agree', open: false } });
        const bands = thread_bands([row({ conversation: A }), graded], join({ threads: { [A]: thread() } }), 'newest');
        const head = band_head(bands[0], { now, join_ok: true });
        assert.equal(head.meta, '2 turns · 1 to grade · started 2h');
        assert.match(head.meta_title, /4 prompts in the conversation/);
        assert.equal(head.first_user, 'the slow tests are blocking every deploy');
        assert.equal(head.first_clipped, false);
        const cut = thread({ first_user: { seq: 0, ts: 1, role: 'human', text: 'the slow tests', chars: 600 } });
        const [clipped] = thread_bands([row({ conversation: A })], join({ threads: { [A]: cut } }), 'newest');
        assert.equal(band_head(clipped, { now, join_ok: true }).first_clipped, true, 'the bridge sent its head only');
    });

    it('a turn’s compact head reads its place in the conversation', () => {
        const r = row({ conversation: A, at: at('12:00') });
        const j = join({ threads: { [A]: thread() } });
        const band = thread_bands([r], j, 'edge')[0];
        assert.equal(turn_head(band, turn(), r.at, { now, join_ok: true }).meta, 'turn 4 of 4 · 12m');
        assert.equal(turn_head(band, turn({ human_ordinal: null }), r.at, { now, join_ok: true }).meta, '12m');
    });

    it('a deck card heads by its turn’s thread, or reads unlinked', () => {
        const j = join({ threads: { [A]: thread() } });
        const linked = card_source('00000000000000ab', turn(), j);
        assert.equal(linked.identity, `t:${A}`);
        assert.equal(linked.thread?.title, 'Drop the slow suite from regular deploys');
        assert.equal(card_source('00000000000000ab', undefined, j).kind, 'unlinked');
        assert.equal(card_source('00000000000000ab', turn({ status: 'pending', thread: null }), j).indexing, true);
        // A labelled text no live verdict ever had: the key's `missing` is final, so no INDEXING head that never fills.
        assert.equal(card_source('00000000000000ab', turn({ status: 'missing', thread: null }), j).indexing, false);
    });

    it('spans an unlinked run in local clock time, with the day only when it is not today', () => {
        const day = (h: number, m: number, d = 7) => new Date(2026, 10, d, h, m).getTime();
        assert.equal(clock_span(day(15, 10), day(15, 40), day(18, 0)), '3:10 – 3:40 pm');
        assert.equal(clock_span(day(11, 50), day(12, 20), day(18, 0)), '11:50 am – 12:20 pm');
        assert.equal(clock_span(day(9, 5), day(9, 5), day(18, 0, 9)), 'Nov 7, 9:05 am');
    });
});

describe('a turn row', () => {
    it('previews the agent’s own closing when linked, else what Laya read', () => {
        const r = row({ text: 'fitted … state' });
        assert.equal(preview_text(r, turn()), 'The agent’s own closing.');
        assert.equal(preview_text(r, turn({ status: 'pending', closing_tail: null })), 'fitted … state');
        assert.equal(preview_text(r, null), 'fitted … state');
    });

    it('asks the join for every verdict and every distinct conversation', () => {
        const rows = [row({ conversation: A }), row({ conversation: A }), row({ conversation: null })];
        const ask = threads_ask_of(rows);
        assert.deepEqual(ask.decisions, rows.map((r) => r.decision_id));
        assert.deepEqual(ask.conversations, [A]);
    });
});

describe('explain_bands', () => {
    it('explains only the states on screen, and why there are no links on an older bridge', () => {
        const older = row({ conversation: A, at: at('10:00') });
        const newer = row({ conversation: B, at: at('11:00') });
        const lines = explain_bands(thread_bands([newer, older], join({ threads: { [A]: thread(), [B]: thread({ thread: B }) } }), 'newest'), 'ok', 'newest');
        assert.equal(lines.length, 2);
        assert.match(lines[1], /^NEW: its rack name held a different conversation/);
        const old = explain_bands(thread_bands([row()], null, 'newest'), 'old_bridge', 'newest');
        assert.match(old[0], /restart the bridge/);
        assert.ok(!old.some((l) => l.startsWith('NOT LINKED')), 'no per-band key without a join');
        assert.deepEqual(explain_bands([], 'ok', 'newest'), []);
        const edge = explain_bands(thread_bands([row()], null, 'edge'), 'loading', 'edge');
        assert.match(edge[0], /^One conversation at a time/);
        assert.match(edge[1], /^EDGE/);
    });
});

// ── the pager, and a conversation's prompt groups ───────────────────────────

const graded = (over: Partial<ReviewRecentRow> = {}) =>
    row({ operator: { answer_id: `20261107T120000-${(n + 1).toString(16).padStart(8, '0')}`, key: '00000000000000aa', answer: 'agree', open: false }, ...over });

describe('the pager', () => {
    it('opens a conversation on its first turn to grade, else its newest', () => {
        const a = graded({ conversation: A, at: at('10:00') });
        const b = row({ conversation: A, at: at('11:00') });
        const c = row({ conversation: A, at: at('12:00') });
        const [band] = thread_bands([c, b, a], null, 'newest');
        assert.equal(opening_row(band), b);
        assert.equal(todo_count(band), 2);
        const [done] = thread_bands([graded({ conversation: B, at: at('09:00') }), graded({ conversation: B, at: at('09:30') })], null, 'newest');
        assert.equal(opening_row(done)?.at, at('09:30'));
    });

    it('after a grade moves to the next open turn, then an earlier one, then nothing', () => {
        const a = row({ conversation: A, at: at('10:00') });
        const b = graded({ conversation: A, at: at('11:00') });
        const c = row({ conversation: A, at: at('12:00') });
        const [band] = thread_bands([a, b, c], null, 'newest');
        assert.equal(next_todo(band, a), c);
        assert.equal(next_todo(band, c), a, 'wraps to an earlier open turn');
        const [lone] = thread_bands([row({ conversation: B })], null, 'newest');
        assert.equal(next_todo(lone, lone.rows[0]), null, 'its own turn does not count');
    });

    it('after a grade moves on by place in time, whether or not TO GRADE took the graded turn out', () => {
        const t = ['10:00', '11:00', '12:00', '13:00'].map((hm) => row({ conversation: A, at: at(hm) }));
        const operator = { answer_id: '20261107T120000-cccccccc', key: t[2].key, answer: 'agree', open: false } as const;
        const kept = thread_bands([t[0], t[1], { ...t[2], operator }, t[3]], null, 'newest')[0];
        const dropped = thread_bands([t[0], t[1], t[3]], null, 'newest')[0];
        assert.equal(next_todo(kept, t[2]), t[3], 'TO GRADE off: the graded turn is still there');
        assert.equal(next_todo(dropped, t[2]), t[3], 'TO GRADE on: it left, and the next is still t4, never t1');
        assert.equal(next_todo(dropped, { decision_id: 'gone', at: at('13:30') }), t[0], 'past the last: back to the first open');
    });

    it('moves on only forward, to a conversation with something to grade', () => {
        const bands = [[graded()], [row()], [graded()], [row()]].map((rows) => ({ rows }));
        assert.equal(next_todo_band(bands, 0), 1);
        assert.equal(next_todo_band(bands, 1), 3);
        assert.equal(next_todo_band(bands, 3), null, 'never back to one skipped earlier');
        assert.equal(opening_band(bands), 1);
        assert.equal(opening_band([{ rows: [graded()] }]), 0);
    });

    it('looks at the conversation shown first, once an older page added turns to it', () => {
        // Five graded turns on page 1, then page 2 brings five older, ungraded turns of the SAME conversation.
        // Ten minutes apart, so the unlinked variant below is one run too.
        const page1 = Array.from({ length: 5 }, (_, i) => graded({ conversation: A, at: at(`12:${i}0`) }));
        const page2 = Array.from({ length: 5 }, (_, i) => row({ conversation: A, at: at(`11:${i + 1}0`) }));
        for (const order of ['newest', 'edge'] as const) {
            const bands = thread_bands([...page1, ...page2], null, order);
            assert.equal(bands.length, 1, order);
            assert.equal(next_todo_band(bands, 0), null, 'forward only: it skips the conversation it stands on');
            assert.equal(next_todo_from(bands, 0), 0, order);
        }
        // An unlinked run is keyed by its oldest turn, so its key moves when older turns join: found by a turn instead.
        const loose1 = page1.map((r) => ({ ...r, conversation: null }));
        const before = thread_bands(loose1, null, 'newest');
        const after = thread_bands([...loose1, ...page2.map((r) => ({ ...r, conversation: null }))], null, 'newest');
        assert.notEqual(before[0].key, after[0].key);
        assert.equal(band_holding(after, before[0].rows.map((r) => r.decision_id), before[0].key), 0);
        assert.equal(band_holding(after, [], before[0].key), -1, 'by key alone it is lost');
        assert.equal(next_todo_from(after, 0), 0);
        assert.equal(next_todo_from([{ rows: [graded()] }, { rows: [row()] }], 0), 1);
        assert.equal(next_todo_from([{ rows: [graded()] }], 0), null);
    });
});

describe('after a grade', () => {
    const ids = (b: { rows: ReviewRecentRow[] }) => b.rows.map((r) => r.decision_id);
    const answer = { answer_id: '20261107T120000-bbbbbbbb', key: '00000000000000bb', answer: 'agree', open: false } as const;
    const grade = (rows: ReviewRecentRow[], r: ReviewRecentRow) => with_answer(rows, r.key, { ...answer, key: r.key });

    it('checks the graded conversation again once an older page cut through it, before moving on', () => {
        // Page 1: B (graded), then A with one open turn. Page 2: two older open turns of A, and C.
        const b = graded({ conversation: B, at: at('12:00') });
        const a_last = row({ conversation: A, at: at('11:00') });
        let rows = [b, a_last];
        const was = thread_bands(rows, null, 'newest');
        const at_a = was.findIndex((x) => x.rows.includes(a_last));
        const g = { ids: ids(was[at_a]), key: was[at_a].key, row: a_last, at: at_a };
        rows = grade(rows, a_last);
        assert.equal(after_grade(thread_bands(rows, null, 'newest'), g), null, 'nothing ahead on page 1: load older');
        const a_older = [row({ conversation: A, at: at('09:00') }), row({ conversation: A, at: at('10:00') })];
        const c = row({ conversation: '44444444-4444-4444-8444-444444444444', at: at('08:00') });
        for (const order of ['newest', 'edge'] as const) {
            const bands = thread_bands([...rows, ...a_older, c], null, order);
            const step = after_grade(bands, g);
            assert.deepEqual(step && { kind: step.kind, band: step.band }, { kind: 'same', band: at_a }, order);
            assert.equal(step?.kind === 'same' ? step.row : null, a_older[0], 'its oldest open turn');
            assert.equal(after_grade_text('Graded', step!), 'Graded');
        }
    });

    it('under TO GRADE moves to the conversation that slid into its place, and never says ← goes back', () => {
        const done = row({ conversation: A, at: at('12:00') });
        const next = row({ conversation: B, at: at('11:00') });
        const before = thread_bands([done, next], null, 'newest');
        const g = { ids: ids(before[0]), key: before[0].key, row: done, at: 0 };
        const shown = visible_rows(grade([done, next], done), null, true);
        const step = after_grade(thread_bands(shown, null, 'newest'), g);
        assert.deepEqual(step, { kind: 'next', band: 0, back: false });
        assert.equal(after_grade_text('Graded', step!), 'Graded. That conversation is done: here is the next one');
        const kept = after_grade(thread_bands(grade([done, next], done), null, 'newest'), g);
        assert.deepEqual(kept, { kind: 'next', band: 1, back: true }, 'TO GRADE off: ← returns to it');
        assert.match(after_grade_text('Graded', kept!), /\(← goes back\)$/);
    });

    it('with nothing ahead says what is left, and holds the conversation while it is on the page', () => {
        const skipped = row({ conversation: B, at: at('12:00') });
        const last = row({ conversation: A, at: at('11:00') });
        const before = thread_bands([skipped, last], null, 'newest');
        const g = { ids: ids(before[1]), key: before[1].key, row: last, at: 1 };
        const rows = grade([skipped, last], last);
        const held = left_after_grade(thread_bands(rows, null, 'newest'), g);
        assert.deepEqual(held, { kind: 'behind', band: 0, held: true });
        assert.equal(after_grade_text('Graded', held), 'Graded. Nothing further ahead: an earlier conversation still has turns to grade');
        const gone = left_after_grade(thread_bands(visible_rows(rows, null, true), null, 'newest'), g);
        assert.deepEqual(gone, { kind: 'behind', band: 0, held: false });
        assert.match(after_grade_text('Graded', gone), /here is an earlier one with turns to grade$/);
        const none = left_after_grade(thread_bands(grade(rows, skipped), null, 'newest'), g);
        assert.deepEqual(none, { kind: 'none', held: true });
        assert.equal(after_grade_text('Graded', none), 'Graded. Nothing else to grade on this page');
    });
});

describe('pager_at', () => {
    it('keeps the operator’s conversation when a band filter hides only the turn they stood on', () => {
        // Eight conversations, newest first; only 5 and 7 have a ⚠️ turn. The cursor is on conversation 5's ✅ turn.
        const convs = Array.from({ length: 8 }, (_, i) => `${i}${i}${i}${i}${i}${i}${i}${i}-0000-4000-8000-000000000000`);
        const rows = convs.flatMap((c, i) => [row({ conversation: c, at: at(`${String(20 - i).padStart(2, '0')}:00`) })]);
        const warn5 = row({ conversation: convs[5], at: at('14:30'), band: 'open' });
        const warn7 = row({ conversation: convs[7], at: at('12:30'), band: 'open' });
        const all = thread_bands([...rows, warn5, warn7], null, 'newest');
        const n5 = all.findIndex((b) => b.rows.includes(warn5));
        const cursor = rows[5].decision_id;
        assert.equal(pager_at(all, { cursor, ids: [], key: null, last: 0 }), n5);
        const warned = thread_bands(visible_rows([...rows, warn5, warn7], 'open', false), null, 'newest');
        const place = { cursor, ids: ids_of(all[n5]), key: all[n5].key, last: n5 };
        assert.equal(warned[pager_at(warned, place)].key, all[n5].key, 'conversation 5, now at index 0, not whatever sits at 5');
        assert.equal(pager_at(warned, { cursor, ids: [], key: null, last: n5 }), 1, 'without its turns: the old place, clamped');
    });
});

function ids_of(b: { rows: ReviewRecentRow[] }): string[] {
    return b.rows.map((r) => r.decision_id);
}

describe('turn_groups', () => {
    const ask = (seq: number, text = `prompt ${seq}`) => ({ seq, ts: 1_700_000_000 + seq, role: 'human' as const, text });

    it('prints a prompt once over every turn that answered it, and counts the prompts skipped between', () => {
        const r1 = row({ conversation: A, at: at('10:00') });
        const r2 = row({ conversation: A, at: at('10:30') });
        const r3 = row({ conversation: A, at: at('11:00') });
        const j = join({
            decisions: {
                [r1.decision_id]: turn({ ask: ask(4), human_ordinal: 2 }),
                [r2.decision_id]: turn({ ask: ask(4), human_ordinal: 2, woke_by: 'task' }),
                [r3.decision_id]: turn({ ask: ask(30), human_ordinal: 5 }),
            },
            threads: { [A]: thread({ human_turns: 5 }) },
        });
        const [band] = thread_bands([r3, r2, r1], j, 'newest');
        const groups = turn_groups(band, j);
        assert.deepEqual(groups.map((g) => g.rows.length), [2, 1]);
        assert.deepEqual(groups.map((g) => g.skipped), [0, 2], 'prompts 3 and 4 have no verdict here');
        assert.equal(group_head(groups[0]), 'YOU · prompt 2 of 5');
        assert.equal(groups[1].anchor?.text, 'prompt 30');
    });

    it('a turn the rack opened, with no prompt of yours, anchors on its own opener; an unlinked turn has none', () => {
        const worker = row({ conversation: A, at: at('10:00') });
        const loose = row({ at: at('10:05') });
        const j = join({ decisions: { [worker.decision_id]: turn({ ask: null, request: ask(0, 'brief from the lead'), human_ordinal: 0, woke_by: 'parent' }) } });
        const [band] = thread_bands([worker], j, 'newest');
        const [g] = turn_groups(band, j);
        assert.equal(g.by, 'other');
        assert.equal(g.anchor?.text, 'brief from the lead');
        assert.equal(group_head(g), 'LEAD');
        const [lg] = turn_groups(thread_bands([loose], j, 'newest')[0], j);
        assert.equal(lg.anchor, null);
        assert.equal(group_head({ by: 'other', ordinal: null, of: null, woke_by: 'peer' }), 'RACK');
    });

    it('never counts a skip across a resumed file, whose prompts count from 1 again', () => {
        const r1 = row({ conversation: A, at: at('10:00') });
        const r2 = row({ conversation: A_RESUMED, at: at('11:00') });
        const j = join({
            decisions: {
                [r1.decision_id]: turn({ ask: ask(4), human_ordinal: 2, file_id: 7 }),
                [r2.decision_id]: turn({ ask: ask(2), human_ordinal: 1, file_id: 9, conversation: A_RESUMED }),
            },
        });
        const groups = turn_groups(thread_bands([r1, r2], j, 'newest')[0], j);
        assert.deepEqual(groups.map((g) => g.skipped), [0, 0]);
    });

    it('knows when the first message already prints as a group’s prompt', () => {
        const r = row({ conversation: A });
        const first = ask(0, 'the slow tests are blocking every deploy');
        const j = join({ decisions: { [r.decision_id]: turn({ ask: first, human_ordinal: 1 }) } });
        const groups = turn_groups(thread_bands([r], j, 'newest')[0], j);
        assert.equal(first_in_groups(groups, first), true);
        assert.equal(first_in_groups(groups, ask(12, 'something else')), false);
        assert.equal(first_in_groups(groups, null), false);
    });
});
