import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { describe, test } from 'node:test';

// The pure half of the Laya review page's agent renderer. The sanitizer half runs in headless Chromium in
// `markdown-sanitize.test.ts`. Why each rule: owned by `src/laya-review/agent-markdown.ts` — see the note there.
import {
    TAIL_CHARS_PER_LINE,
    agent_message_source,
    render_agent_markdown_unsafe,
    repair_tail,
    tail_at_block,
} from '../../src/laya-review/agent-markdown.ts';

const FENCE = '```';
const src = (relative: string): string => readFileSync(new URL(`../../src/${relative}`, import.meta.url), 'utf8');

describe('repair_tail', () => {
    test('a tail that starts inside a fence gets the fence reopened, so its head renders as code', () => {
        const tail = `const x = 1;\n${FENCE}\nDone. Want me to ship it?`;
        const repaired = repair_tail(tail);
        assert.equal(repaired, `${FENCE}\n${tail}`);
        const html = render_agent_markdown_unsafe(repaired);
        assert.match(html, /<pre><code>const x = 1;\n<\/code><\/pre>/);
        assert.match(html, /<p>Done\. Want me to ship it\?<\/p>/);
    });

    test('unrepaired, the same tail would render the rest of the message as code', () => {
        const html = render_agent_markdown_unsafe(`const x = 1;\n${FENCE}\nDone. Want me to ship it?`);
        assert.match(html, /<code>Done\. Want me to ship it\?/);
    });

    test('balanced fences are left alone', () => {
        const text = `Ran it:\n${FENCE}sh\nnpm test\n${FENCE}\nAll green.`;
        assert.equal(repair_tail(text), text);
        assert.equal(repair_tail('no fences at all'), 'no fences at all');
    });

    test('an odd count whose first fence carries a language is an open fence at the END, not a cut head', () => {
        const text = `Start here:\n${FENCE}ts\nconst unfinished = true;`;
        assert.equal(repair_tail(text), text);
    });

    test('the reopened fence matches the closer, so a longer or tilde fence still closes', () => {
        assert.equal(repair_tail('inner ``` stays content\n````\nafter'), '````\ninner ``` stays content\n````\nafter');
        assert.equal(repair_tail('a\n~~~\nb'), '~~~\na\n~~~\nb');
    });

    test('a fence nested in a list item counts, at any indent', () => {
        const tail = `  echo hi\n   ${FENCE}\n2. Then the next step`;
        assert.equal(repair_tail(tail), `${FENCE}\n${tail}`);
    });

    test('inline triple backticks are not a fence', () => {
        const text = '```a``` inline\nand more';
        assert.equal(repair_tail(text), text);
    });
});

describe('tail_at_block', () => {
    test('a text within budget comes back whole', () => {
        assert.equal(tail_at_block('short', 100), 'short');
    });

    test('cuts at a paragraph break when one keeps half the budget', () => {
        const text = `${'a'.repeat(80)}\n\nsecond paragraph here\n\nthird`;
        const tail = tail_at_block(text, 40);
        assert.equal(tail, 'second paragraph here\n\nthird');
    });

    test('falls back to a line break, then to a word boundary — never mid-word', () => {
        const lines = `${'x'.repeat(60)}\nline two is here\nline three`;
        assert.equal(tail_at_block(lines, 30), 'line two is here\nline three');

        const prose = 'alpha bravo charlie delta echo foxtrot golf hotel india juliet';
        const tail = tail_at_block(prose, 20);
        assert.ok(tail.length <= 20, tail);
        assert.ok(prose.endsWith(tail), tail);
        assert.match(prose, new RegExp(`(^|\\s)${tail}$`), 'the cut lands between words');
    });

    test('a paragraph break too close to the end loses to a line break that keeps more', () => {
        const text = `${'z'.repeat(50)}\nkeep this whole line of text please\nand this one too\n\nend`;
        assert.equal(tail_at_block(text, 60), 'keep this whole line of text please\nand this one too\n\nend');
    });

    test('a word cut never leaves a line starting with a fence that was not one', () => {
        // Unchecked, the first space would leave `${FENCE}x word word`: a new fence the message never had.
        const text = `${'q'.repeat(30)} ${FENCE}x word word`;
        assert.equal(tail_at_block(text, 14), 'word word');
    });

    test('one token longer than the budget is cut hard', () => {
        const token = 'k'.repeat(100);
        assert.equal(tail_at_block(token, 10), 'k'.repeat(10));
    });
});

