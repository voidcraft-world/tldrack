/**
 * Directory containment across the ONE seam where paths arrive in two spellings.
 *
 * The bridge's `/snapshot` ships a tmux pane's cwd ABSOLUTE
 * (`#{pane_current_path}` → `/Users/u/worktrees/my-app/x/ui`),
 * while every path a PLUGIN GET returns is `~`-collapsed
 * (`~/worktrees/my-app/x`): what a GET body returns is the
 * disclosure boundary, and a page has no `$HOME` to expand a tilde with.
 * So "is this session inside that worktree?" cannot be a prefix test. It is a
 * SUFFIX test: strip the `~`, and accept when the absolute path contains the
 * remainder at a segment boundary. `~/a/b` therefore contains
 * `/Users/u/a/b/c` and `/home/u/a/b`, and never `/Users/u/a/bc` — the sibling
 * prefix is the false positive `attach_only_sessions` exists to avoid.
 *
 * Two callers, one rule: `rack-view.ts::attach_only_sessions` (the `attach:`
 * invariant for every non-main worktree) and `rack-view.ts::island_for_session`
 * (which worktree an agent stands in). Absolute containers use the plain
 * segment-boundary prefix test; `~` on its own is the home directory itself
 * and contains nothing here — an agent "inside your home" is not on any island.
 *
 * Deliberately import-free: bare `node --test` loads it.
 */

const HOME = '~';
const SEP = '/';

/** Strip trailing separators. The root `/` and `~` stay themselves. */
export function normalize_dir(path: string): string {
    let end = path.length;
    while (end > 1 && path.charCodeAt(end - 1) === 47 /* '/' */) end -= 1;
    return path.slice(0, end);
}

function is_collapsed(path: string): boolean {
    return path === HOME || path.startsWith(HOME + SEP);
}

/** `candidate` equals `root` or sits below it — `/a/b` never contains `/a/bc`. */
function prefix_contains(root: string, candidate: string): boolean {
    if (candidate === root) return true;
    return candidate.startsWith(root === SEP ? SEP : root + SEP);
}

/**
 * Does `container` hold `candidate`?
 *
 * - `~`-collapsed container: segment-boundary SUFFIX match of the remainder
 *   after the `~` against an absolute candidate; a collapsed candidate is
 *   compared by prefix (both sides speak the same dialect there).
 * - Absolute container: segment-boundary prefix match.
 * - `~` alone, or an empty container: never.
 */
export function path_contains(container: string, candidate: string): boolean {
    const root = normalize_dir(container);
    const target = normalize_dir(candidate);
    if (root === '' || root === HOME || target === '') return false;
    if (!is_collapsed(root)) return prefix_contains(root, target);
    if (is_collapsed(target)) return prefix_contains(root, target);
    // `~/x/y` → `/x/y`: it starts with a separator, so every occurrence already
    // begins on a segment boundary; only the END needs checking. Walk every
    // occurrence — `/a/b` inside `/x/a/bc/a/b` fails at the first and passes at
    // the second.
    const remainder = root.slice(HOME.length);
    let from = 0;
    for (;;) {
        const at = target.indexOf(remainder, from);
        if (at < 0) return false;
        const end = at + remainder.length;
        if (end === target.length || target.charCodeAt(end) === 47) return true;
        from = at + 1;
    }
}

/**
 * The same directory spelled two ways? `~/x` and `/Users/u/x` are; `~/x` and
 * `/Users/u/x/y` are not. This is the "one agent, two feeds" test: a
 * Claude process whose (collapsed) cwd is a tmux session's (absolute) cwd is
 * that session's agent, not a second one.
 */
export function same_dir(a: string, b: string): boolean {
    const left = normalize_dir(a);
    const right = normalize_dir(b);
    if (left === right) return left !== '' && left !== HOME;
    if (is_collapsed(left) === is_collapsed(right)) return false;
    const collapsed = is_collapsed(left) ? left : right;
    const absolute = is_collapsed(left) ? right : left;
    if (collapsed === HOME) return false;
    const remainder = collapsed.slice(HOME.length);
    return absolute.endsWith(remainder);
}

/**
 * The most specific root holding `candidate`, or null. Nested worktrees make
 * this necessary: a session in `~/wt/a/sub` is on `~/wt/a/sub`, not `~/wt/a`.
 * Ties (the same root listed twice) keep the first — the result is the CALLER's
 * string, so it can be mapped straight back to its row.
 */
export function longest_containing_root(
    candidate: string,
    roots: readonly string[],
): string | null {
    let best: string | null = null;
    let best_length = -1;
    for (const root of roots) {
        if (!path_contains(root, candidate)) continue;
        const length = normalize_dir(root).length;
        if (length > best_length) {
            best = root;
            best_length = length;
        }
    }
    return best;
}
