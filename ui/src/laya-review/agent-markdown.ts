/**
 * agent-markdown.ts — THE renderer for an agent's message (Claude, Codex) and the operator's own on the Laya review
 * page. `AgentMessage.vue` is its one consumer, and it reaches this module only through `import()`: a host
 * application may import `LayaReviewDeck` statically, and a static edge here would put marked and DOMPurify in that
 * host's entry chunk.
 *
 * Not `markdown-doc-render.ts`'s `render_prose_fragment`: that one carries markmap (about 270 KB gz) and
 * highlight.js, and joins single newlines. An agent's message is read the way the terminal showed it, so:
 * - **`breaks: true`.** A single newline is a line break, as it was in the terminal.
 * - **Raw HTML is TEXT.** The terminal prints `<details>` or "edit the <AgentMessage> card" literally; as markup the
 *   word would vanish into a stripped tag. So marked's `html` tokens are escaped before the sanitizer sees anything.
 * - **An image is a link** to it, by its alt text: no remote pixel loads because a message was read.
 * - **No highlight.js.** Code is a plain block; the colours carry nothing a grader needs.
 *
 * The pure half (`repair_tail`, `tail_at_block`, `agent_message_source`, `render_agent_markdown_unsafe`) runs under
 * `node --test`. `render_agent_markdown` needs a DOM: its sanitizer is created on first use, never at import.
 *
 * The wire still serves text. This page is what renders it, sanitized.
 */
import DOMPurify, { type Config as PurifyConfig, type DOMPurify as Purifier, type UponSanitizeAttributeHookEvent } from 'dompurify';
import { Marked, type Tokens } from 'marked';
import { render_task_box } from '../markdown/markdown-purify.ts';
import { escape_html, prose_html } from '../utils/trusted-html.ts';

import { scan_fences } from './fence-lines.ts';

// ── Fences ───────────────────────────────────────────────────────────────────

// What a fence line is: owned by `fence-lines.ts` — see the note there.

/**
 * A tail that begins INSIDE a code fence gets that fence reopened at its top, so its head renders as the code it is.
 * Unrepaired, the fence's closing line opens a new block and the whole rest of the message renders as code.
 *
 * The tell is an unbalanced text whose first fence line is BARE: a closer never carries an info string, so a first
 * line like "```ts" is an opener, and the open fence is the one at the end (marked closes that at end of text, which
 * is right). The repair is applied only when it balances the text. One shape stays ambiguous: a complete message
 * whose first fence is bare and whose last is left open reads as a tail. Claude closes its fences; tails are common.
 */
export function repair_tail(text: string): string {
    const { open_at_end, first } = scan_fences(text);
    if (!open_at_end || !first || first.info !== '') return text;
    const repaired = `${first.fence}\n${text}`;
    return scan_fences(repaired).open_at_end ? text : repaired;
}

// ── Tails ────────────────────────────────────────────────────────────────────

/** A cut keeps at least this share of the budget before it settles for a weaker boundary. */
const TAIL_KEEP_SHARE = 0.5;

/** The first match of `pattern` in `text` starting in [from, to), as the index just past it. */
function first_boundary(text: string, pattern: RegExp, from: number, to: number, accept: (end: number) => boolean = () => true): number | null {
    const sticky = new RegExp(pattern.source, 'g');
    sticky.lastIndex = from;
    for (let match = sticky.exec(text); match && match.index < to; match = sticky.exec(text)) {
        const end = match.index + match[0].length;
        if (accept(end)) return end;
        if (match[0].length === 0) sticky.lastIndex++;
    }
    return null;
}

/**
 * The end of `text`, at most `max_chars` long, cut where a reader would: at a paragraph break, else a line break,
 * else between words — never mid-word, and never so that a fence marker is split (a cut between words never leaves
 * a line starting with ``` or ~~~ that was not one). Paragraph and line cuts must keep at least half the budget, or
 * the tail would shrink to a sliver because the only blank line sat near the end. One token longer than the whole
 * budget has no word boundary left to keep, and is cut hard.
 */
