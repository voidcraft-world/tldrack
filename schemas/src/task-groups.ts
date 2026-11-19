import { z } from 'zod';

const commit = z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/);

/** Worktree task-group wire contract. Python owns the milestone arithmetic and the
 * two modes (`plugins/worktree/worktree/task_groups.py`); a group without them
 * is `fixed` / `batch`. Only an `open` group may be empty or hold more than 32. */
export const task_group_schema = z.object({
    id: z.string().uuid(),
    label: z.string().min(1).max(120),
    parent_id: z.string().uuid(),
    parent_session: z.string(),
    auto_pr: z.boolean(),
    sealed: z.boolean(),
    membership: z.enum(['fixed', 'open']).optional(),
    release: z.enum(['batch', 'each']).optional(),
    percent: z.number().int().min(0).max(100),
    total: z.number().int().min(0).max(64),
    ready: z.number().int().nonnegative(),
    opened: z.number().int().nonnegative(),
    blockers: z.number().int().nonnegative(),
    phase: z.enum(['working', 'blocked', 'ready', 'opening', 'complete']),
    workers: z.array(z.object({
        branch: z.string(), session: z.string(),
        stage: z.enum(['queued', 'research', 'implement', 'validate', 'ready', 'blocked', 'failed']),
        note: z.string(), error: z.string().nullable(),
        pr_status: z.enum(['pending', 'opening', 'open', 'error']),
        pr_state: z.enum(['open', 'merged', 'closed']).optional(),
        pr_url: z.string().regex(/^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+$/).nullable(),
        /** The head the PR carries as this plugin last published or indexed it. */
        pr_head: commit.optional(),
        /** release `each` only: the PR's check rollup, and the head that rollup describes. */
        ci: z.enum(['pending', 'passing', 'failing', 'none']).optional(),
        ci_head: commit.optional(),
        /** Its branch's work is on the base without its own PR merging: stamped once and final
         * (`plugins/worktree/worktree/task_groups.py` § Delivered owns why). A malformed stamp reads as absent. */
        landing: z.object({
            how: z.enum(['ancestor', 'pr', 'squash', 'commits']),
            pr: z.number().int().positive().nullable(),
            branch: z.string(),
        }).optional().catch(undefined),
    })).max(64),
}).refine(g => g.total === g.workers.length && g.ready <= g.total && g.opened <= g.total && g.blockers <= g.total)
    .refine(g => g.membership === 'open' || (g.workers.length >= 1 && g.workers.length <= 32));
export type TaskGroup = z.infer<typeof task_group_schema>;
