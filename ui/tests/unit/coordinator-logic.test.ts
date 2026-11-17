/**
 * Pure logic for the rack's coordinator audit.
 *
 * Runs under bare `node --test` (strip-only TypeScript), so the module under
 * test must stay dependency-free: no DOM, no vue.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    age_label,
    empty_coordinator_view,
    parse_coordinator_state,
    rank_findings,
    read_resumed,
    resume_refusal,
    severity_label,
    type CoordinatorFinding,
} from '../../src/engine/terminal/coordinator-logic.ts';

// An invented CoordinatorState body: every id, label, time and count here is made up.
const STATE_BODY = {
    status: 'idle',
    scanned_at: '2026-01-02T03:04:05Z',
    scanned: 100,
    ranked: true,
    ranked_by: 'laya',
    dismissed: 2,
    error: null,
    findings: [
        {
            key: 'a1b2c3d4:blocked',
            session_id: 'a1b2c3d4-1111-4444-8888-aaaaaaaaaaaa',
            label: 'Coordinator agent',
            cwd: '/Users/x/code/my-app',
            project: '-Users-x-code-my-app',
            branch: 'main',
            last_active: '2026-01-02T02:52:00Z',
            age_hours: 0.2,
            kind: 'blocked',
            severity: 0,
            headline: 'waiting on a permission prompt',
            detail: 'Next: approve the write to coordinator-logic.ts',
            live_session: 'vc-3',
            work_class: null,
        },
        {
            key: 'e5f6a7b8:rejected-plan',
            session_id: 'e5f6a7b8-2222-4444-8888-bbbbbbbbbbbb',
            label: 'Add the widget',
            cwd: '/Users/x/code/my-app',
            project: '-Users-x-code-my-app',
            branch: null,
            last_active: '2025-12-08T02:34:00Z',
            age_hours: 600.5,
            kind: 'rejected-plan',
            severity: 1,
            headline: 'final ExitPlanMode was rejected',
            detail: 'restart tldrack so docs-reader mounts',
            live_session: null,
            work_class: 'open-work',
        },
    ],
};

/** A complete finding to vary one field of — ranking tests only care about two. */
function finding(overrides: Partial<CoordinatorFinding>): CoordinatorFinding {
    return {
        key: 'k',
        session_id: 's',
        label: 'l',
        cwd: null,
        project: null,
        branch: null,
        last_active: null,
        age_hours: null,
        kind: 'open-offer',
        severity: 3,
        headline: '',
        detail: '',
        live_session: null,
        work_class: null,
        ...overrides,
    };
}

// ── parsing ──────────────────────────────────────────────────────────────────

test('parse_coordinator_state reads a well-formed envelope', () => {
    const view = parse_coordinator_state(STATE_BODY);
    assert.equal(view.reachable, true);
    assert.equal(view.status, 'idle');
    assert.equal(view.scanned_at, '2026-01-02T03:04:05Z');
    assert.equal(view.scanned, 100);
    assert.equal(view.ranked, true);
    assert.equal(view.ranked_by, 'laya');
    assert.equal(view.dismissed, 2);
    assert.equal(view.error, null);
    assert.equal(view.findings.length, 2);
});

test('parse_coordinator_state carries every finding field through', () => {
    const [blocked, refused] = parse_coordinator_state(STATE_BODY).findings;
    assert.equal(blocked.key, 'a1b2c3d4:blocked');
    assert.equal(blocked.severity, 0);
    assert.equal(blocked.live_session, 'vc-3');
    assert.equal(blocked.branch, 'main');
    assert.equal(refused.branch, null);
    assert.equal(refused.age_hours, 600.5);
    assert.equal(refused.work_class, 'open-work');
});

test('parse_coordinator_state accepts an empty payload as a real, empty audit', () => {
    const view = parse_coordinator_state({});
    assert.deepEqual(view, { ...empty_coordinator_view(), reachable: true });
});

test('parse_coordinator_state degrades to an empty, UNREACHABLE view on junk', () => {
    for (const junk of [null, undefined, 42, 'nope', [], true]) {
        const view = parse_coordinator_state(junk);
        assert.equal(view.reachable, false, `junk: ${String(junk)}`);
        assert.deepEqual(view.findings, []);
        assert.equal(view.status, 'idle');
    }
});