export function tail_at_block(text: string, max_chars: number): string {
    const source = text ?? '';
    if (source.length <= max_chars) return source;
    const start = source.length - Math.max(0, Math.floor(max_chars));
    const keep_until = start + Math.floor(max_chars * (1 - TAIL_KEEP_SHARE));
    const cut =
        first_boundary(source, /\n[ \t]*\n/, Math.max(0, start - 1), keep_until) ??
        first_boundary(source, /\n/, Math.max(0, start - 1), keep_until) ??
        first_boundary(source, /\s+/, Math.max(0, start - 1), source.length, (end) => !/^(?:`{3}|~{3})/.test(source.slice(end, end + 3))) ??
        start;
    return source.slice(cut).replace(/^(?:[ \t]*\n)+/, '');
}

/**
 * How many characters a tail keeps per line it shows. Generous on purpose: the box clips to `tail_lines` anyway, so
 * this only bounds the render work, and a prose line in a wide column runs about 110 characters before paragraph
 * gaps and the half-budget rule in `tail_at_block` take their share.
 */
export const TAIL_CHARS_PER_LINE = 240;

/**
 * What a message renders from. Full: the whole text, fence-repaired (a row's text can itself be a tail of the
 * message). Tail: its last blocks, enough for `tail_lines` lines, fence-repaired; `cut` says text was dropped.
 */
export function agent_message_source(text: string, tail_lines: number | null): { source: string; cut: boolean } {
    const whole = (text ?? '').trimEnd();
    if (tail_lines === null) return { source: repair_tail(whole), cut: false };
    const tail = tail_at_block(whole, Math.max(1, tail_lines) * TAIL_CHARS_PER_LINE);
    return { source: repair_tail(tail), cut: tail !== whole };
}

// ── Markdown → HTML (unsanitized) ────────────────────────────────────────────

/** Its own instance, so these renderer overrides never reach another `marked` consumer. */
const agent_marked = new Marked({
    gfm: true,
    breaks: true,
    renderer: {
        checkbox: render_task_box,
        html({ text, block }: Tokens.HTML | Tokens.Tag): string {
            const literal = escape_html(text);
            return block ? `<p>${literal.trim().replace(/\n/g, '<br>\n')}</p>\n` : literal;
        },
        image({ href, text }: Tokens.Image): string {
            return `<a href="${escape_html(href)}">${escape_html(text || href)}</a>`;
        },
    },
});

/** Markdown to HTML with this page's rules. NOT safe for a sink on its own: `render_agent_markdown` sanitizes it. */
export function render_agent_markdown_unsafe(text: string): string {
    return agent_marked.parse(text ?? '', { async: false }) as string;
}

// ── The sanitizer (browser only) ─────────────────────────────────────────────

function frozen<T>(list: T[]): T[] {
    return Object.freeze(list) as T[];
}

/**
 * An ALLOWLIST, not `MARKDOWN_PURIFY_CONFIG`'s forbid list: these messages are text an agent wrote, often quoting a
 * web page, a log or a diff verbatim, so anything marked does not emit for plain markdown has no business here.
 * - No img, svg, math, media, iframe or form controls: no remote pixel, no fake sign-in form, no foreign markup.
 * - No `style`, `id`, `name` or `data-*`: nothing paints over the page or clobbers a `document.<name>` global.
 * - Links are http(s) or mailto only; `javascript:`, `data:` and relative hrefs lose the attribute.
 * - `class`, `align` and `start` pass by name here and are narrowed to marked's own values by
 *   `limit_attribute_values`, so a hand-written `class` cannot borrow a page style.
 */
export const AGENT_PURIFY_CONFIG: PurifyConfig = Object.freeze({
    ALLOWED_TAGS: frozen([
        'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'strong', 'em', 'del', 's',
        'code', 'pre', 'blockquote', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'span',
    ]),
    ALLOWED_ATTR: frozen(['href', 'title', 'class', 'align', 'start']),
    ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
    // DOMPurify tests EVERY attribute value against that regexp unless the name is URI-safe (`class` and `title`
    // already are), so without this `align="right"` and `start="3"` read as bad links and are dropped.
    ADD_URI_SAFE_ATTR: frozen(['align', 'start']),
    ALLOW_DATA_ATTR: false,
    ALLOW_UNKNOWN_PROTOCOLS: false,
});

/**
 * `uponSanitizeAttribute`: `class` only as the task box or a code block's language, `align` only on a table cell,
 * `start` only on a numbered list (marked emits `<ol start="3">` for a list resumed after a code block, and without
 * it the numbering restarts at 1). It only ever narrows: DOMPurify still applies `ALLOWED_ATTR` after it.
 *
 * Self-contained, with no closure over this module, so `markdown-sanitize.test.ts` runs this exact function in
 * headless Chromium.
 */
export function limit_attribute_values(node: Element, event: UponSanitizeAttributeHookEvent): void {
    const tag = node.nodeName;
    const value = event.attrValue;
    if (event.attrName === 'class') {
        event.keepAttr = (tag === 'SPAN' && value === 'vc-task-box') || (tag === 'CODE' && /^language-[\w+#.-]{1,40}$/.test(value));
    } else if (event.attrName === 'align') {
        event.keepAttr = (tag === 'TH' || tag === 'TD') && /^(?:left|center|right)$/.test(value);
    } else if (event.attrName === 'start') {
        event.keepAttr = tag === 'OL' && /^\d{1,6}$/.test(value);
    }
}

/**
 * `afterSanitizeAttributes`: every link opens in a new tab, without a handle on this page. DOMPurify drops `target`
 * by default, and a link followed in place navigates the review page (or the host page it is mounted in) away from the
 * grading. Self-contained for the same reason as `limit_attribute_values`.
 */
export function force_link_target(node: Element): void {
    if (node.nodeName !== 'A' || !node.hasAttribute('href')) return;
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
}

/**
 * A PRIVATE instance, never DOMPurify's shared default: a hook added there would also run inside
 * `markdown-doc-render.ts`'s sanitizing (a note's links would start opening new tabs, its classes would be
 * narrowed). Created on first use, because there is no `window` under `node --test`.
 */
let purifier: Purifier | null = null;

function agent_purifier(): Purifier {
    if (purifier) return purifier;
    const instance = DOMPurify(window);
    instance.addHook('uponSanitizeAttribute', limit_attribute_values);
    instance.addHook('afterSanitizeAttributes', force_link_target);
    purifier = instance;
    return instance;
}

/** An agent's message as sanitized HTML for `v-html`, through the Trusted Types policy (`utils/trusted-html.ts`). */
export function render_agent_markdown(text: string): string {
    return prose_html(agent_purifier().sanitize(render_agent_markdown_unsafe(text), AGENT_PURIFY_CONFIG));
}
