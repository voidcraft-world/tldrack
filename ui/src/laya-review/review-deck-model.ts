/**
 * The REVIEW deck's rules — keys, the card merge, labels and readouts — pure, so they run under bare
 * `node --test`. `LayaReviewDeck.vue` owns only the fetches and the DOM; the wire is
 * `schemas/src/laya-review.ts`. How the strata are drawn is the plugin's (`laya_bridge/review.py`).
 */
import { DECIDE_SURFACES } from '@tldrack/schemas/decide.ts';
import {
    REVIEW_GRADES,
    layaReviewSurfaceSchema,
    type ReviewAgreement,
    type ReviewAnswer,
    type ReviewAnswerRequest,
    type ReviewCard,
    type ReviewKappaInterval,
    type ReviewNeeds,
    type ReviewRateAtCut,
    type ReviewRetestCard,
    type ReviewRetestRequest,
    type ReviewStats,
} from '@tldrack/schemas/laya-review.ts';

import type { LayaFail, LayaFailKind } from '../decide/laya-review-client.ts';
import type { LocalAccessState } from '../engine/terminal/local-network-gate.ts';
import { RECENT_VERDICTS, answer_request, edge_note, is_open_verdict, type RecentVerdict } from './recent-model.ts';

/** Cards per fetch, and the hand size that triggers the next one — the next card is always already here. */
export const PAGE = 20;
export const REFILL_AT = 5;

export type DeckAction = ReviewAnswer | 'undo';

const KEY_ACTIONS: ReadonlyMap<string, DeckAction> = new Map([
    ['KeyA', 'agree'],
    ['KeyF', 'flip'],
    ['KeyS', 'skip'],
    ['KeyU', 'undo'],
]);

/** The parts of a `KeyboardEvent` the deck reads — a real one fits, and so does a test's object. */
export interface DeckKeyEvent {
    code: string;
    metaKey: boolean;
    ctrlKey: boolean;
    altKey: boolean;
    shiftKey: boolean;
    repeat: boolean;
    target: EventTarget | null;
}

function is_typing_target(target: EventTarget | null): boolean {
    const el = target as { tagName?: unknown; isContentEditable?: unknown } | null;
    if (!el) return false;
    return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable === true;
}

/**
 * Plain keys only. Any modifier leaves the key to the browser and the OS (⌘A, ⌘F, …), a held key never answers
 * twice, and a field being typed in keeps its letters.
 */
function plain_key<T>(e: DeckKeyEvent, keys: ReadonlyMap<string, T>): T | null {
    if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || e.repeat) return null;
    if (is_typing_target(e.target)) return null;
    return keys.get(e.code) ?? null;
}

/** A, F, S, U: the deck's keys on a surface whose answers are AGREE and FLIP (`plain_key` owns which presses count). */
export function deck_key(e: DeckKeyEvent): DeckAction | null {
    return plain_key(e, KEY_ACTIONS);
}

/**
 * The hand after a fetch: what is held keeps its order (the card on screen never jumps), then each fetched card
 * not already held and not answered on this page, in the plugin's order. A retest card merges the same way; whether
 * a retest or a card is the one on screen is `screen_after`'s.
 */
export function merge_cards<T extends { item_id: string }>(held: readonly T[], fetched: readonly T[], answered: ReadonlySet<string>): T[] {
    const out = held.filter((card) => !answered.has(card.item_id));
    const seen = new Set(out.map((card) => card.item_id));
    for (const card of fetched) {
        if (seen.has(card.item_id) || answered.has(card.item_id)) continue;
        seen.add(card.item_id);
        out.push(card);
    }
    return out;
}

/** Which list the screen reads: the head of the blind retests, or the head of the deck cards. */
export type DeckScreen = 'retest' | 'card';

/**
 * THE ITEM ON SCREEN IS STICKY. A blind retest leads the hand, but only at an ADVANCE: a fresh hand was dealt, or the
 * item on screen was answered and left. Between advances the screen keeps the list it reads, whatever a refill
 * fetched: a refill lands some hundred milliseconds into the read (and, mounted inside a host application's page, on
 * every refresh that host asks for), and a retest taking the screen then would swap the text under the operator. On a three-way surface
 * the card and the retest share D, R, N and S, so the key meant for the card would write a `retest` row, a κ SELF
 * pair, for a text nobody read. A fetched retest therefore waits behind the card on screen until that card leaves.
 *
 * The head of either list never moves under a refill (`merge_cards`), so which list is read is the whole state. A
 * list that has emptied yields the screen at once: nobody is reading an empty deck. The operator's own UNDO is the one
 * other thing that changes the screen, and the deck sets that itself: the card it deals back is shown ahead of a
 * retest still held.
 */
export function screen_after(now: DeckScreen, held: { retests: number; cards: number }, advance: boolean): DeckScreen {
    if (advance || now === 'retest') return held.retests > 0 ? 'retest' : 'card';
    return held.cards === 0 && held.retests > 0 ? 'retest' : 'card';
}

