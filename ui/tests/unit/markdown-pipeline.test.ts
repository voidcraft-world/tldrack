import test from 'node:test';
import assert from 'node:assert/strict';
import {
    build_prose_html_doc,
    extract_first_heading,
    render_prose_markdown,
    splice_mermaid_svgs,
    wrap_mermaid_fence,
} from '../../src/markdown/markdown-pipeline.ts';

test('long document containment retains all content and requires explicit opt-in', () => {
    const body = '<p>Searchable content &amp; selectable text.</p>'.repeat(1000);
    const optimized = build_prose_html_doc(body, { title: 'Long', contain_long_document: true });
    assert.ok(optimized.includes('<article class="vc-md-doc vc-md-doc--long">'));
    assert.ok(optimized.includes(body));
    const regular = build_prose_html_doc(body, { title: 'Other consumer' });
    assert.ok(regular.includes('<article class="vc-md-doc">'));
    const short = build_prose_html_doc('<p>Short</p>', { title: 'Short', contain_long_document: true });
    assert.ok(short.includes('<article class="vc-md-doc">'));
});

test('long document placeholders size the block axis only and never skip a table', () => {
    const doc = build_prose_html_doc('<p>x</p>', { title: 'Long', contain_long_document: true });
    const skipped = doc.match(/\.vc-md-doc--long > :is\(([^)]*)\) \{ content-visibility: auto; \}/);
    assert.ok(skipped, 'containment selector found');
    assert.ok(!skipped[1].split(',').map((s) => s.trim()).includes('table'));
    // The two-axis shorthand gave every skipped block a 300px WIDTH too.
    assert.ok(!doc.includes('contain-intrinsic-size'));
    assert.ok(doc.includes('.vc-md-doc--long > p { contain-intrinsic-block-size: auto'));
});

test('mermaid fence becomes an indexed placeholder and its source is collected', () => {
    const md = '# Doc\n\n```mermaid\ngraph TD\n  A --> B\n```\n\ndone';
    const { body_html, mermaid_sources } = render_prose_markdown(md);

    assert.equal(mermaid_sources.length, 1);
    assert.equal(mermaid_sources[0], 'graph TD\n  A --> B');
    assert.ok(body_html.includes('<div data-vc-mermaid="0"></div>'));
    assert.ok(!body_html.includes('graph TD'));
});

test('multiple mermaid fences are index-aligned with their placeholders', () => {
    const md = '```mermaid\nfirst\n```\n\ntext between\n\n```mermaid\nsecond\n```';
    const { body_html, mermaid_sources } = render_prose_markdown(md);

    assert.deepEqual(mermaid_sources, ['first', 'second']);
    assert.ok(body_html.indexOf('data-vc-mermaid="0"') < body_html.indexOf('data-vc-mermaid="1"'));
});

test('a mermaid fence nested inside a larger fenced block is NOT extracted', () => {
    const md = '````md\n```mermaid\ngraph TD\n```\n````';
    const { body_html, mermaid_sources } = render_prose_markdown(md);

    assert.equal(mermaid_sources.length, 0);
    assert.ok(!body_html.includes('data-vc-mermaid'));
    assert.ok(body_html.includes('mermaid'));
});

test('known-language fences get highlight.js classes baked in at build time', () => {
    const md = '```ts\nconst x: number = 1;\n```';
    const { body_html } = render_prose_markdown(md);

    assert.ok(body_html.includes('language-ts'));
    assert.ok(body_html.includes('hljs'));
    assert.ok(body_html.includes('<span'));
});

test('unknown-language fences render as escaped plain code', () => {
    const md = '```notareallang\na < b && c > d\n```';
    const { body_html } = render_prose_markdown(md);

    assert.ok(body_html.includes('a &lt; b &amp;&amp; c &gt; d'));
    assert.ok(!body_html.includes('language-notareallang'));
});

test('gfm tables and task lists render', () => {
    const md = '| a | b |\n|---|---|\n| 1 | 2 |\n\n- [x] done\n- [ ] todo';
    const { body_html } = render_prose_markdown(md);

    assert.ok(body_html.includes('<table>'));
    // A glyph, not an <input>: the sanitizer forbids form controls.
    assert.ok(body_html.includes('☑') && body_html.includes('☐'));
    assert.ok(!body_html.includes('<input'));
});

test('splice replaces every placeholder with its rendered content', () => {
    const html = '<p>a</p><div data-vc-mermaid="0"></div><p>b</p><div data-vc-mermaid="1"></div>';
    const result = splice_mermaid_svgs(html, ['<svg>one</svg>', '<svg>two</svg>']);

    assert.equal(result, '<p>a</p><div class="vc-mermaid"><svg>one</svg></div><p>b</p><div class="vc-mermaid"><svg>two</svg></div>');
});

test('splice leaves placeholders with no rendered entry intact', () => {
    const html = '<div data-vc-mermaid="0"></div><div data-vc-mermaid="5"></div>';
    const result = splice_mermaid_svgs(html, ['<svg>one</svg>']);

    assert.ok(result.includes('<div class="vc-mermaid"><svg>one</svg></div>'));
    assert.ok(result.includes('<div data-vc-mermaid="5"></div>'));
});

test('extract_first_heading strips heading markers and falls back to plain lines', () => {
    assert.equal(extract_first_heading('# My Title\n\nbody'), 'My Title');
    assert.equal(extract_first_heading('\n\nplain first line\n# later'), 'plain first line');
    assert.equal(extract_first_heading('   \n\n'), null);
    assert.equal(extract_first_heading(''), null);
});

test('wrap_mermaid_fence wraps trimmed source and empties stay empty', () => {
    assert.equal(wrap_mermaid_fence('graph TD\n  A --> B\n'), '```mermaid\ngraph TD\n  A --> B\n```');
    assert.equal(wrap_mermaid_fence('   \n'), '');
    assert.equal(wrap_mermaid_fence(''), '');
    // Round-trip: the fenced output is extracted back out by the prose renderer.
    const { mermaid_sources } = render_prose_markdown(wrap_mermaid_fence('graph TD'));
    assert.deepEqual(mermaid_sources, ['graph TD']);
});

test('prose doc wrapper contains base target, title, body, and extra css', () => {
    const doc = build_prose_html_doc('<p>hello</p>', { title: 'A <Title>', css_texts: ['.x{color:red}'] });

    assert.ok(doc.includes('<base target="_blank" />'));
    assert.ok(doc.includes('<title>A &lt;Title&gt;</title>'));
    assert.ok(doc.includes('<p>hello</p>'));
    assert.ok(doc.includes('.x{color:red}'));
    assert.ok(doc.includes('#0d1117'));
});
