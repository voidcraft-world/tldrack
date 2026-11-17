import { z } from 'zod';

const session_id = z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);

/** Bridge spawn identity. Session names remain transport addresses and can change. */
export const session_lineage_schema = z.object({
    id: session_id,
    parent_id: session_id.optional(),
    parent_name: z.string().regex(/^[A-Za-z0-9_-]{1,200}$/).optional(),
});
export type SessionLineage = z.infer<typeof session_lineage_schema>;
