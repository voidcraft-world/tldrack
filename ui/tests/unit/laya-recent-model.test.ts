import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { reviewRecentRowSchema, type ReviewRecentRow } from '@tldrack/schemas/laya-review.ts';

import {
    BAND_MARK,
    EDGE_NEAR,
    RECENT_VERDICTS,
    age_text,
    chip_text,
    ts_age_text,
    verdict_chip,
    answer_request,
    band_readouts,
    drawn_note,
    edge_note,
    explain_order,
    operator_verdict,
    grade,
    needs_readout,
    open_rows_of,
    recent_key,
    scrolls_itself,
    recent_tally,
    review_title,
    row_mark,
    row_mark_words,
    visible_rows,
    with_answer,
} from '../../src/laya-review/recent-model.ts';
import { ANSWER_TO_READ_EMOJI } from '../../src/config/terminal-status-flags.ts';
import { laya_review_href, session_from_search, view_from_search } from '../../src/laya-review/open.ts';

// The RECENT list: grade the newest live verdicts DONE / OPEN.

let n = 0;
function row(over: Partial<ReviewRecentRow> = {}): ReviewRecentRow {
    n += 1;
    const base: ReviewRecentRow = {
        key: n.toString(16).padStart(16, '0'),
        decision_id: `20261030T120000-${n.toString(16).padStart(12, '0')}`,
        at: '2026-10-30T12:00:00Z',
        subject: 'wt-a',
        p: 0.2,
        threshold: 0.7,
        done_threshold: 0.4,
        mark: 'done',
        band: 'done',
        model: 'tldrack-agent-open-work',
        checkpoint: null,
        text: 'shipped',
        text_chars: 7,
        stratum: 'recent',
        reference_open: false,
        operator: null,
    };
    return reviewRecentRowSchema.parse({ ...base, ...over });
}

const said = (open: boolean | null, answer: 'agree' | 'flip' | 'skip' = 'agree', needs?: 'you' | 'running' | 'read') =>
    ({ answer_id: `a_${'0'.repeat(15)}${open ? 1 : 2}`, key: '0'.repeat(16), answer, open, ...(needs ? { needs } : {}) });

describe('answer_request', () => {
    it('maps a verdict onto agree/flip against the reference, and refuses an unanswerable row', () => {
        const done_ref = row({ reference_open: false });
        assert.deepEqual(answer_request(done_ref, 'agent_open_work', 'done', false), { surface: 'agent_open_work', key: done_ref.key, answer: 'agree', reference_open: false });
        assert.deepEqual(answer_request(done_ref, 'agent_open_work', 'you', false), { surface: 'agent_open_work', key: done_ref.key, answer: 'flip', reference_open: false, needs: 'you' });
        assert.equal(answer_request(done_ref, 'agent_open_work', 'running', false)?.needs, 'running');
        assert.equal(answer_request(done_ref, 'agent_open_work', 'running', true)?.answer, 'agree', 'a moved reference re-maps the same verdict');
        assert.equal(answer_request(done_ref, 'agent_open_work', 'done', false)?.needs, undefined, 'a bare DONE has no half');
        assert.deepEqual(answer_request(done_ref, 'agent_open_work', 'read', false), { surface: 'agent_open_work', key: done_ref.key, answer: 'agree', reference_open: false, needs: 'read' });
        assert.equal(answer_request(done_ref, 'agent_open_work', 'read', true)?.answer, 'flip', 'READ records DONE');
        assert.equal(answer_request(done_ref, 'agent_open_work', 'skip', true)?.answer, 'skip');
        assert.equal(answer_request(done_ref, 'agent_open_work', 'done', null), null);
    });
});

