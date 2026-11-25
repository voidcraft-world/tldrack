import test from 'node:test';
import assert from 'node:assert/strict';

import { DIAGRAM_PAGE, EVALS_PAGE, RACK_PAGE, REVIEW_PAGE, SETTINGS_PAGE } from '../../src/config/pages.ts';
import { DEFAULT_TAB, SETTINGS_TABS, page_links, tab_for_key, tab_from_hash, tab_hash } from '../../src/settings/settings-tabs.ts';

test('the page has its four tabs, in order, and opens on the first', () => {
    assert.deepEqual(SETTINGS_TABS.map((tab) => tab.id), ['setup', 'features', 'appearance', 'directories']);
    assert.equal(DEFAULT_TAB, 'setup');
});

test('the hash names a tab: with or without its #, in any case', () => {
    assert.equal(tab_from_hash('#features'), 'features');
    assert.equal(tab_from_hash('features'), 'features');
    assert.equal(tab_from_hash('#APPEARANCE'), 'appearance');
    for (const tab of SETTINGS_TABS) assert.equal(tab_from_hash(tab_hash(tab.id)), tab.id);
});

test('an empty or unknown hash is the first tab, never an error', () => {
    for (const hash of ['', '#', '#nope', '#features/extra', '#__proto__', '# features']) assert.equal(tab_from_hash(hash), 'setup', hash);
});

test('the arrow keys walk the strip and wrap; Home and End jump; any other key is not the strip’s', () => {
    assert.equal(tab_for_key('setup', 'ArrowRight'), 'features');
    assert.equal(tab_for_key('directories', 'ArrowRight'), 'setup');
    assert.equal(tab_for_key('setup', 'ArrowLeft'), 'directories');
    assert.equal(tab_for_key('appearance', 'ArrowLeft'), 'features');
    assert.equal(tab_for_key('appearance', 'Home'), 'setup');
    assert.equal(tab_for_key('features', 'End'), 'directories');
    for (const key of ['Enter', ' ', 'Tab', 'ArrowDown', 'a']) assert.equal(tab_for_key('setup', key), null, key);
});

test('the other pages are resolved beside this one, wherever it was served from', () => {
    const at_root = page_links(`http://127.0.0.1:7682/${SETTINGS_PAGE}#features`);
    assert.deepEqual(
        at_root.map((link) => link.href),
        [RACK_PAGE, REVIEW_PAGE, DIAGRAM_PAGE, EVALS_PAGE].map((page) => `http://127.0.0.1:7682/${page}`),
    );
    // Behind a proxy that serves the pages under a path, they stay under that path.
    const proxied = page_links(`https://machine.example/rack/${SETTINGS_PAGE}?x=1#setup`);
    assert.equal(proxied[0]?.href, `https://machine.example/rack/${RACK_PAGE}`);
    assert.ok(proxied.every((link) => link.label && link.title));
});

test('an address that is not a URL yields no links', () => {
    assert.deepEqual(page_links('not a url'), []);
});
