import test from 'node:test';
import assert from 'node:assert/strict';

import type { PageReturn } from '../../src/agent-rack/page-return.ts';
import { parse_update, type UpdateAction, type UpdateReply } from '../../src/update/update-client.ts';
import { create_update_poller } from '../../src/update/update-poller.ts';
import { FAST_POLL_MS, FOCUS_READ_MS, SLOW_POLL_MS, in_flight, update_level, type UpdateState } from '../../src/update/update-status.ts';

function reply(over: Record<string, unknown> = {}): UpdateReply {
    const view = parse_update({ supported: true, branch: 'main', head: '1c169f8', page: { id: 'page-1', source: 'current' }, boot_id: 'boot-1', ...over });
    assert.ok(view);
    return { kind: 'view', view };
}
const SILENT: UpdateReply = { kind: 'silent', error: 'the bridge is not reachable' };

/** A page with a clock, a visibility, one timer slot and a bridge whose next answers are queued. */
function fake_page(first: UpdateReply = reply()) {
    const page = {
        now: 0,
        hidden: false,
        reads: 0,
        posts: [] as UpdateAction[],
        /** What the bridge says next; the last one repeats. */
        answers: [first] as UpdateReply[],
        press_answer: reply({ phase: 'pulling' }) as UpdateReply,
        timer: null as { run: () => void; ms: number } | null,
        returns: new Set<(how: PageReturn) => void>(),
        states: [] as UpdateState[],
        /** The wait the poller last asked for, or null when no timer is set. */
        wait(): number | null {
            return this.timer?.ms ?? null;
        },
    };
    const poller = create_update_poller({
        read: async () => {
            page.reads += 1;
            return page.answers.length > 1 ? page.answers.shift()! : page.answers[0]!;
        },
        post: async (action) => {
            page.posts.push(action);
            return page.press_answer;
        },
        now: () => page.now,
        is_hidden: () => page.hidden,
        set_timer: (run, ms) => (page.timer = { run, ms }),
        clear_timer: (handle) => {
            if (page.timer === handle) page.timer = null;
        },
        watch_return: (on_return) => {
            page.returns.add(on_return);
            return () => void page.returns.delete(on_return);
        },
        tracks_page: true,
    });
    /** Let every promise already resolved run its continuation. */
    const settle = async (): Promise<void> => {
        for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
    };
    return {
        page,
        poller,
        settle,
        subscribe: () => poller.subscribe((state) => page.states.push(state)),
        /** The wait the poller asked for runs out. */
        fire: async (): Promise<void> => {
            const timer = page.timer;
            assert.ok(timer, 'no timer is set');
            page.timer = null;
            page.now += timer.ms;
            timer.run();
            await settle();
        },
        come_back: async (how: PageReturn): Promise<void> => {
            for (const on_return of page.returns) on_return(how);
            await settle();
        },
    };
}

test('the first subscriber reads once and sets the slow timer; a second shares both', async () => {
    const { page, poller, settle, subscribe } = fake_page();
    assert.equal(page.reads, 0, 'nothing is read before anybody asks');
    subscribe();
    subscribe();
    await settle();
    assert.equal(page.reads, 1);
    assert.equal(page.wait(), SLOW_POLL_MS);
    assert.equal(poller.state().view?.head, '1c169f8');
    assert.equal(page.returns.size, 1);
});

test('each slow tick is one read, and the next tick is set again', async () => {
    const { page, settle, subscribe, fire } = fake_page();
    subscribe();
    await settle();
    await fire();
    await fire();
    assert.equal(page.reads, 3);
    assert.equal(page.wait(), SLOW_POLL_MS);
});

test('a hidden tab sets no timer, a tick that fires hidden reads nothing, and the return reads at once', async () => {
    const { page, settle, subscribe, fire, come_back } = fake_page();
    subscribe();
    await settle();
    page.hidden = true;
    await fire();
    assert.equal(page.reads, 1, 'hidden since the timer was set: rest');
    assert.equal(page.timer, null);
    page.hidden = false;
    await come_back('visible');
    assert.equal(page.reads, 2);
    assert.equal(page.wait(), SLOW_POLL_MS);
});

