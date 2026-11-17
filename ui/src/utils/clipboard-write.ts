/**
 * Put `text` on the clipboard: the async Clipboard API first, then a hidden
 * textarea + `execCommand('copy')` — the path that still works in a frame
 * embedded without clipboard permission, while the gesture's user activation is
 * live. The textarea takes focus, so `refocus` hands it back. Never throws.
 *
 * ONE owner: the terminal's OSC 52 copy (`engine/terminal/terminal-clipboard.ts`)
 * calls it, and so does any COPY button a page adds.
 */
export async function write_clipboard(text: string, refocus?: () => void): Promise<boolean> {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch {
        return exec_command_copy(text, refocus);
    }
}

function exec_command_copy(text: string, refocus?: () => void): boolean {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none;';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
        ok = document.execCommand('copy');
    } catch {
        ok = false;
    }
    area.remove();
    refocus?.();
    return ok;
}
