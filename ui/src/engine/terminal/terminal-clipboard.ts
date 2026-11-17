/**
 * Selecting text in a rack terminal copies it — the OSC 52 leg.
 *
 * tmux owns the mouse (`set -g mouse on`), so a drag in any pane is a tmux
 * copy-mode selection, and on release tmux's `copy-pipe-and-cancel` hands the
 * text to each attached client as an OSC 52 escape (`set-clipboard external`;
 * tmux's `xterm*` terminal-features include `clipboard`). A native terminal
 * honours it. ttyd 1.7.7's xterm.js ships no OSC 52 handler, so without this a
 * copy in a browser pane is dropped without a trace and ⌘V pastes whatever came
 * before.
 *
 * It lands in the VIEWER's clipboard — the browser's, on whatever device is
 * looking — which is right for a rack pointed at another machine too. That is why this is
 * not tmux's `copy-command pbcopy`: that writes the tmux HOST's clipboard.
 *
 * Write-only. A `?` payload is a clipboard READ request; it is swallowed and
 * never answered, since answering hands the clipboard to whatever printed it.
 * With `set-clipboard external` tmux also drops OSC 52 that programs emit, so
 * the only writer that reaches this handler is tmux's own copy.
 *
 * The ttyd page carries an ES5 port of this file in its injected script
 * (`bridge/tldrack/index_patch.py`).
 */
import type { IDisposable, Terminal } from '@xterm/xterm';
import { write_clipboard } from '../../utils/clipboard-write.ts';

// The two-route write: owned by `utils/clipboard-write.ts` — see the note there.
export { write_clipboard };

/**
 * The `allow` every cross-origin ttyd iframe needs. Chrome gates
 * `navigator.clipboard.writeText` inside a cross-origin frame on the
 * `clipboard-write` permissions policy, which only the embedder can grant —
 * and the policy is fixed at navigation, so set it BEFORE `src`.
 */
export const TTYD_FRAME_ALLOW = 'clipboard-write';

const OSC_CLIPBOARD = 52;

/** OSC 52's selection field: clipboard, primary, secondary, select, cut buffers. */
const SELECTION_RE = /^[cpqs0-7]*$/;

/**
 * The text an OSC 52 WRITE carries, or null for anything else.
 *
 * `data` is what xterm hands an OSC handler — everything after `52;`, i.e.
 * `<selection>;<base64>`. Null for a read request (`?`), an empty payload, a
 * malformed selection or bad base64. Bytes decode as UTF-8, replacing any
 * invalid sequence rather than dropping the whole copy.
 */
export function decode_osc52_write(data: string): string | null {
    const sep = data.indexOf(';');
    if (sep < 0) return null;
    const selection = data.slice(0, sep);
    const payload = data.slice(sep + 1);
    if (!SELECTION_RE.test(selection)) return null;
    if (payload === '' || payload === '?') return null;
    let binary: string;
    try {
        binary = atob(payload);
    } catch {
        return null;
    }
    const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
    return new TextDecoder('utf-8').decode(bytes);
}

/**
 * Copy every OSC 52 write `term` receives. `on_result` reports each attempt so
 * the surface can flash it — a copy that fails silently is the bug this fixes.
 * The handler claims every OSC 52, so xterm never falls through to a default.
 */
export function install_osc52_copy(
    term: Pick<Terminal, 'parser' | 'focus'>,
    on_result: (ok: boolean) => void,
): IDisposable {
    return term.parser.registerOscHandler(OSC_CLIPBOARD, (data) => {
        const text = decode_osc52_write(data);
        if (text !== null) {
            void write_clipboard(text, () => term.focus()).then(on_result);
        }
        return true;
    });
}
