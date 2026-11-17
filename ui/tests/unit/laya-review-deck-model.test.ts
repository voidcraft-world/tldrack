import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { reviewCardSchema, reviewRetestCardSchema, reviewRetestRequestSchema, reviewAnswerRequestSchema, reviewStatsSchema, type ReviewCard, type ReviewStats } from '@tldrack/schemas/laya-review.ts';
import type { LayaFail } from '../../src/decide/laya-review-client.ts';
import {
    DEAL_STRATA,
    PAGE,
    REFILL_AT,
    agreement_readout,
    answer_caption,
    card_edge,
    card_title,
    challenger_note,
    compact_stats,
    deal_options,
    deal_waiting,
    deck_down,
    deficit_readout,
    deck_kappas,
    deck_key,
    deck_progress,
    explain_card,
    explain_deal,
    explain_stats,
    explain_verdicts,
    flip_label,
    interval_words,
    is_three_way,
    kappa_progress,
    kappa_readout,
    laya_readout,
    merge_cards,
    needs_refill,
    rate_words,
    reference_source,
    retest_choices,
    retest_key,
    retest_request,
    screen_after,
    self_kappa,
    short_key,
    show_switcher,
    stratum_from_search,
    stratum_label,
    stratum_tag_text,
    surface_from_search,
    surface_label,
    tail_title,
    three_way_readout,
    trained_tag,
    undo_target,
    verdict_caption,
    verdict_key,
    verdict_request,
    verdict_word,
    wave_tag,
    type DeckKeyEvent,
} from '../../src/laya-review/review-deck-model.ts';
import { laya_review_href, order_from_search } from '../../src/laya-review/open.ts';

// Fixtures parse through the contract, so a schema change that strands them fails here, not silently.
// CI is what the bridge computes for an invented 24-pair table (9, 1, 2, 12): κ 0.746, lopsided, so the ± is its downside.
const CI = { lo: 0.364, hi: 0.908, half_width: 0.382, level: 0.95, target_half_width: 0.15, pairs_for_target: 112 };
const STATS: ReviewStats = reviewStatsSchema.parse({
    surface: 'agent_open_work',
    label: 'OPEN WORK',
    question: 'Did the agent leave work open?',
    to_review: 412,
    backlog: 140,
    reviewed: 88,
    agree: 60,
    flip: 24,
    skip: 4,
    by_stratum: { sample: { reviewed: 50, remaining: 12 } },
    kappa_min_pairs: 20,
    operator_vs_sonnet: { n: 60, kappa: 0.712, agree_rate: 0.88, basis: 'sample' },
    operator_vs_laya: { n: 0, kappa: null, agree_rate: null, basis: 'scores' },
    precision: { window_days: 7, shown: 9, dismissed: 2, value: 0.78 },
    recall_est: { n_flagged: 9, n_unflagged: 4, value: null },
    sweep_ready: { rows: 12, open: 4, done: 8, ready: false, reason: null },
    last_answer_id: 'a_0123456789abcdef',
    last_answer_at: '2026-10-26T23:30:00Z',
});
// The same surface from a bridge that sends κ's interval and Laya's precision and recall at its cut.
const WITH_CI: ReviewStats = reviewStatsSchema.parse({
    ...STATS,
    operator_vs_sonnet: { ...STATS.operator_vs_sonnet, n: 24, kappa: 0.746, kappa_ci: CI },
    operator_vs_laya: {
        n: 24,
        kappa: 0.657,
        agree_rate: 0.833,
        basis: 'sample',
        // the bridge's output for (8, 2, 2, 12)
        kappa_ci: { ...CI, lo: 0.259, hi: 0.855, half_width: 0.398, pairs_for_target: 127 },
        at_cut: { precision: { n: 10, value: 0.8, lo: 0.49, hi: 0.954 }, recall: { n: 4, value: null, lo: null, hi: null } },
    },
});

function card(key: string, over: Partial<ReviewCard> = {}): ReviewCard {
    return reviewCardSchema.parse({
        item_id: `agent_open_work:${key}`,
        key,
        surface: 'agent_open_work',
        stratum: 'sample',
        text: 'Hand-off: the migration is written; the deploy is still owed.',
        text_chars: 61,
        truncated: false,
        held_out: true,
        reference: { by: 'sonnet-4.6', open: true, why: 'the deploy is owed' },
        sonnet: { by: 'sonnet-4.6', open: true, why: 'the deploy is owed' },
        laya: { p: 0.83, threshold: 0.7, model: 'voidcraft-agent-open-work', checkpoint: 'voidcraft-agent-open-work-20260101-0000', question_version: 2, source: 'scores' },
        golden: null,
        decision: null,
        answers: ['agree', 'flip', 'skip'],
        ...over,
    });
}

const KEY_A = '0123456789abcdef';
const KEY_B = 'fedcba9876543210';
const KEY_C = '00000000000000aa';

function key_event(code: string, over: Partial<DeckKeyEvent> = {}): DeckKeyEvent {
    return { code, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, repeat: false, target: null, ...over };
}