/**
 * Fetch more once the hand is down to `REFILL_AT` and the plugin says more wait than are held. "Wait" counts the
 * backlog too: `to_review` leaves the `rest` stratum out, but the queue still deals it, so a hand of BACKLOG cards
 * would otherwise run dry before the last counted card is asked for. `dry` is set when the last refill brought
 * nothing new, so a queue the plugin cannot serve past never loops.
 */
export function needs_refill(held: number, stats: ReviewStats | null, dry: boolean, deal: DealStratum | null = null): boolean {
    return !dry && stats !== null && held <= REFILL_AT && deal_waiting(stats, deal) > held;
}

// ── DEAL: narrowing the deck to one stratum ─────────────────────────────────

/**
 * The strata DEAL can narrow the deck to, in the order its buttons draw: SAMPLE first, since κ is measured on it
 * alone, then UNCERTAIN, the fastest way to the clean labels a RE-SWEEP waits on. The plugin's queue takes
 * `?stratum=` (`laya_bridge/review.py` `queue`); `recent` is the RECENT view's own answers and is never dealt, so it is
 * not here. A stratum with no cards left draws no button (`deal_options`), so the strip stays as short as the work.
 */
export const DEAL_STRATA = ['sample', 'uncertain', 'challenger', 'live_sample', 'suspect', 'disagree', 'unflagged', 'flagged', 'golden', 'rest'] as const;
export type DealStratum = (typeof DEAL_STRATA)[number];

export function is_deal_stratum(raw: unknown): raw is DealStratum {
    return typeof raw === 'string' && (DEAL_STRATA as readonly string[]).includes(raw);
}

/** `?stratum=` off the page's own URL — a bookmark, so it is checked like any other input. */
export function stratum_from_search(search: string): DealStratum | null {
    const raw = new URLSearchParams(search).get('stratum');
    return is_deal_stratum(raw) ? raw : null;
}

/** How many cards the deck can still deal: one stratum's remaining, or (ALL) everything counted plus the backlog. */
export function deal_waiting(stats: ReviewStats, deal: DealStratum | null): number {
    return deal === null ? stats.to_review + stats.backlog : (stats.by_stratum[deal]?.remaining ?? 0);
}

export interface DealOption {
    deal: DealStratum | null;
    label: string;
    waiting: number;
    title: string;
}

/**
 * DEAL's buttons: ALL, then each stratum with cards left. SAMPLE always draws (it is the one κ waits on, and an
 * empty one should say so), and so does the stratum on screen, so the way back out never disappears.
 */
export function deal_options(stats: ReviewStats, current: DealStratum | null): DealOption[] {
    const all: DealOption = { deal: null, label: 'ALL', waiting: deal_waiting(stats, null), title: 'Every kind of card, in turn' };
    const narrowed = DEAL_STRATA.filter((s) => s === 'sample' || s === current || deal_waiting(stats, s) > 0).map((s) => ({
        deal: s,
        label: stratum_label(s).label,
        waiting: deal_waiting(stats, s),
        title: stratum_label(s).why,
    }));
    return [all, ...narrowed];
}

/** κ SONNET's progress toward its first value: SAMPLE answers (never a SKIP) so far, against the minimum. */
export function kappa_progress(stats: ReviewStats): { n: number; min: number; left: number; ready: boolean } {
    const { n } = stats.operator_vs_sonnet;
    const min = stats.kappa_min_pairs;
    return { n, min, left: Math.max(0, min - n), ready: n >= min };
}

// ── EXPLAIN: the sentence version of every mark on the deck ─────────────────
// The decision since 2026-11-04: this page has one reader, the operator, so it spells everything out. EXPLAIN is ON
// by default (`review-prefs.ts`). Elsewhere a second telling of a mark is off by default; here it is the point.

/** What the two answers MEAN, under the question. */
export function explain_verdicts(surface: string): string {
    if (surface === 'agent_open_work') {
        return 'OPEN = yes: something is unfinished, not started, blocked, or waiting on an answer. DONE = no: nothing is left. You are judging the agent’s closing message, not whether you have to act.';
    }
    return 'YES or NO answers the question above, for the text on the card.';
}

/** The strip's figures, one line each. */
export function explain_stats(stats: ReviewStats): string[] {
    const { done, total } = deck_progress(stats);
    const sonnet = stats.operator_vs_sonnet;
    const laya = stats.operator_vs_laya;
    const { n, min, left, ready } = kappa_progress(stats);
    const scale = '1 = you always agree, 0 = no better than luck, above 0.6 is strong';
    const sonnet_line = ready
        ? sonnet.kappa === null
            ? `κ SONNET: undefined while all ${n} SAMPLE answers share one label; it needs at least one of each.`
            : `κ SONNET ${agreement_readout(sonnet, min).text}: how far you agree with Sonnet beyond chance, over ${n} SAMPLE answers (${scale}).${interval_sentence(sonnet.kappa_ci)}`
        : `κ SONNET: how far you agree with Sonnet beyond chance (${scale}). Counted only on SAMPLE cards you answer, never one you SKIP: ${n} of ${min} so far, so it appears after ${left} more.`;
    const laya_line =
        laya.kappa !== null
            ? `κ LAYA ${agreement_readout(laya, min).text}: the same, against Laya’s flag (its score at or past the cut), over ${laya.n} SAMPLE answers.${interval_sentence(laya.kappa_ci)}${at_cut_sentence(laya)}`
            : laya.n === 0 && stats.reviewed > 0
              ? 'κ LAYA: the same, against Laya’s flag. No SAMPLE answer has a Laya score yet: run SCORE (laya-lab review-open-work).'
              : `κ LAYA: the same, against Laya’s flag (its score at or past the cut), on SAMPLE answers Laya scored: ${laya.n} of ${min} so far.`;
    const short = deficit_readout(stats);
    const split = three_way_readout(stats);
    const self = self_kappa(stats);
    return [
        `${done}/${total}: cards answered, out of every card counted for review (the BACKLOG is extra and not in the total).`,
        sonnet_line,
        laya_line,
        ...(self ? [`κ SELF ${self.shown}: ${self.explain}`] : []),
        ...(short ? [`${short.text}: ${short.title}.`] : []),
        ...(split ? [`${split.text}: ${split.title}.`] : []),
    ];
}

