import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    DECIDE_SURFACE_NAME,
    LAYA_DATA_RELATIVE_PATH,
    LAYA_FINE_TUNE_PREFIX,
    LAYA_LEDGER_CONVERSATION,
    LAYA_LEDGER_KEEP_TEXT,
    LAYA_LEDGER_RACK_SURFACES,
    LAYA_LEDGER_STATE_MAX,
    LAYA_MODEL_NAME,
    LAYA_LEDGER_UNTAGGED,
    DECIDE_SURFACES,
    decideSurfaceSchema,
    fine_tune_name,
    is_laya_local_model_id,
    layaLedgerAnswerSchema,
    layaLedgerDecisionRowSchema,
    layaLedgerRowSchema,
    layaLedgerSurfaceSchema,
    layaLedgerTagSchema,
    layaReactRequestSchema,
    rackSurfaceSchema,
} from '../src/decide.ts';

/**
 * The Laya decision ledger's row contract. `plugins/laya` writes these rows in Python and its
 * contract test pipes them through these schemas, so every rule pinned here is one the writer must keep:
 * text only on an allow-listed surface, the `Z` timestamp `utc_stamp` writes, and answers shaped the way
 * `ledger.py` `decisions_from` records them.
 */

/** A host's own surface: any name of the surface shape is one, and the ledger keeps it hash-only. */
const HOST_SURFACE = 'notes_router';
const NOUL = { q: 'open_work', qh: '0123456789ab', type: 'noul', choice: 'yes', p: 0.82 };
/** Claude Code's session id is a UUIDv4, Codex's thread id a UUIDv7: one shape takes both. */
const CLAUDE_SESSION = '0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab';
const CODEX_THREAD = '019a5f3e-7b2c-7d41-9e8f-0123456789ab';
const NOT_A_CONVERSATION: unknown[] = [
    '', 'vc-8', 'not-a-uuid', `${CLAUDE_SESSION}\n`, `\n${CLAUDE_SESSION}`, CLAUDE_SESSION.slice(0, -1), `${CLAUDE_SESSION}0`,
    CLAUDE_SESSION.replaceAll('-', ''), `../${CLAUDE_SESSION}`, '019a5f3e-7b2c-7d41-9e8f-0123456789aG', 'call the bank on Tuesday',
    7, null,
];

const DECISION = {
    kind: 'decision',
    v: 1,
    decision_id: '20261026T233000-0123456789ab',
    at: '2026-10-26T23:30:00Z',
    surface: 'agent_open_work',
    subject: 'wt-laya-review|pane:3',
    question_version: 2,
    threshold: 0.7,
    model: 'voidcraft-agent-open-work',
    model_id: 'laya-local/voidcraft-agent-open-work@voidcraft-agent-open-work-20261020-101500',
    checkpoint_path: 'checkpoints/voidcraft-agent-open-work-20261020-101500',
    state_hash: 'a'.repeat(64),
    state: 'Hand-off: tests pass, the PR is not opened yet.',
    state_truncated: false,
    decisions: [NOUL],
    ms: 41,
};

const REACTION = {
    kind: 'reaction',
    v: 1,
    decision_id: '20261026T233000-0123456789ab',
    at: '2026-10-26T23:31:12Z',
    surface: 'agent_open_work',
    action: 'shown',
};

function issue_paths(result: { success: boolean; error?: { issues: { path: PropertyKey[] }[] } }): string[] {
    assert.equal(result.success, false, 'expected a refusal');
    return (result.error?.issues ?? []).map((issue) => issue.path.join('.'));
}

