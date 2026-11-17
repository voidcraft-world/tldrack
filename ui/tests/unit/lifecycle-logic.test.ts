import test from 'node:test';
import assert from 'node:assert/strict';
import {
    REQUIRED_UNANSWERED_PROBES,
    SILENCE_THRESHOLD_MS,
    decide_lifecycle_action,
    type LifecycleInput,
} from '../../src/companion/lifecycle-logic.ts';

const NOW = 1_000_000;

/** A satellite whose main window beat one tick ago, being looked at. */
function input(overrides: Partial<LifecycleInput> = {}): LifecycleInput {
    return {
        now: NOW,
        last_main_seen: NOW - 3000,
        unanswered_probes: 0,
        in_use: true,
        ...overrides,
    };
}

/** How long the main has been silent, as a `last_main_seen` for `NOW`. */
function silent_for(ms: number): number {
    return NOW - ms;
}

// ── the healthy case ────────────────────────────────────────────────────────

test('a beating main is idle — no probe, no close', () => {
    assert.equal(decide_lifecycle_action(input()), 'idle');
    assert.equal(decide_lifecycle_action(input({ last_main_seen: silent_for(SILENCE_THRESHOLD_MS) })), 'idle');
});

test('probes reset with the beat: a fresh beat is idle even after a probe run', () => {
    // The caller zeroes the counter in mark_main_alive, but the boundary is
    // asserted here too — a stale counter surviving a beat would close a window
    // whose main is demonstrably alive.
    assert.equal(
        decide_lifecycle_action(input({ unanswered_probes: REQUIRED_UNANSWERED_PROBES, in_use: false })),
        'idle',
    );
});

// ── silence is a question, not a verdict ────────────────────────────────────

test('silence probes first — it never closes on the strength of a missed beat', () => {
    const silent = input({ last_main_seen: silent_for(SILENCE_THRESHOLD_MS + 1), in_use: false });
    assert.equal(decide_lifecycle_action(silent), 'probe');
    // This is THE regression. A hidden main's setInterval is throttled to as
    // little as once a minute, so a whole minute of silence must still only ask.
    assert.equal(decide_lifecycle_action({ ...silent, last_main_seen: silent_for(60_000) }), 'probe');
});

test('every probe short of the budget keeps probing', () => {
    for (let sent = 0; sent < REQUIRED_UNANSWERED_PROBES; sent += 1) {
        assert.equal(
            decide_lifecycle_action(
                input({ last_main_seen: silent_for(60_000), unanswered_probes: sent, in_use: false }),
            ),
            'probe',
            `${sent} probes in should still be asking`,
        );
    }
});

// ── the verdict, once the probes go unanswered ──────────────────────────────

test('a hidden satellite closes once the probe budget is spent', () => {
    assert.equal(
        decide_lifecycle_action(
            input({
                last_main_seen: silent_for(60_000),
                unanswered_probes: REQUIRED_UNANSWERED_PROBES,
                in_use: false,
            }),
        ),
        'close',
    );
});

test('a satellite the user can SEE is never closed — it reports orphaned instead', () => {
    // The frozen/discarded main cannot answer a ping either, so probing alone
    // cannot separate it from a crash. This is the half that does.
    const spent = input({
        last_main_seen: silent_for(600_000),
        unanswered_probes: REQUIRED_UNANSWERED_PROBES * 10,
        in_use: true,
    });
    assert.equal(decide_lifecycle_action(spent), 'orphaned');
});

test('orphaned keeps probing, so a main that comes back reconnects', () => {
    // `orphaned` is a non-idle action, and the caller pings on every non-idle
    // action — that is the whole recovery path for a discarded main tab, which
    // reloads the moment the user clicks it.
    const orphaned = decide_lifecycle_action(
        input({ last_main_seen: silent_for(600_000), unanswered_probes: 99, in_use: true }),
    );
    assert.notEqual(orphaned, 'idle');
    assert.notEqual(orphaned, 'close');
});

// ── the budget itself ───────────────────────────────────────────────────────

test('the probe budget is generous by design — the cost of being wrong is asymmetric', () => {
    // Waiting costs a stale window for a few more seconds; closing wrongly
    // destroys what the user was typing. Anything under 2 probes is a single
    // dropped message away from the old bug.
    assert.ok(REQUIRED_UNANSWERED_PROBES >= 2);
    assert.ok(SILENCE_THRESHOLD_MS >= 10_000);
});
