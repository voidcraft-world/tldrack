import test from 'node:test';
import assert from 'node:assert/strict';

import { parse_update, type UpdateReply, type UpdateView } from '../../src/update/update-client.ts';
import {
    FAST_POLL_MS,
    FOCUS_READ_MS,
    RESTART_WAIT_MS,
    RUN_SETTLE_MS,
    SLOW_POLL_MS,
    after_press,
    after_reply,
    checked_words,
    code_spans,
    finished_words,
    in_flight,
    initial_update_state,
    notice_line,
    notice_words,
    only_checking,
    page_is_newer,
    phase_words,
    plan_steps,
    poll_delay_ms,
    reads_on_return,
    shown_phase,
    update_headline,
    update_level,
    update_notice,
    without_code_marks,
    type UpdateState,
} from '../../src/update/update-status.ts';

const T0 = 1_790_000_600_000; // ms; ten minutes after the default view's check
const CHECKED = 1_790_000_000;

/** An up-to-date, idle view; `over` is what a case is about. */
function view(over: Record<string, unknown> = {}): UpdateView {
    const parsed = parse_update({
        supported: true,
        branch: 'main',
        upstream: 'origin/main',
        head: '1c169f8',
        target: '1c169f8',
        page: { id: 'page-1', source: 'current' },
        code: 'current',
        checked_at: CHECKED,
        boot_id: 'boot-1',
        ...over,
    });
    assert.ok(parsed);
    return parsed;
}
const behind = (over: Record<string, unknown> = {}): UpdateView => view({ behind: 3, target: 'abc1234', available: true, ...over });
const got = (data: UpdateView): UpdateReply => ({ kind: 'view', view: data });
const SILENT: UpdateReply = { kind: 'silent', error: 'the bridge is not reachable' };

/** A state that has read `data` once, on a page the bridge served. */
function seen(data: UpdateView, tracks_page = true): UpdateState {
    return after_reply(initial_update_state(tracks_page), got(data), T0);
}

// ── the level, and the words that go with it ─────────────────────────────────

test('nothing to say is nothing drawn: no view yet, no support, or simply up to date', () => {
    assert.equal(update_level(initial_update_state(true)), 'none');
    assert.equal(update_level(seen(view())), 'none');
    assert.equal(update_level(seen(view({ supported: false, reason: 'not a git checkout', behind: 3, available: true }))), 'none');
    const unsupported = after_reply(seen(behind()), { kind: 'unsupported', status: 404, error: 'not found' }, T0);
    assert.equal(unsupported.view, null);
    assert.equal(update_notice(unsupported), null);
    // Behind, but nothing is in the way and nothing can run (the bridge says so): not this page's call to invent one.
    assert.equal(update_level(seen(view({ behind: 2 }))), 'none');
});

test('READY when a press would do something, counted in commits when the press is a pull', () => {
    const notice = update_notice(seen(behind()));
    assert.deepEqual(notice, { level: 'ready', label: 'UPDATE READY', detail: '3 commits', action: 'settings' });
    assert.equal(notice_line(notice!), 'UPDATE READY · 3 commits');
    assert.equal(notice_words(notice!), 'update ready: 3 commits');
    assert.equal(notice_line(update_notice(seen(behind({ behind: 1 })))!), 'UPDATE READY · 1 commit');
});

test('READY with no pull says what the press would do instead', () => {
    assert.equal(notice_line(update_notice(seen(view({ available: true, page: { id: 'page-1', source: 'stale' } })))!), 'UPDATE READY · rebuild the page');
    assert.equal(notice_line(update_notice(seen(view({ available: true, code: 'stale' })))!), 'UPDATE READY · restart the bridge');
    // A pull is blocked, but the page can still be rebuilt: READY, for that half.
    const half = seen(behind({ blocked: '2 uncommitted changes', page: { id: 'page-1', source: 'stale' } }));
    assert.equal(notice_line(update_notice(half)!), 'UPDATE READY · rebuild the page');
});

