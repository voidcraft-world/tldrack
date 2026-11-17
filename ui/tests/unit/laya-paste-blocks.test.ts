import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { paste_label, paste_size_title, paste_title, split_pastes, tail_segments, type PasteSegment } from '../../src/laya-review/paste-blocks.ts';

// Pastes in an operator's message, folded on the Laya review page (paste-blocks.ts owns the format and why).

const open = (id: string) => `<pasted_content id="${id}">`;
const close = (id: string) => `</pasted_content id="${id}">`;
const paste = (id: string | null, text: string, cut = false): PasteSegment => ({
    kind: 'paste',
    id,
    text,
    lines: text === '' ? 0 : text.split('\n').length,
    chars: text.length,
    cut,
});
const text = (t: string): PasteSegment => ({ kind: 'text', text: t });

describe('split_pastes', () => {
    it('leaves a message without a paste as one text run, unchanged', () => {
        for (const t of ['', 'plain words', '  lead\n\n"""\nnot a paste\n"""\n  ', 'mid-line `<pasted_content id="x">` mention']) {
            assert.deepEqual(split_pastes(t), [text(t)]);
        }
    });

    it('folds one closed paste beside the operator’s words and drops the """ fences around it', () => {
        const t = `please write the notes\n\n/summary \n\n"""\n\n\n${open('a1b2')}\nthe first line\nsecond\n${close('a1b2')}\n\n"""\n`;
        assert.deepEqual(split_pastes(t), [text('please write the notes\n\n/summary'), paste('a1b2', 'the first line\nsecond')]);
    });

    it('keeps the body verbatim, less only the one newline at each end', () => {
        const [, only] = split_pastes(`see\n${open('a')}\n\n  indented\n\n${close('a')}`);
        assert.deepEqual(only, paste('a', '\n  indented\n'));
    });

    it('splits two pastes and keeps the words between them', () => {
        const t = `first\n${open('a')}\none\nuno\n${close('a')}\n"""\nbetween\n"""\n${open('b')}\ntwo\nthree\n${close('b')}\nlast words`;
        assert.deepEqual(split_pastes(t), [text('first'), paste('a', 'one\nuno'), text('between'), paste('b', 'two\nthree'), text('last words')]);
    });

    it('runs an unclosed paste to the end of the message', () => {
        assert.deepEqual(split_pastes(`look:\n${open('z9')}\nline 1\nline 2\n`), [text('look:'), paste('z9', 'line 1\nline 2')]);
    });

    it('keeps an escaped nested paste inside the outer one, as text', () => {
        const inner = `<\\pasted_content id="in">\nold reply\n<\\/pasted_content id="in">`;
        const segs = split_pastes(`${open('out')}\nbefore\n${inner}\nafter\n${close('out')}\ntail`);
        assert.deepEqual(segs, [paste('out', `before\n${inner}\nafter`), text('tail')]);
    });

    it('a raw opener inside a paste is text too: only the outer id closes it', () => {
        const segs = split_pastes(`note\n${open('a')}\nx\n${open('b')}\ny\n${close('b')}\nz\n${close('a')}`);
        assert.deepEqual(segs, [text('note'), paste('a', `x\n${open('b')}\ny\n${close('b')}\nz`)]);
    });

    it('a closer with another id does not end the paste', () => {
        const segs = split_pastes(`note\n${open('a')}\nx\n${close('b')}\ny`);
        assert.deepEqual(segs, [text('note'), paste('a', `x\n${close('b')}\ny`)]);
        const later = split_pastes(`${open('a')}\nx\n${close('b')}\ny\n${close('a')}\nz`);
        assert.deepEqual(later, [paste('a', `x\n${close('b')}\ny`), text('z')]);
    });

    it('drops runs that are only whitespace and fences', () => {
        const t = `\n\n"""\n\n${open('a')}\n1\n2\n${close('a')}\n\n"""\n\nso\n"""\n${open('b')}\n3\n4\n${close('b')}\n  \n\n`;
        assert.deepEqual(split_pastes(t), [paste('a', '1\n2'), text('so'), paste('b', '3\n4')]);
    });

    it('drops only a fence that touches a folded paste, never one in the words', () => {
        const t = `"""\nquoted\n"""\nthen\n\n"""\n${open('a')}\n1\n2\n${close('a')}`;
        assert.deepEqual(split_pastes(t), [text('"""\nquoted\n"""\nthen'), paste('a', '1\n2')]);
    });

    it('reads a tag that starts its own line, indented or with spaces inside, and a bare one with no id', () => {
        assert.deepEqual(split_pastes(`so\n  <pasted_content  id = "s p" >\nx\ny\n\t</pasted_content id="s p">`), [text('so'), paste('s p', 'x\ny')]);
        assert.deepEqual(split_pastes('<pasted_content>\nx\ny\n</pasted_content>\nafter'), [paste(null, 'x\ny'), text('after')]);
        // A bare closer does not close an id'd paste, and an id'd closer does not close a bare one.
        assert.deepEqual(split_pastes(`so\n${open('a')}\nx\n</pasted_content>`), [text('so'), paste('a', 'x\n</pasted_content>')]);
    });

    it('an empty paste beside words folds as no lines; alone it is nothing', () => {
        assert.deepEqual(split_pastes(`so\n${open('e')}\n${close('e')}`), [text('so')]);
    });
});