/** DEAL's line: what the deck deals now, and what that does for κ. */
export function explain_deal(stats: ReviewStats, deal: DealStratum | null): string {
    const { n, min, left, ready } = kappa_progress(stats);
    const toward = ready ? `κ SONNET has its ${min}.` : `${n} of ${min} answered toward κ SONNET, ${left} to go.`;
    if (deal === null) return `ALL mixes every kind of card in turn. Pick SAMPLE to deal only the cards κ is measured on (${toward})`;
    const why = stratum_label(deal).why;
    if (deal === 'sample') return `Only SAMPLE cards: ${why}. Every answer here counts toward κ; SKIP does not. ${toward}`;
    return `Only ${stratum_label(deal).label} cards: ${why}. These do not count toward κ; ${DEAL_USE[deal] ?? 'they find where Laya and Sonnet go wrong.'}`;
}

/** What a narrowed deal is FOR, where it is not finding Laya's and Sonnet's mistakes. */
const DEAL_USE: Readonly<Partial<Record<DealStratum, string>>> = {
    uncertain: 'a label on one moves a cut on RE-SWEEP, or teaches a fine-tune, more than any other.',
    live_sample: 'they measure how often the rack’s marks are right, so grade them as they come.',
    challenger: 'each label takes a side, and a promotion reads how often you sided with the challenger.',
};

/** The card's own lines: why it was dealt, then what each mark on it means. `three_way`: its answers are DONE, READ,
 *  RUNNING and NEEDS YOU (`is_three_way`), so the reference line says what those do to the label. */
export function explain_card(card: ReviewCard, three_way = false): string[] {
    const source = reference_source(card);
    const lines = [`Why this card: ${stratum_label(card.stratum).why}.`];
    const said = card.reference.why ? `, because “${card.reference.why}”` : '';
    const keeps = three_way
        ? `${card.reference.open ? 'RUNNING and NEEDS YOU keep that label (both are OPEN); DONE and READ' : 'DONE and READ keep that label; RUNNING and NEEDS YOU (both OPEN)'} overrule it.`
        : 'That label is what AGREE keeps and FLIP reverses.';
    lines.push(`${source} labelled it ${verdict_word(card.surface, card.reference.open)}${said}. ${keeps}`);
    if (card.laya) {
        const { p, threshold } = card.laya;
        const flagged = p >= threshold;
        lines.push(
            `LAYA ${p.toFixed(2)} is Laya’s score that the answer is yes. It flags ${verdict_word(card.surface, true)} at ${threshold.toFixed(2)} or above (the mark on the bar), so it ${flagged ? 'flagged' : 'did not flag'} this one.`,
        );
    }
    if (trained_tag(card)) lines.push('TRAINED: Laya’s fine-tune trained on this text, so its score here is not a fair test of it.');
    if (card.held_out === null) lines.push('Unknown whether Laya’s fine-tune trained on this text.');
    if (card.truncated) lines.push(`TAIL: ${tail_title(card)}; the start was cut.`);
    if (card.decision) lines.push(`The agent rack judged this turn live at ${card.decision.at}${card.decision.shown ? ' and showed you its verdict' : ''}.`);
    const near = card_edge(card);
    if (near) lines.push(`${near.text}: ${near.title}.`);
    if (card.wave) lines.push(wave_tag(card.wave).title + '.');
    const rival = challenger_note(card);
    if (rival) lines.push(`${rival}.`);
    return lines;
}

/** A `challenger` card's sentence: what the challenger checkpoint answered, against the served model. Null on any other card. */
export function challenger_note(card: ReviewCard): string | null {
    const rival = card.challenger;
    if (!rival) return null;
    const flags = rival.p >= rival.threshold;
    return `CHALLENGER ${rival.p.toFixed(2)}: the checkpoint waiting to be promoted (${rival.name}) ${flags ? 'would have flagged' : 'would not have flagged'} this turn at its own cut ${rival.threshold.toFixed(2)}, the other answer than the served model’s. Your label says which of the two was right`;
}

