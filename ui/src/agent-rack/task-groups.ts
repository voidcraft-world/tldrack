import { task_group_schema, type TaskGroup } from '@tldrack/schemas/task-groups.ts';
export type { TaskGroup };

export function parse_task_groups(value: unknown): TaskGroup[] | undefined {
    if (!Array.isArray(value)) return undefined;
    return value.flatMap(raw => {
        const parsed = task_group_schema.safeParse(raw);
        return parsed.success ? [parsed.data] : [];
    });
}

/** UUID-only join; a reused terminal name cannot inherit an old batch. */
export function groups_for_parent(groups: readonly TaskGroup[] | undefined, node_id: string | undefined): TaskGroup[] {
    const id = node_id?.replace(/^missing:/, '');
    return id ? (groups ?? []).filter(group => group.parent_id === id) : [];
}

type GroupWorker = TaskGroup['workers'][number];

/** Its own PR merged, or its branch landed: owned by `plugins/worktree/worktree/task_groups.py` § Delivered — see the note there. */
export function worker_delivered(worker: GroupWorker): boolean {
    return worker.pr_state === 'merged' || !!worker.landing;
}

/** A worker's word on its group bar: how it was delivered, else the stage it last reported. */
export function worker_stage_word(worker: GroupWorker): string {
    if (worker.pr_state === 'merged') return 'merged';
    if (worker.landing) return worker.landing.pr === null ? 'landed' : `landed in #${worker.landing.pr}`;
    return worker.stage;
}