test('HELD when a pull is blocked, a run waits, or the last run failed: each in the bridge’s own words', () => {
    const blocked = update_notice(seen(view({ behind: 3, blocked: 'on branch docs/x, not main' })));
    assert.deepEqual(blocked, { level: 'held', label: 'UPDATE HELD', detail: 'on branch docs/x, not main', action: 'settings' });
    assert.equal(notice_line(blocked!), 'UPDATE HELD · on branch docs/x, not main');
    assert.equal(notice_words(blocked!), 'update held: on branch docs/x, not main');

    assert.equal(notice_line(update_notice(seen(behind({ waiting: 'busy: creating worktree feat-x' })))!), 'UPDATE WAITING · busy: creating worktree feat-x');
    // A failure outranks "ready": the press that would retry it is the same press, and the reason must be seen first.
    const failed = update_notice(seen(behind({ error: 'npm run build exited 1' })));
    assert.deepEqual([failed?.level, notice_line(failed!)], ['held', 'UPDATE FAILED · npm run build exited 1']);
    // Blocked with nothing to pull is not news.
    assert.equal(update_level(seen(view({ blocked: '2 uncommitted changes' }))), 'none');
    // A long sentence is clipped for the tray; the card shows it whole.
    const long = update_notice(seen(behind({ error: 'x'.repeat(400) })));
    assert.equal(long?.detail?.length, 140);
});

test('BUSY while a check or a run is in flight, whoever started it', () => {
    const pulling = update_notice(seen(behind({ phase: 'pulling' })));
    assert.deepEqual([pulling?.level, notice_line(pulling!)], ['busy', 'UPDATING · pulling 3 commits']);
    assert.equal(notice_line(update_notice(seen(view({ phase: 'building' })))!), 'UPDATING · building the page');
    assert.equal(notice_line(update_notice(seen(view({ phase: 'checking' })))!), 'CHECKING FOR UPDATES');
    // A run outranks everything else there is to say.
    assert.equal(update_level(seen(behind({ phase: 'verifying', error: 'old failure', blocked: 'x' }))), 'busy');
    // Pressed here, and the bridge has not named a phase yet.
    assert.equal(notice_line(update_notice(after_press(seen(behind()), 'apply', T0))!), 'UPDATING · starting');
    assert.equal(notice_line(update_notice(after_press(seen(behind()), 'check', T0))!), 'CHECKING FOR UPDATES');
});

test('every run opens with a look at the remote: an UPDATE pressed here is an update from its first moment', () => {
    const checking = behind({ phase: 'checking' });
    // Somebody else's, or the timed one: all this page knows is that the remote is being asked.
    assert.equal(only_checking(seen(checking)), true);
    // CHECK NOW, pressed here.
    const check = after_reply(after_press(seen(behind()), 'check', T0), got(checking), T0 + 50, 'press');
    assert.deepEqual([only_checking(check), update_headline(check, T0)?.line], [true, 'Checking the remote…']);
    // UPDATE, pressed here: the same phase on the wire, another thing on the page.
    const apply = after_reply(after_press(seen(behind()), 'apply', T0), got(checking), T0 + 50, 'press');
    assert.equal(only_checking(apply), false);
    assert.equal(notice_line(update_notice(apply)!), 'UPDATING · checking the remote');
    assert.deepEqual(update_headline(apply, T0), { tag: 'UPDATING', tone: 'plain', line: 'Updating: checking the remote…' });
    assert.equal(only_checking(seen(behind())), false, 'nothing in flight is not a check');
});

test('every phase has words, and one this page does not know is shown as spelled', () => {
    const words = ['checking', 'pulling', 'building', 'syncing', 'verifying', 'restarting'].map((phase) => phase_words(phase, behind()));
    assert.deepEqual(words, [
        'checking the remote',
        'pulling 3 commits',
        'building the page',
        'installing bridge dependencies',
        'checking that the new code starts',
        'restarting the bridge',
    ]);
    assert.equal(phase_words('pulling', view()), 'pulling');
    assert.equal(phase_words('migrating', null), 'migrating');
});

test('a name the bridge marked in backticks is a code span where markup is drawn, and plain where it is not', () => {
    assert.deepEqual(code_spans('the checkout is on branch `docs/x`, not `main`'), [
        { text: 'the checkout is on branch ', code: false },
        { text: 'docs/x', code: true },
        { text: ', not ', code: false },
        { text: 'main', code: true },
    ]);
    assert.deepEqual(code_spans('no marks here'), [{ text: 'no marks here', code: false }]);
    assert.deepEqual(code_spans('an unpaired ` stays text'), [{ text: 'an unpaired ` stays text', code: false }]);
    assert.deepEqual(code_spans('`tldrack kill`, then `tldrack up`').map((span) => span.text), ['tldrack kill', ', then ', 'tldrack up']);
    assert.deepEqual(code_spans(''), []);
    assert.equal(without_code_marks('restart the bridge to run it: `tldrack kill`, then `tldrack up`'), 'restart the bridge to run it: tldrack kill, then tldrack up');
    // The tray's line and the ⚙'s title are plain text.
    const notice = update_notice(seen(view({ behind: 3, blocked: 'the checkout is on branch `docs/x`, not `main`' })));
    assert.equal(notice_line(notice!), 'UPDATE HELD · the checkout is on branch docs/x, not main');
});

