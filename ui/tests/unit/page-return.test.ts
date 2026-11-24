import test from 'node:test';
import assert from 'node:assert/strict';
import { watch_page_return, type PageReturn, type PageReturnEnv } from '../../src/agent-rack/page-return.ts';

/** A page with a visibility to flip and a focus to give. */
function fake_page() {
    const on = { visibilitychange: new Set<() => void>(), focus: new Set<() => void>() };
    const page = { hidden: false };
    const env: PageReturnEnv = {
        document: {
            get hidden() { return page.hidden; },
            addEventListener: (_type, listener) => void on.visibilitychange.add(listener),
            removeEventListener: (_type, listener) => void on.visibilitychange.delete(listener),
        },
        window: {
            addEventListener: (_type, listener) => void on.focus.add(listener),
            removeEventListener: (_type, listener) => void on.focus.delete(listener),
        },
    };
    return {
        env,
        on,
        set_hidden: (hidden: boolean) => {
            page.hidden = hidden;
            for (const listener of on.visibilitychange) listener();
        },
        focus: () => { for (const listener of on.focus) listener(); },
    };
}

test('a page shown again is a return; a page being hidden is not', () => {
    const page = fake_page();
    const seen: PageReturn[] = [];
    watch_page_return((how) => seen.push(how), page.env);
    page.set_hidden(true);
    assert.deepEqual(seen, [], 'leaving is the rest, not the wake');
    page.set_hidden(false);
    assert.deepEqual(seen, ['visible']);
});

test('a window taking focus is told apart, so a caller can rate-limit the noisy one', () => {
    const page = fake_page();
    const seen: PageReturn[] = [];
    watch_page_return((how) => seen.push(how), page.env);
    page.focus();
    page.focus();
    assert.deepEqual(seen, ['focus', 'focus']);
});

test('stopping removes both listeners, and no page at all is a watcher that does nothing', () => {
    const page = fake_page();
    const seen: PageReturn[] = [];
    const stop = watch_page_return((how) => seen.push(how), page.env);
    stop();
    assert.equal(page.on.visibilitychange.size + page.on.focus.size, 0);
    page.set_hidden(false);
    page.focus();
    assert.deepEqual(seen, []);
    // Under `node --test` there is no document: the default finds nothing and must not throw.
    assert.doesNotThrow(() => watch_page_return(() => {})());
    assert.doesNotThrow(() => watch_page_return(() => {}, null)());
});