/** The stratum tag's text: the stratum's name, with the challenger's own score on a `challenger` card (the number that put it there). */
export function stratum_tag_text(card: ReviewCard): string {
    const label = stratum_label(card.stratum).label;
    return card.challenger ? `${label} ${card.challenger.p.toFixed(2)}` : label;
}

/** Each answer button's caption: the label it records, and whether it moves κ. */
export function answer_caption(card: ReviewCard, kind: ReviewAnswer): string {
    const source = reference_source(card);
    const counts = card.stratum === 'sample' ? ' · counts toward κ' : '';
    const all = card.wave ? ` on all ${card.wave.n}` : '';
    if (kind === 'agree') return `records ${verdict_word(card.surface, card.reference.open)}${all}, same as ${source}${counts}`;
    if (kind === 'flip') return `records ${verdict_word(card.surface, !card.reference.open)}${all}, overruling ${source}${counts}`;
    return card.wave ? `no label on any of the ${card.wave.n} · the card leaves the deck` : 'no label · the card leaves the deck';
}

/** `?surface=` off the page's own URL — a bookmark, so it is checked like any other input. */
export function surface_from_search(search: string): string | null {
    const raw = new URLSearchParams(search).get('surface');
    return raw !== null && layaReviewSurfaceSchema.safeParse(raw).success ? raw : null;
}

/** The surface's mono name: the decide registry's label (`schemas/src/decide.ts`), else the name itself. */
export function surface_label(surface: string): string {
    const known = Object.hasOwn(DECIDE_SURFACES, surface) ? (DECIDE_SURFACES as Record<string, { label: string }>)[surface] : undefined;
    return known?.label ?? surface.replaceAll('_', ' ').toUpperCase();
}

/** The label a verdict records: open work is OPEN or DONE; any other yes/no question reads YES or NO. */
export function verdict_word(surface: string, open: boolean): string {
    if (surface === 'agent_open_work') return open ? 'OPEN' : 'DONE';
    return open ? 'YES' : 'NO';
}

/** FLIP names the label it will record — the opposite of the reference's. */
export function flip_label(card: ReviewCard): string {
    return `FLIP → ${verdict_word(card.surface, !card.reference.open)}`;
}

export const REVIEW_STRATA: ReadonlyMap<string, { label: string; why: string }> = new Map([
    ['sample', { label: 'SAMPLE', why: 'A random sample of Sonnet’s labels — the cards κ is measured on' }],
    ['uncertain', { label: 'UNCERTAIN', why: 'Laya’s score sits within 0.10 of a cut — one nudge from another mark, so this label moves the cut most' }],
    ['live_sample', { label: 'LIVE SAMPLE', why: 'A random sample of the rack’s live verdicts — how often its marks are right is measured on these' }],
    ['challenger', { label: 'CHALLENGER', why: 'The checkpoint waiting to be promoted would have answered this live turn the other way than the served model did' }],
    ['suspect', { label: 'SUSPECT', why: 'A check that reads only the text, trained on Sonnet’s other labels, doubts this one — likely mislabelled' }],
    ['disagree', { label: 'DISAGREE', why: 'Laya’s current score disagrees with Sonnet’s label' }],
    ['golden', { label: 'GOLDEN', why: 'A golden case the served model got wrong, or a new one that counts once you AGREE' }],
    ['flagged', { label: 'LAYA FLAGGED', why: 'Laya’s score reached the flagging threshold on that turn; review it to catch false alarms' }],
    ['unflagged', { label: 'NEVER FLAGGED', why: 'Laya’s score stayed below the flagging threshold on that turn; sampled to catch missed work, not confirmed correct' }],
    ['rest', { label: 'BACKLOG', why: 'Not sampled: the rest of the labelled texts' }],
]);

export function stratum_label(stratum: string): { label: string; why: string } {
    return REVIEW_STRATA.get(stratum) ?? { label: stratum.replaceAll('_', ' ').toUpperCase(), why: `Sampled as ${stratum}` };
}

/** Who AGREE agrees with: the plugin's `reference.by`, read as one of the three sources it can be. */
export function reference_source(card: ReviewCard): 'SONNET' | 'GOLDEN' | 'LAYA' {
    const by = card.reference.by.toLowerCase();
    if (by.startsWith('golden')) return 'GOLDEN';
    if (by.includes('sonnet') || by.includes('claude')) return 'SONNET';
    if (by.includes('laya')) return 'LAYA';
    return card.sonnet ? 'SONNET' : card.golden ? 'GOLDEN' : 'LAYA';
}

/** TRAINED is the exception worth ink: the served fine-tune saw this text. Held out draws nothing; unknown goes in the title. */
export function trained_tag(card: ReviewCard): boolean {
    return card.held_out === false;
}

/** The TAIL tag's title: how much of a truncated text the card shows. */
export function tail_title(card: Pick<ReviewCard, 'text' | 'text_chars'>): string {
    return `Showing the last ${card.text.length.toLocaleString('en-US')} of ${card.text_chars.toLocaleString('en-US')} characters`;
}

