// markdown-pipeline.ts — the pure markdown→HTML core: marked with highlight.js,
// mermaid fences set aside for the browser half, and the standalone document
// wrapper.
//
// Node-testable by design: only `marked` and `highlight.js/lib/common` are
// imported — no DOM globals. Everything that needs a browser (DOMPurify,
// mermaid.render, blob URLs, Vite `?raw` CSS imports) lives in
// markdown-doc-render.ts.
import { Marked, type Tokens } from 'marked';
import hljs from 'highlight.js/lib/common';
import { escape_html } from '../utils/trusted-html.ts';
import { render_task_box } from './markdown-purify.ts';

// The sanitizer config and the task-box glyph: owned by `markdown-purify.ts` — see the note there.
export { MARKDOWN_PURIFY_CONFIG, render_task_box } from './markdown-purify.ts';

export interface ProseRenderResult {
    /** marked output with each ```mermaid fence replaced by a placeholder div. */
    body_html: string;
    /** Fence bodies, index-aligned with the `data-vc-mermaid="N"` placeholders. */
    mermaid_sources: string[];
}

// Single-attribute placeholder so DOMPurify's serialize round-trip can't
// reorder attributes out from under the splice regex.
const MERMAID_PLACEHOLDER_REGEX = /<div data-vc-mermaid="(\d+)"><\/div>/g;

// Re-exported, not re-implemented: `utils/trusted-html.ts` is the owner. Kept
// exported here because `markdown-doc-render.ts` takes it from this module beside
// the rest of the pipeline.
export { escape_html };

/**
 * Render markdown to GitHub-style HTML via marked (GFM), with two twists at the
 * code-fence level (marked's lexer handles fence nesting/indentation correctly,
 * so this never touches regex on the raw markdown):
 *   - ```mermaid fences are diverted to placeholder divs; their sources are
 *     returned for isolated host-side mermaid.render() + splice_mermaid_svgs().
 *   - other fences get highlight.js classes baked in at build time — a blob-URL
 *     iframe can't be post-mount highlighted from outside.
 */
export function render_prose_markdown(markdown: string): ProseRenderResult {
    const mermaid_sources: string[] = [];

    // Local instance: the custom renderer must never leak into other marked
    // consumers (marked's default export is a shared singleton).
    const marked_instance = new Marked({
        gfm: true,
        renderer: {
            checkbox: render_task_box,
            code({ text, lang }: Tokens.Code): string {
                const language = (lang ?? '').trim().split(/\s+/)[0];
                if (language === 'mermaid') {
                    const index = mermaid_sources.length;
                    mermaid_sources.push(text);
                    return `<div data-vc-mermaid="${index}"></div>\n`;
                }
                if (language && hljs.getLanguage(language)) {
                    const highlighted = hljs.highlight(text, { language }).value;
                    return `<pre><code class="hljs language-${escape_html(language)}">${highlighted}</code></pre>\n`;
                }
                return `<pre><code class="hljs">${escape_html(text)}</code></pre>\n`;
            },
        },
    });

    const body_html = marked_instance.parse(markdown ?? '', { async: false }) as string;
    return { body_html, mermaid_sources };
}

/**
 * Replace each mermaid placeholder with its rendered content (an SVG string, or
 * an error card for fences that failed to render). Indexes without a rendered
 * entry leave the placeholder intact. Safe to run on DOMPurify-sanitized HTML —
 * that's the point: the SVGs are spliced in AFTER sanitization so DOMPurify
 * never strips mermaid's <foreignObject>/<style> internals.
 */
export function splice_mermaid_svgs(body_html: string, rendered: string[]): string {
    return body_html.replace(MERMAID_PLACEHOLDER_REGEX, (placeholder, index_str: string) => {
        const content = rendered[Number(index_str)];
        if (content === undefined) return placeholder;
        return `<div class="vc-mermaid">${content}</div>`;
    });
}

/**
 * Wrap raw mermaid source in a fenced block so it renders as a diagram inside
 * a markdown document. The single definition of that fence.
 */
export function wrap_mermaid_fence(source: string): string {
    const body = (source ?? '').trim();
    return body ? '```mermaid\n' + body + '\n```' : '';
}

// The display-name heuristic: owned by `first-heading.ts` — see the note there.
export { extract_first_heading } from './first-heading.ts';

/**
 * Wrap a rendered (and already-sanitized) prose fragment in a full standalone
 * HTML document for the blob-URL iframe: GitHub-dark canvas + prose CSS, links
 * open in a new tab via <base target="_blank">, caller CSS (e.g. the
 * highlight.js theme) appended last.
 */