describe('grading', () => {
    it('reads the operator verdict back, and grades it against the band; 💬 has no right answer', () => {
        assert.equal(operator_verdict(row()), null);
        assert.equal(operator_verdict(row({ operator: said(true) })), 'open', 'an OPEN answer with no half (the deck)');
        assert.equal(operator_verdict(row({ operator: said(true, 'agree', 'you') })), 'you');
        assert.equal(operator_verdict(row({ operator: said(true, 'agree', 'running') })), 'running');
        assert.equal(operator_verdict(row({ operator: said(false, 'agree', 'read') })), 'read');
        assert.equal(grade(row({ band: 'done', operator: said(false, 'agree', 'read') })), 'right', 'READ is done work');
        assert.equal(grade(row({ band: 'open', operator: said(false, 'agree', 'read') })), 'wrong');
        assert.equal(grade(row({ band: 'open', operator: said(true, 'agree', 'running') })), 'right', 'RUNNING is still open work');
        assert.equal(grade(row({ band: 'done', operator: said(true, 'agree', 'you') })), 'wrong');
        assert.equal(operator_verdict(row({ operator: said(null, 'skip') })), 'skip');
        assert.equal(grade(row({ band: 'done', operator: said(false) })), 'right');
        assert.equal(grade(row({ band: 'done', operator: said(true) })), 'wrong', 'a ✅ on open work is the costly miss');
        assert.equal(grade(row({ band: 'open', operator: said(true) })), 'right');
        assert.equal(grade(row({ band: 'reply', operator: said(true) })), null);
        assert.equal(grade(row({ band: 'done', operator: said(null, 'skip') })), null);
    });

    it('tallies per band, as review_data.py recent_rows does', () => {
        const rows = [
            row({ band: 'done', operator: said(false) }),
            row({ band: 'done', operator: said(true) }),
            row({ band: 'done' }),
            row({ band: 'open', operator: said(true) }),
            row({ band: 'reply', p: 0.5, operator: said(false) }),
            row({ band: 'reply', p: 0.5, operator: said(null, 'skip') }),
        ];
        const t = recent_tally(rows);
        assert.equal(t.n, 6);
        assert.equal(t.reviewed, 4);
        assert.equal(t.skipped, 1);
        assert.deepEqual(t.bands.done, { n: 3, reviewed: 2, right: 1, open: 1 });
        assert.deepEqual(t.bands.open, { n: 1, reviewed: 1, right: 1, open: 1 });
        assert.deepEqual(t.bands.reply, { n: 2, reviewed: 1, right: 0, open: 0 });
        const [done, reply, open] = band_readouts(t);
        assert.equal(done.text, '1/2');
        assert.equal(done.mark, '✅');
        assert.equal(reply.text, '1 done · 0 open');
        assert.equal(open.text, '1/1');
        assert.deepEqual(t.needs, { you: 0, running: 0, read: 0 });
        assert.equal(needs_readout(t), null, 'nothing split yet');
    });

    it('splits the OPEN answers into who the turn was waiting on', () => {
        const t = recent_tally([
            row({ band: 'open', operator: said(true, 'agree', 'you') }),
            row({ band: 'open', operator: said(true, 'agree', 'running') }),
            row({ band: 'open', operator: said(true, 'agree', 'running') }),
            row({ band: 'open', operator: said(true) }),
        ]);
        assert.deepEqual(t.needs, { you: 1, running: 2, read: 0 });
        assert.equal(t.bands.open.open, 4);
        assert.equal(needs_readout(t)?.text, '1/3');
    });

    it('turns a band amber once more than one in five graded answers are wrong, from 5 graded', () => {
        const wrongs = (bad: number, total: number) =>
            band_readouts(recent_tally(Array.from({ length: total }, (_, i) => row({ band: 'done', operator: said(i < bad) }))))[0].warn;
        assert.equal(wrongs(1, 4), false, 'too few to judge');
        assert.equal(wrongs(1, 5), false);
        assert.equal(wrongs(2, 5), true);
    });
});