test('parse_coordinator_state reads the scanning status the rescan ladder keys off', () => {
    assert.equal(parse_coordinator_state({ status: 'scanning' }).status, 'scanning');
    // Anything that isn't literally 'scanning' must not strand the panel in a
    // spinner — an unknown status reads as idle.
    assert.equal(parse_coordinator_state({ status: 'wat' }).status, 'idle');
});

test('parse_coordinator_state coerces wrong-typed envelope fields to safe defaults', () => {
    const view = parse_coordinator_state({
        status: 7,
        scanned_at: 12345,
        scanned: 'many',
        ranked: 'yes',
        dismissed: -4,
        error: false,
        findings: 'nope',
    });
    assert.equal(view.status, 'idle');
    assert.equal(view.scanned_at, null);
    assert.equal(view.scanned, 0);
    assert.equal(view.ranked, false);
    assert.equal(view.dismissed, 0);
    assert.equal(view.error, null);
    assert.deepEqual(view.findings, []);
});

test('parse_coordinator_state keeps the error string when the plugin reports one', () => {
    assert.equal(parse_coordinator_state({ error: 'claude -p not on PATH' }).error, 'claude -p not on PATH');
});

test('a garbage finding is dropped without taking the good ones with it', () => {
    const view = parse_coordinator_state({
        findings: [
            null,
            7,
            'string',
            [],
            {}, // no key AND no session — nothing to dismiss, nothing to name
            { kind: 'open-offer' },
            STATE_BODY.findings[0],
        ],
    });
    assert.deepEqual(
        view.findings.map((f) => f.key),
        ['a1b2c3d4:blocked']
    );
});

test('an unknown kind is KEPT at the bottom rung, never dropped', () => {
    // A newer plugin teaching itself a sixth signal must not blank this panel,
    // and must not be able to claim severity 0 for a row we cannot judge.
    const view = parse_coordinator_state({
        findings: [{ key: 'ff00ff00:invented-signal', session_id: 'ff00ff00-1', kind: 'invented-signal', severity: 0 }],
    });
    assert.equal(view.findings.length, 1);
    assert.equal(view.findings[0].kind, 'invented-signal');
    assert.equal(view.findings[0].severity, 4);
});

test('missing optional fields degrade to nulls, a derived key and a derived label', () => {
    const view = parse_coordinator_state({ findings: [{ session_id: 'abcdef1234-5678' }] });
    const row = view.findings[0];
    assert.equal(row.key, 'abcdef12:unknown');
    assert.equal(row.label, 'abcdef12');
    assert.equal(row.cwd, null);
    assert.equal(row.project, null);
    assert.equal(row.branch, null);
    assert.equal(row.last_active, null);
    assert.equal(row.age_hours, null);
    assert.equal(row.live_session, null);
    assert.equal(row.work_class, null);
    assert.equal(row.headline, '');
    assert.equal(row.detail, '');
    assert.equal(row.severity, 4);
});

test('a missing severity falls back to the kind ladder', () => {
    const rows = parse_coordinator_state({
        findings: [
            { key: 'k1', session_id: 's1', kind: 'blocked' },
            { key: 'k2', session_id: 's2', kind: 'dismissed-question' },
            { key: 'k3', session_id: 's3', kind: 'open-offer' },
            { key: 'k4', session_id: 's4', kind: 'unactioned-recommendation' },
        ],
    }).findings;
    assert.deepEqual(
        rows.map((r) => r.severity),
        [0, 1, 3, 4]
    );
});

test('stated-next-step only earns rung 2 once the deep pass calls it open work', () => {
    const rows = parse_coordinator_state({
        findings: [
            { key: 'a', session_id: 's', kind: 'stated-next-step', work_class: 'open-work' },
            { key: 'b', session_id: 's', kind: 'stated-next-step', work_class: 'routine' },
            { key: 'c', session_id: 's', kind: 'stated-next-step' },
            { key: 'd', session_id: 's', kind: 'stated-next-step', work_class: 'invented' },
        ],
    }).findings;
    assert.deepEqual(
        rows.map((r) => r.severity),
        [2, 4, 4, 4]
    );
    assert.equal(rows[3].work_class, null);
});

