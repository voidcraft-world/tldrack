import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    DEFAULT_CARD_REPO,
    REVIEW_KEY,
    layaVerifierCardSchema,
    layaVerifierPublishSchema,
    operatorAnswerRowSchema,
    layaLabRequestSchema,
    layaOpsStateSchema,
    reviewAnswerRequestSchema,
    reviewCardSchema,
    reviewQueueSchema,
    reviewRecentRowSchema,
    reviewRecentSummarySchema,
    reviewResultSchema,
    reviewRetestCardSchema,
    reviewRetestRequestSchema,
    reviewRetestResultSchema,
    reviewStatsSchema,
    reviewUndoRequestSchema,
} from '../src/laya-review.ts';

// Strip-vs-strict: owned by schemas/src/laya-review.ts — see the note there.
// The operator's log rows are strict too: κ and the threshold sweep are computed from them.

const AGREEMENT = { n: 24, kappa: 0.71, agree_rate: 0.88, basis: 'sample' };

const STATS = {
    surface: 'agent_open_work',
    label: 'OPEN WORK',
    question: 'Did the agent leave work open?',
    to_review: 12,
    backlog: 140,
    reviewed: 30,
    agree: 24,
    flip: 4,
    skip: 2,
    by_stratum: { sample: { reviewed: 20, remaining: 5 }, disagree: { reviewed: 10, remaining: 7 } },
    kappa_min_pairs: 20,
    operator_vs_sonnet: AGREEMENT,
    operator_vs_laya: { ...AGREEMENT, n: 3, kappa: null, agree_rate: null },
    precision: { window_days: 7, shown: 9, dismissed: 2, value: 0.78 },
    recall_est: { n_flagged: 9, n_unflagged: 4, value: null },
    sweep_ready: { rows: 28, open: 9, done: 19, ready: false, reason: 'needs 40 clean rows, has 28' },
    last_answer_id: 'a_0123456789abcdef',
    last_answer_at: '2026-10-26T23:30:00Z',
};

const CARD = {
    item_id: 'agent_open_work:0123456789abcdef',
    key: '0123456789abcdef',
    surface: 'agent_open_work',
    stratum: 'sample',
    text: 'Hand-off: the migration is written; the deploy is still owed.',
    text_chars: 61,
    truncated: false,
    held_out: true,
    reference: { by: 'sonnet-4.6', open: true, why: 'the deploy is owed' },
    sonnet: { by: 'sonnet-4.6', open: true, why: 'the deploy is owed' },
    laya: { p: 0.81, threshold: 0.7, model: 'voidcraft-agent-open-work', checkpoint: 'voidcraft-agent-open-work-20261020-101500', question_version: 2, source: 'ledger' },
    golden: null,
    decision: { decision_id: '20261026T233000-0123456789ab', at: '2026-10-26T23:30:00Z', shown: true, reaction: 'dismissed' },
    answers: ['agree', 'flip', 'skip'],
};

const QUEUE = {
    v: 1,
    ok: true,
    message: '12 to review',
    surfaces: [STATS],
    surface: 'agent_open_work',
    cards: [CARD],
    sources: { labels: 'ok', ledger: 'ok', scores: 'missing' },
    generated_at: '2026-10-26T23:31:00Z',
};

const ANSWER_ROW = {
    v: 1,
    op: 'answer',
    answer_id: 'a_0123456789abcdef',
    at: '2026-10-26T23:30:00Z',
    by: 'operator',
    surface: 'agent_open_work',
    key: '0123456789abcdef',
    stratum: 'sample',
    answer: 'agree',
    open: true,
    against: { by: 'sonnet-4.6', open: true },
    laya: { p: 0.81, threshold: 0.7, source: 'ledger', checkpoint_path: 'checkpoints/voidcraft-agent-open-work-20261020-101500' },
    decision_id: '20261026T233000-0123456789ab',
    held_out: true,
};

/** One `/review/recent` row as `review_data.py` `recent_rows` writes it. */
const RECENT_ROW = {
    key: '0123456789abcdef',
    decision_id: '20261026T233000-0123456789ab',
    at: '2026-10-26T23:30:00Z',
    subject: 'vc-8',
    conversation: '0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab',
    p: 0.82,
    threshold: 0.7,
    done_threshold: 0.4,
    mark: 'open',
    band: 'open',
    edge: { cut: 'open', delta: 0.12 },
    model: 'voidcraft-agent-open-work',
    checkpoint: 'voidcraft-agent-open-work-20261020-101500',
    text: 'Hand-off: the migration is written; the deploy is still owed.',
    text_chars: 61,
    stratum: 'flagged',
    reference_open: true,
    operator: null,
};