describe('ledger rows', () => {
    it('parses an agent_open_work decision that keeps its text, and a reaction to it', () => {
        assert.deepEqual(layaLedgerRowSchema.parse(DECISION), DECISION);
        assert.deepEqual(layaLedgerRowSchema.parse(REACTION), REACTION);
    });

    it('a decision may say how busy the Mac was and whether the model was cold; older rows say neither', () => {
        const timed = { ...DECISION, load: 0.412, cold: true };
        assert.deepEqual(layaLedgerRowSchema.parse(timed), timed);
        assert.ok(!('load' in DECISION) && !('cold' in DECISION));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, load: -0.1 })).includes('load'));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, cold: 'yes' })).includes('cold'));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...REACTION, cold: true })).length > 0);
    });

    it('keeps text only on a LAYA_LEDGER_KEEP_TEXT surface', () => {
        const host = { ...DECISION, surface: HOST_SURFACE, model: 'english', model_id: 'laya-local/english@55cf4c4', checkpoint_path: null };
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse(host)).includes('state'));
        assert.ok(layaLedgerRowSchema.safeParse({ ...host, state: null }).success);
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, surface: 'untagged' })).includes('state'));
        assert.ok(layaLedgerRowSchema.safeParse({ ...DECISION, surface: 'untagged', state: null }).success);
    });

    it('refuses a hash-only row that claims a truncation', () => {
        const hash_only = { ...DECISION, surface: HOST_SURFACE, state: null };
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...hash_only, state_truncated: true })).includes('state_truncated'));
        assert.ok(layaLedgerRowSchema.safeParse({ ...DECISION, state: 'x'.repeat(LAYA_LEDGER_STATE_MAX), state_truncated: true }).success);
    });

    it('is strict, and refuses a malformed id, digest or oversized text', () => {
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, keep_text: true })).length > 0);
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...REACTION, note: 'hi' })).length > 0);
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, decision_id: '2026-10-26T233000-0123456789ab' })).includes('decision_id'));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, decision_id: '20261026T233000-0123456789AB' })).includes('decision_id'));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, state_hash: 'a'.repeat(63) })).includes('state_hash'));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, state: 'x'.repeat(LAYA_LEDGER_STATE_MAX + 1) })).includes('state'));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, decisions: [] })).includes('decisions'));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, kind: 'note' })).length > 0);
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...REACTION, action: 'clicked' })).includes('action'));
    });

    it('takes a subject only in the tag’s shape, so no operator text rides it onto a hash-only row', () => {
        const hash_only = { ...DECISION, surface: HOST_SURFACE, state: null };
        assert.ok(layaLedgerRowSchema.safeParse(hash_only).success);
        for (const subject of ['call the bank about the overdraft on Tuesday at 9am please', 'a/b', '', 'x'.repeat(129)]) {
            assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...hash_only, subject })).includes('subject'), subject);
        }
    });

    it('takes a conversation only as a UUID, so no hook text rides it onto a row', () => {
        for (const conversation of [CLAUDE_SESSION, CODEX_THREAD, CODEX_THREAD.toUpperCase()]) {
            const tagged = { ...DECISION, conversation };
            assert.deepEqual(layaLedgerRowSchema.parse(tagged), tagged, conversation);
        }
        assert.ok(!('conversation' in DECISION) && layaLedgerRowSchema.safeParse(DECISION).success, 'optional: older rows have none');
        for (const conversation of NOT_A_CONVERSATION) {
            assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, conversation })).includes('conversation'), String(conversation));
        }
    });

    it('names a served model, never free text', () => {
        assert.ok(layaLedgerRowSchema.safeParse({ ...DECISION, model: 'english' }).success);
        for (const model of ['English', 'voidcraft agent open work', 'voidcraft_agent', '', 'a'.repeat(65)]) {
            assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, model })).includes('model'), model);
        }
    });

    it('keeps checkpoint_path relative to laya/, or null for a built-in', () => {
        assert.ok(layaLedgerRowSchema.safeParse({ ...DECISION, checkpoint_path: null }).success);
        assert.ok(LAYA_DATA_RELATIVE_PATH.test('checkpoints/voidcraft-agent-open-work-20261020-1015'));
        assert.ok(LAYA_DATA_RELATIVE_PATH.test('checkpoints/.cache/v1.2..3'));
        const escapes = [
            '/srv/laya/checkpoints/x',
            '../../../etc/passwd',
            'checkpoints/../../x',
            'checkpoints/..',
            './checkpoints/x',
            'checkpoints\\x',
            'checkpoints//x',
            'checkpoints/x/',
            '',
        ];
        for (const checkpoint_path of escapes) {
            assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...DECISION, checkpoint_path })).includes('checkpoint_path'), checkpoint_path);
        }
    });

    it('takes only a laya-local model id, or null for none', () => {
        assert.ok(layaLedgerDecisionRowSchema.safeParse({ ...DECISION, model_id: 'laya-local/english@55cf4c4' }).success);
        assert.ok(layaLedgerDecisionRowSchema.safeParse({ ...DECISION, model_id: null }).success);
        for (const model_id of ['laya-local', 'other/some-model-1.0', 'laya-local/English@55cf4c4', '']) {
            assert.ok(issue_paths(layaLedgerDecisionRowSchema.safeParse({ ...DECISION, model_id })).includes('model_id'), model_id);
        }
    });

    it('reads a model id’s name with the same rule as a row’s model', () => {
        for (const name of ['english', 'voidcraft-agent-open-work', 'English', 'a_b', '9lives', 'a'.repeat(64), 'a'.repeat(65)]) {
            assert.equal(is_laya_local_model_id(`laya-local/${name}@55cf4c4`), LAYA_MODEL_NAME.test(name), name);
        }
    });

    it('pins the Z timestamp utc_stamp writes, never isoformat()’s offset', () => {
        assert.ok(layaLedgerRowSchema.safeParse({ ...REACTION, at: '2026-10-26T23:30:00Z' }).success);
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...REACTION, at: '2026-10-26T23:30:00+00:00' })).includes('at'));
        assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...REACTION, at: '2026-10-26T23:30:00' })).includes('at'));
    });

    it('takes any surface name, a host’s own and untagged included, and refuses any other shape', () => {
        assert.equal(layaLedgerSurfaceSchema, decideSurfaceSchema);
        for (const surface of [...rackSurfaceSchema.options, HOST_SURFACE, LAYA_LEDGER_UNTAGGED]) {
            assert.ok(layaLedgerRowSchema.safeParse({ ...REACTION, surface }).success, surface);
        }
        for (const surface of ['Notes', 'notes-router', '9lives', '_x', '', 'x'.repeat(65), 'a b']) {
            assert.ok(issue_paths(layaLedgerRowSchema.safeParse({ ...REACTION, surface })).includes('surface'), surface);
            assert.ok(!DECIDE_SURFACE_NAME.test(surface), surface);
        }
    });
});

