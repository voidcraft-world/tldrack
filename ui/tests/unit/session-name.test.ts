import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import {
    ATTACH_ONLY_PREFIX,
    SESSION_NAME_RE,
    SESSION_NAME_WARNING,
    attach_only_arg,
    is_valid_session_name,
} from '../../src/engine/terminal/session-name.ts';

describe('is_valid_session_name', () => {
    it('accepts letters, digits, hyphen and underscore', () => {
        for (const name of ['claude', 'term-2', 'my_session', 'A1', 'a-b_c-9']) {
            assert.equal(is_valid_session_name(name), true, `${name} should be valid`);
        }
    });

    it('rejects empty, whitespace, dots and other punctuation', () => {
        for (const name of ['', ' ', 'has space', 'dot.name', 'slash/name', 'emoji😀', 'a b']) {
            assert.equal(is_valid_session_name(name), false, `${name} should be invalid`);
        }
    });

    it('anchors the whole string (no partial match)', () => {
        // Without ^…$ anchoring, 'ok name' would match on the 'ok' prefix.
        assert.equal(is_valid_session_name('ok name'), false);
    });

    it('matches the bridge guard byte-for-byte', () => {
        // Mirror of bridge/tldrack/snapshot.py SESSION_NAME_RE.
        // If the bridge widens/narrows its rule, this literal must move with it.
        assert.equal(SESSION_NAME_RE.source, '^[A-Za-z0-9_-]+$');
    });

    it('names the allowed set in the user-facing warning', () => {
        assert.equal(SESSION_NAME_WARNING, 'Session names must match [A-Za-z0-9_-]+');
    });
});

describe('the attach-only ?arg= grammar', () => {
    it('cannot collide with a session name', () => {
        // The whole scheme rests on `:` being impossible in a real name — it is
        // what makes `attach:foo` unambiguously a PREFIX rather than a session
        // someone happened to call that. tmux rejects it too.
        assert.equal(ATTACH_ONLY_PREFIX.includes(':'), true);
        assert.equal(is_valid_session_name(ATTACH_ONLY_PREFIX), false);
        assert.equal(is_valid_session_name('attach:foo'), false);
    });

    it('prefixes the bare name', () => {
        assert.equal(attach_only_arg('svc-vite'), 'attach:svc-vite');
    });
});