/** Its summary, one page in: `offset` rows of the `pool` came before it. */
const RECENT_SUMMARY = {
    n: 1,
    skipped: 0,
    done_at: 0.4,
    bands: {
        done: { n: 0, reviewed: 0, right: 0, open: 0 },
        reply: { n: 0, reviewed: 0, right: null, open: 0 },
        open: { n: 1, reviewed: 0, right: 0, open: 0 },
    },
    order: 'newest',
    todo: false,
    pool: 140,
    offset: 100,
    next: 'n~2026-11-07T12:00:00Z~0123456789abcdef',
};

const UNDO_ROW = { v: 1, op: 'undo', answer_id: 'a_0123456789abcdef', at: '2026-10-26T23:31:00Z', by: 'operator', surface: 'agent_open_work' };

const SERVED = {
    name: 'voidcraft-agent-open-work',
    surface: 'agent_open_work',
    checkpoint: 'voidcraft-agent-open-work-20261020-101500',
    model_id: 'laya-local/voidcraft-agent-open-work@voidcraft-agent-open-work-20261020-101500',
    threshold: { at: 0.7, source: 'code', swept_at: null },
    eval: null,
    golden: { at: '20261020-101900', cases: 24, accuracy: 0.92, kappa: null, brier: 0.08 },
    scores: { at: '2026-10-26T20:00:00Z', rows: 140, current: true },
};

const OPS = {
    v: 1,
    ok: true,
    message: 'ready',
    status: { state: 'ready' },
    served: [SERVED],
    review: [STATS],
    flags_7d: { agent_open_work: { decisions: 40, shown: 9, dismissed: 2, accepted: 3 } },
    sweeps: [{ surface: 'agent_open_work', at: null, rows: 28, labels: null, best: null, live: 0.7, source: 'code', kappa: null }],
    golden: [{ surface: 'agent_open_work', suite: 'agent-open-work', public: { cases: 24, open: 12, done: 12 }, private: { cases: 0, open: 0, done: 0 }, candidates: 4 }],
    jobs: [{ verb: 'sweep', surface: 'agent_open_work', state: 'idle', exit_code: null, session: 'laya-sweep', cli: 'cd lab && .venv/bin/laya-lab sweep agent-open-work --labels operator --apply' }],
    lab: { ok: true, hint: null },
};

describe('review responses (default strip)', () => {
    it('parses a queue and strips what a newer bridge adds, at any depth', () => {
        assert.deepEqual(reviewQueueSchema.parse(QUEUE), QUEUE);
        const newer = reviewQueueSchema.parse({ ...QUEUE, build: 'v2', cards: [{ ...CARD, embedding: [0.1] }] });
        assert.equal('build' in newer, false);
        assert.equal('embedding' in newer.cards[0]!, false);
    });

    it('never carries a path as a checkpoint', () => {
        for (const checkpoint of ['checkpoints/voidcraft-agent-open-work', '..\\x']) {
            const card = { ...CARD, laya: { ...CARD.laya, checkpoint } };
            assert.equal(reviewCardSchema.safeParse(card).success, false, checkpoint);
        }
    });

    it('reads a 409 already_answered body, error and hint included', () => {
        const body = {
            v: 1,
            ok: false,
            code: 'already_answered',
            message: 'this item is already answered',
            error: 'this item is already answered',
            hint: 'UNDO it first',
            answer: { answer_id: 'a_0123456789abcdef', key: '0123456789abcdef', answer: 'flip', open: false },
            card: null,
            stats: null,
        };
        assert.deepEqual(reviewResultSchema.parse(body), body);
        const { error: _error, hint: _hint, ...bare } = body;
        assert.ok(reviewResultSchema.safeParse(bare).success);
    });
});

