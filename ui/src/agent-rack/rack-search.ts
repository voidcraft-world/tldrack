import type { RackRow } from '../engine/terminal/multi-terminal-layout.ts';

/**
 * The search box's text as one question: trimmed, whitespace collapsed. THE normal form both halves
 * compare by: the page's box against `ChatSearchView.query`, and the host's `chat-search.ts` against
 * the last question it asked.
 */
export function chat_query(text: string): string {
    return text.trim().replace(/\s+/g, ' ');
}

/** The terms a query must ALL meet: whitespace-separated, case-folded. The bridge splits the same
 *  way (`tldrack/ledger/rack_search.py` `parse_query`). */
export function query_terms(query: string): string[] {
    return chat_query(query).toLocaleLowerCase().split(' ').filter(Boolean);
}

/**
 * `matches` with what gives them their identity: every ancestor row, and the cohort heading each
 * one sits under. A narrowed list reveals a match inside a folded band or family without changing
 * the saved fold, and a child never shows without the rows that say whose it is.
 */
function with_context(rows: readonly RackRow[], matches: readonly RackRow[]): RackRow[] {
    const keep = new Set(matches);
    const ancestors = new Set(matches.flatMap((row) => row.ancestors ?? []));
    for (const row of rows) if (row.node_id && ancestors.has(row.node_id)) keep.add(row);
    const headings = new Map(rows.filter((row) => row.kind === 'cohort').map((row) => [row.cohort, row]));
    let heading: RackRow | undefined;
    for (const row of rows) {
        if (row.kind === 'cohort') heading = row;
        if (keep.has(row) && heading) {
            keep.add(heading);
            const parent = heading.cohort_parent ? headings.get(heading.cohort_parent) : undefined;
            if (parent) keep.add(parent);
        }
    }
    return rows.filter((row) => keep.has(row));
}

/** Only the sessions the operator starred (`star-client.ts`), each with its context rows. */
export function starred_rack_rows(rows: readonly RackRow[]): RackRow[] {
    return with_context(rows, rows.filter((row) => row.kind === 'session' && row.starred === true));
}

/**
 * Search narrows the list to its matches, each with its context rows (`with_context`).
 *
 * A session row matches when its name and auto title meet every term, or when `chat` (the bridge's
 * answer for this query, `RackPayload.chat_search.matches`) names it: its conversation met the rest.
 */
export function search_rack_rows(rows: readonly RackRow[], query: string,
    chat: Readonly<Record<string, unknown>> = {}): RackRow[] {
    const terms = query_terms(query);
    if (!terms.length) return [...rows];
    const matches = rows.filter((row) => row.kind === 'session' && (
        (row.session !== undefined && Object.hasOwn(chat, row.session))
        || terms.every((term) => `${row.session ?? ''} ${row.auto_name ?? ''}`.toLocaleLowerCase().includes(term))));
    return with_context(rows, matches);
}

/** Most marks one snippet draws: a one-letter term must not turn a line into hundreds of nodes. */
const MAX_MARKS = 40;

/**
 * `text` cut into plain and matched runs, for every term of `query` (case-folded, overlaps merged).
 * Rendered as Vue text nodes and `<mark>`s, never as markup: a snippet is transcript text.
 */
export function highlight_terms(text: string, query: string): { text: string; match: boolean }[] {
    const lower = text.toLocaleLowerCase();
    const spans: [number, number][] = [];
    for (const term of query_terms(query)) {
        for (let at = lower.indexOf(term); at >= 0 && spans.length < MAX_MARKS * 4; at = lower.indexOf(term, at + term.length)) {
            spans.push([at, at + term.length]);
        }
    }
    spans.sort((a, b) => a[0] - b[0]);
    const merged: [number, number][] = [];
    for (const span of spans) {
        const last = merged[merged.length - 1];
        if (last && span[0] <= last[1]) last[1] = Math.max(last[1], span[1]);
        else if (merged.length < MAX_MARKS) merged.push([...span]);
    }
    const parts: { text: string; match: boolean }[] = [];
    let start = 0;
    for (const [from, to] of merged) {
        if (from > start) parts.push({ text: text.slice(start, from), match: false });
        parts.push({ text: text.slice(from, to), match: true });
        start = to;
    }
    if (start < text.length) parts.push({ text: text.slice(start), match: false });
    return parts;
}

/** Characters of context a row keeps ahead of the first match. */
const SNIPPET_LEAD = 24;

/**
 * A matched line as one row of text whose first match is near its START: whitespace collapsed, and
 * cut `SNIPPET_LEAD` characters ahead of the first term (at a word boundary, behind an `…`). The
 * bridge's snippet keeps 80 characters ahead of it, which on a narrow rack ellipsizes the match
 * itself off the row's end. The tooltip keeps the whole snippet.
 */
export function focus_snippet(text: string, query: string): string {
    const line = text.replace(/\s+/g, ' ').trim();
    const lower = line.toLocaleLowerCase();
    const first = Math.min(...query_terms(query).map((term) => lower.indexOf(term)).filter((at) => at >= 0));
    if (!Number.isFinite(first) || first <= SNIPPET_LEAD) return line;
    let cut = first - SNIPPET_LEAD;
    const space = line.indexOf(' ', cut);
    if (space >= 0 && space < first) cut = space + 1;
    return `…${line.slice(cut)}`;
}