/** The card's sentence version, for its stratum tag's title: why it was sampled, then what the marks leave unsaid (TAIL carries its own). */
export function card_title(card: ReviewCard): string {
    const lines = [stratum_label(card.stratum).why];
    const rival = challenger_note(card);
    if (rival) lines.push(rival);
    if (card.held_out === null) lines.push('Unknown whether the served fine-tune trained on this text');
    if (card.decision) {
        const reaction = card.decision.reaction ? ` · ${card.decision.reaction}` : '';
        lines.push(`Live decision ${card.decision.at}${card.decision.shown ? ' · shown' : ''}${reaction}`);
    }
    return lines.join('\n');
}

export interface LayaReadout {
    text: string;
    /** Percent strings for the meter's fill and its threshold mark. */
    fill: string;
    mark: string;
    flagged: boolean;
    title: string;
}

export function laya_readout(card: ReviewCard): LayaReadout | null {
    const { laya } = card;
    if (!laya) return null;
    const ckpt = laya.checkpoint ? ` · ${laya.checkpoint}` : ' · no fine-tune';
    return {
        text: `LAYA ${laya.p.toFixed(2)}`,
        fill: `${(laya.p * 100).toFixed(1)}%`,
        mark: `${(laya.threshold * 100).toFixed(1)}%`,
        flagged: laya.p >= laya.threshold,
        title: `p ${laya.p.toFixed(3)} against the cut ${laya.threshold.toFixed(2)} — ${laya.model}${ckpt} · from ${laya.source}`,
    };
}

export interface DeckProgress {
    done: number;
    total: number;
    /** 0–1; 0 for an empty surface, never NaN. */
    ratio: number;
    text: string;
}

export function deck_progress(stats: ReviewStats | null): DeckProgress {
    const done = stats?.reviewed ?? 0;
    const total = done + (stats?.to_review ?? 0);
    return { done, total, ratio: total > 0 ? done / total : 0, text: `${done}/${total}` };
}

export interface KappaReadout {
    text: string;
    title: string;
}

export interface KappaReadoutOptions {
    /** Replaces the count when there is nothing to pair at all. */
    no_pairs_hint?: string;
    /** κ's interval; absent from a bridge older than it, and then the value reads alone. */
    ci?: ReviewKappaInterval | null;
}

/** A κ is settled once its half-width is down to the target (`agreement.py` KAPPA_TARGET_HALF_WIDTH). */
function is_settled(ci: ReviewKappaInterval): boolean {
    return ci.half_width <= ci.target_half_width;
}

/** `95% interval 0.34 to 0.90 · ±0.15 needs about 124`: what the ± stands for, and how far it is from settled. */
export function interval_words(ci: ReviewKappaInterval): string {
    const range = `${Math.round(ci.level * 100)}% interval ${ci.lo.toFixed(2)} to ${ci.hi.toFixed(2)}`;
    const target = `±${ci.target_half_width.toFixed(2)}`;
    return is_settled(ci) ? `${range} · within ${target}` : `${range} · ${target} needs about ${ci.pairs_for_target}`;
}

/**
 * κ as a readout: `0.74 ±0.40` once the interval is there, so a κ from 24 pairs never passes for a settled one. Null is
 * never a zero: below `min` pairs it says how far off the first value is, past it (every pair one class, so κ is
 * undefined) it says that.
 */
export function kappa_readout(k: number | null, n: number, min: number, options: KappaReadoutOptions = {}): KappaReadout {
    const { no_pairs_hint, ci } = options;
    if (k !== null) {
        const over = `Cohen’s κ over ${n} paired answers`;
        return ci ? { text: `${k.toFixed(2)} ±${ci.half_width.toFixed(2)}`, title: `${over} · ${interval_words(ci)}` } : { text: k.toFixed(2), title: over };
    }
    if (n === 0 && no_pairs_hint) return { text: '—', title: no_pairs_hint };
    if (n < min) return { text: '—', title: `after ${min} paired answers (${n} so far)` };
    return { text: '—', title: `Undefined while all ${n} pairs share one label` };
}

/** One agreement block as the readout prints it: `kappa_readout` over its own κ, n and interval. */
export function agreement_readout(block: ReviewAgreement, min: number, no_pairs_hint?: string): KappaReadout {
    return kappa_readout(block.kappa, block.n, min, { no_pairs_hint, ci: block.kappa_ci });
}

/** EXPLAIN's second telling of the ±. */
function interval_sentence(ci: ReviewKappaInterval | null | undefined): string {
    if (!ci) return '';
    const target = `±${ci.target_half_width.toFixed(2)}`;
    const settled = is_settled(ci) ? `within ${target}, so it is settled` : `${target} needs about ${ci.pairs_for_target} answers`;
    return ` With ${Math.round(ci.level * 100)}% confidence it lies between ${ci.lo.toFixed(2)} and ${ci.hi.toFixed(2)}; ${settled}.`;
}

/** `0.80 (0.49 to 0.95) on 10 flagged`, or null below its minimum. */
export function rate_words(rate: ReviewRateAtCut | undefined, of: string): string | null {
    if (!rate || rate.value === null || rate.lo === null || rate.hi === null) return null;
    return `${rate.value.toFixed(2)} (${rate.lo.toFixed(2)} to ${rate.hi.toFixed(2)}) on ${rate.n} ${of}`;
}

