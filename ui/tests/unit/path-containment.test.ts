import test from 'node:test';
import assert from 'node:assert/strict';
import {
    longest_containing_root,
    normalize_dir,
    path_contains,
    same_dir,
} from '../../src/engine/terminal/path-containment.ts';

// The two spellings that meet at this seam (invented paths):
// `/snapshot` ships a pane's cwd absolute, every plugin GET ships `~`.
const COLLAPSED = '~/worktrees/my-app/x';
const ABSOLUTE = '/Users/u/worktrees/my-app/x';

test('normalize_dir strips trailing separators and nothing else', () => {
    assert.equal(normalize_dir('/a/b/'), '/a/b');
    assert.equal(normalize_dir('/a/b///'), '/a/b');
    assert.equal(normalize_dir('~/a/'), '~/a');
    assert.equal(normalize_dir('/'), '/');
    assert.equal(normalize_dir('~'), '~');
    assert.equal(normalize_dir(''), '');
});

// ── the suffix match: a `~` container against an absolute candidate ────────

test('a collapsed container holds an absolute cwd inside it', () => {
    assert.equal(path_contains(COLLAPSED, `${ABSOLUTE}/ui`), true);
    assert.equal(path_contains(COLLAPSED, ABSOLUTE), true, 'the tree itself counts');
    assert.equal(path_contains(COLLAPSED, `${ABSOLUTE}/`), true, 'trailing slash is noise');
    // Any home prefix — the front end has no $HOME to expand a tilde with.
    assert.equal(path_contains(COLLAPSED, '/home/someone/worktrees/my-app/x/a'), true);
});

test('the sibling prefix is the false positive it exists to refuse', () => {
    // `/a/b` must not contain `/a/bc` — this is the attach-only bug in miniature.
    assert.equal(path_contains(COLLAPSED, `${ABSOLUTE}-2`), false);
    assert.equal(path_contains('~/a/b', '/Users/u/a/bc'), false);
    assert.equal(path_contains('~/a/b', '/Users/u/a/bc/a/b'), true,
        'a later occurrence still passes when the first fails the boundary');
});

test('a different tree is a different tree', () => {
    assert.equal(path_contains('~/code/my-app', ABSOLUTE), false);
    assert.equal(path_contains(COLLAPSED, '/Users/u/code/my-app'), false);
});

test('~ alone never contains anything', () => {
    // "Inside your home" is not an island.
    assert.equal(path_contains('~', '/Users/u/anything'), false);
    assert.equal(path_contains('~/', '/Users/u/anything'), false);
    assert.equal(path_contains('', '/Users/u/anything'), false);
});

// ── the other dialect pairs ────────────────────────────────────────────────

test('collapsed vs collapsed is a prefix test', () => {
    assert.equal(path_contains(COLLAPSED, `${COLLAPSED}/src`), true);
    assert.equal(path_contains(COLLAPSED, COLLAPSED), true);
    assert.equal(path_contains(COLLAPSED, `${COLLAPSED}-2`), false);
    // Not a suffix search once both sides speak `~`: `/x/y` inside `~/foo/x/y`
    // is a different directory.
    assert.equal(path_contains('~/x/y', '~/foo/x/y'), false);
});

test('absolute vs absolute is a segment-boundary prefix test', () => {
    assert.equal(path_contains(ABSOLUTE, `${ABSOLUTE}/src`), true);
    assert.equal(path_contains(ABSOLUTE, ABSOLUTE), true);
    assert.equal(path_contains(ABSOLUTE, `${ABSOLUTE}-2`), false);
    assert.equal(path_contains('/', '/anything'), true, 'the root holds everything');
    // An absolute container never suffix-matches: that rule is for `~` only.
    assert.equal(path_contains('/worktrees/my-app/x', ABSOLUTE), false);
});

test('an absolute container does not hold a collapsed candidate', () => {
    assert.equal(path_contains(ABSOLUTE, COLLAPSED), false);
});

// ── same_dir: one agent seen through two feeds ─────────────────────────────

test('same_dir equates the two spellings of one directory and nothing deeper', () => {
    assert.equal(same_dir(COLLAPSED, ABSOLUTE), true);
    assert.equal(same_dir(ABSOLUTE, COLLAPSED), true);
    assert.equal(same_dir(COLLAPSED, `${ABSOLUTE}/ui`), false);
    assert.equal(same_dir(COLLAPSED, `${ABSOLUTE}-2`), false);
    assert.equal(same_dir(ABSOLUTE, ABSOLUTE), true);
    assert.equal(same_dir(COLLAPSED, COLLAPSED), true);
    assert.equal(same_dir(`${COLLAPSED}/`, ABSOLUTE), true);
    assert.equal(same_dir('~', '/Users/u'), false);
    assert.equal(same_dir('', ''), false);
});

// ── longest root ───────────────────────────────────────────────────────────

test('longest_containing_root picks the most specific tree', () => {
    const roots = ['~/wt', '~/wt/a', '~/wt/a/sub', '~/code'];
    assert.equal(longest_containing_root('/Users/u/wt/a/sub/x', roots), '~/wt/a/sub');
    assert.equal(longest_containing_root('/Users/u/wt/a/other', roots), '~/wt/a');
    assert.equal(longest_containing_root('/Users/u/wt/b', roots), '~/wt');
    assert.equal(longest_containing_root('/Users/u/elsewhere', roots), null);
});

test('longest_containing_root returns the CALLER string, first of equal length', () => {
    // So the result maps straight back to its row by identity.
    const roots = ['~/wt/a/', '~/wt/a'];
    assert.equal(longest_containing_root('/Users/u/wt/a', roots), '~/wt/a/');
    assert.equal(longest_containing_root('/x', []), null);
});