test('an out-of-range severity is clamped rather than trusted', () => {
    const rows = parse_coordinator_state({
        findings: [
            { key: 'a', session_id: 's', kind: 'blocked', severity: -3 },
            { key: 'b', session_id: 's', kind: 'blocked', severity: 99 },
            { key: 'c', session_id: 's', kind: 'blocked', severity: Number.NaN },
        ],
    }).findings;
    assert.deepEqual(
        rows.map((r) => r.severity),
        [0, 4, 0]
    );
});

test('a headline is flattened to one line and both texts are capped', () => {
    const row = parse_coordinator_state({
        findings: [{ key: 'k', session_id: 's', headline: `line one\nline two ${'x'.repeat(400)}`, detail: 'y'.repeat(900) }],
    }).findings[0];
    assert.ok(!row.headline.includes('\n'));
    assert.ok(row.headline.startsWith('line one line two'));
    assert.equal(row.headline.length, 160);
    assert.equal(row.detail.length, 600);
});

test('empty_coordinator_view is idle, empty and unreachable', () => {
    const view = empty_coordinator_view();
    assert.equal(view.reachable, false);
    assert.equal(view.status, 'idle');
    assert.equal(view.scanned, 0);
    assert.deepEqual(view.findings, []);
});

// ── ranking ──────────────────────────────────────────────────────────────────

test('rank_findings sorts by severity ascending, then by last activity descending', () => {
    const ranked = rank_findings(
        [
            finding({ key: 'old-offer', severity: 3, last_active: '2026-01-01T00:00:00Z' }),
            finding({ key: 'routine', severity: 4, last_active: '2026-08-02T00:00:00Z' }),
            finding({ key: 'new-offer', severity: 3, last_active: '2026-08-01T00:00:00Z' }),
            finding({ key: 'blocked', severity: 0, last_active: '2026-01-01T00:00:00Z' }),
        ],
        0
    );
    assert.deepEqual(
        ranked.map((f) => f.key),
        ['blocked', 'new-offer', 'old-offer', 'routine']
    );
});

test('rank_findings sorts a finding with no timestamp last within its rung', () => {
    const ranked = rank_findings(
        [
            finding({ key: 'undated', severity: 1, last_active: null }),
            finding({ key: 'garbage-date', severity: 1, last_active: 'not-a-date' }),
            finding({ key: 'dated', severity: 1, last_active: '2026-01-01T00:00:00Z' }),
        ],
        0
    );
    assert.equal(ranked[0].key, 'dated');
});

test('rank_findings caps at max', () => {
    const rows = [0, 1, 2, 3, 4].map((severity) => finding({ key: `s${severity}`, severity }));
    assert.deepEqual(
        rank_findings(rows, 2).map((f) => f.key),
        ['s0', 's1']
    );
    assert.equal(rank_findings(rows, 5).length, 5);
    assert.equal(rank_findings(rows, 99).length, 5);
});

test('rank_findings treats a non-positive max as "no cap"', () => {
    const rows = [finding({ key: 'a' }), finding({ key: 'b' })];
    assert.equal(rank_findings(rows, 0).length, 2);
    assert.equal(rank_findings(rows, -1).length, 2);
});

test('rank_findings never mutates the caller list — it holds the service view', () => {
    const rows = [finding({ key: 'b', severity: 4 }), finding({ key: 'a', severity: 0 })];
    rank_findings(rows, 5);
    assert.deepEqual(
        rows.map((f) => f.key),
        ['b', 'a']
    );
});

test('rank_findings handles an empty list', () => {
    assert.deepEqual(rank_findings([], 5), []);
});

// ── display helpers ──────────────────────────────────────────────────────────

test('severity_label names every rung', () => {
    assert.deepEqual([0, 1, 2, 3, 4].map(severity_label), ['BLOCKED', 'REFUSED', 'OPEN', 'OFFER', 'ROUTINE']);
});