// ── what UPDATE will do ──────────────────────────────────────────────────────

test('the plan is only the steps that apply, and a pull promises the other two with their condition', () => {
    assert.deepEqual(plan_steps(behind()), [
        'pull 3 commits',
        'rebuild the page, if its source changed',
        'restart the bridge, if its code changed',
    ]);
    assert.deepEqual(plan_steps(behind({ page: { id: 'p', source: 'stale' }, code: 'stale' })), [
        'pull 3 commits',
        'rebuild the page',
        'restart the bridge',
    ]);
    assert.deepEqual(plan_steps(view()), []);
    assert.deepEqual(plan_steps(view({ page: { id: 'p', source: 'stale' } })), ['rebuild the page']);
    // A build nobody stamped asks for nothing on its own: the bridge rebuilds it only with a pull.
    assert.deepEqual(plan_steps(view({ page: { id: 'p', source: 'unknown' } })), []);
    assert.deepEqual(plan_steps(view({ code: 'stale' })), ['restart the bridge']);
    // An unknown code state is not a reason to restart.
    assert.deepEqual(plan_steps(view({ code: 'unknown' })), []);
    // A blocked pull is not a step, and so brings nothing with it.
    assert.deepEqual(plan_steps(behind({ blocked: 'on branch docs/x, not main' })), []);
    assert.deepEqual(plan_steps(behind({ blocked: '2 uncommitted changes', code: 'stale' })), ['restart the bridge']);
});

// ── a newer build of the page ────────────────────────────────────────────────

test('the first page id a page load sees is its own; a later, different one is a newer build', () => {
    let state = seen(view());
    assert.equal(state.first_page_id, 'page-1');
    assert.equal(page_is_newer(state), false);
    state = after_reply(state, got(view({ page: { id: 'page-2', source: 'current' } })), T0);
    assert.equal(state.first_page_id, 'page-1', 'the page on screen did not change');
    assert.equal(page_is_newer(state), true);
    const notice = update_notice(state);
    assert.deepEqual(notice, { level: 'ready', label: 'NEW PAGE BUILD', detail: null, action: 'reload' });
    assert.equal(notice_line(notice!), 'NEW PAGE BUILD');
    assert.equal(notice_words(notice!), 'new page build');
    // It outranks an update that is also ready: reload first, then the page on screen can be trusted.
    const both = after_reply(state, got(behind({ page: { id: 'page-2', source: 'current' } })), T0);
    assert.equal(update_notice(both)?.action, 'reload');
});

test('a null id is not a build: it neither becomes this page’s own nor counts as newer', () => {
    let state = seen(view({ page: { id: null, source: 'unknown' } }));
    assert.equal(state.first_page_id, null);
    state = after_reply(state, got(view()), T0);
    assert.equal(state.first_page_id, 'page-1');
    state = after_reply(state, got(view({ page: { id: null, source: 'unknown' } })), T0);
    assert.equal(page_is_newer(state), false);
});

test('a page the bridge did not serve never judges the build: a reload would fetch the same bundle', () => {
    let state = seen(view(), false);
    state = after_reply(state, got(view({ page: { id: 'page-2', source: 'current' } })), T0);
    assert.equal(state.first_page_id, null);
    assert.equal(page_is_newer(state), false);
    assert.equal(update_level(state), 'none');
});

// ── a run this page started ──────────────────────────────────────────────────

