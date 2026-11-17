import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { after, before, describe, test } from 'node:test';

// Markdown here is written by agents, so what it can carry into a `v-html`, a prose frame and the
// mind map is an attack surface. The config's why: owned by `src/markdown/markdown-purify.ts`
// (`MARKDOWN_PURIFY_CONFIG`).
//
// DOMPurify needs a real DOM and there is none under `node --test`, so the behaviour half runs the
// app's own DOMPurify build in headless Chromium (Playwright, already a dev dependency). With no
// browser it SKIPS, loudly; the source pins below hold either way.

import { MARKDOWN_PURIFY_CONFIG } from '../../src/markdown/markdown-pipeline.ts';
import { AGENT_PURIFY_CONFIG, force_link_target, limit_attribute_values, render_agent_markdown_unsafe } from '../../src/laya-review/agent-markdown.ts';

const src = (relative: string): string => readFileSync(new URL(`../../src/${relative}`, import.meta.url), 'utf8');

/** `haystack` contains `needle` — an anchor that must be FOUND, so a moved line fails here. */
function has(haystack: string, needle: string, what: string): void {
    assert.notEqual(haystack.indexOf(needle), -1, `${what}: \`${needle}\` not found`);
}

describe('the one config', () => {
    test('forbids form controls, style, dialog, popover and invoker attributes, and names', () => {
        const config = MARKDOWN_PURIFY_CONFIG as { FORBID_TAGS: string[]; FORBID_ATTR: string[]; SANITIZE_NAMED_PROPS: boolean };
        for (const tag of ['form', 'input', 'button', 'select', 'textarea', 'style', 'dialog']) {
            assert.ok(config.FORBID_TAGS.includes(tag), tag);
        }
        for (const attr of ['style', 'popover', 'popovertarget', 'popovertargetaction', 'command', 'commandfor', 'action', 'formaction']) {
            assert.ok(config.FORBID_ATTR.includes(attr), attr);
        }
        assert.equal(config.SANITIZE_NAMED_PROPS, true);
        assert.ok(Object.isFrozen(MARKDOWN_PURIFY_CONFIG), 'a caller must not be able to loosen it');
    });

    test('the prose fragment and every mind-map node sanitize with it', () => {
        const render = src('markdown/markdown-doc-render.ts');
        has(render, 'DOMPurify.sanitize(body_html, MARKDOWN_PURIFY_CONFIG)', 'render_prose_fragment');
        has(render, 'DOMPurify.sanitize(node.content, MARKDOWN_PURIFY_CONFIG)', 'sanitize_markmap_tree');
        assert.equal(render.match(/DOMPurify\.sanitize\(/g)?.length, 2, 'a third sanitize call must use the same config');
        // The diagram page's v-html is fed only by the pipeline.
        has(src('agent-rack-diagram/AgentRackDiagram.vue'), 'render_prose_fragment(', 'AgentRackDiagram');
    });
});

describe('the agent renderer (Laya review)', () => {
    const agent = src('laya-review/agent-markdown.ts');

    test('sanitizes on a private instance, never the shared default', () => {
        has(agent, 'DOMPurify(window)', 'agent_purifier');
        has(agent, 'agent_purifier().sanitize(render_agent_markdown_unsafe(text), AGENT_PURIFY_CONFIG)', 'render_agent_markdown');
        assert.ok(!agent.includes('DOMPurify.sanitize('), 'a hook on the default instance would change markdown-doc-render.ts too');
        assert.ok(!/DOMPurify\.addHook\(/.test(agent), 'hooks go on the private instance');
        assert.equal(agent.match(/\.sanitize\(/g)?.length, 1, 'one sanitize call, with the agent config');
    });

    test('pulls no highlighter and no markmap', () => {
        for (const banned of ['highlight.js', 'markmap', 'markdown-doc-render', 'markdown-pipeline']) {
            assert.ok(!agent.includes(`'${banned}`) && !agent.includes(`/${banned}`), `agent-markdown.ts imports ${banned}`);
        }
    });

    test('the config is an allowlist with no media, forms, style or names', () => {
        const config = AGENT_PURIFY_CONFIG as { ALLOWED_TAGS: string[]; ALLOWED_ATTR: string[]; ALLOWED_URI_REGEXP: RegExp; ALLOW_DATA_ATTR: boolean };
        for (const tag of ['img', 'svg', 'math', 'video', 'audio', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'select', 'textarea', 'style', 'script', 'dialog', 'details', 'link', 'meta', 'base']) {
            assert.ok(!config.ALLOWED_TAGS.includes(tag), tag);
        }
        assert.deepEqual([...config.ALLOWED_ATTR].sort(), ['align', 'class', 'href', 'start', 'title']);
        assert.equal(config.ALLOW_DATA_ATTR, false);
        for (const href of ['javascript:alert(1)', 'data:text/html,x', 'vbscript:x', 'file:///etc/passwd', '/relative', '#frag']) {
            assert.ok(!config.ALLOWED_URI_REGEXP.test(href), href);
        }
        assert.ok(config.ALLOWED_URI_REGEXP.test('https://example.com') && config.ALLOWED_URI_REGEXP.test('mailto:a@b.c'));
        assert.ok(Object.isFrozen(AGENT_PURIFY_CONFIG) && Object.isFrozen(config.ALLOWED_TAGS) && Object.isFrozen(config.ALLOWED_ATTR));
    });

    test('every prose_html caller is one the trusted-html header names', () => {
        const root = new URL('../../src/', import.meta.url);
        const callers = (readdirSync(root, { recursive: true }) as string[])
            .filter((path) => /\.(ts|vue)$/.test(path) && path !== 'utils/trusted-html.ts')
            .filter((path) => readFileSync(new URL(path, root), 'utf8').includes('prose_html('))
            .sort();
        assert.deepEqual(callers, ['agent-rack-diagram/AgentRackDiagram.vue', 'laya-review/agent-markdown.ts'], 'a new caller goes in the prose_html note in utils/trusted-html.ts');
        const header = src('utils/trusted-html.ts');
        for (const caller of callers) has(header, caller, 'the prose_html note');
        // And the reverse: every file the note names is here and calls it.
        const note = header.slice(header.indexOf('`prose_html`'), header.indexOf('`static_html`'));
        const named = [...note.matchAll(/- `([\w./-]+\.(?:ts|vue))`/g)].map((match) => match[1]).sort();
        assert.deepEqual(named, callers, 'the prose_html note names a file that does not call it');
    });
});

describe('the mind map', () => {
    const render = src('markdown/markdown-doc-render.ts');
    const body_of = (name: string): string => {
        const start = render.indexOf(`export function ${name}(`);
        assert.notEqual(start, -1, `${name} not found`);
        return render.slice(start, render.indexOf('\n}\n', start));
    };

    test('raw HTML is off at the parser', () => {
        has(render, 'markmap_transformer.md.set({ html: false })', 'markmap transformer');
    });

    test('every node is sanitized BEFORE the template inlines the tree', () => {
        const build = body_of('build_mindmap_doc');
        const walk = build.indexOf('sanitize_markmap_tree(root)');
        assert.notEqual(walk, -1, 'build_mindmap_doc does not walk the tree');
        assert.ok(walk < build.indexOf('fillTemplate('), 'the tree is inlined before it is sanitized');
        // fillTemplate escapes only a lowercase </script>; the walk closes the rest.
        has(render, "replace(/<(\\/script|!--)/gi, '&lt;$1')", 'the script-block escape');
    });

    test('the follow-up is recorded at the mechanism', () => {
        has(render, 'FOLLOW-UP: sandbox the mind-map frame', 'build_mindmap_doc');
    });
});

// ── The real sanitizer, in a real browser ───────────────────────────────────

type Browser = { newPage(): Promise<Page>; close(): Promise<void> };
type Page = {
    setContent(html: string): Promise<void>;
    addScriptTag(options: { path: string }): Promise<unknown>;
    evaluate<R, A>(fn: (arg: A) => R, arg: A): Promise<R>;
};

let browser: Browser | null = null;
let page: Page | null = null;
let unavailable = '';

before(async () => {
    try {
        const { chromium } = (await import('playwright')) as unknown as {
            chromium: { launch(options?: Record<string, unknown>): Promise<Browser> };
        };
        // The bundled build when it is installed; else the machine's Chrome.
        browser = await chromium.launch().catch(() => chromium.launch({ channel: 'chrome' }));
        page = await browser.newPage();
        await page.setContent('<!doctype html><html><body></body></html>');
        const purify = createRequire(import.meta.url).resolve('dompurify').replace(/purify\.cjs\.js$/, 'purify.js');
        await page.addScriptTag({ path: purify });
    } catch (error) {
        unavailable = `no headless browser: ${error instanceof Error ? error.message.split('\n')[0] : String(error)}`;
    }
});

after(async () => {
    await browser?.close();
});

async function sanitize(dirty: string): Promise<string> {
    return page!.evaluate(
        ({ html, config }) => (globalThis as unknown as { DOMPurify: { sanitize(h: string, c: unknown): string } }).DOMPurify.sanitize(html, config),
        { html: dirty, config: { ...MARKDOWN_PURIFY_CONFIG } },
    );
}

/** The agent renderer's sanitizer, rebuilt in the page from the module's OWN config and hook functions (each is
 *  self-contained for exactly this), so these cases test what ships rather than a copy. */
async function sanitize_agent(dirty: string): Promise<string> {
    const { ALLOWED_URI_REGEXP, ...config } = AGENT_PURIFY_CONFIG as { ALLOWED_URI_REGEXP: RegExp };
    return page!.evaluate(
        ({ html, rest, uri, hooks }) => {
            // oxlint-disable-next-line no-eval -- the page rebuilds this repository's own hook sources, sent as text
            const make = (source: string): unknown => (0, eval)(`(${source})`);
            const factory = (globalThis as unknown as { DOMPurify: (root: Window) => { addHook(name: string, hook: unknown): void; sanitize(h: string, c: unknown): string } }).DOMPurify;
            const purifier = factory(window);
            purifier.addHook('uponSanitizeAttribute', make(hooks.attr));
            purifier.addHook('afterSanitizeAttributes', make(hooks.link));
            return purifier.sanitize(html, { ...rest, ALLOWED_URI_REGEXP: new RegExp(uri.source, uri.flags) });
        },
        {
            html: dirty,
            rest: config,
            uri: { source: ALLOWED_URI_REGEXP.source, flags: ALLOWED_URI_REGEXP.flags },
            hooks: { attr: limit_attribute_values.toString(), link: force_link_target.toString() },
        },
    );
}

describe('the agent sanitizer (headless Chromium)', () => {
    test('script, event handlers, images, svg and media are stripped', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize_agent(
            '<p>ok</p><script>alert(1)</script><img src="https://evil.example/p.png" onerror="alert(1)"><svg onload="alert(1)"><circle/></svg><video src="x.mp4"></video><iframe src="https://evil.example"></iframe><p onclick="alert(1)">tap</p>',
        );
        assert.match(clean, /<p>ok<\/p>/);
        assert.match(clean, /<p>tap<\/p>/);
        for (const banned of ['<script', 'alert', '<img', 'onerror', '<svg', 'onload', '<video', '<iframe', 'onclick', 'evil.example']) {
            assert.ok(!clean.includes(banned), `${banned} survived: ${clean}`);
        }
    });

    test('javascript:, data: and relative hrefs lose the attribute; real links open in a new tab, without an opener', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize_agent('<a href="javascript:alert(1)">a</a><a href="data:text/html,x">b</a><a href="/admin">c</a><a href="https://example.com/x" target="_self">d</a>');
        assert.ok(!/javascript:|data:|href="\/admin"/.test(clean), clean);
        assert.match(clean, /<a href="https:\/\/example\.com\/x" target="_blank" rel="noopener noreferrer">d<\/a>/);
        assert.equal(clean.match(/target=/g)?.length, 1, `only a link with an href gets a target: ${clean}`);
    });

    test('style, form controls, ids, names and data attributes are stripped', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize_agent(
            '<p style="position:fixed;inset:0" id="location" name="cookie" data-x="1">overlay</p><style>body{display:none}</style><form action="https://evil.example"><input name="password"><button formaction="https://evil.example">Sign in</button><select><option>x</option></select><textarea></textarea></form>',
        );
        for (const banned of ['style', 'id=', 'name=', 'data-x', '<form', 'action=', '<input', '<button', '<select', '<textarea', 'evil.example']) {
            assert.ok(!clean.includes(banned), `${banned} survived: ${clean}`);
        }
        assert.match(clean, /<p>overlay<\/p>/);
    });

    test('class, align and start survive only as marked emits them', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize_agent(
            '<span class="vc-task-box" aria-hidden="true">☐</span><code class="language-ts">x</code><p class="lr-explain">y</p><code class="language-ts evil">z</code><table><tbody><tr><td align="right">1</td><td align="justify">2</td></tr></tbody></table><ol start="3"><li>c</li></ol><ol start="x"><li>d</li></ol><p align="center">e</p>',
        );
        assert.match(clean, /<span class="vc-task-box" aria-hidden="true">☐<\/span>/);
        assert.match(clean, /<code class="language-ts">x<\/code>/);
        assert.match(clean, /<td align="right">1<\/td><td>2<\/td>/);
        assert.match(clean, /<ol start="3">/);
        for (const banned of ['lr-explain', 'evil', 'justify', 'start="x"', '<p align']) {
            assert.ok(!clean.includes(banned), `${banned} survived: ${clean}`);
        }
    });

    test('the whole path: markdown through marked and the sanitizer keeps the prose and drops the payload', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize_agent(
            render_agent_markdown_unsafe('**Done.** See [docs](https://example.com) or [this](javascript:alert(1)).\n\n- [x] shipped\n\n![pixel](https://evil.example/p.png)\n\n<img src=x onerror=alert(1)>'),
        );
        assert.match(clean, /<strong>Done\.<\/strong>/);
        assert.match(clean, /<a href="https:\/\/example\.com" target="_blank" rel="noopener noreferrer">docs<\/a>/);
        assert.match(clean, /<span class="vc-task-box" aria-hidden="true">☑<\/span> shipped/);
        // The image became a link (no pixel loads); the raw tag is text.
        assert.match(clean, /<a href="https:\/\/evil\.example\/p\.png" target="_blank" rel="noopener noreferrer">pixel<\/a>/);
        assert.match(clean, /&lt;img src=x onerror=alert\(1\)&gt;/);
        assert.ok(!/<img|javascript:/.test(clean), clean);
    });
});