describe('split_pastes: a paste that is the operator’s own words', () => {
    // Invented messages, in the shapes a dictated or pasted prompt takes.
    const dictated = `This note is going to be ${'a long dictated prompt, '.repeat(110)}and that is the plan.`;

    it('a lone one-line paste (dictation) is the message, not PASTED · 1 line', () => {
        assert.ok(dictated.length > 2_500);
        assert.deepEqual(split_pastes(`${open('d1')}\n${dictated}\n${close('d1')}`), [text(dictated)]);
        assert.deepEqual(split_pastes(`"""\n${open('d1')}\n${dictated}\n${close('d1')}\n"""`), [text(`"""\n${dictated}\n"""`)],
            'fences only drop around a paste that still folds');
    });

    it('a typed letter before a dictation that carries on its word joins it: W + ell, so…', () => {
        assert.deepEqual(split_pastes(`W\n\n${open('w2')}\nell, here is one more thing\n${close('w2')}`), [text('Well, here is one more thing')]);
        assert.deepEqual(split_pastes(`W\n\n${open('w2')}\nell, here is one more thing`), [text('Well, here is one more thing')], 'unclosed too');
        assert.deepEqual(split_pastes(`OK\n\n${open('w3')}\nThe next part.\n${close('w3')}`), [text('OK\n\nThe next part.')], 'a capital starts its own sentence');
    });

    it('a one-line paste before the operator’s typed words reads as one text, its tag lines gone', () => {
        assert.deepEqual(split_pastes(`${open('p1')}\nfix the login redirect\n${close('p1')}\nand then deploy`), [text('fix the login redirect\nand then deploy')]);
        assert.deepEqual(split_pastes(`first\n${open('p1')}\nmiddle\n${close('p1')}\n\nlast`), [text('first\nmiddle\n\nlast')]);
    });

    it('words beside a long pasted document still fold the document', () => {
        const document = Array.from({ length: 46 }, (_, i) => `document line ${i}`).join('\n');
        assert.deepEqual(split_pastes(`summarise this page for me\n\n${open('p2')}\n${document}\n${close('p2')}`), [text('summarise this page for me'), paste('p2', document)]);
    });

    it('a message that is only pasted documents is those documents', () => {
        assert.deepEqual(split_pastes(`${open('a')}\n1\n2\n${close('a')}\n\n"""\n${open('b')}\n3\n4\n${close('b')}`), [text('1\n2\n\n3\n4')]);
    });
});

