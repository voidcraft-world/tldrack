// rack-diagram-sections.ts — splits rack-architecture.md (the diagram's owner) into the tabs the
// ⌗ HOW window shows. Pure, so it runs under `node --test`; the mermaid render is the page's job.

export interface RackDiagramSection {
    /** URL-hash key, from the heading: "Status pipeline" → "status-pipeline". */
    id: string;
    title: string;
    /** The plain sentence(s) between the heading and the fence. */
    lead: string;
    /** The fence body, without the ```mermaid markers. Empty when a section has none. */
    mermaid: string;
}

const FENCE_RE = /```mermaid\n([\s\S]*?)```/;

export function section_id(title: string): string {
    return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

/** Every `## ` section in document order. Text above the first one (the `# ` title, the
 *  owner comment) is not a tab. */
export function split_rack_diagram(markdown: string): RackDiagramSection[] {
    const text = markdown.replace(/<!--[\s\S]*?-->/g, '');
    return text
        .split(/^## /m)
        .slice(1)
        .map((chunk) => {
            const newline = chunk.indexOf('\n');
            const title = (newline === -1 ? chunk : chunk.slice(0, newline)).trim();
            const body = newline === -1 ? '' : chunk.slice(newline + 1);
            const fence = FENCE_RE.exec(body);
            const lead = (fence ? body.slice(0, fence.index) : body).replace(/\s+/g, ' ').trim();
            return { id: section_id(title), title, lead, mermaid: fence ? fence[1].trim() : '' };
        })
        .filter((section) => section.title.length > 0);
}