test('a run is followed from its press to the idle view after it', () => {
    let state = after_press(seen(behind()), 'apply', T0);
    assert.equal(in_flight(state), true);
    // The press's own answer never ends the run, busy or not.
    state = after_reply(state, got(behind({ phase: 'pulling' })), T0 + 50, 'press');
    assert.equal(in_flight(state), true);
    state = after_reply(state, got(behind({ phase: 'building' })), T0 + 2_000);
    assert.equal(shown_phase(state), 'building');
    state = after_reply(state, got(view({ head: 'abc1234', boot_id: 'boot-2', page: { id: 'page-2', source: 'current' } })), T0 + 60_000);
    assert.equal(in_flight(state), false);
    assert.deepEqual(state.finished, { action: 'apply', restarted: true });
    assert.equal(finished_words(state), 'Update finished. Now at main @ abc1234. The bridge restarted.');
    assert.equal(page_is_newer(state), true, 'the run rebuilt the page this tab is still showing');
});

test('a whole run can fit between two reads: it is over on the first idle view, with no failed read and no phase seen', () => {
    const pressed = after_press(seen(behind()), 'apply', T0);
    // Pulled, built and restarted inside one poll: a new process, a new head, nothing left to do.
    const fast = after_reply(pressed, got(view({ head: 'abc1234', boot_id: 'boot-2', page: { id: 'page-2', source: 'current' } })), T0 + 2_000);
    assert.equal(in_flight(fast), false);
    assert.deepEqual(fast.finished, { action: 'apply', restarted: true });
    // Each of these alone says the run is over, even on a view that did not otherwise move.
    for (const over of [{ available: false }, { error: 'the page build failed' }, { waiting: 'the restart waits for: creating worktree feat-x' }]) {
        assert.equal(in_flight(after_reply(pressed, got(behind(over)), T0 + 2_000)), false, JSON.stringify(over));
    }
    // Still available, nothing moved, nothing said: the run has not started yet.
    assert.equal(in_flight(after_reply(pressed, got(behind()), T0 + 2_000)), true);
});

test('a press whose answer is still idle waits for a sign the run happened, then for the settle time', () => {
    let state = after_press(seen(view()), 'check', T0);
    state = after_reply(state, got(view()), T0 + 50, 'press');
    // Nothing moved and no busy phase was seen: too early to call it over.
    state = after_reply(state, got(view()), T0 + 2_000);
    assert.equal(in_flight(state), true);
    assert.equal(shown_phase(state), 'checking');
    // The view moved (a newer `checked_at`): that is the check's result.
    const moved = after_reply(state, got(view({ checked_at: CHECKED + 700 })), T0 + 4_000);
    assert.equal(in_flight(moved), false);
    assert.equal(finished_words(moved), 'Checked: nothing new.');
    assert.equal(finished_words(after_reply(state, got(behind({ checked_at: CHECKED + 700 })), T0 + 4_000)), 'Checked: 3 commits to pull.');
    // Or nothing ever moves, and the settle time ends it.
    const settled = after_reply(state, got(view()), T0 + RUN_SETTLE_MS);
    assert.equal(in_flight(settled), false);
});

test('a refused press starts nothing, and its sentence stands until the next press', () => {
    let state = after_press(seen(behind()), 'apply', T0);
    state = after_reply(state, { kind: 'refused', status: 409, error: 'busy: creating worktree feat-x' }, T0 + 50, 'press');
    assert.equal(in_flight(state), false);
    assert.equal(state.press_error, 'busy: creating worktree feat-x');
    state = after_reply(state, got(behind()), T0 + 100);
    assert.equal(state.press_error, 'busy: creating worktree feat-x', 'a read does not clear what a press was told');
    assert.equal(after_press(state, 'apply', T0 + 5_000).press_error, null);
    // A press the bridge never heard started nothing either.
    const unheard = after_reply(after_press(seen(behind()), 'apply', T0), SILENT, T0 + 50, 'press');
    assert.deepEqual([in_flight(unheard), unheard.press_error], [false, 'the bridge is not reachable']);
});

test('a run that pulled but could not restart yet ends too, and the view says what it waits for', () => {
    let state = after_press(seen(behind()), 'apply', T0);
    state = after_reply(state, got(behind({ phase: 'checking' })), T0 + 50, 'press');
    state = after_reply(state, got(view({ head: 'abc1234', code: 'stale', available: true, waiting: 'the restart waits for: creating worktree feat-x' })), T0 + 40_000);
    assert.equal(in_flight(state), false);
    assert.equal(finished_words(state), null, '"finished" beside "waiting" would be two answers');
    assert.equal(notice_line(update_notice(state)!), 'UPDATE WAITING · the restart waits for: creating worktree feat-x');
});