describe('deck_key', () => {
    it('maps the four plain keys', () => {
        assert.equal(deck_key(key_event('KeyA')), 'agree');
        assert.equal(deck_key(key_event('KeyF')), 'flip');
        assert.equal(deck_key(key_event('KeyS')), 'skip');
        assert.equal(deck_key(key_event('KeyU')), 'undo');
        assert.equal(deck_key(key_event('KeyQ')), null);
        // A prototype key is not a binding.
        assert.equal(deck_key(key_event('constructor')), null);
    });

    it('leaves every modified key to the browser and the OS', () => {
        for (const mod of ['metaKey', 'ctrlKey', 'altKey', 'shiftKey'] as const) {
            assert.equal(deck_key(key_event('KeyA', { [mod]: true })), null, mod);
        }
    });

    it('never answers twice from a held key', () => {
        assert.equal(deck_key(key_event('KeyA', { repeat: true })), null);
    });

    it('keeps letters typed into a field', () => {
        for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT']) {
            assert.equal(deck_key(key_event('KeyA', { target: { tagName } as unknown as EventTarget })), null, tagName);
        }
        assert.equal(deck_key(key_event('KeyA', { target: { tagName: 'DIV', isContentEditable: true } as unknown as EventTarget })), null);
        assert.equal(deck_key(key_event('KeyA', { target: { tagName: 'BUTTON', isContentEditable: false } as unknown as EventTarget })), 'agree');
    });
});

describe('merge_cards', () => {
    it('keeps the held order, appends new cards in the plugin order, and drops duplicates', () => {
        const held = [card(KEY_A), card(KEY_B)];
        const merged = merge_cards(held, [card(KEY_B), card(KEY_C), card(KEY_C)], new Set());
        assert.deepEqual(merged.map((c) => c.key), [KEY_A, KEY_B, KEY_C]);
        assert.equal(merged[1], held[1], 'a held card is the same object, not the refetched copy');
    });

    it('never deals a card answered on this page, held or fetched', () => {
        const merged = merge_cards([card(KEY_A)], [card(KEY_B), card(KEY_C)], new Set([`agent_open_work:${KEY_A}`, `agent_open_work:${KEY_C}`]));
        assert.deepEqual(merged.map((c) => c.key), [KEY_B]);
    });
});

describe('screen_after', () => {
    it('leads with a retest only when the hand advances', () => {
        assert.equal(screen_after('card', { retests: 2, cards: 9 }, true), 'retest', 'a fresh hand: the retests lead');
        assert.equal(screen_after('card', { retests: 0, cards: 9 }, true), 'card');
        assert.equal(screen_after('retest', { retests: 1, cards: 9 }, true), 'retest', 'the next retest');
        assert.equal(screen_after('retest', { retests: 0, cards: 9 }, true), 'card', 'the last retest was answered');
    });

    it('keeps the card on screen when a refill fetches a retest', () => {
        assert.equal(screen_after('card', { retests: 1, cards: 4 }, false), 'card', 'the retest waits behind the card being read');
        assert.equal(screen_after('card', { retests: 3, cards: 1 }, false), 'card');
        assert.equal(screen_after('retest', { retests: 2, cards: 30 }, false), 'retest', 'and a retest being read stays too');
        // ...until that card is answered: then the held retest takes the screen.
        assert.equal(screen_after('card', { retests: 1, cards: 3 }, true), 'retest');
    });

    it('gives an empty screen to whatever arrives', () => {
        assert.equal(screen_after('card', { retests: 1, cards: 0 }, false), 'retest', 'nobody is reading an empty deck');
        assert.equal(screen_after('retest', { retests: 0, cards: 2 }, false), 'card');
        assert.equal(screen_after('card', { retests: 0, cards: 0 }, false), 'card');
        assert.equal(screen_after('retest', { retests: 0, cards: 0 }, true), 'card');
    });
});

describe('needs_refill', () => {
    it('fetches at REFILL_AT while the plugin says more wait, and stops when dry', () => {
        assert.ok(PAGE > REFILL_AT);
        assert.equal(needs_refill(REFILL_AT, STATS, false), true);
        assert.equal(needs_refill(REFILL_AT + 1, STATS, false), false);
        assert.equal(needs_refill(3, { ...STATS, to_review: 3, backlog: 0 }, false), false, 'everything left is held');
        // The queue deals backlog cards too: one counted card still on the server behind a hand of BACKLOG.
        assert.equal(needs_refill(4, { ...STATS, to_review: 1, backlog: 140 }, false), true);
        assert.equal(needs_refill(3, { ...STATS, to_review: 0, backlog: 3 }, false), false);
        assert.equal(needs_refill(0, STATS, true), false, 'the last refill brought nothing');
        assert.equal(needs_refill(0, null, false), false);
    });

    it('narrowed by DEAL, counts only what that stratum has left', () => {
        assert.equal(needs_refill(5, STATS, false, 'sample'), true, '12 SAMPLE cards wait, 5 are held');
        assert.equal(needs_refill(5, { ...STATS, by_stratum: { sample: { reviewed: 50, remaining: 5 } } }, false, 'sample'), false);
        assert.equal(needs_refill(0, STATS, false, 'golden'), false, 'a stratum the stats do not list has nothing left');
    });
});