describe('ledger answers (the decisions_from shapes)', () => {
    it('parses a noul, a choice, a score and an abstain', () => {
        const choice = { q: 'kind', qh: 'bbbbbbbbbbbb', type: 'choice', choice: 'todo', p: 0.95, alt: [{ choice: 'note', p: 0.03 }] };
        const score = { q: 'urgency', qh: 'cccccccccccc', type: 'score', choice: '1', score: 0.62, p: 0.14, alt: [{ choice: '0', p: 0.47 }] };
        const abstain = { q: 'urgency', qh: 'cccccccccccc', type: 'score', choice: '', p: 0, abstain: true };
        for (const answer of [NOUL, choice, score, abstain]) assert.deepEqual(layaLedgerAnswerSchema.parse(answer), answer);
    });

    it('records an abstain as empty, and every other answer with its choice', () => {
        const abstain = { ...NOUL, choice: '', p: 0, abstain: true };
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...abstain, p: 0.5 })).includes('abstain'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...abstain, choice: 'yes' })).includes('abstain'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, choice: '' })).includes('choice'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, abstain: false })).includes('abstain'));
    });

    it('sets score on exactly the score answers, and never gives a noul alternatives', () => {
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, score: 1 })).includes('score'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, type: 'score', choice: '1' })).includes('score'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, alt: [{ choice: 'no', p: 0.18 }] })).includes('alt'));
    });

    it('refuses a bad question name or hash, an out-of-range p, a bool p and an unknown key', () => {
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, q: 'Open-Work' })).includes('q'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, qh: '0123456789a' })).includes('qh'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, p: 1.01 })).includes('p'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, p: true })).includes('p'));
        assert.ok(issue_paths(layaLedgerAnswerSchema.safeParse({ ...NOUL, confidence: 0.8 })).length > 0);
    });
});