describe('the RECENT list', () => {
    it('reads a row with its conversation, with a null one, and from a bridge that sends none', () => {
        assert.deepEqual(reviewRecentRowSchema.parse(RECENT_ROW), RECENT_ROW);
        assert.equal(reviewRecentRowSchema.parse({ ...RECENT_ROW, conversation: null }).conversation, null);
        const { conversation: _dropped, ...older_bridge } = RECENT_ROW;
        assert.equal(reviewRecentRowSchema.parse(older_bridge).conversation, undefined);
    });

    it('refuses a conversation that is not a UUID, so the page never bands by hook text', () => {
        for (const conversation of ['', 'vc-8', 'not-a-uuid', `${RECENT_ROW.conversation}\n`, RECENT_ROW.conversation.slice(1), 7]) {
            assert.equal(reviewRecentRowSchema.safeParse({ ...RECENT_ROW, conversation }).success, false, String(conversation));
        }
    });

    it('reads the page offset the summary echoes, and a summary from a bridge that predates it', () => {
        assert.deepEqual(reviewRecentSummarySchema.parse(RECENT_SUMMARY), RECENT_SUMMARY);
        assert.equal(reviewRecentSummarySchema.parse({ ...RECENT_SUMMARY, offset: 0 }).offset, 0);
        const { offset: _dropped, ...older_bridge } = RECENT_SUMMARY;
        assert.equal(reviewRecentSummarySchema.parse(older_bridge).offset, undefined);
        for (const offset of [-1, 1.5, '100', null]) {
            assert.equal(reviewRecentSummarySchema.safeParse({ ...RECENT_SUMMARY, offset }).success, false, String(offset));
        }
    });

    it('reads where the next page starts, null at the end, absent from a bridge that predates it', () => {
        assert.equal(reviewRecentSummarySchema.parse({ ...RECENT_SUMMARY, next: null }).next, null);
        const { next: _dropped, ...older_bridge } = RECENT_SUMMARY;
        assert.equal(reviewRecentSummarySchema.parse(older_bridge).next, undefined);
        const edge = 'e~0.010000~2026-11-07T12:00:00Z~golden:tests-red-1';
        assert.equal(reviewRecentSummarySchema.parse({ ...RECENT_SUMMARY, next: edge }).next, edge);
        for (const next of ['', 'n~', 'e~0.01~2026-11-07T12:00:00Z~0123456789abcdef', 'n~2026-11-07T12:00:00Z~0123456789abcdef\n', 7]) {
            assert.equal(reviewRecentSummarySchema.safeParse({ ...RECENT_SUMMARY, next }).success, false, String(next));
        }
    });
});

describe('review requests (strict)', () => {
    it('refuses an extra key on every request', () => {
        const answer = { surface: 'agent_open_work', key: '0123456789abcdef', answer: 'agree', reference_open: true };
        assert.ok(reviewAnswerRequestSchema.safeParse(answer).success);
        assert.equal(reviewAnswerRequestSchema.safeParse({ ...answer, by: 'operator' }).success, false);
        const undo = { surface: 'agent_open_work', answer_id: 'a_0123456789abcdef' };
        assert.ok(reviewUndoRequestSchema.safeParse(undo).success);
        assert.equal(reviewUndoRequestSchema.safeParse({ ...undo, key: '0123456789abcdef' }).success, false);
        const lab = { verb: 'sweep', surface: 'agent_open_work' };
        assert.ok(layaLabRequestSchema.safeParse(lab).success);
        assert.equal(layaLabRequestSchema.safeParse({ ...lab, args: ['--apply'] }).success, false);
        assert.equal(layaLabRequestSchema.safeParse({ ...lab, verb: 'train' }).success, false);
    });

    it('takes a 16-hex key or a golden case id, never a path', () => {
        assert.ok(REVIEW_KEY.test('0123456789abcdef'));
        assert.ok(REVIEW_KEY.test('golden:f-0123456789abcdef'));
        for (const key of ['golden:../x', 'golden:', '0123456789ABCDEF', '0123456789abcde', 'golden:-x']) {
            assert.equal(REVIEW_KEY.test(key), false, key);
        }
    });
});