describe('DEAL', () => {
    const MIXED: ReviewStats = {
        ...STATS,
        by_stratum: {
            sample: { reviewed: 50, remaining: 12 },
            disagree: { reviewed: 3, remaining: 0 },
            unflagged: { reviewed: 0, remaining: 40 },
            recent: { reviewed: 9, remaining: 0 },
        },
    };

    it('reads ?stratum= as a bookmark: only a stratum the deck can deal', () => {
        assert.equal(stratum_from_search('?surface=agent_open_work&stratum=sample'), 'sample');
        assert.equal(stratum_from_search('?stratum=recent'), null, 'RECENT answers are never dealt');
        assert.equal(stratum_from_search('?stratum=__proto__'), null);
        assert.equal(stratum_from_search(''), null);
        assert.equal(DEAL_STRATA[0], 'sample', 'SAMPLE leads: κ is measured on it alone');
    });

    it('keeps DEAL in the address only on the deck view', () => {
        assert.equal(laya_review_href('agent_open_work', 'deck', { stratum: 'sample' }), '/review.html?surface=agent_open_work&stratum=sample');
        assert.equal(laya_review_href('agent_open_work', 'recent', { stratum: 'sample' }), '/review.html?surface=agent_open_work&view=recent');
        assert.equal(laya_review_href(undefined, 'deck', { stratum: null }), '/review.html');
        assert.equal(laya_review_href(undefined, 'recent', { order: 'edge' }), '/review.html?view=recent&order=edge');
        assert.equal(laya_review_href(undefined, 'recent', { order: 'newest' }), '/review.html?view=recent');
        assert.equal(laya_review_href(undefined, 'deck', { order: 'edge' }), '/review.html', 'the order is the RECENT view’s alone');
        assert.equal(order_from_search('?view=recent&order=edge'), 'edge');
        assert.equal(order_from_search('?order=oldest'), 'newest');
    });

    it('counts what each choice can still deal', () => {
        assert.equal(deal_waiting(MIXED, null), 412 + 140, 'ALL: everything counted, plus the backlog');
        assert.equal(deal_waiting(MIXED, 'sample'), 12);
        assert.equal(deal_waiting(MIXED, 'golden'), 0);
    });

    it('draws ALL, SAMPLE always, and only the strata with cards left — plus the one on screen', () => {
        assert.deepEqual(deal_options(MIXED, null).map((o) => [o.label, o.waiting]), [['ALL', 552], ['SAMPLE', 12], ['NEVER FLAGGED', 40]]);
        const empty_sample = { ...MIXED, by_stratum: { ...MIXED.by_stratum, sample: { reviewed: 62, remaining: 0 } } };
        assert.deepEqual(deal_options(empty_sample, null).map((o) => o.deal), [null, 'sample', 'unflagged'], 'an empty SAMPLE still says so');
        assert.deepEqual(deal_options(MIXED, 'disagree').map((o) => o.deal), [null, 'sample', 'disagree', 'unflagged'], 'the way back out stays');
        assert.match(deal_options(MIXED, null)[1].title, /κ is measured on/);
    });
});

describe('κ progress', () => {
    const SHORT: ReviewStats = { ...STATS, operator_vs_sonnet: { n: 7, kappa: null, agree_rate: 0.86, basis: 'sample' } };

    it('counts SAMPLE answers toward the minimum', () => {
        assert.deepEqual(kappa_progress(SHORT), { n: 7, min: 20, left: 13, ready: false });
        assert.deepEqual(kappa_progress(STATS), { n: 60, min: 20, left: 0, ready: true });
    });

    it('shows 7/20 on the strip until κ has its pairs, then the value', () => {
        assert.equal(deck_kappas(SHORT).sonnet.shown, '7/20');
        assert.equal(deck_kappas(SHORT).sonnet.text, '—', 'the shared readout keeps its dash');
        assert.equal(deck_kappas(STATS).sonnet.shown, '0.71');
        assert.equal(deck_kappas(STATS).laya.shown, '—', 'nothing scored: the title points at SCORE, not at 0/20');
        const all_one = { ...STATS, operator_vs_sonnet: { n: 25, kappa: null, agree_rate: 1, basis: 'sample' } };
        assert.equal(deck_kappas(all_one).sonnet.shown, '—');
    });
});

describe('EXPLAIN', () => {
    const SHORT: ReviewStats = { ...STATS, operator_vs_sonnet: { n: 7, kappa: null, agree_rate: 0.86, basis: 'sample' } };

    it('says what OPEN and DONE mean', () => {
        assert.match(explain_verdicts('agent_open_work'), /^OPEN = yes: .* DONE = no/);
        assert.match(explain_verdicts('news_gate'), /YES or NO/);
    });

    it('explains every figure on the strip, with the count κ still needs', () => {
        const [progress, sonnet, laya] = explain_stats(SHORT);
        assert.match(progress, /^88\/500: /);
        assert.match(sonnet, /7 of 20 so far, so it appears after 13 more/);
        assert.match(laya, /run SCORE/);
        assert.match(explain_stats(STATS)[1], /^κ SONNET 0\.71: .* over 60 SAMPLE answers/);
    });

    it('tells the interval, and Laya’s precision and recall at its cut, once they exist', () => {
        const [, sonnet, laya] = explain_stats(WITH_CI);
        assert.match(sonnet, /^κ SONNET 0\.75 ±0\.38: .* between 0\.36 and 0\.91; ±0\.15 needs about 112 answers\.$/);
        assert.match(laya, /^κ LAYA 0\.66 ±0\.40: .* At its cut, precision 0\.80 \(0\.49 to 0\.95\) on 10 it flagged\.$/);
        assert.equal(rate_words({ n: 4, value: null, lo: null, hi: null }, 'it flagged'), null, 'below its minimum: nothing to tell');
    });

    it('says what DEAL deals, and whether it moves κ', () => {
        assert.match(explain_deal(SHORT, null), /Pick SAMPLE/);
        assert.match(explain_deal(SHORT, 'sample'), /counts toward κ; SKIP does not\. 7 of 20 answered toward κ SONNET, 13 to go/);
        assert.match(explain_deal(SHORT, 'disagree'), /do not count toward κ/);
        assert.match(explain_deal(STATS, 'sample'), /κ SONNET has its 20/);
    });

    it('explains the card: why it was dealt, the reference, and Laya’s meter', () => {
        const lines = explain_card(card(KEY_A));
        assert.match(lines[0], /^Why this card: A random sample/);
        assert.equal(lines[1], 'SONNET labelled it OPEN, because “the deploy is owed”. That label is what AGREE keeps and FLIP reverses.');
        assert.match(lines[2], /LAYA 0\.83 .* flags OPEN at 0\.70 or above .* so it flagged this one/);
        assert.equal(lines.length, 3, 'held out, whole and not live: nothing more to say');
        const odd = explain_card(card(KEY_B, { held_out: false, truncated: true, text_chars: 9000, laya: null }));
        assert.ok(odd.some((l) => l.startsWith('TRAINED: ')));
        assert.ok(odd.some((l) => l.startsWith('TAIL: Showing the last')));
        assert.ok(!odd.some((l) => l.startsWith('LAYA ')));
    });

    it('captions each answer with the label it records, and κ only on SAMPLE', () => {
        const c = card(KEY_A);
        assert.equal(answer_caption(c, 'agree'), 'records OPEN, same as SONNET · counts toward κ');
        assert.equal(answer_caption(c, 'flip'), 'records DONE, overruling SONNET · counts toward κ');
        assert.equal(answer_caption(c, 'skip'), 'no label · the card leaves the deck');
        assert.equal(answer_caption(card(KEY_B, { stratum: 'disagree' }), 'agree'), 'records OPEN, same as SONNET');
    });
});