describe('agent_message_source', () => {
    test('full mode keeps the whole text and is never cut', () => {
        const { source, cut } = agent_message_source('hello\nworld\n\n', null);
        assert.equal(source, 'hello\nworld');
        assert.equal(cut, false);
    });

    test('tail mode keeps about tail_lines lines of budget and says when it cut', () => {
        const long = Array.from({ length: 200 }, (_, i) => `paragraph ${i} ${'w '.repeat(30)}`).join('\n\n');
        const { source, cut } = agent_message_source(long, 4);
        assert.equal(cut, true);
        assert.ok(source.length <= 4 * TAIL_CHARS_PER_LINE, `${source.length}`);
        assert.ok(source.endsWith('paragraph 199 ' + 'w '.repeat(30).trimEnd()));
        assert.equal(agent_message_source('two\nlines', 4).cut, false);
    });

    test('a tail cut inside a code block is repaired', () => {
        const code = Array.from({ length: 120 }, (_, i) => `line_${i}();`).join('\n');
        const message = `Here is the change:\n\n${FENCE}ts\n${code}\n${FENCE}\n\nShip it?`;
        const { source, cut } = agent_message_source(message, 2);
        assert.equal(cut, true);
        assert.ok(source.startsWith(`${FENCE}\n`), source.slice(0, 40));
        assert.match(render_agent_markdown_unsafe(source), /<p>Ship it\?<\/p>/);
    });
});

describe('render_agent_markdown_unsafe', () => {
    test('a single newline is a line break, as the terminal showed it', () => {
        assert.equal(render_agent_markdown_unsafe('line one\nline two').trim(), '<p>line one<br>line two</p>');
    });

    test('task boxes are glyphs, never form controls', () => {
        const html = render_agent_markdown_unsafe('- [ ] todo\n- [x] done');
        assert.ok(!html.includes('<input'), html);
        assert.match(html, /<span class="vc-task-box" aria-hidden="true">☐<\/span> todo/);
        assert.match(html, /<span class="vc-task-box" aria-hidden="true">☑<\/span> done/);
    });

    test('links render; bare URLs autolink', () => {
        const html = render_agent_markdown_unsafe('See [the docs](https://example.com/a) and https://example.org');
        assert.match(html, /<a href="https:\/\/example\.com\/a">the docs<\/a>/);
        assert.match(html, /<a href="https:\/\/example\.org">https:\/\/example\.org<\/a>/);
    });

    test('raw HTML is shown as text, as the terminal printed it', () => {
        const html = render_agent_markdown_unsafe('edit the <AgentMessage> card\n\n<details>\n<summary>x</summary>\n</details>');
        assert.match(html, /edit the &lt;AgentMessage&gt; card/);
        assert.match(html, /&lt;details&gt;<br>/);
        assert.ok(!/<details|<summary|<AgentMessage/i.test(html), html);
    });

    test('an image is a link to it, by its alt text', () => {
        const html = render_agent_markdown_unsafe('![the chart](https://example.com/c.png)');
        assert.ok(!html.includes('<img'), html);
        assert.match(html, /<a href="https:\/\/example\.com\/c\.png">the chart<\/a>/);
    });

    test('code blocks are plain, with no highlighter markup', () => {
        const html = render_agent_markdown_unsafe(`${FENCE}ts\nconst a = 1;\n${FENCE}`);
        assert.match(html, /<pre><code class="language-ts">const a = 1;\n<\/code><\/pre>/);
        assert.ok(!html.includes('hljs'), html);
    });

    test('a numbered list resumed after a code block keeps its number', () => {
        const html = render_agent_markdown_unsafe(`1. first\n\n${FENCE}\nx\n${FENCE}\n\n2. second`);
        assert.match(html, /<ol start="2">/);
    });

    test('compact GFM: tables and strikethrough', () => {
        const html = render_agent_markdown_unsafe('| a | b |\n|:-|-:|\n| 1 | 2 |\n\n~~gone~~');
        assert.match(html, /<th align="left">a<\/th>/);
        assert.match(html, /<td align="right">2<\/td>/);
        assert.match(html, /<del>gone<\/del>/);
    });
});

describe('the renderer stays out of the page entry chunk', () => {
    test('AgentMessage.vue reaches agent-markdown.ts only through import()', () => {
        const component = src('laya-review/AgentMessage.vue');
        assert.notEqual(component.indexOf("import('./agent-markdown.ts')"), -1, 'the dynamic import was not found');
        assert.ok(!/^\s*import\s+(?!type\b)[^;]*from\s+'\.\/agent-markdown\.ts'/m.test(component), 'a static import of agent-markdown.ts');
    });

    test('nothing else in the laya-review graph imports it', () => {
        const dir = new URL('../../src/laya-review/', import.meta.url);
        for (const name of readdirSync(dir)) {
            if (!/\.(ts|vue)$/.test(name) || name === 'agent-markdown.ts' || name === 'AgentMessage.vue') continue;
            const text = readFileSync(new URL(name, dir), 'utf8');
            assert.ok(!/from\s+'[^']*agent-markdown(\.ts)?'/.test(text), `${name} imports agent-markdown.ts statically`);
        }
    });
});