describe('split_pastes: a tag inside a code fence is an example', () => {
    it('an agent quoting the format in a fence keeps its message whole', () => {
        const t = `Done. Two changes:\n\n\`\`\`\nnpm test\n\`\`\`\n\nThe ledger stores a paste as:\n\n\`\`\`\n${open('a1b2')}\n…\n${close('a1b2')}\n\`\`\`\n\nSay go to deploy.`;
        assert.deepEqual(split_pastes(t), [text(t)]);
        const tilde = `~~~\n${open('x')}\na\nb\n${close('x')}\n~~~\nthen\n${open('y')}\nc\nd\n${close('y')}`;
        assert.deepEqual(split_pastes(tilde), [text(`~~~\n${open('x')}\na\nb\n${close('x')}\n~~~\nthen`), paste('y', 'c\nd')], 'a real paste after the fence still folds');
    });
});

describe('split_pastes: a clipped message', () => {
    it('marks the paste the clip cut, only when the message was clipped', () => {
        const head = `look:\n${open('c1')}\nline 1\nline 2`;
        assert.deepEqual(split_pastes(head, { clipped: true }), [text('look:'), paste('c1', 'line 1\nline 2', true)]);
        assert.deepEqual(split_pastes(head), [text('look:'), paste('c1', 'line 1\nline 2')], 'the whole text: a paste left open is its whole size');
        assert.deepEqual(split_pastes(`look:\n${open('c1')}\n1\n2\n${close('c1')}`, { clipped: true })[1], paste('c1', '1\n2'), 'closed before the clip: exact');
    });
});

describe('tail_segments', () => {
    const cut = (t: string, max: number) => t.slice(t.length - max);
    const segs: PasteSegment[] = [text('a'.repeat(50)), paste('p', 'x\ny'), text('b'.repeat(20))];

    it('keeps every run when the budget reaches the top', () => {
        assert.deepEqual(tail_segments(segs, 200, 10, cut), { segments: segs, cut: false });
    });

    it('cuts the text run the budget runs out in and drops what is above it', () => {
        const { segments, cut: was_cut } = tail_segments(segs, 40, 10, cut);
        assert.equal(was_cut, true);
        assert.deepEqual(segments, [text('a'.repeat(10)), segs[1], segs[2]]);
    });

    it('stops at a spent budget, and a paste costs one line', () => {
        assert.deepEqual(tail_segments(segs, 30, 10, cut), { segments: [segs[1], segs[2]], cut: true });
        assert.deepEqual(tail_segments([paste('p', 'x'), paste('q', 'y')], 5, 10, cut), { segments: [paste('q', 'y')], cut: true });
    });
});

describe('paste_label and its titles', () => {
    it('says line or lines, groups thousands, and the size of a few long paragraphs', () => {
        assert.equal(paste_label({ lines: 2, chars: 40, cut: false }), 'PASTED · 2 lines');
        assert.equal(paste_label({ lines: 42, chars: 3104, cut: false }), 'PASTED · 42 lines');
        assert.equal(paste_label({ lines: 1204, chars: 30_000, cut: false }), 'PASTED · 1,204 lines');
        assert.equal(paste_label({ lines: 3, chars: 4210, cut: false }), 'PASTED · 3 lines · 4.2K chars');
    });

    it('a clipped paste reads as a lower bound, never a wrong size', () => {
        assert.equal(paste_label({ lines: 26, chars: 1130, cut: true }), 'PASTED · 26+ lines');
        assert.equal(paste_label({ lines: 2, chars: 1130, cut: true }), 'PASTED · 2+ lines · 1.1K+ chars');
        assert.match(paste_size_title({ lines: 26, chars: 1130, cut: true }), /^Pasted text, at least 26 lines and 1,130 characters: the message was clipped here$/);
    });

    it('promises a press only where a press opens it', () => {
        assert.equal(paste_size_title({ lines: 42, chars: 3104, cut: false }), 'Pasted text, 42 lines, 3,104 characters');
        assert.equal(paste_title({ lines: 42, chars: 3104, cut: false }), 'Pasted text, 42 lines, 3,104 characters: press to show it');
        assert.equal(paste_title({ lines: 1, chars: 1, cut: false }), 'Pasted text, 1 line, 1 character: press to show it');
    });
});