describe('labels', () => {
    it('names a surface from the decide registry, else from itself', () => {
        assert.equal(surface_label('agent_open_work'), 'OPEN WORK');
        assert.equal(surface_label('notes_triage'), 'NOTES TRIAGE');
        assert.equal(surface_label('toString'), 'TOSTRING');
    });

    it('says OPEN/DONE for open work and YES/NO elsewhere', () => {
        assert.equal(verdict_word('agent_open_work', true), 'OPEN');
        assert.equal(verdict_word('agent_open_work', false), 'DONE');
        assert.equal(verdict_word('news_gate', true), 'YES');
        assert.equal(verdict_word('news_gate', false), 'NO');
    });

    it('FLIP names the label it will record — the reference flipped', () => {
        assert.equal(flip_label(card(KEY_A)), 'FLIP → DONE');
        assert.equal(flip_label(card(KEY_A, { reference: { by: 'sonnet-4.6', open: false, why: null } })), 'FLIP → OPEN');
    });

    it('labels every stratum, and a newer one without breaking', () => {
        assert.deepEqual(
            ['sample', 'disagree', 'golden', 'flagged', 'unflagged', 'rest'].map((s) => stratum_label(s).label),
            ['SAMPLE', 'DISAGREE', 'GOLDEN', 'LAYA FLAGGED', 'NEVER FLAGGED', 'BACKLOG'],
        );
        assert.equal(stratum_label('late_night').label, 'LATE NIGHT');
        assert.ok(stratum_label('sample').why.length > 0);
    });

    it('reads the reference source from reference.by', () => {
        assert.equal(reference_source(card(KEY_A)), 'SONNET');
        assert.equal(reference_source(card(KEY_A, { reference: { by: 'golden:agent-open-work', open: false, why: 'x' } })), 'GOLDEN');
        assert.equal(reference_source(card(KEY_A, { reference: { by: 'laya:voidcraft-agent-open-work', open: true, why: null } })), 'LAYA');
        assert.equal(reference_source(card(KEY_A, { reference: { by: 'someone', open: true, why: null }, sonnet: null })), 'LAYA');
    });
});

describe('trained_tag', () => {
    it('draws only for a text the served fine-tune trained on', () => {
        assert.equal(trained_tag(card(KEY_A, { held_out: false })), true);
        assert.equal(trained_tag(card(KEY_A, { held_out: true })), false);
        assert.equal(trained_tag(card(KEY_A, { held_out: null })), false);
    });

    it('puts an unknown held-out state in the title, and only there', () => {
        assert.match(card_title(card(KEY_A, { held_out: null })), /Unknown whether the served fine-tune trained/);
        assert.doesNotMatch(card_title(card(KEY_A, { held_out: true })), /fine-tune/);
    });

    it('TAIL titles its own truncation; the stratum title leaves it to TAIL', () => {
        const tail = card(KEY_A, { truncated: true, text_chars: 9_000 });
        assert.equal(tail_title(tail), 'Showing the last 61 of 9,000 characters');
        assert.doesNotMatch(card_title(tail), /characters/);
    });
});