function at_cut_sentence(block: ReviewAgreement): string {
    const precision = rate_words(block.at_cut?.precision, 'it flagged');
    const recall = rate_words(block.at_cut?.recall, 'you called open');
    const parts = [precision && `precision ${precision}`, recall && `recall ${recall}`].filter(Boolean);
    return parts.length ? ` At its cut, ${parts.join(', ')}.` : '';
}

/** The deck's κ: the readout, and `shown` — the value, or `7/20` while the pairs are still short of the minimum. */
export interface DeckKappa extends KappaReadout {
    shown: string;
}

function deck_kappa(block: ReviewAgreement, min: number, no_pairs_hint?: string): DeckKappa {
    const readout = agreement_readout(block, min, no_pairs_hint);
    const counting = block.kappa === null && block.n < min && !(block.n === 0 && no_pairs_hint);
    return { ...readout, shown: counting ? `${block.n}/${min}` : readout.text };
}

/** κ SONNET, and κ LAYA, whose empty case is usually "nothing scored yet", so its title names the command that scores. */
export function deck_kappas(stats: ReviewStats): { sonnet: DeckKappa; laya: DeckKappa } {
    const { operator_vs_sonnet: sonnet, operator_vs_laya: laya, kappa_min_pairs: min } = stats;
    const unscored = stats.reviewed > 0 ? 'run SCORE (laya-lab review-open-work)' : undefined;
    return { sonnet: deck_kappa(sonnet, min), laya: deck_kappa(laya, min, unscored) };
}

/** `.71`, `-.12`: a figure on the phone strip, its leading zero dropped. */
function short_figure(value: number): string {
    return value.toFixed(2).replace(/^(-?)0\./, '$1.');
}

/** The phone strip: `88/500 · κ .74±.40` (the ± once the bridge sends it). */
export function compact_stats(stats: ReviewStats): string {
    const { kappa: k, kappa_ci: ci } = stats.operator_vs_sonnet;
    const short = k === null ? '—' : `${short_figure(k)}${ci ? `±${short_figure(ci.half_width)}` : ''}`;
    return `${deck_progress(stats).text} · κ ${short}`;
}

/**
 * A switcher is a question only when a surface OTHER than the one on screen has work. Counting the one on screen
 * would hide the only way out of an ALL REVIEWED surface that a link or a bookmark named while another waits.
 */
export function show_switcher(surfaces: readonly ReviewStats[], current: string | null): boolean {
    return surfaces.some((s) => s.surface !== current && s.to_review > 0);
}

/** `Back: 3f9a…` after an undo — the key's head, or a golden case's name. */
export function short_key(key: string): string {
    return key.startsWith('golden:') ? key.slice('golden:'.length) : `${key.slice(0, 4)}…`;
}

export interface UndoEntry {
    answer_id: string;
    surface: string;
    item_id: string;
    /** The press graded a wave (its answer came back with a `wave_id`). */
    wave?: boolean;
    /** The press answered a blind retest: undone by the same route, and dealt back by the next queue read. */
    retest?: boolean;
}

/**
 * What U undoes: this page's newest press, else the plugin's last live answer for the surface on screen. A wave's
 * press comes back whole (`wave: true`); RECENT undoes by row instead, so a regrade there never takes a wave's other
 * turns with it. `wave` rides only when it is true, because requests are strict and a bridge that predates waves
 * refuses a field it does not know.
 */
export function undo_target(
    stack: readonly UndoEntry[],
    stats: ReviewStats | null,
): { answer_id: string; surface: string; wave?: true } | null {
    const top = stack.at(-1);
    if (top) return { answer_id: top.answer_id, surface: top.surface, ...(top.wave ? { wave: true as const } : {}) };
    if (!stats?.last_answer_id) return null;
    return { answer_id: stats.last_answer_id, surface: stats.surface, ...(stats.last_answer_wave ? { wave: true as const } : {}) };
}

// ── acquisition: the short class, the near cut, the wave ─────────────────────

/** `OPEN SHORT ▲` while a class is short of the clean labels a sweep needs: the UNCERTAIN cards lean its way. */
export function deficit_readout(stats: ReviewStats): { text: string; title: string } | null {
    const short = stats.deficit;
    if (!short) return null;
    const { open, done } = stats.sweep_ready;
    const word = verdict_word(stats.surface, short === 'open');
    return {
        text: `${word} SHORT ▲`,
        title: `${open} clean OPEN and ${done} clean DONE labels so far: the UNCERTAIN cards nearest the ${word} cut come first until it has enough`,
    };
}

/** `0.02 FROM ⚠️` on a card within a hair of a cut: the RECENT list's own note (`recent-model.ts` owns its words). */
export function card_edge(card: ReviewCard): { text: string; title: string } | null {
    if (!card.laya) return null;
    const note = edge_note({ edge: card.edge ?? undefined, p: card.laya.p });
    return note ? { text: note.text, title: note.title } : null;
}

