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
