// markdown-doc-render.ts — the browser-only half of the markdown pipeline:
// DOMPurify sanitization, mermaid rendering in isolation on the page, and the
// standalone prose and mind-map documents. Here it renders the diagram page
// (`agent-rack-diagram/AgentRackDiagram.vue`); the document builders are for a
// host application that shows markdown in a frame. Kept out of
// markdown-pipeline.ts so that module stays importable under `node --test`
// (DOMPurify and mermaid need a DOM; the `?raw` CSS import needs Vite).
import DOMPurify from 'dompurify';
import { Transformer } from 'markmap-lib';
import { fillTemplate } from 'markmap-render';
// The mind-map frame's two runtime scripts, served from our own build instead of the jsDelivr URLs
// `fillTemplate` defaults to — same versions, the installed packages. d3's `exports` map does not
// name its UMD file, so it is reached by its path under `node_modules`.
import d3_umd_url from '../../node_modules/d3/dist/d3.min.js?url';
import markmap_view_url from 'markmap-view/dist/browser/index.js?url';
import github_dark_css from 'highlight.js/styles/github-dark.css?raw';
import {
    MARKDOWN_PURIFY_CONFIG,
    build_prose_html_doc,
    escape_html,
    extract_first_heading,
    render_prose_markdown,
    splice_mermaid_svgs,
} from './markdown-pipeline.ts';

type Mermaid = typeof import('mermaid')['default'];

// The sole mermaid initializer.
//
// mermaid is loaded LAZILY: it is ~1.26 MB raw / 392 KB gz once its d3/dagre
// graph stack is counted, and a static import here would make it a static
// dependency of every chunk that imports this file. Only a document with a
// mermaid fence pays.
let mermaid_render_counter = 0;

/**
 * The memoized module promise — **assigned synchronously**, which is the whole
 * point of the `??=` shape.
 *
 * Do NOT "simplify" this to memoizing the resolved module
 * (`if (mod) return mod; mod = (await import(…)).default; mod.initialize(…)`):
 * that puts an `await` between the guard and the assignment, so two concurrent
 * callers both pass the guard and both run `initialize()`. The concurrency is
 * real: a caller that renders on every change starts a fresh async render each
 * time. Storing the promise makes exactly-once initialization structural.
 */
let mermaid_promise: Promise<Mermaid> | null = null;

function ensure_mermaid_initialized(): Promise<Mermaid> {
    mermaid_promise ??= (async () => {
        const mermaid = (await import('mermaid')).default;
        // 'strict' (not 'loose') is load-bearing, and the sanitize-then-splice order
        // is exactly why. The mermaid SVG is spliced in AFTER DOMPurify runs, so it
        // is never sanitized by us — mermaid's own escaping is the only thing
        // standing between a node label and a <foreignObject> in a same-origin
        // document (a `v-html` sink and a blob: iframe are both this origin's).
        // 'loose' disables that escaping, which turns any markdown from outside
        // into script execution.
        // `look`/`layout` pin mermaid 11's rendering: 12 defaulted to the 'neo' look and
        // ELK layout, which re-flows every existing diagram and pulls the ELK chunk.
        mermaid.initialize({
            startOnLoad: false,
            theme: 'dark',
            securityLevel: 'strict',
            look: 'classic',
            layout: 'dagre',
        });
        return mermaid;
    })();
    return mermaid_promise;
}

/**
 * Markdown → sanitized prose HTML fragment with mermaid SVGs spliced in.
 * Order matters: marked output (fences already diverted to placeholders) is
 * DOMPurify-sanitized FIRST, then each fence is rendered in isolation and the
 * SVGs spliced in — so sanitization never strips mermaid's internals, and raw
 * HTML in the markdown never reaches the document unsanitized.
 * A fence that fails to render becomes an inline error card; one bad diagram
 * never blanks the doc.
 */
export async function render_prose_fragment(markdown: string): Promise<string> {
    const { body_html, mermaid_sources } = render_prose_markdown(markdown);
    // The config: owned by `markdown-purify.ts` `MARKDOWN_PURIFY_CONFIG` — see the note there.
    const sanitized = DOMPurify.sanitize(body_html, MARKDOWN_PURIFY_CONFIG);
    if (mermaid_sources.length === 0) return sanitized;

    // Resolved once, up front, so a failed chunk fetch degrades to the same
    // inline error cards a bad diagram already produces — never a rejected
    // render that blanks the whole document.
    let mermaid: Mermaid | null = null;
    let load_error: string | null = null;
    try {
        mermaid = await ensure_mermaid_initialized();
    } catch (error) {
        load_error = error instanceof Error ? error.message : String(error);
    }

    const rendered = await Promise.all(
        mermaid_sources.map(async (source) => {
            if (!mermaid) {
                return `<pre class="vc-mermaid-error">Mermaid failed to load:\n${escape_html(load_error ?? 'unknown error')}</pre>`;
            }
            try {
                const { svg } = await mermaid.render(`vc-mermaid-${mermaid_render_counter++}`, source);
                return svg;
            } catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                return `<pre class="vc-mermaid-error">Mermaid render failed:\n${escape_html(message)}</pre>`;
            }
        })
    );
    return splice_mermaid_svgs(sanitized, rendered);
}

/** Full standalone GitHub-dark prose document (the blob-URL iframe's content). */
export async function build_prose_doc(markdown: string): Promise<string> {
    const fragment = await render_prose_fragment(markdown);
    return build_prose_html_doc(fragment, {
        title: extract_first_heading(markdown) ?? 'Markdown',
        css_texts: [github_dark_css],
        contain_long_document: true,
    });
}

/** A plain-text file shown as the literal text it is — wrapped, never markdown-parsed. */
const PLAIN_TEXT_CSS = `
    .vc-plain-text { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
`;

