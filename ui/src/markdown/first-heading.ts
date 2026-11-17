/**
 * First non-empty line stripped of leading '#' markers: the display name of a
 * markdown document.
 *
 * Its own module, with no imports, because a caller may need a title before it
 * renders anything: reached through `markdown-pipeline.ts`, the title would put
 * marked and highlight.js on that caller's static graph. `markdown-pipeline.ts`
 * re-exports it.
 */
export function extract_first_heading(markdown: string): string | null {
    const first_line = (markdown ?? '')
        .split('\n')
        .map((l) => l.replace(/^#+\s*/, '').trim())
        .find((l) => l.length > 0);
    return first_line ?? null;
}
