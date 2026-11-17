/**
 * The OSC 52 decode behind copy-on-select in every browser terminal. The ttyd
 * page runs an ES5 port of the same rules, so a change here is a change there.
 */
import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

import { decode_osc52_write } from '../../src/engine/terminal/terminal-clipboard.ts';

function b64(text: string): string {
    const bytes = new TextEncoder().encode(text);
    return btoa(String.fromCharCode(...bytes));
}

describe('decode_osc52_write', () => {
    it('decodes a clipboard write', () => {
        assert.equal(decode_osc52_write(`c;${b64('git status')}`), 'git status');
    });

    it('keeps UTF-8 intact — box drawing, emoji, newlines', () => {
        const text = '╭─ ✂ copied 👾\n│ line two';
        assert.equal(decode_osc52_write(`c;${b64(text)}`), text);
    });

    it('accepts an empty selection field and the other selection names', () => {
        assert.equal(decode_osc52_write(`;${b64('a')}`), 'a');
        assert.equal(decode_osc52_write(`p;${b64('b')}`), 'b');
        assert.equal(decode_osc52_write(`cs0;${b64('c')}`), 'c');
    });

    it('never answers a read request', () => {
        assert.equal(decode_osc52_write('c;?'), null);
        assert.equal(decode_osc52_write(';?'), null);
    });

    it('refuses empty, separator-less, malformed and bad-base64 payloads', () => {
        assert.equal(decode_osc52_write('c;'), null);
        assert.equal(decode_osc52_write('c'), null);
        assert.equal(decode_osc52_write(`x;${b64('a')}`), null);
        assert.equal(decode_osc52_write('c;not*base64!'), null);
    });
});