describe('the operator answer log', () => {
    it('parses an answer row and an undo row', () => {
        assert.deepEqual(operatorAnswerRowSchema.parse(ANSWER_ROW), ANSWER_ROW);
        assert.deepEqual(operatorAnswerRowSchema.parse(UNDO_ROW), UNDO_ROW);
        const golden = { ...ANSWER_ROW, key: 'golden:f-0123456789abcdef', against: { by: 'golden', open: true, case_id: 'f-0123456789abcdef' }, laya: null, decision_id: null, held_out: null };
        assert.ok(operatorAnswerRowSchema.safeParse(golden).success);
    });

    it('refuses an answer row without `against`, or with an unknown key', () => {
        const { against: _against, ...bare } = ANSWER_ROW;
        assert.equal(operatorAnswerRowSchema.safeParse(bare).success, false);
        assert.equal(operatorAnswerRowSchema.safeParse({ ...ANSWER_ROW, note: 'hi' }).success, false);
        assert.equal(operatorAnswerRowSchema.safeParse({ ...UNDO_ROW, key: '0123456789abcdef' }).success, false);
        assert.equal(operatorAnswerRowSchema.safeParse({ ...ANSWER_ROW, at: '2026-10-26T23:30:00+00:00' }).success, false);
    });

    it('keeps laya.checkpoint_path relative to laya/, or null for a built-in', () => {
        assert.ok(operatorAnswerRowSchema.safeParse({ ...ANSWER_ROW, laya: { ...ANSWER_ROW.laya, checkpoint_path: null } }).success);
        for (const checkpoint_path of ['/abs/../x', '/Users/someone/laya/checkpoints/x', 'checkpoints/../../x', 'checkpoints\\x']) {
            const result = operatorAnswerRowSchema.safeParse({ ...ANSWER_ROW, laya: { ...ANSWER_ROW.laya, checkpoint_path } });
            assert.equal(result.success, false, checkpoint_path);
            assert.ok(result.error?.issues.some((issue) => issue.path.join('.') === 'laya.checkpoint_path'), checkpoint_path);
        }
    });

    it('records the operator’s verdict as `open`: the reference on agree, its negation on flip, nothing on skip', () => {
        const { open: _open, ...skip } = { ...ANSWER_ROW, answer: 'skip' };
        assert.ok(operatorAnswerRowSchema.safeParse({ ...ANSWER_ROW, answer: 'flip', open: false }).success);
        assert.ok(operatorAnswerRowSchema.safeParse(skip).success);
        const wrong = [
            { ...ANSWER_ROW, open: false },
            { ...ANSWER_ROW, answer: 'flip', open: true },
            { ...ANSWER_ROW, answer: 'skip', open: true },
            { ...skip, answer: 'agree' },
        ];
        for (const row of wrong) {
            const result = operatorAnswerRowSchema.safeParse(row);
            assert.equal(result.success, false, JSON.stringify(row));
            assert.ok(result.error?.issues.some((issue) => issue.path.join('.') === 'open'));
        }
    });
});

describe('the blind retest', () => {
    const RETEST_ROW = { v: 1, op: 'retest', answer_id: 'a_00000000000000aa', at: '2026-11-09T12:00:00Z', by: 'operator', surface: 'agent_open_work', key: '0123456789abcdef', of: 'a_0123456789abcdef', open: true, needs: 'you' };
    const RETEST_CARD = { item_id: 'agent_open_work:retest:0123456789abcdef', key: '0123456789abcdef', surface: 'agent_open_work', of: 'a_0123456789abcdef', text: 'Phase two is not started.', text_chars: 25, truncated: false, at: null, needs: ['you', 'running'] };
    const AGREEMENT = { n: 0, kappa: null, agree_rate: null, basis: 'retest', kappa_ci: null };

    it('is its own row in the operator log: no reference, no stratum, and `needs` only beside OPEN', () => {
        assert.deepEqual(operatorAnswerRowSchema.parse(RETEST_ROW), RETEST_ROW);
        const { open: _open, needs: _needs, ...cannot_say } = RETEST_ROW;
        assert.ok(operatorAnswerRowSchema.safeParse(cannot_say).success, 'no `open`: the operator could not say');
        assert.ok(operatorAnswerRowSchema.safeParse({ ...cannot_say, open: false }).success);
        const { of: _of, ...no_of } = RETEST_ROW;
        for (const row of [{ ...RETEST_ROW, open: false }, { ...cannot_say, needs: 'you' }, no_of, { ...RETEST_ROW, stratum: 'rest' }, { ...RETEST_ROW, against: { by: 'sonnet', open: true } }, { ...RETEST_ROW, answer: 'agree' }]) {
            assert.equal(operatorAnswerRowSchema.safeParse(row).success, false, JSON.stringify(row));
        }
    });

    it('deals a card that cannot carry what a first look showed', () => {
        const parsed = reviewRetestCardSchema.parse({ ...RETEST_CARD, reference: { by: 'sonnet', open: true, why: 'x' }, laya: { p: 0.9 }, stratum: 'rest', answer: 'agree' });
        assert.deepEqual(parsed, RETEST_CARD, 'anything else a bridge sent is stripped before the page sees it');
        assert.ok(reviewQueueSchema.safeParse({ ...QUEUE, retests: [RETEST_CARD] }).success);
        assert.ok(reviewQueueSchema.safeParse(QUEUE).success, 'an older bridge sends none');
        assert.equal(reviewQueueSchema.safeParse({ ...QUEUE, retests: Array(6).fill(RETEST_CARD) }).success, false);
    });

    it('takes a strict request and answers with the retest row’s own id', () => {
        const request = { surface: 'agent_open_work', key: '0123456789abcdef', of: 'a_0123456789abcdef', open: true, needs: 'running' };
        assert.ok(reviewRetestRequestSchema.safeParse(request).success);
        assert.ok(reviewRetestRequestSchema.safeParse({ ...request, open: null, needs: undefined }).success, 'cannot say');
        for (const bad of [{ ...request, open: false }, { ...request, open: null }, { ...request, reference_open: true }, { ...request, of: 'nope' }]) {
            assert.equal(reviewRetestRequestSchema.safeParse(bad).success, false, JSON.stringify(bad));
        }
        const result = { v: 1, ok: true, message: 'recorded', retest: { answer_id: 'a_00000000000000aa', key: '0123456789abcdef', of: 'a_0123456789abcdef', open: true, needs: 'running' }, stats: STATS };
        assert.ok(reviewRetestResultSchema.safeParse(result).success);
        assert.ok(reviewResultSchema.safeParse({ v: 1, ok: true, message: 'retest undone', answer: null, card: null, stats: STATS }).success);
    });

    it('reads the three-way counts and the operator’s κ against themselves off the stats', () => {
        const stats = reviewStatsSchema.parse({
            ...STATS,
            three_way: { done: 22, running: 0, you: 2, min: 20, deficit: 'running', lean: 'running' },
            retest: { percent: 10, after_days: 7, due: 3, pairs: 0, open: AGREEMENT, needs: { you: AGREEMENT, running: AGREEMENT } },
        });
        assert.equal(stats.three_way?.deficit, 'running');
        const gate = { done: 1, running: 20, you: 20, min: 20, deficit: 'done', lean: null };
        assert.ok(reviewStatsSchema.safeParse({ ...STATS, three_way: gate }).success, 'DONE short, no lean');
        assert.equal(reviewStatsSchema.safeParse({ ...STATS, three_way: { ...gate, lean: 'done' } }).success, false, 'nothing leans toward DONE');
        assert.equal(stats.retest?.needs.you?.basis, 'retest');
        assert.ok(reviewStatsSchema.safeParse({ ...STATS, three_way: null }).success, 'a surface without the OPEN halves');
        assert.equal(reviewStatsSchema.safeParse({ ...STATS, three_way: { done: 1, running: 0, you: 0, min: 20, deficit: 'open', lean: null } }).success, false);
    });
});