test('a failed run ends like any other, and the view’s own error is the sentence', () => {
    let state = after_press(seen(behind()), 'apply', T0);
    state = after_reply(state, got(behind({ phase: 'building' })), T0 + 50, 'press');
    state = after_reply(state, got(behind({ error: 'npm run build exited 1' })), T0 + 30_000);
    assert.equal(in_flight(state), false);
    assert.equal(finished_words(state), null);
    assert.equal(update_headline(state, T0 + 30_000)?.tag, 'FAILED');
});

// ── the restart ──────────────────────────────────────────────────────────────

test('no answer during a run is the restart: the state is kept and drawn as restarting, with no error', () => {
    let state = after_press(seen(behind()), 'apply', T0);
    state = after_reply(state, got(behind({ phase: 'verifying' })), T0 + 50, 'press');
    state = after_reply(state, SILENT, T0 + 2_000);
    assert.equal(in_flight(state), true);
    assert.equal(shown_phase(state), 'restarting');
    assert.equal(state.read_error, null);
    assert.equal(notice_line(update_notice(state)!), 'UPDATING · restarting the bridge');
    assert.equal(update_headline(state, T0)?.line, 'Updating: restarting the bridge…');
    // Still silent a minute and a half on: still believed.
    state = after_reply(state, SILENT, T0 + 2_000 + RESTART_WAIT_MS - 1);
    assert.equal(in_flight(state), true);
    // The bridge is back as a new process, idle: the restart landed.
    const landed = after_reply(state, got(view({ head: 'abc1234', boot_id: 'boot-2' })), T0 + 95_000);
    assert.equal(in_flight(landed), false);
    assert.deepEqual(landed.finished, { action: 'apply', restarted: true });
    assert.equal(landed.no_view_since, null);
});

test('a run somebody else started (auto-update) is waited out the same way', () => {
    let state = seen(behind({ phase: 'restarting' }));
    state = after_reply(state, SILENT, T0 + 2_000);
    assert.deepEqual([in_flight(state), shown_phase(state), state.read_error], [true, 'restarting', null]);
    state = after_reply(state, got(view({ boot_id: 'boot-2' })), T0 + 20_000);
    assert.equal(in_flight(state), false);
    assert.equal(state.finished, null, 'this page did not start it, so it has nothing to report');
});

test('silence past the wait gives the run up as lost, with a sentence, until the bridge answers again', () => {
    let state = after_press(seen(behind()), 'apply', T0);
    state = after_reply(state, SILENT, T0 + 2_000);
    state = after_reply(state, SILENT, T0 + 2_000 + RESTART_WAIT_MS);
    assert.equal(state.lost, true);
    assert.equal(in_flight(state), false);
    assert.match(state.read_error ?? '', /has not answered for two minutes/);
    assert.equal(poll_delay_ms(state, false), SLOW_POLL_MS);
    // Even a stale view that still names a phase does not put the fast poll back.
    const stuck = after_reply(after_reply(seen(behind({ phase: 'restarting' })), SILENT, T0), SILENT, T0 + RESTART_WAIT_MS);
    assert.deepEqual([stuck.lost, in_flight(stuck)], [true, false]);
    const back = after_reply(state, got(view({ boot_id: 'boot-2' })), T0 + 600_000);
    assert.deepEqual([back.lost, back.read_error, back.no_view_since], [false, null, null]);
});

test('silence with nothing in flight is a bridge that is down: no error of the card’s own, and the view stays', () => {
    const state = after_reply(seen(behind()), SILENT, T0 + 1_000);
    assert.equal(state.no_view_since, T0 + 1_000);
    assert.equal(state.read_error, null);
    assert.equal(state.view?.behind, 3);
    assert.equal(after_reply(state, SILENT, T0 + 9_000).no_view_since, T0 + 1_000, 'the clock starts at the first silence');
});

test('a refused read is shown as itself, and counts against the wait only during a run', () => {
    const refused: UpdateReply = { kind: 'refused', status: 500, error: 'update state unreadable' };
    const idle = after_reply(seen(view()), refused, T0);
    assert.deepEqual([idle.read_error, idle.no_view_since], ['update state unreadable', null]);
    assert.equal(after_reply(idle, got(view()), T0).read_error, null);
    const flying = after_reply(after_press(seen(behind()), 'apply', T0), refused, T0 + 2_000);
    assert.deepEqual([in_flight(flying), flying.no_view_since], [true, T0 + 2_000]);
    assert.equal(after_reply(flying, refused, T0 + 2_000 + RESTART_WAIT_MS).lost, true);
});

