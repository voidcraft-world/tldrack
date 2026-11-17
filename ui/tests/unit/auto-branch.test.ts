import test from 'node:test';
import assert from 'node:assert/strict';
import {
    AUTO_BRANCH_MAX,
    auto_branch,
    branch_from_clock,
    branch_from_task,
    unique_branch,
} from '../../src/agent-rack/auto-branch.ts';

/** `plugins/worktree/worktree/names.py::BRANCH_RE` — every name minted here must pass it. */
const PLUGIN_BRANCH_RE = /^[a-zA-Z0-9][a-zA-Z0-9._/-]*$/;

const AT = new Date(2026, 9, 27, 14, 32); // local 10-27 14:32

test('branch_from_task: the request\'s topic, filler dropped, at most four words', () => {
    assert.equal(branch_from_task('can you fix the nav sidebar'), 'fix-nav-sidebar');
    assert.equal(branch_from_task('Please add a search box to the settings page'), 'add-search-box-settings');
    assert.equal(branch_from_task('Fix   the  bug!!! in rack-view.ts'), 'fix-bug-rack-view');
    assert.equal(branch_from_task('Café menu: résumé link'), 'cafe-menu-resume-link', 'accents fold, not drop');
});

test('branch_from_task: nothing to say is empty, so the clock names it', () => {
    assert.equal(branch_from_task(''), '');
    assert.equal(branch_from_task('   '), '');
    assert.equal(branch_from_task('can you just do it please'), '');
    assert.equal(branch_from_task('🏝️ ✨'), '');
});

test('branch_from_task: stays inside the slug cap at a whole word', () => {
    const name = branch_from_task('refactor authentication middleware orchestration layer');
    assert.ok(name.length <= AUTO_BRANCH_MAX, name);
    assert.equal(name, 'refactor-authentication');
    const long = branch_from_task('a'.repeat(60));
    assert.equal(long.length, AUTO_BRANCH_MAX, 'one over-long word is cut, not lost');
});

test('branch_from_clock: who + local MMDD-HHMM', () => {
    assert.equal(branch_from_clock('A', AT), 'a-1027-1432');
    assert.equal(branch_from_clock('', AT), 'wt-1027-1432');
    assert.equal(branch_from_clock('Claude B', AT), 'claude-b-1027-1432');
});

test('unique_branch: the first suffix no known branch holds, inside the cap', () => {
    assert.equal(unique_branch('fix-nav', new Set()), 'fix-nav');
    assert.equal(unique_branch('fix-nav', new Set(['fix-nav'])), 'fix-nav-2');
    assert.equal(unique_branch('fix-nav', new Set(['fix-nav', 'fix-nav-2'])), 'fix-nav-3');
    const full = 'x'.repeat(AUTO_BRANCH_MAX - 1) + 'y';
    const next = unique_branch(full, new Set([full]));
    assert.ok(next.length <= AUTO_BRANCH_MAX, next);
    assert.ok(next.endsWith('-2'));
    // A cut that lands on a hyphen does not leave `--2`.
    const hyphen = `${'a'.repeat(AUTO_BRANCH_MAX - 3)}-bc`;
    assert.ok(!unique_branch(hyphen, new Set([hyphen])).includes('--'));
});

test('auto_branch: task first, clock as the fallback, always plugin-valid', () => {
    assert.equal(auto_branch('fix the nav', 'A', AT, new Set()), 'fix-nav');
    assert.equal(auto_branch('', 'CODEX A', AT, new Set()), 'codex-a-1027-1432');
    assert.equal(auto_branch('', 'CODEX A', AT, new Set(['codex-a-1027-1432'])), 'codex-a-1027-1432-2');
    for (const task of ['', 'fix the nav', '!!!', 'é', '中文任务', 'a'.repeat(200)]) {
        const name = auto_branch(task, 'A', AT, new Set());
        assert.match(name, PLUGIN_BRANCH_RE, `${JSON.stringify(task)} → ${name}`);
        assert.ok(name.length <= AUTO_BRANCH_MAX);
    }
});