describe('OPS state', () => {
    it('parses with sweep_ready.reason null or a sentence', () => {
        assert.deepEqual(layaOpsStateSchema.parse(OPS), OPS);
        const ready = { ...STATS, sweep_ready: { ...STATS.sweep_ready, ready: true, reason: null } };
        assert.ok(layaOpsStateSchema.safeParse({ ...OPS, review: [ready] }).success);
    });

    it('never carries a path as a served checkpoint', () => {
        const served = [{ ...SERVED, checkpoint: 'checkpoints/voidcraft-agent-open-work-20261020-101500' }];
        assert.equal(layaOpsStateSchema.safeParse({ ...OPS, served }).success, false);
    });
});

describe('the verifier card names its own repository', () => {
    const files = { 'VERIFIER.md': 'v', 'golden/agent-open-work.jsonl': '{}', 'REPRODUCE.md': 'r' };
    const card = (repository: string) => ({ v: 1, repository, at: '2026-11-09T10:00:00Z', pairs: 20, ready: true,
        digest: 'a'.repeat(64), files });

    it('takes the default or any owner/name, and nothing else', () => {
        assert.equal(DEFAULT_CARD_REPO, 'voidcraft-world/tldrack');
        assert.ok(layaVerifierCardSchema.safeParse(card(DEFAULT_CARD_REPO)).success);
        assert.ok(layaVerifierCardSchema.safeParse(card('someone/their-fork')).success);
        for (const bad of ['nope', 'a/b/c', 'https://github.com/a/b']) {
            assert.ok(!layaVerifierCardSchema.safeParse(card(bad)).success, bad);
        }
    });

    it('a publish answer is empty until a pull request exists, and then one of the card\'s own repository', () => {
        const reply = { v: 1, ok: true, message: 'm' };
        assert.ok(layaVerifierPublishSchema.safeParse({ ...reply, url: '', repository: 'someone/fork' }).success);
        assert.ok(layaVerifierPublishSchema.safeParse({ ...reply, url: 'https://github.com/someone/fork/pull/3', repository: 'someone/fork' }).success);
        assert.ok(!layaVerifierPublishSchema.safeParse({ ...reply, url: 'https://github.com/voidcraft-world/tldrack/pull/3', repository: 'someone/fork' }).success);
        assert.ok(!layaVerifierPublishSchema.safeParse({ ...reply, url: 'https://evil.example/pull/3' }).success);
    });
});