// ── when to ask again ────────────────────────────────────────────────────────

test('the poll is slow while idle, fast while a run is in flight, and absent while the tab is hidden', () => {
    assert.equal(SLOW_POLL_MS, 5 * 60_000);
    assert.equal(FAST_POLL_MS, 2_000);
    assert.equal(poll_delay_ms(initial_update_state(true), false), SLOW_POLL_MS);
    assert.equal(poll_delay_ms(seen(behind()), false), SLOW_POLL_MS);
    assert.equal(poll_delay_ms(seen(view({ phase: 'building' })), false), FAST_POLL_MS);
    assert.equal(poll_delay_ms(after_press(seen(behind()), 'apply', T0), false), FAST_POLL_MS);
    // No support is asked about slowly too: a restart may bring a bridge that has it.
    assert.equal(poll_delay_ms(after_reply(initial_update_state(true), { kind: 'unsupported', status: 404, error: 'x' }, T0), false), SLOW_POLL_MS);
    for (const state of [seen(behind()), seen(view({ phase: 'building' }))]) assert.equal(poll_delay_ms(state, true), null);
});

test('coming back to the tab always reads; a window focus only when the last read is old', () => {
    assert.equal(reads_on_return('visible', 0), true);
    assert.equal(reads_on_return('focus', FOCUS_READ_MS - 1), false);
    assert.equal(reads_on_return('focus', FOCUS_READ_MS), true);
    assert.equal(reads_on_return('focus', Infinity), true);
});

// ── the card's headline ──────────────────────────────────────────────────────

test('the headline says where the checkout stands, in words', () => {
    assert.equal(update_headline(initial_update_state(true), T0), null);
    assert.deepEqual(update_headline(seen(view()), T0), { tag: 'UP TO DATE', tone: 'ok', line: 'Up to date · main @ 1c169f8 · checked 10 min ago' });
    assert.deepEqual(update_headline(seen(behind()), T0), {
        tag: 'UPDATE READY',
        tone: 'plain',
        line: '3 commits behind origin/main · main @ 1c169f8 · checked 10 min ago',
    });
    assert.deepEqual(update_headline(seen(view({ behind: 1, blocked: 'on branch docs/x, not main', branch: 'docs/x' })), T0), {
        tag: 'HELD',
        tone: 'warn',
        line: 'Held: on branch docs/x, not main · 1 commit behind origin/main · checked 10 min ago',
    });
    assert.deepEqual(update_headline(seen(view({ phase: 'building' })), T0), { tag: 'UPDATING', tone: 'plain', line: 'Updating: building the page…' });
    assert.deepEqual(update_headline(seen(view({ phase: 'checking' })), T0), { tag: 'CHECKING', tone: 'plain', line: 'Checking the remote…' });
    assert.equal(
        update_headline(seen(view({ available: true, code: 'stale', page: { id: 'p', source: 'stale' } })), T0)?.line,
        'main @ 1c169f8 has every commit, but the served page is older than its source, and the running bridge is older than the code on disk · checked 10 min ago',
    );
    // "Up to date" is a claim about the remote, so a checkout that never asked does not make it.
    assert.deepEqual(update_headline(seen(view({ checked_at: null })), T0), { tag: 'NOT CHECKED', tone: 'plain', line: 'Not checked yet · main @ 1c169f8' });
    assert.equal(update_headline(seen(view({ supported: false })), T0), null);
});

test('how long ago the remote was asked', () => {
    const at = (seconds_ago: number): string => checked_words(CHECKED, (CHECKED + seconds_ago) * 1000);
    assert.equal(checked_words(null, T0), 'never checked');
    assert.equal(at(0), 'checked just now');
    assert.equal(at(59), 'checked just now');
    assert.equal(at(-30), 'checked just now', 'a clock a little behind the bridge’s is not the future');
    assert.equal(at(60), 'checked 1 min ago');
    assert.equal(at(59 * 60), 'checked 59 min ago');
    assert.equal(at(60 * 60), 'checked 1 h ago');
    assert.equal(at(47 * 3600), 'checked 47 h ago');
    assert.equal(at(72 * 3600), 'checked 3 days ago');
});
