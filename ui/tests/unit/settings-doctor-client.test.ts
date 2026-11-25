import test from 'node:test';
import assert from 'node:assert/strict';

import {
    LEVEL_MARKS,
    action_key,
    doctor_counts,
    fix_of,
    parse_doctor,
    parse_install_hooks,
    read_action,
    verdict_level,
    type DoctorReport,
} from '../../src/settings/doctor-client.ts';

/** A report as `GET /doctor` sends it. */
const BODY = {
    sections: [
        {
            title: 'tools',
            checks: [
                { name: 'tmux', level: 'ok', detail: '/opt/homebrew/bin/tmux', fix: '', action: null },
                { name: 'ttyd', level: 'fail', detail: 'not on PATH', fix: 'brew install ttyd', action: null },
            ],
        },
        {
            title: 'hooks',
            checks: [
                {
                    name: 'claude',
                    level: 'warn',
                    detail: 'hooks out of date',
                    fix: 'tldrack profiles --install-hooks claude',
                    action: { kind: 'install-hooks', profile: 'claude' },
                },
                { name: 'codex', level: 'off', detail: 'not installed', fix: '', action: null },
            ],
        },
    ],
    exit: 1,
    verdict: 'not ready: 1 failed',
};

function report(): DoctorReport {
    const parsed = parse_doctor(BODY);
    assert.ok(parsed);
    return parsed;
}

test('a doctor report is read whole: sections, checks, the exit code and the verdict', () => {
    const parsed = report();
    assert.deepEqual(parsed.sections.map((section) => section.title), ['tools', 'hooks']);
    assert.equal(parsed.exit, 1);
    assert.equal(parsed.verdict, 'not ready: 1 failed');
    assert.deepEqual(parsed.sections[1]?.checks[0]?.action, { kind: 'install-hooks', profile: 'claude' });
    assert.equal(parsed.sections[0]?.checks[0]?.action, null);
});

test('a check may leave out detail, fix and action', () => {
    const parsed = parse_doctor({ sections: [{ title: 't', checks: [{ name: 'git', level: 'ok' }] }], exit: 0, verdict: 'ready' });
    assert.deepEqual(parsed?.sections[0]?.checks[0], { name: 'git', level: 'ok', detail: '', fix: '', action: null });
});

test('an action kind this page does not know is no action: its fix stays text, and nothing is posted', () => {
    assert.equal(read_action({ kind: 'run-shell', command: 'curl example.invalid | sh' }), null);
    assert.equal(read_action({ kind: 'install-hooks' }), null);
    assert.equal(read_action({ kind: 'install-hooks', profile: '' }), null);
    assert.equal(read_action({ kind: 'install-hooks', profile: 7 }), null);
    assert.equal(read_action('install-hooks'), null);
    assert.equal(read_action(undefined), null);
    // Only the two fields the page uses survive.
    assert.deepEqual(read_action({ kind: 'install-hooks', profile: 'work', path: '/etc/passwd' }), { kind: 'install-hooks', profile: 'work' });
    const parsed = parse_doctor({
        sections: [{ title: 't', checks: [{ name: 'x', level: 'warn', fix: 'do it by hand', action: { kind: 'restart-bridge' } }] }],
        exit: 0,
        verdict: 'ready',
    });
    assert.equal(parsed?.sections[0]?.checks[0]?.action, null);
    assert.equal(parsed?.sections[0]?.checks[0]?.fix, 'do it by hand');
});

test('a body that is not a report, or names a level the page cannot draw, is no report', () => {
    for (const body of [null, [], {}, { sections: [], exit: '0', verdict: 'ready' }, { sections: [], exit: 0 }]) {
        assert.equal(parse_doctor(body), null, JSON.stringify(body));
    }
    const odd = { sections: [{ title: 't', checks: [{ name: 'x', level: 'purple' }] }], exit: 0, verdict: 'ready' };
    assert.equal(parse_doctor(odd), null);
});

test('every level has a mark, and the counts add up', () => {
    assert.deepEqual(LEVEL_MARKS, { ok: '✓', warn: '!', fail: '✗', off: '·' });
    assert.deepEqual(doctor_counts(report()), { ok: 1, warn: 1, fail: 1, off: 1 });
});

test('the verdict follows the exit code first, then any warning', () => {
    assert.equal(verdict_level(report()), 'fail');
    assert.equal(verdict_level({ ...report(), exit: 0 }), 'warn');
    const clean = parse_doctor({ sections: [{ title: 't', checks: [{ name: 'git', level: 'ok' }, { name: 'laya', level: 'off' }] }], exit: 0, verdict: 'ready' });
    assert.ok(clean);
    assert.equal(verdict_level(clean), 'ok');
});

test('a fix is printed for every level but ok', () => {
    const [tmux, ttyd] = report().sections[0]!.checks;
    assert.equal(fix_of(ttyd!), 'brew install ttyd');
    assert.equal(fix_of({ ...tmux!, fix: 'stale advice' }), '');
    assert.equal(fix_of({ ...tmux!, level: 'off', fix: 'tldrack laya install' }), 'tldrack laya install');
});

test('an action’s key names its kind and its profile', () => {
    assert.equal(action_key({ kind: 'install-hooks', profile: 'claude' }), 'install-hooks:claude');
});

test('the hook install’s 200 is read as the bridge sends it', () => {
    const reply = parse_install_hooks({ ok: true, changed: true, message: 'Claude: hooks installed — new sessions pick them up', backup: null, notes: [] });
    assert.deepEqual(reply, { ok: true, changed: true, message: 'Claude: hooks installed — new sessions pick them up', backup: null, notes: [] });
    // `notes` and `backup` are extras: a reply without them is still the answer.
    assert.equal(parse_install_hooks({ ok: true, changed: false, message: 'already current' })?.changed, false);
    // A refusal body is never read as a success.
    assert.equal(parse_install_hooks({ ok: false, error: 'unknown profile, or one with no hooks file' }), null);
});