export function build_prose_html_doc(body_html: string, opts: { title: string; css_texts?: string[]; contain_long_document?: boolean }): string {
    const extra_css = (opts.css_texts ?? []).map((css) => `<style>${css}</style>`).join('\n');
    return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<base target="_blank" />
<title>${escape_html(opts.title)}</title>
<style>${GITHUB_DARK_PROSE_CSS}</style>
${extra_css}
</head>
<body>
<article class="vc-md-doc${opts.contain_long_document && body_html.length >= 24000 ? ' vc-md-doc--long' : ''}">
${body_html}
</article>
</body>
</html>`;
}

// GitHub-dark document styling (the familiar #0d1117 canvas), matched to how
// github.com renders markdown: bordered h1/h2, striped-border tables, muted
// blockquotes, task-list checkboxes, horizontally-scrolling code blocks.
const GITHUB_DARK_PROSE_CSS = `
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
    background: #0d1117;
    color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif;
    font-size: 16px;
    line-height: 1.6;
}
.vc-md-doc { padding: 28px 34px; word-wrap: break-word; }
.vc-md-doc > *:first-child { margin-top: 0; }
/* Keep all text in the DOM for selection and browser find. Only offscreen painting/layout is skipped.
   Placeholders are BLOCK-axis only (width still comes from the column) and sized per kind: the
   scrollbar reads them until a block first renders, after which "auto" keeps its real height.
   No tables: a max-content table has no column width to fall back on and collapses while skipped. */
.vc-md-doc--long > :is(p, pre, blockquote, ul, ol, .vc-mermaid) { content-visibility: auto; }
.vc-md-doc--long > p { contain-intrinsic-block-size: auto 4.5em; }
.vc-md-doc--long > :is(ul, ol) { contain-intrinsic-block-size: auto 6em; }
.vc-md-doc--long > blockquote { contain-intrinsic-block-size: auto 4em; }
.vc-md-doc--long > pre { contain-intrinsic-block-size: auto 10em; }
.vc-md-doc--long > .vc-mermaid { contain-intrinsic-block-size: auto 300px; }
h1, h2, h3, h4, h5, h6 {
    margin: 1.3em 0 0.5em;
    font-weight: 600;
    line-height: 1.25;
}
h1 { font-size: 2em; border-bottom: 1px solid #30363d; padding-bottom: 0.3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid #30363d; padding-bottom: 0.3em; }
h3 { font-size: 1.25em; }
h4 { font-size: 1em; }
p { margin: 0.6em 0 1em; }
a { color: #4493f8; text-decoration: none; }
a:hover { text-decoration: underline; }
ul, ol { padding-left: 2em; margin: 0.5em 0 1em; }
li { margin: 0.25em 0; }
li > p { margin: 0.3em 0; }
li.task-list-item { list-style: none; margin-left: -1.4em; }
li .vc-task-box { margin-right: 0.35em; }
blockquote {
    margin: 0.8em 0;
    padding: 0 1em;
    color: #848d97;
    border-left: 0.25em solid #30363d;
}
code {
    font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
    font-size: 85%;
    background: rgba(110, 118, 129, 0.4);
    padding: 0.2em 0.4em;
    border-radius: 6px;
}
pre {
    background: #161b22;
    border-radius: 6px;
    padding: 16px;
    overflow-x: auto;
    margin: 0.8em 0 1em;
    line-height: 1.45;
}
pre code {
    background: transparent;
    padding: 0;
    border-radius: 0;
    font-size: 85%;
    white-space: pre;
}
table {
    border-collapse: collapse;
    border-spacing: 0;
    margin: 0.8em 0 1em;
    display: block;
    max-width: 100%;
    overflow-x: auto;
}
th, td { border: 1px solid #30363d; padding: 6px 13px; }
th { font-weight: 600; }
tr { background: #0d1117; }
tr:nth-child(2n) { background: #161b22; }
img { max-width: 100%; }
hr { border: none; border-top: 1px solid #30363d; height: 0; margin: 1.5em 0; }
.vc-mermaid {
    display: flex;
    justify-content: center;
    margin: 0.8em 0 1em;
    padding: 12px;
    background: #161b22;
    border-radius: 6px;
    overflow-x: auto;
}
.vc-mermaid svg { max-width: 100%; height: auto; }
.vc-mermaid-error {
    color: #ff7b72;
    background: #161b22;
    border: 1px solid rgba(255, 123, 114, 0.4);
    border-radius: 6px;
    padding: 12px 16px;
    white-space: pre-wrap;
}
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #484f58; }
`;