/** `WAVE ×7`: how many turn ends one answer grades, and the sentence version for its title. */
export function wave_tag(wave: NonNullable<ReviewCard['wave']>): { text: string; title: string } {
    return {
        text: `WAVE ×${wave.n}`,
        title: `${wave.n} turn ends in a row from one session drew the same mark: one answer records the same verdict on each, and UNDO takes all ${wave.n} back`,
    };
}

export interface DeckDown {
    kind: LayaFailKind;
    head: string;
    why: string;
    how?: string;
    /** Draw CONNECT: local tools are off on this device, which one press opens. */
    connect: boolean;
}

const RESTART_HOW = 'Restart the bridge';

/** The gate's three ways to stand in the way, by its state when the call failed (`open` = Chrome's prompt is up). */
const GATE_HEADS: Readonly<Record<LocalAccessState, string>> = { locked: 'LOCAL TOOLS OFF', denied: 'LOCAL TOOLS BLOCKED', open: 'BROWSER PROMPT' };

/**
 * One down state per failure kind (`LayaFailKind`): a mono head, the why, and the remedy. `access` is the gate's
 * state (`local_access_state()`): CONNECT can only open a gate that is merely off — a Chrome block needs its
 * settings, and a prompt needs an answer (RETRY brings a dismissed one back).
 */
export function deck_down(f: LayaFail<unknown>, access: LocalAccessState): DeckDown {
    const base = { kind: f.kind, connect: false };
    switch (f.kind) {
        case 'gate':
            return { ...base, head: GATE_HEADS[access], why: f.error, connect: access === 'locked' };
        case 'down':
            return { ...base, head: 'BRIDGE DOWN', why: f.error, how: f.hint };
        case 'plugin':
            return { ...base, head: 'LAYA PLUGIN OFF', why: f.error, how: f.hint };
        case 'restart':
            // The error carries the remedy inline (for one-line surfaces); here `how` is its one telling.
            return { ...base, head: 'BRIDGE TOO OLD', why: 'The running bridge predates this Laya route', how: RESTART_HOW };
        case 'invalid':
            return { ...base, head: 'UNREADABLE ANSWER', why: f.error, how: 'This page and the bridge disagree on the wire: rebuild the page, or restart the bridge' };
        case 'refused':
            return { ...base, head: f.code ? f.code.replaceAll('_', ' ').toUpperCase() : `REFUSED · ${f.status}`, why: f.error, how: f.hint };
    }
}

// ── the four-way grade ───────────────────────────────────────────────────────

/**
 * A surface with a four-way grade (`stats.three_way`, a name older than READ: `review_data.py` owns the rule) answers
 * every card DONE, READ, RUNNING or NEEDS YOU, the RECENT list's own verdicts and keys, so each label says which DONE or
 * which OPEN it was. AGREE and FLIP stay the answers of a surface without one. The request is still agree or flip against the card's reference
 * (`verdict_request`), so κ and every count read it as before.
 */
export function is_three_way(stats: ReviewStats | null): boolean {
    return Boolean(stats?.three_way);
}

export type VerdictAction = RecentVerdict | 'undo';

const VERDICT_KEYS: ReadonlyMap<string, VerdictAction> = new Map([
    ...RECENT_VERDICTS.map((v) => [`Key${v.key}`, v.verdict] as const),
    ['KeyU', 'undo'],
]);

/** D, B, R, N, S, U: a four-way deck's keys, and a retest card's (`plain_key` owns which presses count). */
export function verdict_key(e: DeckKeyEvent): VerdictAction | null {
    return plain_key(e, VERDICT_KEYS);
}

/** The `/review/answer` body for a four-way press on a deck card; a wave card names the riders it showed. */
export function verdict_request(card: ReviewCard, verdict: RecentVerdict): ReviewAnswerRequest {
    const request = answer_request(card, card.surface, verdict, card.reference.open) as ReviewAnswerRequest;
    return card.wave ? { ...request, wave: card.wave.members.map((m) => m.key) } : request;
}

/** EXPLAIN's caption under a four-way button: what it means, then whether it keeps or overrules the reference. */
export function verdict_caption(card: ReviewCard, verdict: RecentVerdict): string {
    const said = RECENT_VERDICTS.find((v) => v.verdict === verdict)?.caption ?? '';
    if (verdict === 'skip') return card.wave ? `${said} · none of the ${card.wave.n} is labelled` : said;
    const source = reference_source(card);
    const side = is_open_verdict(verdict) === card.reference.open ? `same as ${source}` : `overruling ${source}`;
    const all = card.wave ? ` · on all ${card.wave.n}` : '';
    return `${said} · ${side}${all}${card.stratum === 'sample' ? ' · counts toward κ' : ''}`;
}

const GRADE_WORDS: Readonly<Record<(typeof REVIEW_GRADES)[number], string>> = { done: 'DONE', read: 'READ', running: 'RUNNING', you: 'NEEDS YOU' };

