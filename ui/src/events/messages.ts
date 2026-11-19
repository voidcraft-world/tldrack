/**
 * The three messages a page and the terminal frame beside it post to each other, and the one rule
 * every `postMessage` name of the rack follows (`message_is`, below).
 *
 * The strings are a contract with the bridge: `bridge/tldrack/index_patch.py`, the script the
 * bridge injects into ttyd's page, spells the same three. A string changed here and not there is
 * a font control or a key bar that does nothing, with no error anywhere.
 */

/** Every message name the rack posts starts with this. */
export const MESSAGE_PREFIX = 'tldrack-';
/**
 * Legacy (step 6.8): the prefix every message carried before 0.1.0. A listener still takes it for
 * one release, so a host or a bridge one release behind keeps working; nothing posts it.
 */
export const LEGACY_MESSAGE_PREFIX = 'voidcraft-';

/** Whether `data` is the message `name`, under its own name or its legacy one. Every listener asks this. */
export function message_is(data: unknown, name: string): boolean {
    const type = typeof data === 'object' && data !== null ? (data as { type?: unknown }).type : undefined;
    if (type === name) return true;
    return (
        typeof type === 'string' &&
        name.startsWith(MESSAGE_PREFIX) &&
        type === LEGACY_MESSAGE_PREFIX + name.slice(MESSAGE_PREFIX.length)
    );
}

/** page → terminal: `{ type, fontSize }`. xterm resizes in place; the socket stays open. */
export const MESSAGE_TERMINAL_CONFIG = 'tldrack-terminal-config';
/** terminal → page: `{ type, step }` for ⌘/Ctrl + = / − / 0, which the patched page consumes. */
export const MESSAGE_TERMINAL_FONT_STEP = 'tldrack-terminal-font-step';
/**
 * page → terminal: one key of the on-screen key bar, `{ type, key: TerminalSoftKey }`. A phone
 * keyboard has no Esc, Tab, Ctrl or arrows.
 *
 * The payload is a TOKEN, never a byte sequence, and that is a security boundary. The injected
 * script takes this message only from the frame that holds it, on loopback or an operator's
 * allowed origin (`index_patch.py` owns the check). Even so, a pass-through of bytes would be
 * remote typing into a shell for whoever passed it; a closed table of tokens can at worst
 * interrupt an agent. For the same reason the table has no Enter and nothing printable: Up
 * followed by Enter is "run the last command again".
 */
export const MESSAGE_TERMINAL_KEY = 'tldrack-terminal-key';

/** The closed set of keys the bar may send. A token `index_patch.py` does not list is ignored. */
export type TerminalSoftKey = 'escape' | 'tab' | 'ctrl_c' | 'up' | 'down';
