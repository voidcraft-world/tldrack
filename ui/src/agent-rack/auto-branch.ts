/**
 * The branch a 🏝 launch cuts when the operator names none — PURE.
 *
 * "What should I call this?" is a default, not a question (the repository's
 * rule: decide the default, ask only what is the operator's). The name is
 * derived from the first task when there is one, else from the account and the
 * clock. The rack's 🏝 sends no task, so it is always the clock there
 * (`AgentRackComponent.vue::launch_island`).
 *
 * What it may produce is bounded by the worktree plugin, which re-validates
 * every name (`plugins/worktree/worktree/names.py`): `BRANCH_RE`'s
 * alphabet, and `SLUG_MAX` — a longer branch still works, but its `wt-<slug>`
 * session name would be cut, and the rack row would not say the whole thing.
 */

/** `names.py::SLUG_MAX` — see the header. */
export const AUTO_BRANCH_MAX = 32;

/** Content words carried from the task, at most. Past four a branch is a sentence. */
const AUTO_BRANCH_WORDS = 4;

/** Words that carry no topic. A task is usually typed as a request — "can you
 *  fix the nav sidebar" — and the branch should be `fix-nav-sidebar`. */
const FILLER = new Set([
    'a', 'about', 'also', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'can', 'could', 'do', 'for',
    'from', 'hey', 'i', 'in', 'into', 'is', 'it', 'its', 'just', 'let', 'lets', 'me', 'my', 'now',
    'of', 'on', 'or', 'our', 'please', 'so', 'some', 'that', 'the', 'then', 'this', 'to', 'us',
    'we', 'with', 'would', 'you', 'your',
]);

function words_of(text: string): string[] {
    return text
        .toLowerCase()
        .normalize('NFKD')
        .replace(/[̀-ͯ]/g, '') // é → e, before the split drops it
        .split(/[^a-z0-9]+/)
        .filter((word) => word !== '');
}

/** Words joined with `-`, stopping at the last whole word that fits. */
function join_within(words: readonly string[], max: number): string {
    let out = '';
    for (const word of words) {
        const next = out ? `${out}-${word}` : word;
        if (next.length > max) break;
        out = next;
    }
    // One word longer than the cap on its own is still a topic: cut it.
    return out || (words[0] ?? '').slice(0, max);
}

/** The task's topic as a branch, or '' when it has none (empty, or all filler). */
export function branch_from_task(task: string): string {
    const words = words_of(task);
    const content = words.filter((word) => !FILLER.has(word));
    return join_within(content.slice(0, AUTO_BRANCH_WORDS), AUTO_BRANCH_MAX);
}

/** `<who>-MMDD-HHMM` — a task-less launch still gets a name the operator can place
 *  ("the A one from 2:32"), in local time because that is the clock they read. */
export function branch_from_clock(who: string, now: Date): string {
    const pad = (value: number): string => String(value).padStart(2, '0');
    const stamp = `${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
    const prefix = join_within(words_of(who), AUTO_BRANCH_MAX - stamp.length - 1) || 'wt';
    return `${prefix}-${stamp}`;
}

/**
 * `base`, or `base-2`, `base-3`… — the first that no known branch holds.
 *
 * Only the branches this client can see (the plugin's managed worktrees). One
 * it cannot — an old local branch — still comes back as the plugin's 409, which
 * the rack shows (`worktree-client.ts::create_notice`), with the form's answer
 * one edit away.
 */
export function unique_branch(base: string, taken: ReadonlySet<string>): string {
    if (!taken.has(base)) return base;
    for (let n = 2; ; n += 1) {
        const suffix = `-${n}`;
        const candidate = `${base.slice(0, AUTO_BRANCH_MAX - suffix.length).replace(/-+$/, '')}${suffix}`;
        if (!taken.has(candidate)) return candidate;
    }
}

/** The name a 🏝 launch will use when the branch field is left blank. */
export function auto_branch(task: string, who: string, now: Date, taken: ReadonlySet<string>): string {
    return unique_branch(branch_from_task(task) || branch_from_clock(who, now), taken);
}