describe('the list', () => {
    it('narrows by band and to the ungraded', () => {
        const rows = [row({ band: 'done', operator: said(false) }), row({ band: 'open' }), row({ band: 'done' })];
        assert.equal(visible_rows(rows, 'done', false).length, 2);
        assert.equal(visible_rows(rows, null, true).length, 2);
        assert.equal(visible_rows(rows, 'done', true).length, 1);
    });

    it('an answer lands on every row of its text', () => {
        const a = row();
        const twin = { ...row(), key: a.key };
        const other = row();
        const after = with_answer([a, twin, other], a.key, said(true));
        assert.deepEqual(after.map((r) => operator_verdict(r)), ['open', 'open', null]);
        assert.equal(open_rows_of([a, twin, other], a.key), 2, 'what one answer takes out of the TO GRADE pool');
        assert.equal(open_rows_of(after, a.key), 0, 'an answer seen arrive from elsewhere counts once');
    });

    it('says what the rack drew when today’s band differs', () => {
        assert.equal(drawn_note(row({ mark: 'reply', band: 'done' })), `was ${BAND_MARK.reply}`);
        assert.equal(drawn_note(row({ mark: 'done', band: 'done' })), null);
    });

    it('ages in one short unit', () => {
        const at = '2026-10-30T12:00:00Z';
        const t0 = Date.parse(at);
        assert.equal(age_text(at, t0 + 30_000), '30s');
        assert.equal(age_text(at, t0 + 5 * 60_000), '5m');
        assert.equal(age_text(at, t0 + 3 * 3_600_000), '3h');
        assert.equal(age_text(at, t0 + 2 * 86_400_000), '2d');
        assert.equal(age_text('nope', t0), '—');
    });
});

describe('recent_key', () => {
    const key = (code: string, extra: Partial<Parameters<typeof recent_key>[0]> = {}) =>
        recent_key({ code, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, repeat: false, target: null, ...extra });
    it('D R N S U grade, J K and the arrows move, Enter expands', () => {
        assert.equal(key('KeyD'), 'done');
        assert.equal(key('KeyR'), 'running');
        assert.equal(key('KeyB'), 'read');
        assert.equal(key('KeyN'), 'you');
        assert.equal(key('KeyO'), null);
        assert.equal(key('KeyS'), 'skip');
        assert.equal(key('KeyU'), 'undo');
        assert.equal(key('KeyJ'), 'next');
        assert.equal(key('ArrowUp'), 'prev');
        assert.equal(key('Enter'), 'expand');
        assert.equal(key('KeyE'), 'edge');
        assert.equal(key('KeyE', { repeat: true }), null, 'a held E must not flicker the order');
        assert.equal(key('KeyA'), null);
    });
    it('← → walk the conversations, never on a held key', () => {
        assert.equal(key('ArrowRight'), 'next_conv');
        assert.equal(key('ArrowLeft'), 'prev_conv');
        assert.equal(key('ArrowRight', { repeat: true }), null, 'a held → would fly past every conversation');
        assert.equal(key('ArrowLeft', { altKey: true }), null, 'Alt+← is the browser’s Back');
    });
    it('leaves an arrow to a code block, a table or a paste that scrolls itself; J/K still move', () => {
        const inside = (selector: string) => ({ tagName: 'CODE', closest: (s: string) => (s.includes(selector) ? {} : null) }) as unknown as EventTarget;
        assert.equal(key('ArrowRight', { target: inside('pre') }), null);
        assert.equal(key('ArrowLeft', { target: inside('table') }), null);
        assert.equal(key('ArrowDown', { target: inside('.am-paste__text') }), null);
        assert.equal(key('KeyJ', { target: inside('pre') }), 'next');
        assert.equal(key('ArrowRight', { target: null }), 'next_conv');
        const wide = { tagName: 'DIV', closest: () => null, scrollWidth: 900, clientWidth: 300, scrollHeight: 40, clientHeight: 40 } as unknown as EventTarget;
        assert.equal(key('ArrowRight', { target: wide }), null, 'it overflows sideways');
        assert.equal(key('ArrowDown', { target: wide }), 'next', 'but not down');
        const page = { tagName: 'BODY', closest: () => null, scrollHeight: 9000, clientHeight: 800 } as unknown as EventTarget;
        assert.equal(key('ArrowDown', { target: page }), 'next', 'the page scrolling is not a scroller of its own');
        assert.equal(scrolls_itself(null, 'ArrowRight'), false);
        assert.equal(scrolls_itself({ tagName: 'PRE', closest: () => ({}) }, 'KeyJ'), false);
    });
    it('leaves Enter to focused controls and disclosures', () => {
        for (const tagName of ['SUMMARY', 'BUTTON', 'A']) {
            assert.equal(key('Enter', { target: { tagName } as unknown as EventTarget }), null);
        }
    });
    it('never a modifier, never a held grade, never a field; a held arrow still moves', () => {
        assert.equal(key('KeyD', { metaKey: true }), null);
        assert.equal(key('KeyD', { shiftKey: true }), null);
        assert.equal(key('KeyD', { repeat: true }), null);
        assert.equal(key('ArrowDown', { repeat: true }), 'next');
        assert.equal(key('KeyD', { target: { tagName: 'INPUT' } as unknown as EventTarget }), null);
    });
});