test('a window focus reads only when the last read is old', async () => {
    const { page, settle, subscribe, come_back } = fake_page();
    subscribe();
    await settle();
    page.now += FOCUS_READ_MS - 1;
    await come_back('focus');
    assert.equal(page.reads, 1);
    page.now += 1;
    await come_back('focus');
    assert.equal(page.reads, 2);
});

test('a press posts once, is followed at the fast pace, and falls back to slow when the run ends', async () => {
    const { page, poller, settle, subscribe, fire } = fake_page(reply({ behind: 3, available: true }));
    subscribe();
    await settle();
    assert.equal(update_level(poller.state()), 'ready');
    const pressed = poller.press('apply');
    void poller.press('apply'); // a second press while the first is out
    await pressed;
    await settle();
    assert.deepEqual(page.posts, ['apply']);
    assert.equal(update_level(poller.state()), 'busy');
    assert.equal(page.wait(), FAST_POLL_MS);
    page.answers = [reply({ phase: 'building' }), reply({ head: 'abc1234', boot_id: 'boot-2' })];
    await fire();
    assert.equal(page.wait(), FAST_POLL_MS);
    await fire();
    assert.equal(in_flight(poller.state()), false);
    assert.deepEqual(poller.state().finished, { action: 'apply', restarted: true });
    assert.equal(page.wait(), SLOW_POLL_MS);
    // And no press is taken while a run is in flight.
    page.answers = [reply({ phase: 'building' })];
    await fire();
    await poller.press('check');
    assert.deepEqual(page.posts, ['apply']);
});

test('through a restart the reads keep failing and keep coming, with no error shown', async () => {
    const { page, poller, settle, subscribe, fire } = fake_page(reply({ behind: 1, available: true }));
    subscribe();
    await settle();
    await poller.press('apply');
    page.answers = [SILENT];
    for (let tick = 0; tick < 20; tick += 1) await fire();
    assert.equal(page.wait(), FAST_POLL_MS);
    assert.equal(poller.state().read_error, null);
    assert.equal(in_flight(poller.state()), true);
    page.answers = [reply({ boot_id: 'boot-2' })];
    await fire();
    assert.equal(in_flight(poller.state()), false);
    assert.equal(page.wait(), SLOW_POLL_MS);
});

test('a refused press is read after at once: the view on screen may be what was wrong', async () => {
    const { page, poller, settle, subscribe } = fake_page(reply({ behind: 3, available: true }));
    subscribe();
    await settle();
    page.press_answer = { kind: 'refused', status: 409, error: '2 uncommitted changes' };
    page.answers = [reply({ behind: 3, blocked: '2 uncommitted changes' })];
    await poller.press('apply');
    await settle();
    assert.equal(page.reads, 2);
    assert.equal(poller.state().press_error, '2 uncommitted changes');
    assert.equal(update_level(poller.state()), 'held');
    assert.equal(page.wait(), SLOW_POLL_MS);
});

test('refresh reads now; a read already out is not doubled', async () => {
    const { page, poller, settle, subscribe } = fake_page();
    subscribe();
    poller.refresh();
    poller.refresh();
    await settle();
    assert.equal(page.reads, 1);
    poller.refresh();
    await settle();
    assert.equal(page.reads, 2);
});

test('the last subscriber to leave stops the timer and the watcher; the state is kept for whoever comes next', async () => {
    const { page, poller, settle, subscribe } = fake_page();
    const first = subscribe();
    const second = subscribe();
    await settle();
    first();
    first(); // twice is once
    assert.ok(page.timer, 'one subscriber is still here');
    second();
    assert.equal(page.timer, null);
    assert.equal(page.returns.size, 0);
    poller.refresh();
    await settle();
    assert.equal(page.reads, 1, 'nobody is listening: nothing is read');
    assert.equal(poller.state().view?.head, '1c169f8');
    subscribe();
    await settle();
    assert.equal(page.reads, 2);
    assert.equal(page.wait(), SLOW_POLL_MS);
});

test('a read answered after its subscriber left is dropped, and the next subscriber gets one of their own', async () => {
    const { page, poller, settle, subscribe } = fake_page(reply({ head: 'old0000' }));
    const leave = subscribe(); // its read is out
    leave();
    page.answers = [reply({ head: 'new1111' })];
    subscribe();
    await settle();
    assert.equal(page.reads, 2);
    assert.equal(poller.state().view?.head, 'new1111');
    assert.equal(page.wait(), SLOW_POLL_MS);
});