describe('readouts', () => {
    it('deck_progress is 0 of 0 on an empty surface, never NaN', () => {
        assert.deepEqual(deck_progress({ ...STATS, reviewed: 0, to_review: 0 }), { done: 0, total: 0, ratio: 0, text: '0/0' });
        assert.deepEqual(deck_progress(null), { done: 0, total: 0, ratio: 0, text: '0/0' });
        assert.equal(deck_progress(STATS).text, '88/500');
        assert.equal(deck_progress(STATS).ratio, 88 / 500);
    });

    it('kappa_readout: a value, or a dash whose title says why', () => {
        assert.deepEqual(kappa_readout(0.712, 60, 20), { text: '0.71', title: 'Cohen’s κ over 60 paired answers' });
        assert.deepEqual(kappa_readout(null, 7, 20), { text: '—', title: 'after 20 paired answers (7 so far)' });
        const no_pairs_hint = 'run SCORE in AGENT RACK › LAYA';
        assert.equal(kappa_readout(null, 0, 20, { no_pairs_hint }).title, 'run SCORE in AGENT RACK › LAYA');
        assert.equal(kappa_readout(null, 3, 20, { no_pairs_hint }).title, 'after 20 paired answers (3 so far)');
        assert.match(kappa_readout(null, 25, 20).title, /share one label/);
    });

    it('kappa_readout: the ± rides the value once the bridge sends the interval, and the title says what it means', () => {
        const readout = kappa_readout(0.746, 24, 20, { ci: CI });
        assert.equal(readout.text, '0.75 ±0.38');
        assert.equal(readout.title, 'Cohen’s κ over 24 paired answers · 95% interval 0.36 to 0.91 · ±0.15 needs about 112');
        // the bridge's output for (56, 6, 7, 61): 130 pairs, downside 0.13
        const settled = kappa_readout(0.8, 130, 20, { ci: { ...CI, lo: 0.669, hi: 0.877, half_width: 0.131, pairs_for_target: 130 } });
        assert.equal(settled.text, '0.80 ±0.13');
        assert.match(settled.title, /· within ±0\.15$/);
        // 24 of 24 agreements: κ sits past hi, and the ± is still its distance to lo, so 24 pairs never read settled
        const perfect = kappa_readout(1, 24, 20, { ci: { ...CI, lo: 0.729, hi: 0.998, half_width: 0.271, pairs_for_target: 48 } });
        assert.equal(perfect.text, '1.00 ±0.27');
        assert.match(perfect.title, /±0\.15 needs about 48$/);
        assert.equal(kappa_readout(null, 7, 20, { ci: CI }).text, '—', 'no κ, no ±: below the minimum the interval is mostly the prior');
        assert.equal(interval_words(CI), '95% interval 0.36 to 0.91 · ±0.15 needs about 112');
    });

    it('agreement_readout reads one block, so every surface prints the same κ ±', () => {
        assert.equal(agreement_readout(WITH_CI.operator_vs_sonnet, 20).text, '0.75 ±0.38');
        assert.equal(agreement_readout(STATS.operator_vs_sonnet, 20).text, '0.71', 'an older bridge: the value alone');
        assert.equal(deck_kappas(WITH_CI).sonnet.shown, '0.75 ±0.38');
    });

    it('κ LAYA with nothing scored points at SCORE, once something was answered', () => {
        assert.equal(deck_kappas(STATS).laya.title, 'run SCORE (laya-lab review-open-work)');
        assert.equal(deck_kappas({ ...STATS, reviewed: 0 }).laya.title, 'after 20 paired answers (0 so far)');
        assert.equal(deck_kappas(STATS).sonnet.text, '0.71');
    });

    it('folds the strip to one phone readout', () => {
        assert.equal(compact_stats(STATS), '88/500 · κ .71');
        assert.equal(compact_stats({ ...STATS, operator_vs_sonnet: { ...STATS.operator_vs_sonnet, kappa: -0.12 } }), '88/500 · κ -.12');
        assert.equal(compact_stats({ ...STATS, operator_vs_sonnet: { ...STATS.operator_vs_sonnet, kappa: null } }), '88/500 · κ —');
        assert.equal(compact_stats(WITH_CI), '88/500 · κ .75±.38');
    });

    it('draws Laya’s p against its cut', () => {
        const laya = laya_readout(card(KEY_A))!;
        assert.equal(laya.text, 'LAYA 0.83');
        assert.equal(laya.fill, '83.0%');
        assert.equal(laya.mark, '70.0%');
        assert.equal(laya.flagged, true);
        assert.equal(laya_readout(card(KEY_A, { laya: null })), null);
    });

    it('show_switcher only when a surface other than the one on screen has work', () => {
        const here = STATS.surface;
        assert.equal(show_switcher([], null), false);
        assert.equal(show_switcher([STATS], here), false);
        assert.equal(show_switcher([STATS, { ...STATS, surface: 'paste', to_review: 0 }], here), false);
        assert.equal(show_switcher([STATS, { ...STATS, surface: 'paste', to_review: 3 }], here), true);
        // ALL REVIEWED here while one other surface waits: the switcher is the way there.
        assert.equal(show_switcher([{ ...STATS, to_review: 0 }, { ...STATS, surface: 'paste', to_review: 3 }], here), true);
    });

    it('short_key names what came back', () => {
        assert.equal(short_key('3f9a0123456789ab'), '3f9a…');
        assert.equal(short_key('golden:case-12'), 'case-12');
    });
});

describe('undo_target', () => {
    it('takes this page’s newest answer first, else the surface’s last', () => {
        const stack = [
            { answer_id: 'a_1111111111111111', surface: 'agent_open_work', item_id: 'x' },
            { answer_id: 'a_2222222222222222', surface: 'agent_open_work', item_id: 'y' },
        ];
        assert.deepEqual(undo_target(stack, STATS), { answer_id: 'a_2222222222222222', surface: 'agent_open_work' });
        assert.deepEqual(undo_target([], STATS), { answer_id: 'a_0123456789abcdef', surface: 'agent_open_work' });
        // A wave's press comes back whole; `wave` rides only then, since an older bridge refuses a field it lacks.
        const wave_entry = { ...stack.at(-1)!, wave: true };
        assert.deepEqual(undo_target([wave_entry], STATS), { answer_id: 'a_2222222222222222', surface: 'agent_open_work', wave: true });
        assert.deepEqual(undo_target([], { ...STATS, last_answer_wave: true }), { answer_id: 'a_0123456789abcdef', surface: 'agent_open_work', wave: true });
        assert.equal(undo_target([], { ...STATS, last_answer_id: null }), null);
        assert.equal(undo_target([], null), null);
    });
});

describe('surface_from_search', () => {
    it('keeps a well-formed surface and refuses anything else', () => {
        assert.equal(surface_from_search('?surface=agent_open_work'), 'agent_open_work');
        assert.equal(surface_from_search(''), null);
        assert.equal(surface_from_search('?surface=..%2Fetc'), null);
        assert.equal(surface_from_search('?surface=Agent'), null);
    });
});

