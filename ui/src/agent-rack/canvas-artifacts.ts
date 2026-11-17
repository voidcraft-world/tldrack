import { z } from 'zod';

const artifact_schema = z.object({
    id: z.string().regex(/^[a-f0-9]{24}$/),
    revision: z.string().regex(/^[a-f0-9]{64}$/),
    name: z.string().min(1).max(512),
    path: z.string().max(2048),
    bytes: z.number().int().min(0).max(8 * 1024 * 1024),
    error: z.string().max(1024).optional(),
});
const shelf_schema = z.object({
    key: z.string().regex(/^[0-9]{1,6}-[0-9]{1,12}$/).nullable(),
    artifacts: z.array(artifact_schema).max(32),
    error: z.string().max(1024).nullable(),
}).refine((shelf) => shelf.key !== null || shelf.artifacts.length === 0);

export type CanvasArtifact = z.infer<typeof artifact_schema>;
export type CanvasArtifacts = z.infer<typeof shelf_schema>;

/** Called only after the canvas frame's source, origin and focused session have matched. */
export function parse_canvas_artifacts(raw: unknown): CanvasArtifacts | null {
    const parsed = shelf_schema.safeParse(raw);
    return parsed.success ? parsed.data : null;
}

export function artifact_viewer_url(origin: string, key: string, artifact: CanvasArtifact): string {
    const url = new URL('/plugin/agent-canvas/artifact', origin);
    url.search = new URLSearchParams({ key, id: artifact.id, revision: artifact.revision }).toString();
    return url.href;
}