describe('DOMPurify with the config (headless Chromium)', () => {
    test('a fake sign-in form loses every control and its action', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize(
            '<p>Session expired</p><form action="https://evil.example/steal"><input name="password" type="password"><select><option>x</option></select><textarea></textarea><button formaction="https://evil.example">Sign in</button></form>',
        );
        assert.match(clean, /Session expired/);
        for (const banned of ['<form', '<input', '<button', '<select', '<textarea', 'action=', 'evil.example']) {
            assert.ok(!clean.includes(banned), `${banned} survived: ${clean}`);
        }
    });

    test('popover and invoker attributes are stripped', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize('<div popover id="x">hi</div><a popovertarget="x" popovertargetaction="show" commandfor="x" command="show-modal">open</a><dialog open>modal</dialog>');
        for (const banned of ['popover', 'command', '<dialog']) {
            assert.ok(!clean.includes(banned), `${banned} survived: ${clean}`);
        }
    });

    test('inline style and <style> are stripped; plain markup survives', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize('<style>body{display:none}</style><p style="position:fixed;inset:0">overlay</p><h2>Title</h2><a href="https://example.com">link</a>');
        assert.ok(!clean.includes('style'), clean);
        assert.match(clean, /<h2>Title<\/h2>/);
        assert.match(clean, /<a href="https:\/\/example\.com">link<\/a>/);
    });

    test('a named element cannot clobber a document global', async (t) => {
        if (unavailable) return t.skip(unavailable);
        const clean = await sanitize('<img name="cookie" src="x.png"><a id="location">x</a>');
        assert.ok(!/name="cookie"/.test(clean) && !/id="location"/.test(clean), clean);
        assert.match(clean, /user-content-/);
    });
});