describe('deck_down', () => {
    const fail = (over: Partial<LayaFail<unknown>>): LayaFail<unknown> => ({ ok: false, kind: 'refused', status: 503, code: null, error: 'x', body: null, ...over });

    it('gives CONNECT only to a gate that is merely off', () => {
        const gate = fail({ kind: 'gate', status: 0 });
        assert.deepEqual([deck_down(gate, 'locked').head, deck_down(gate, 'locked').connect], ['LOCAL TOOLS OFF', true]);
        // A Chrome block needs its settings, and a pending prompt an answer: CONNECT would do nothing for either.
        assert.deepEqual([deck_down(gate, 'denied').head, deck_down(gate, 'denied').connect], ['LOCAL TOOLS BLOCKED', false]);
        assert.deepEqual([deck_down(gate, 'open').head, deck_down(gate, 'open').connect], ['BROWSER PROMPT', false]);
        for (const kind of ['down', 'plugin', 'restart', 'refused', 'invalid'] as const) {
            assert.equal(deck_down(fail({ kind }), 'locked').connect, false, kind);
            assert.equal(deck_down(fail({ kind }), 'open').kind, kind);
        }
    });

    it('shows a refusal’s hint as the remedy and its code as the head', () => {
        const down = deck_down(fail({ code: 'params_unavailable', error: 'The laya data folder is not there', hint: 'restart the bridge' }), 'open');
        assert.deepEqual(down, { kind: 'refused', head: 'PARAMS UNAVAILABLE', why: 'The laya data folder is not there', how: 'restart the bridge', connect: false });
        assert.equal(deck_down(fail({ code: null, status: 403 }), 'open').head, 'REFUSED · 403');
    });

    it('a bridge too old names its remedy once', () => {
        const down = deck_down(fail({ kind: 'restart', status: 404, error: 'The running bridge predates this Laya route. Restart the bridge.' }), 'open');
        assert.match(down.how ?? '', /Restart the bridge/);
        assert.doesNotMatch(down.why, /restart/i);
    });
});


/** A wave card's `wave` block: the lead and two riders. */
const WAVE = {
    n: 3,
    members: [
        { key: KEY_B, at: '2026-11-09T03:00:00Z', p: 0.81, stratum: 'flagged', text: 'Still waiting on two child agents.', text_chars: 34 },
        { key: KEY_C, at: '2026-11-09T03:02:00Z', p: 0.79, stratum: 'recent', text: 'One child left.', text_chars: 15 },
    ],
};

describe('acquisition', () => {
    it('deals UNCERTAIN right after SAMPLE, and names every new stratum', () => {
        assert.deepEqual(DEAL_STRATA.slice(0, 2), ['sample', 'uncertain']);
        for (const s of ['uncertain', 'live_sample', 'suspect'] as const) {
            assert.ok(DEAL_STRATA.includes(s), s);
            assert.notEqual(stratum_label(s).why, `Sampled as ${s}`, `${s} has its own words`);
        }
        assert.equal(stratum_label('live_sample').label, 'LIVE SAMPLE');
        assert.match(explain_deal(STATS, 'live_sample'), /how often the rack/);
    });

    it('a challenger card leads with the challenger’s own score and says which way it went', () => {
        assert.ok(DEAL_STRATA.includes('challenger'));
        assert.equal(DEAL_STRATA.indexOf('challenger'), 2, 'right after UNCERTAIN: its grades gate a promotion');
        const rival = card(KEY_A, { stratum: 'challenger', challenger: { name: 'voidcraft-agent-open-work-20260102-0000', p: 0.3, threshold: 0.7 } });
        assert.equal(stratum_tag_text(rival), 'CHALLENGER 0.30');
        assert.match(challenger_note(rival) ?? '', /would not have flagged this turn at its own cut 0\.70/);
        assert.ok(explain_card(rival).some((line) => line.startsWith('CHALLENGER 0.30')));
        assert.match(card_title(rival), /CHALLENGER 0\.30/);
        assert.match(explain_deal(STATS, 'challenger'), /sided with the challenger/);
        const plain = card(KEY_A, { stratum: 'uncertain' });
        assert.equal(stratum_tag_text(plain), 'UNCERTAIN');
        assert.equal(challenger_note(plain), null);
        assert.equal(challenger_note(card(KEY_A, { stratum: 'flagged', challenger: null })), null);
    });

    it('says which class is short, and nothing once neither is', () => {
        const short = deficit_readout({ ...STATS, deficit: 'open' });
        assert.equal(short?.text, 'OPEN SHORT ▲');
        assert.match(short?.title ?? '', /4 clean OPEN and 8 clean DONE/);
        assert.equal(deficit_readout({ ...STATS, deficit: null }), null);
        assert.equal(deficit_readout(STATS), null, 'an older bridge sends none');
        assert.ok(explain_stats({ ...STATS, deficit: 'done' }).some((line) => line.startsWith('DONE SHORT ▲')));
    });

    it('notes a card within a hair of a cut, in the RECENT list’s own words', () => {
        assert.equal(card_edge(card(KEY_A, { stratum: 'uncertain', edge: { cut: 'open', delta: 0.03 } }))?.text, '⚠️ BY 0.03');
        assert.equal(card_edge(card(KEY_A, { edge: { cut: 'open', delta: 0.13 } })), null, 'past EDGE_NEAR: no note');
        assert.equal(card_edge(card(KEY_A, { edge: null })), null);
        assert.equal(card_edge(card(KEY_A, { laya: null, edge: { cut: 'open', delta: 0 } })), null);
    });

    it('tags a wave and says one answer grades all of it', () => {
        const wave = card(KEY_A, { stratum: 'uncertain', wave: WAVE });
        assert.equal(wave_tag(WAVE).text, 'WAVE ×3');
        assert.match(answer_caption(wave, 'agree'), /records OPEN on all 3/);
        assert.match(answer_caption(wave, 'skip'), /any of the 3/);
        assert.ok(explain_card(wave).some((line) => /UNDO takes all 3 back/.test(line)));
        assert.doesNotMatch(answer_caption(card(KEY_A), 'agree'), /on all/);
    });

    it('parses a wave card the plugin may send', () => {
        assert.equal(card(KEY_A, { wave: WAVE }).wave?.members.length, 2);
        assert.throws(() => card(KEY_A, { wave: { ...WAVE, n: 1 } }), 'a wave is two or more');
    });
});