test('severity_label clamps out-of-range and non-finite input instead of blanking', () => {
    assert.equal(severity_label(-2), 'BLOCKED');
    assert.equal(severity_label(9), 'ROUTINE');
    assert.equal(severity_label(Number.NaN), 'ROUTINE');
});

test('age_label reports hours below a day', () => {
    assert.equal(age_label(0), '0h');
    assert.equal(age_label(3.4), '3h');
    assert.equal(age_label(23.9), '23h'); // never rounds up past its own bucket
});

test('age_label crosses to days at exactly 24 hours', () => {
    assert.equal(age_label(24), '1d');
    assert.equal(age_label(47), '1d');
    assert.equal(age_label(167.9), '6d');
});

test('age_label crosses to weeks at exactly 168 hours', () => {
    assert.equal(age_label(168), '1w');
    assert.equal(age_label(335), '1w');
    assert.equal(age_label(600.5), '3w');
});

test('age_label reports an em dash for unknown or nonsense ages', () => {
    assert.equal(age_label(null), '—');
    assert.equal(age_label(Number.NaN), '—');
    assert.equal(age_label(Number.POSITIVE_INFINITY), '—');
});

test('age_label clamps clock skew rather than printing a future', () => {
    assert.equal(age_label(-5), '0h');
});

// ── the /resume sidecar ──────────────────────────────────────────────────────
//
// These exist to defend one trap: `parse_coordinator_state` is TOTAL and drops
// every field it does not know, `resumed` included. Without coverage here the
// obvious cleanup (a caller's RESUME folded into a shared POST helper that
// returns a parsed view and nothing else) compiles, runs, keeps the tests green,
// and makes every RESUME report that the coordinator named no session while the
// tmux session is in fact up.

test('parse_coordinator_state DROPS the resumed sidecar — this is why read_resumed exists', () => {
    const body = { ...STATE_BODY, resumed: { key: 'a1b2c3d4:blocked', session: 'resume-foo', created: true } };
    // Through `unknown`: CoordinatorView has no index signature, so TS rejects the
    // direct assertion (TS2352). Reaching for a key the type does not declare is
    // the whole point of the assertion — that the parser dropped it.
    assert.equal((parse_coordinator_state(body) as unknown as Record<string, unknown>).resumed, undefined);
    assert.deepEqual(read_resumed(body), { session: 'resume-foo', created: true });
});

test('read_resumed reports created:false for the idempotent second RESUME', () => {
    assert.deepEqual(
        read_resumed({ resumed: { session: 'resume-foo', created: false } }),
        { session: 'resume-foo', created: false },
    );
});

test('read_resumed answers a null session for anything it cannot trust', () => {
    // A 200 with no usable session name is a plugin bug, not a refusal — but the
    // caller must not "follow" the live pane to nothing either way.
    for (const body of [null, undefined, 'nope', [], {}, { resumed: null }, { resumed: {} },
        { resumed: { session: '' } }, { resumed: { session: 7 } }]) {
        assert.deepEqual(read_resumed(body), { session: null, created: false }, JSON.stringify(body ?? null));
    }
});

test('read_resumed never lets a truthy non-true created through', () => {
    assert.equal(read_resumed({ resumed: { session: 'resume-foo', created: 'yes' } }).created, false);
});

test('resume_refusal prefers the plugin own error, which names the refusal that fired', () => {
    assert.equal(
        resume_refusal(503, { error: 'tmux not found — install with: brew install tmux' }),
        'tmux not found — install with: brew install tmux',
    );
});

test('resume_refusal explains each status when the body carries no error', () => {
    assert.match(resume_refusal(400, null), /re-run AUDIT/);
    assert.match(resume_refusal(503, {}), /tmux/);
    // 404 keeps its one meaning across every bridge route: a bridge that
    // predates the route, not a missing finding.
    assert.match(resume_refusal(404, { error: '' }), /restart it/);
});

test('resume_refusal reports an unknown status rather than swallowing it', () => {
    assert.equal(resume_refusal(418, null), 'resume failed (HTTP 418)');
});