/**
 * A file's rendered body AND the whole document around it, for a host that shows
 * a file from disk in a frame and follows its changes.
 *
 * The fragment is what a host swaps into an already-loaded document; the full
 * document is what a fresh or rebuilt frame loads. Both come from one render, so
 * the two can never show different text. `prose` goes through the same
 * sanitize-then-splice pipeline as any markdown; anything else is escaped into a
 * `<pre>`, because markdown-parsing a `.txt` would mangle it.
 */
export async function build_live_file_docs(
    text: string,
    mode: 'prose' | 'plain',
    title: string,
): Promise<{ fragment: string; full: string }> {
    const fragment = mode === 'prose'
        ? await render_prose_fragment(text)
        : `<pre class="vc-plain-text">${escape_html(text)}</pre>`;
    const css_texts = mode === 'prose' ? [github_dark_css] : [PLAIN_TEXT_CSS];
    return { fragment, full: build_prose_html_doc(fragment, { title, css_texts }) };
}

const LIVE_FILE_CARD_CSS = `
    html, body { height: 100%; }
    .vc-md-doc { height: 100%; box-sizing: border-box; display: flex; align-items: center; justify-content: center; }
    .vc-live-card { display: flex; flex-direction: column; align-items: center; gap: 14px; text-align: center; max-width: 80%; }
    .vc-live-card__name { font-size: 28px; font-weight: 600; overflow-wrap: anywhere; }
    .vc-live-card__tag {
        font-family: 'SF Mono', 'Monaco', 'Cascadia Code', 'Roboto Mono', Consolas, monospace;
        font-size: 15px; letter-spacing: 0.14em; color: rgba(214, 180, 128, 0.95);
    }
    .vc-live-card__detail { font-size: 15px; color: rgba(230, 237, 243, 0.7); }
    .vc-live-card__action {
        margin-top: 6px; padding: 8px 22px; cursor: pointer;
        font-family: 'SF Mono', 'Monaco', 'Cascadia Code', 'Roboto Mono', Consolas, monospace;
        font-size: 14px; letter-spacing: 0.12em;
        background: rgba(13, 17, 23, 0.85); color: rgba(176, 255, 240, 0.95);
        border: 1px solid rgba(176, 255, 240, 0.45); border-radius: 2px;
    }
    .vc-live-card__action:hover { background: rgba(176, 255, 240, 0.12); border-color: rgba(176, 255, 240, 0.8); }
`;

/**
 * The card such a frame shows while it has no file body to show: the file's
 * name, one mono readout, at most one sentence, at most one button. Every string
 * is escaped here, because the file name is whatever the disk calls it. The
 * button carries `data-vc-action`, which the host wires after the frame loads.
 */
export function build_live_file_card_doc(
    file_name: string,
    card: { tag: string; detail: string | null },
    action: { id: string; label: string } | null,
): string {
    const detail = card.detail ? `<div class="vc-live-card__detail">${escape_html(card.detail)}</div>` : '';
    const button = action
        ? `<button class="vc-live-card__action" data-vc-action="${escape_html(action.id)}">${escape_html(action.label)}</button>`
        : '';
    const body = `<div class="vc-live-card">
<div class="vc-live-card__name">${escape_html(file_name)}</div>
<div class="vc-live-card__tag">${escape_html(card.tag)}</div>
${detail}
${button}
</div>`;
    return build_prose_html_doc(body, { title: file_name, css_texts: [LIVE_FILE_CARD_CSS] });
}

// Markmap's Transformer is stateless across transforms — one shared instance.
const markmap_transformer = new Transformer();
/** A markmap tree node (markmap-common's `IPureNode`, a transitive dependency — derived, not imported). */
type MarkmapNode = ReturnType<Transformer['transform']>['root'];
// markmap-lib ships markdown-it with `html: true`, and markmap-view writes every node with
// `.html()` into a frame that runs scripts, so raw HTML in a document would execute on sight for
// anyone who opened it. Off at the parser; the sanitize walk below is the second lock.
markmap_transformer.md.set({ html: false });

/**
 * DOMPurify every node's `content` in place, with the one markdown config. Plugin output
 * (highlight, KaTeX, checkbox) is HTML the parser builds, so `html: false` alone does not
 * cover it.
 *
 * Then one more step, for the template: `fillTemplate` inlines the tree as JSON inside a
 * `<script>`, and its escape catches only a lowercase `</script>`. A sanitized node can still
 * carry `</SCRIPT` or `<!--` inside an attribute value, so both become `&lt;`. That is the
 * same value once markmap-view parses it, and it can no longer end the script block.
 */
function sanitize_markmap_tree(node: MarkmapNode): void {
    const clean = DOMPurify.sanitize(node.content, MARKDOWN_PURIFY_CONFIG);
    node.content = clean.replace(/<(\/script|!--)/gi, '&lt;$1');
    for (const child of node.children ?? []) sanitize_markmap_tree(child);
}

/** Full standalone markmap mind-map document. Synchronous. */
// FOLLOW-UP: sandbox the mind-map frame too (it runs markmap's scripts, unsandboxed, same-origin).
export function build_mindmap_doc(markdown: string): string {
    const { root, features } = markmap_transformer.transform(markdown);
    sanitize_markmap_tree(root);
    const assets = markmap_transformer.getUsedAssets(features);
    // Absolute: the frame is a blob: document, where a root-relative src resolves to nothing.
    // `{ type: 'script', data: { src } }` is markmap-common's `buildJSItem`, spelled here because
    // markmap-common is only a transitive dependency.
    const base_js = [d3_umd_url, markmap_view_url].map((url) => ({
        type: 'script' as const,
        data: { src: new URL(url, location.href).href },
    }));
    return fillTemplate(root, assets, { baseJs: base_js });
}
