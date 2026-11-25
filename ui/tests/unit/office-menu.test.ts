import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { office_menu, office_menu_key } from '../../src/agent-office/office-menu.ts';
import { worktree_button_view, type WorktreeButtonRow } from '../../src/engine/terminal/rack-view.ts';
import type { OfficeAgent } from '../../src/engine/terminal/agent-office-logic.ts';

function agent(over: Partial<OfficeAgent> = {}): OfficeAgent {
    return { kind: 'agent', session: 'wt-nav', label: 'Fix the nav', state: 'busy', pose: 'working', screen: 'CODE',
        tool: null, detail: null, lead: false, focused: false, ...over };
}
function worktree(over: Partial<WorktreeButtonRow> = {}): WorktreeButtonRow {
    return { branch: 'fix/nav', dirty: false, ahead: 2, pr_status: null, pr_url: null, pr_error: null, ...over };
}
const LANDED: Partial<WorktreeButtonRow> = { landed: true, landing: { how: 'pr', pr: 7 } };

describe('the office menu', () => {
    test('offers the row\'s own actions: open, the worktree button, close', () => {
        const items = office_menu(agent(), worktree(), null, true);
        assert.deepEqual(items.map(item => [item.id, item.press]), [['select', 'send'], ['pr', 'arm'], ['kill', 'arm']]);
        // No worktree behind the session: nothing to PR or clean.
        assert.deepEqual(office_menu(agent(), undefined, null, true).map(item => item.id), ['select', 'kill']);
        // The focused session is already in the pane, as on its row.
        assert.deepEqual(office_menu(agent({ focused: true }), undefined, null, true).map(item => item.id), ['kill']);
        // A service cabinet is closed like any session, and never carries a worktree button.
        assert.deepEqual(office_menu(agent({ kind: 'service', session: 'svc-vite' }), worktree(), null, true).map(item => item.id),
            ['select', 'kill']);
    });

    test('the worktree entry is the rack row\'s button, word for word, through every state', () => {
        const rows: WorktreeButtonRow[] = [
            worktree(), worktree({ dirty: true }), worktree({ pr_status: 'opening' }),
            worktree({ pr_status: 'open', pr_url: 'https://github.com/demo/demo/pull/7' }),
            worktree({ pr_status: 'error', pr_error: 'push rejected' }),
            worktree(LANDED), worktree({ ...LANDED, cleanup_status: 'running' }),
            worktree({ ...LANDED, cleanup_status: 'error', cleanup_error: 'tree is dirty' }),
        ];
        for (const row of rows) {
            for (const armed of [null, 'pr', 'cleanup'] as const) {
                const view = worktree_button_view(row, armed);
                const item = office_menu(agent(), row, armed, true).find(entry => entry.id === 'pr' || entry.id === 'cleanup');
                assert.ok(item);
                assert.deepEqual([item.id, item.label, item.detail, item.press, item.url], [view.kind, view.label, view.title, view.press, view.url]);
            }
        }
    });

    test('only the armed entry reads armed, and an arm for the other verb arms nothing', () => {
        const armed_pr = office_menu(agent(), worktree(), 'pr', true);
        assert.deepEqual(armed_pr.map(item => [item.label, item.armed]), [['▶ OPEN', false], ['PR?', true], ['✕ CLOSE', false]]);
        // The button turned CLEAN under a PR arm: it shows CLEAN, unarmed.
        const turned = office_menu(agent(), worktree(LANDED), 'pr', true).find(item => item.id === 'cleanup');
        assert.deepEqual([turned?.label, turned?.armed], ['CLEAN', false]);
        const closing = office_menu(agent(), worktree(), 'kill', true).find(item => item.id === 'kill');
        assert.deepEqual([closing?.label, closing?.armed, closing?.detail], ['CLOSE?', true, "Click again: closes 'wt-nav'"]);
    });

    test('a rack that cannot act leaves every entry dead but the link to GitHub', () => {
        const open = worktree({ pr_status: 'open', pr_url: 'https://github.com/demo/demo/pull/7' });
        assert.deepEqual(office_menu(agent(), open, null, false).map(item => [item.id, item.press]),
            [['select', 'none'], ['pr', 'open'], ['kill', 'none']]);
        assert.deepEqual(office_menu(agent(), worktree(), null, false).map(item => item.press), ['none', 'none', 'none']);
    });

    test('an arm is keyed by session and entry', () => {
        assert.notEqual(office_menu_key('a', 'kill'), office_menu_key('b', 'kill'));
        assert.notEqual(office_menu_key('a', 'pr'), office_menu_key('a', 'cleanup'));
    });
});