describe('ledger tags and reactions (what callers send)', () => {
    it('parses a full tag and refuses a bad subject, a removed field, or a bool where a number goes', () => {
        const tag = { surface: 'agent_open_work', subject: 'wt-laya-review|pane:3', question_version: 2, threshold: 0.7, done_threshold: 0.4 };
        assert.deepEqual(layaLedgerTagSchema.parse(tag), tag);
        assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, subject: 'a/b' })).includes('subject'));
        assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, subject: 'x'.repeat(129) })).includes('subject'));
        assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, keep_text: false })).length > 0);
        assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, question_version: true })).includes('question_version'));
        assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, threshold: 1.5 })).includes('threshold'));
        assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, done_threshold: -0.1 })).includes('done_threshold'));
        assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, surface: LAYA_LEDGER_UNTAGGED })).includes('surface'));
        assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, surface: 'Notes Router' })).includes('surface'));
    });

    it('tags a host’s own surface: the host’s registry names it, never this package', () => {
        const tag = { surface: HOST_SURFACE, subject: 'note:42', question_version: 1 };
        assert.deepEqual(layaLedgerTagSchema.parse(tag), tag);
    });

    it('takes the rack’s conversation id on a tag, and refuses anything but a UUID', () => {
        const tag = { surface: 'agent_open_work', subject: 'vc-8', conversation: CODEX_THREAD, question_version: 2, threshold: 0.7 };
        assert.deepEqual(layaLedgerTagSchema.parse(tag), tag);
        assert.ok(LAYA_LEDGER_CONVERSATION.test(CLAUDE_SESSION) && LAYA_LEDGER_CONVERSATION.test(CODEX_THREAD));
        for (const conversation of NOT_A_CONVERSATION) {
            assert.ok(issue_paths(layaLedgerTagSchema.safeParse({ ...tag, conversation })).includes('conversation'), String(conversation));
        }
    });

    it('takes a strict react request', () => {
        const react = { decision_id: '20261026T233000-0123456789ab', action: 'dismissed' };
        assert.deepEqual(layaReactRequestSchema.parse(react), react);
        assert.ok(issue_paths(layaReactRequestSchema.safeParse({ ...react, surface: HOST_SURFACE })).length > 0);
        assert.ok(issue_paths(layaReactRequestSchema.safeParse({ ...react, action: 'opened' })).includes('action'));
    });
});

describe('the ledger surface sets', () => {
    it('keeps text for agent_open_work only, and gates exactly the rack’s own surfaces', () => {
        assert.deepEqual(LAYA_LEDGER_KEEP_TEXT, ['agent_open_work']);
        assert.deepEqual(LAYA_LEDGER_RACK_SURFACES, ['agent_open_work', 'coordinator']);
        assert.deepEqual(LAYA_LEDGER_RACK_SURFACES, rackSurfaceSchema.options);
        for (const surface of LAYA_LEDGER_KEEP_TEXT) assert.ok(LAYA_LEDGER_RACK_SURFACES.includes(surface), surface);
    });

    it('labels every rack surface, and every rack surface is a surface name', () => {
        assert.deepEqual(Object.keys(DECIDE_SURFACES), rackSurfaceSchema.options);
        for (const [surface, info] of Object.entries(DECIDE_SURFACES)) {
            assert.ok(DECIDE_SURFACE_NAME.test(surface), surface);
            assert.match(info.label, /^[A-Z][A-Z ]{0,39}$/, surface);
        }
    });

    it('names a fine-tune after its surface, under the stored prefix', () => {
        assert.equal(fine_tune_name('agent_open_work'), 'voidcraft-agent-open-work');
        assert.equal(fine_tune_name(HOST_SURFACE), `${LAYA_FINE_TUNE_PREFIX}notes-router`);
        assert.ok(LAYA_MODEL_NAME.test(fine_tune_name(HOST_SURFACE)));
    });
});