describe('the three-way grade on the deck', () => {
    const THREE: ReviewStats = reviewStatsSchema.parse({ ...STATS, three_way: { done: 22, running: 5, you: 2, min: 20, deficit: 'you', lean: 'you' } });

    it('is on only where the bridge sends the three-way counts', () => {
        assert.equal(is_three_way(THREE), true);
        assert.equal(is_three_way(STATS), false, 'an older bridge, or a surface without the OPEN halves');
        assert.equal(is_three_way(reviewStatsSchema.parse({ ...STATS, three_way: null })), false);
        assert.equal(is_three_way(null), false);
    });

    it('reads D, R, N, S and U, and leaves A and F to the two-answer deck', () => {
        assert.deepEqual(['KeyD', 'KeyR', 'KeyN', 'KeyS', 'KeyU'].map((code) => verdict_key(key_event(code))), ['done', 'running', 'you', 'skip', 'undo']);
        assert.equal(verdict_key(key_event('KeyA')), null);
        assert.equal(verdict_key(key_event('KeyD', { metaKey: true })), null);
        assert.equal(verdict_key(key_event('KeyD', { repeat: true })), null);
        assert.equal(deck_key(key_event('KeyD')), null);
    });

    it('maps a verdict onto the card’s reference, with the OPEN half and the wave it showed', () => {
        const open = card(KEY_A);
        assert.deepEqual(verdict_request(open, 'you'), { surface: 'agent_open_work', key: KEY_A, answer: 'agree', reference_open: true, needs: 'you' });
        assert.deepEqual(verdict_request(open, 'done'), { surface: 'agent_open_work', key: KEY_A, answer: 'flip', reference_open: true });
        assert.deepEqual(verdict_request(open, 'skip'), { surface: 'agent_open_work', key: KEY_A, answer: 'skip', reference_open: true });
        const done = card(KEY_B, { reference: { by: 'sonnet-4.6', open: false, why: null } });
        assert.deepEqual(verdict_request(done, 'running'), { surface: 'agent_open_work', key: KEY_B, answer: 'flip', reference_open: false, needs: 'running' });
        assert.equal(verdict_request(done, 'done').answer, 'agree');
        const wave = verdict_request(card(KEY_A, { stratum: 'uncertain', wave: WAVE }), 'running');
        assert.deepEqual(wave.wave, WAVE.members.map((m) => m.key));
        for (const request of [verdict_request(open, 'you'), verdict_request(done, 'running'), wave]) assert.ok(reviewAnswerRequestSchema.safeParse(request).success);
    });

    it('captions a verdict with what it does to the reference', () => {
        assert.match(verdict_caption(card(KEY_A), 'you'), /needs your answer.* · same as SONNET · counts toward κ$/);
        assert.match(verdict_caption(card(KEY_A), 'done'), / · overruling SONNET · counts toward κ$/);
        assert.match(verdict_caption(card(KEY_A, { stratum: 'flagged', wave: WAVE }), 'running'), / · same as SONNET · on all 3$/);
        assert.doesNotMatch(verdict_caption(card(KEY_A), 'skip'), /SONNET/);
        assert.ok(explain_card(card(KEY_A), true).some((line) => line.endsWith('RUNNING and NEEDS YOU keep that label (both are OPEN); DONE overrules it.')));
        assert.ok(explain_card(card(KEY_B, { reference: { by: 'sonnet-4.6', open: false, why: null } }), true).some((line) => line.endsWith('DONE keeps that label; RUNNING and NEEDS YOU (both OPEN) overrule it.')));
        assert.ok(explain_card(card(KEY_A)).some((line) => /AGREE keeps and FLIP reverses/.test(line)));
    });

    it('says which class is short of its floor, and nothing once each has it', () => {
        const short = three_way_readout(THREE);
        assert.equal(short?.text, 'NEEDS YOU 2/20 ▲');
        assert.match(short?.title ?? '', /22 DONE, 5 RUNNING and 2 NEEDS YOU labels so far.*live cards likely to be NEEDS YOU come first$/);
        assert.equal(three_way_readout({ ...THREE, three_way: { ...THREE.three_way!, running: 0, deficit: 'running', lean: 'running' } })?.text, 'RUNNING 0/20 ▲');
        assert.equal(three_way_readout({ ...THREE, three_way: { ...THREE.three_way!, deficit: null, lean: null } }), null);
        assert.equal(three_way_readout(STATS), null);
    });

    it('claims a lean only toward an OPEN half: DONE short is the gate’s readout and nothing comes first', () => {
        // DONE the shortest class while the halves are short too: the deck leans toward a half, so the tag names that half.
        const mixed = three_way_readout({ ...THREE, three_way: { done: 1, running: 3, you: 2, min: 20, deficit: 'done', lean: 'you' } });
        assert.equal(mixed?.text, 'NEEDS YOU 2/20 ▲');
        assert.match(mixed?.title ?? '', /^1 DONE, 3 RUNNING and 2 NEEDS YOU labels so far.*likely to be NEEDS YOU come first$/);
        // Both halves hold the floor: DONE keeps the test waiting, and no live card leans toward it.
        const gate = three_way_readout({ ...THREE, three_way: { done: 14, running: 20, you: 21, min: 20, deficit: 'done', lean: null } });
        assert.equal(gate?.text, 'DONE 14/20');
        assert.doesNotMatch(gate?.title ?? '', /come first/);
        assert.match(gate?.title ?? '', /each needs 20 before a NEEDS-YOU mark can be tested$/);
        assert.ok(explain_stats(THREE).some((line) => line.startsWith('NEEDS YOU 2/20 ▲: ')));
    });
});

