/**
 * fence-lines.ts — THE reading of a code fence line in a message on the Laya review page. Two readers need it: the
 * renderer's tail repair (`agent-markdown.ts` `repair_tail`) and the paste split (`paste-blocks.ts`, where a tag inside
 * a fence is an example, not a paste). Pure, with no imports, so `paste-blocks.ts` stays free of marked and DOMPurify
 * (why the renderer loads lazily: owned by `agent-markdown.ts` — see the note there).
 */

/**
 * A fence line: 3+ backticks or tildes, then an info string. Any indent counts, not CommonMark's three spaces:
 * agents nest fences inside numbered list items far more often than they write 4-space indented code.
 * A backtick fence's info string may not contain a backtick (CommonMark 4.5), so "```a``` b" is inline code.
 */
const FENCE_LINE = /^[ \t]*(`{3,}|~{3,})(.*)$/;

export interface FenceLine {
    /** The marker run itself (```` ``` ````, `~~~~`). */
    fence: string;
    /** The info string, trimmed. A closer never has one. */
    info: string;
}

export function fence_line(line: string): FenceLine | null {
    const match = FENCE_LINE.exec(line);
    if (!match) return null;
    const info = match[2].trim();
    if (match[1][0] === '`' && info.includes('`')) return null;
    return { fence: match[1], info };
}

/** Whether `text`, read from its first line OUTSIDE any fence, ends inside one; plus the first fence line seen. */
export function scan_fences(text: string): { open_at_end: boolean; first: FenceLine | null } {
    let open: FenceLine | null = null;
    let first: FenceLine | null = null;
    for (const line of text.split('\n')) {
        const marker = fence_line(line);
        if (!marker) continue;
        first ??= marker;
        if (!open) open = marker;
        // A closer: the opener's character, at least its length, no info string.
        else if (marker.fence[0] === open.fence[0] && marker.fence.length >= open.fence.length && marker.info === '') open = null;
    }
    return { open_at_end: open !== null, first };
}
