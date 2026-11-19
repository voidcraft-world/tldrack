import test from 'node:test';
import assert from 'node:assert/strict';
import { MESSAGE_TERMINAL_CONFIG, MESSAGE_TERMINAL_FONT_STEP, MESSAGE_TERMINAL_KEY, message_is } from '../../src/events/messages.ts';
import { MESSAGE_AGENT_RACK_ACTION, MESSAGE_AGENT_RACK_DATA } from '../../src/agent-rack/rack-transport.ts';

test('every rack message is a tldrack- name', () => {
    for (const name of [MESSAGE_TERMINAL_CONFIG, MESSAGE_TERMINAL_FONT_STEP, MESSAGE_TERMINAL_KEY, MESSAGE_AGENT_RACK_DATA, MESSAGE_AGENT_RACK_ACTION]) {
        assert.match(name, /^tldrack-[a-z-]+$/);
    }
});

test('message_is takes a message under its own name and its legacy one, and nothing else', () => {
    assert.ok(message_is({ type: 'tldrack-terminal-font-step', step: 1 }, MESSAGE_TERMINAL_FONT_STEP));
    // Legacy (step 6.8): a bridge or a host one release behind.
    assert.ok(message_is({ type: 'voidcraft-terminal-font-step', step: 1 }, MESSAGE_TERMINAL_FONT_STEP));
    assert.ok(message_is({ type: 'voidcraft-agent-rack-data' }, MESSAGE_AGENT_RACK_DATA));
    assert.ok(!message_is({ type: 'voidcraft-terminal-config' }, MESSAGE_TERMINAL_FONT_STEP));
    assert.ok(!message_is({ type: 'other-terminal-font-step' }, MESSAGE_TERMINAL_FONT_STEP));
    assert.ok(!message_is({ type: 'voidcraft-x' }, 'some-x'), 'only a tldrack- name has a legacy twin');
    for (const data of [null, undefined, 'tldrack-terminal-font-step', 7, {}]) {
        assert.ok(!message_is(data, MESSAGE_TERMINAL_FONT_STEP));
    }
});