describe('edge cases', () => {
    it('says how far a near miss fell short of a mark, and by how much a mark only just made it', () => {
        assert.equal(edge_note(row({ p: 0.68, edge: { cut: 'open', delta: -0.02 } }))?.text, '0.02 FROM ⚠️');
        assert.equal(edge_note(row({ p: 0.71, edge: { cut: 'open', delta: 0.01 } }))?.text, '⚠️ BY 0.01');
        assert.equal(edge_note(row({ p: 0.7, edge: { cut: 'open', delta: 0 } }))?.text, '⚠️ BY 0.00', 'p at the cut is a ⚠️');
        assert.equal(edge_note(row({ p: 0.41, edge: { cut: 'done', delta: 0.01 } }))?.text, '0.01 FROM ✅');
        assert.equal(edge_note(row({ p: 0.4, edge: { cut: 'done', delta: 0 } }))?.text, '0.00 FROM ✅', 'p at the cut is not a ✅');
        assert.equal(edge_note(row({ p: 0.38, edge: { cut: 'done', delta: -0.02 } }))?.text, '✅ BY 0.02');
        assert.match(edge_note(row({ p: 0.68, edge: { cut: 'open', delta: -0.02 } }))?.title ?? '', /cut is 0\.70/);
        assert.equal(edge_note(row({ p: 0.68, edge: { cut: 'open', delta: -0.02 } }))?.cut.toFixed(2), '0.70', 'the cut, for the score line');
    });
    it('stays quiet away from a cut, and on a bridge that sends no edge', () => {
        assert.equal(edge_note(row({ p: 0.6, edge: { cut: 'open', delta: -0.1 } })), null);
        assert.notEqual(edge_note(row({ p: 0.65, edge: { cut: 'open', delta: -EDGE_NEAR } })), null, 'the boundary is in');
        assert.equal(edge_note(row({ p: 0.68 })), null);
        // A deck card has no row: its edge rides beside its Laya p (`review-deck-model.ts` card_edge).
        assert.equal(edge_note({ edge: { cut: 'done', delta: -0.04 }, p: 0.36 })?.text, '✅ BY 0.04');
    });
    it('explains the order in words, with the pool when the bridge sent one', () => {
        assert.match(explain_order('edge', false, 100, 1500), /^EDGE: the 100 of 1,500 verdicts/);
        assert.match(explain_order('edge', true, 100, 1200), /100 of 1,200 ungraded verdicts/);
        assert.match(explain_order('newest', false, 100, null), /^NEWEST: the 100 verdicts, newest first/);
    });
});

describe('the page address', () => {
    it('names a view only when it is not the deck, so older bookmarks still open the deck', () => {
        assert.equal(laya_review_href(), '/review.html');
        assert.equal(laya_review_href('agent_open_work'), '/review.html?surface=agent_open_work');
        assert.equal(laya_review_href('agent_open_work', 'recent'), '/review.html?surface=agent_open_work&view=recent');
        assert.equal(laya_review_href(undefined, 'recent'), '/review.html?view=recent');
        assert.equal(view_from_search('?view=recent'), 'recent');
        assert.equal(view_from_search('?view=nope'), 'deck');
        assert.equal(view_from_search(''), 'deck');
    });

    it('keeps the rack session the LABEL button opened, on the RECENT view only, and round-trips it', () => {
        const session = { name: 'vc-3', since: 1_700_000_000 };
        const href = laya_review_href(undefined, 'recent', { session });
        assert.equal(href, '/review.html?view=recent&session=vc-3&since=1700000000');
        assert.deepEqual(session_from_search(href.split('?')[1]), session);
        assert.equal(laya_review_href(undefined, 'deck', { session }), '/review.html', 'the deck is never narrowed to one session');
        assert.equal(laya_review_href(undefined, 'recent', { session: { name: 'no spaces', since: null } }), '/review.html?view=recent');
        assert.deepEqual(session_from_search('?session=docs-voice&since=17'), { name: 'docs-voice', since: null });
        assert.equal(session_from_search('?session=a%20b'), null);
        assert.equal(session_from_search(''), null);
    });
});