describe('the blind retest on the deck', () => {
    const BLIND = reviewRetestCardSchema.parse({
        item_id: `agent_open_work:retest:${KEY_A}`, key: KEY_A, surface: 'agent_open_work', of: 'a_0123456789abcdef',
        text: 'Hand-off: the migration is written; the deploy is still owed.', text_chars: 61, truncated: false, at: null, needs: ['you', 'running'],
    });
    const AGREEMENT = { n: 0, kappa: null, agree_rate: null, basis: 'retest', kappa_ci: null };
    const retest = (over: object = {}) => ({ percent: 10, after_days: 7, due: 2, pairs: 0, open: AGREEMENT, needs: { you: AGREEMENT, running: AGREEMENT }, ...over });

    it('offers the three-way verdicts where the surface takes the halves, else DONE, OPEN and SKIP', () => {
        assert.deepEqual(retest_choices(BLIND).map((c) => [c.id, c.key, c.open, c.needs]), [
            ['done', 'D', false, undefined], ['running', 'R', true, 'running'], ['you', 'N', true, 'you'], ['skip', 'S', null, undefined],
        ]);
        assert.deepEqual(retest_choices({ needs: [] }).map((c) => [c.id, c.key, c.open]), [['done', 'D', false], ['open', 'O', true], ['skip', 'S', null]]);
        assert.deepEqual(retest_choices({ needs: ['you'] }).map((c) => c.id), ['done', 'you', 'skip']);
    });

    it('sends only what the second look said, and never a reference', () => {
        const [done, running, , skip] = retest_choices(BLIND);
        assert.deepEqual(retest_request(BLIND, done!), { surface: 'agent_open_work', key: KEY_A, of: 'a_0123456789abcdef', open: false });
        assert.deepEqual(retest_request(BLIND, running!), { surface: 'agent_open_work', key: KEY_A, of: 'a_0123456789abcdef', open: true, needs: 'running' });
        assert.deepEqual(retest_request(BLIND, skip!), { surface: 'agent_open_work', key: KEY_A, of: 'a_0123456789abcdef', open: null });
        for (const choice of retest_choices(BLIND)) assert.ok(reviewRetestRequestSchema.safeParse(retest_request(BLIND, choice)).success, choice.id);
    });

    it('reads the card’s own keys, and U', () => {
        const choices = retest_choices(BLIND);
        assert.equal((retest_key(key_event('KeyN'), choices) as { id: string }).id, 'you');
        assert.equal(retest_key(key_event('KeyU'), choices), 'undo');
        assert.equal(retest_key(key_event('KeyA'), choices), null);
        assert.equal(retest_key(key_event('KeyO'), choices), null, 'OPEN is a choice only where there are no halves');
        assert.equal(retest_key(key_event('KeyD', { ctrlKey: true }), choices), null);
    });

    it('merges into the hand like any card, and its id never meets a deck card’s', () => {
        assert.deepEqual(merge_cards([], [BLIND, BLIND], new Set()), [BLIND]);
        assert.deepEqual(merge_cards([BLIND], [BLIND], new Set([BLIND.item_id])), []);
        assert.notEqual(BLIND.item_id, card(KEY_A).item_id);
    });

    it('reads κ SELF as pairs until it has a κ, and draws nothing with no pair and none due', () => {
        const counting = self_kappa(reviewStatsSchema.parse({ ...STATS, retest: retest({ pairs: 6, open: { ...AGREEMENT, n: 6 } }) }));
        assert.equal(counting?.shown, '6/20');
        assert.match(counting?.title ?? '', /^You against your own label 7\+ days earlier, graded blind · after 20 paired answers \(6 so far\) · NEEDS YOU — \(0\) · RUNNING — \(0\) · 2 due a second look$/);
        const settled = self_kappa(reviewStatsSchema.parse({
            ...STATS,
            retest: retest({ due: 0, pairs: 24, open: { ...AGREEMENT, n: 24, kappa: 0.746, kappa_ci: CI }, needs: { you: { ...AGREEMENT, n: 22, kappa: 0.61 }, running: AGREEMENT } }),
        }));
        assert.equal(settled?.shown, '0.75 ±0.38');
        assert.match(settled?.title ?? '', /NEEDS YOU 0\.61 \(22\)/);
        assert.equal(self_kappa(reviewStatsSchema.parse({ ...STATS, retest: retest({ due: 0 }) })), null, 'nothing to show yet');
        assert.equal(self_kappa(STATS), null, 'an older bridge');
        assert.ok(explain_stats(reviewStatsSchema.parse({ ...STATS, retest: retest() })).some((line) => /^κ SELF 0\/20: .*ceiling/.test(line)));
        assert.ok(!explain_stats(STATS).some((line) => line.startsWith('κ SELF')));
    });

    it('titles a truncated retest card’s TAIL like a deck card’s', () => {
        assert.equal(tail_title({ text: 'abc', text_chars: 4200 }), 'Showing the last 3 of 4,200 characters');
    });
});