/**
 * `NEEDS YOU 2/20 ▲` while an OPEN half is short of its floor: the live cards lean its way until it has it, so the
 * tag names the half the deck leans toward (`three_way.lean`), not the gate's shortest class. `DONE 14/20` or
 * `READ 3/20`, with no ▲, once both OPEN halves hold the floor and a DONE class alone keeps the challenger test
 * waiting: nothing leans toward them (`review_data.py` `three_way` owns both readings).
 */
export function three_way_readout(stats: ReviewStats): { text: string; title: string } | null {
    const grades = stats.three_way;
    if (!grades?.deficit) return null;
    const shown = grades.lean ?? grades.deficit;
    const word = GRADE_WORDS[shown];
    const read = grades.read ?? 0;
    const counts = `${grades.done} DONE, ${read} READ, ${grades.running} RUNNING and ${grades.you} NEEDS YOU labels so far: each needs ${grades.min} before the four-way challenger can be tested`;
    return {
        text: `${word} ${shown === 'read' ? read : grades[shown]}/${grades.min}${grades.lean ? ' ▲' : ''}`,
        title: grades.lean ? `${counts}, so live cards likely to be ${word} come first` : counts,
    };
}

// ── the blind retest ─────────────────────────────────────────────────────────

export interface RetestChoice {
    id: string;
    label: string;
    /** The key's letter, for the hint; its `KeyboardEvent.code` is `Key${key}`. */
    key: string;
    title: string;
    caption: string;
    /** What the press records: OPEN, DONE, or null for "cannot say". */
    open: boolean | null;
    needs?: ReviewNeeds;
}

/** The one choice a surface without OPEN halves adds: a plain OPEN. */
const PLAIN_OPEN: RetestChoice = { id: 'open', label: 'OPEN', key: 'O', title: 'Something is still not done', caption: 'Something is still not done', open: true };

/**
 * A retest card's answers: the RECENT list's verdicts where the surface takes the halves (so both looks can be
 * compared class for class), else DONE, OPEN and SKIP. SKIP is "cannot say": it spends the retest and pairs with nothing.
 */
export function retest_choices(card: Pick<ReviewRetestCard, 'needs'>): RetestChoice[] {
    const choices: RetestChoice[] = [];
    for (const v of RECENT_VERDICTS) {
        const half = v.verdict === 'running' || v.verdict === 'you' || v.verdict === 'read' ? v.verdict : null;
        if (half !== null && !card.needs.includes(half)) continue;
        const choice: RetestChoice = { id: v.verdict, label: v.label, key: v.key, title: v.title, caption: v.caption, open: v.verdict === 'skip' ? null : is_open_verdict(v.verdict) };
        if (half !== null) choice.needs = half;
        if (v.verdict === 'skip') {
            if (card.needs.length === 0) choices.push(PLAIN_OPEN);
            choices.push({ ...choice, title: 'Cannot say from this text: no second answer is recorded', caption: 'Cannot say: no second answer' });
        } else choices.push(choice);
    }
    return choices;
}

/** The choice a plain key names on this card, U for undo, or null. */
export function retest_key(e: DeckKeyEvent, choices: readonly RetestChoice[]): RetestChoice | 'undo' | null {
    const keys = new Map<string, RetestChoice | 'undo'>([['KeyU', 'undo']]);
    for (const choice of choices) keys.set(`Key${choice.key}`, choice);
    return plain_key(e, keys);
}

/** The `/review/retest` body for a choice: `needs` rides only when the choice has one (requests are strict). */
export function retest_request(card: ReviewRetestCard, choice: RetestChoice): ReviewRetestRequest {
    return { surface: card.surface, key: card.key, of: card.of, open: choice.open, ...(choice.needs ? { needs: choice.needs } : {}) };
}

/** What the operator is told a retest card is, where a deck card names its reference. */
export const RETEST_NOTE = 'You graded this one at least a week ago. Grade it again from the text alone: your first answer, Sonnet’s label and Laya’s score are hidden until the two are compared.';

/**
 * κ SELF: the operator's agreement with their own earlier label on OPEN against DONE, from the blind retests, and in
 * the title one κ per OPEN half. Null from a bridge without retests, and while there is nothing to show yet (no pair
 * and none due).
 */
export function self_kappa(stats: ReviewStats): (DeckKappa & { explain: string }) | null {
    const retest = stats.retest;
    if (!retest || (retest.pairs === 0 && retest.due === 0)) return null;
    const min = stats.kappa_min_pairs;
    const open = deck_kappa(retest.open, min);
    const halves = Object.entries(retest.needs).map(([name, block]) => {
        const word = name === 'you' ? 'NEEDS YOU' : name.toUpperCase();
        return `${word} ${agreement_readout(block, min).text} (${block.n})`;
    });
    const due = `${retest.due} due a second look`;
    return {
        ...open,
        title: [`You against your own label ${retest.after_days}+ days earlier, graded blind · ${open.title}`, ...halves, due].join(' · '),
        explain:
            `how far a second, blind look at a card you graded ${retest.after_days} or more days earlier agrees with your first (about ${retest.percent} in 100 graded cards come back once). ` +
            `It is the ceiling for every other κ here: Laya cannot agree with you more than you agree with yourself. ${retest.pairs} of ${min} pairs so far, ${due}.`,
    };
}