describe('review_title', () => {
    it('uses the rack display name, with an indexed title before the technical identity', () => {
        assert.equal(review_title('vc-7', 'get the branch ready, please', 'Older title'), 'get the branch ready, please');
        assert.equal(review_title('vc-7', null, 'PR #12'), 'PR #12');
        assert.equal(review_title('vc-7', '  ', null), 'vc-7');
        assert.equal(review_title(null, null, null), 'Conversation');
    });
});


describe('the graded chip and the grade buttons', () => {
    it('draws one button per verdict, each with its key, tooltip and EXPLAIN caption', () => {
        assert.deepEqual(RECENT_VERDICTS.map((v) => v.verdict), ['done', 'read', 'running', 'you', 'skip']);
        assert.deepEqual(RECENT_VERDICTS.map((v) => v.key), ['D', 'B', 'R', 'N', 'S'], 'the keys recent_key reads');
        assert.equal(RECENT_VERDICTS[1]?.label, '📖 READ', 'the mark the rack and the list already draw');
        for (const v of RECENT_VERDICTS) assert.ok(v.title && v.caption && v.caption !== v.title);
    });

    it('reads a graded turn back as its button’s label and RIGHT or WRONG', () => {
        assert.equal(verdict_chip(row()), null, 'ungraded: no chip');
        const right = verdict_chip(row({ band: 'done', operator: said(false) }));
        assert.deepEqual(right, { label: '✓ DONE', grade: 'right' });
        assert.equal(chip_text(right as NonNullable<typeof right>), '✓ DONE · RIGHT');
        const wrong = verdict_chip(row({ band: 'done', operator: said(true, 'agree', 'you') }));
        assert.equal(chip_text(wrong as NonNullable<typeof wrong>), '⚠ NEEDS YOU · WRONG');
        assert.deepEqual(verdict_chip(row({ band: 'reply', operator: said(false) })), { label: '✓ DONE', grade: null }, '💬 has no right answer');
        assert.equal(verdict_chip(row({ band: 'open', operator: said(true) }))?.label, 'OPEN', 'an OPEN answer recorded without its half');
        assert.equal(chip_text({ label: 'SKIP', grade: null }), 'SKIP');
    });

    it('ages a ledger stamp in the same short unit', () => {
        const now = Date.parse('2026-11-07T12:12:00Z');
        assert.equal(ts_age_text(Date.parse('2026-11-07T12:00:00Z') / 1000, now), '12m');
        assert.equal(ts_age_text(null, now), null);
        assert.equal(ts_age_text(undefined, now), null);
        assert.equal(ts_age_text(Number.NaN, now), null);
    });
});

describe('row_mark: 📖 for a done turn that answered a question', () => {
    it('a ✅ the operator asked for draws 📖, as the rack does', () => {
        assert.equal(row_mark('done', true), ANSWER_TO_READ_EMOJI);
        assert.match(row_mark_words('done', true), /answered your question/);
    });
    it('only on the done band, and only when the bridge says asked', () => {
        assert.equal(row_mark('done', false), BAND_MARK.done);
        assert.equal(row_mark('done', null), BAND_MARK.done); // not linked yet
        assert.equal(row_mark('done', undefined), BAND_MARK.done); // an older bridge
        assert.equal(row_mark('reply', true), BAND_MARK.reply);
        assert.equal(row_mark('open', true), BAND_MARK.open);
        assert.equal(row_mark_words('open', true), row_mark_words('open', false));
    });
});
