/**
 * The three things every ttyd `<iframe>` embed owes the FONT setting, as one
 * composable, so two surfaces that embed a terminal cannot drift apart on them:
 *
 * 1. **Live font push.** `push_font_size()` posts `MESSAGE_TERMINAL_CONFIG` into
 *    the frame; the bridge's patched page resizes xterm in place, socket intact.
 *    `'*'` because the iframe can be cross-origin (ttyd's port against the
 *    page's origin), where an origin-scoped post would never arrive.
 * 2. **Re-push on every document load** (`on_frame_load`). Selecting a session
 *    renavigates the frame and the fresh page knows nothing about the size the
 *    operator picked.
 * 3. **⌘/Ctrl + = / − / 0 inside the terminal.** The patched page CONSUMES those
 *    keys (preventDefault + stopImmediatePropagation, so the browser never
 *    page-zooms) and posts the intent to `window.parent`. A surface with no
 *    listener swallows the keystroke and does nothing, which is worse than an
 *    unpatched page. The `event.source` check is not optional: a window also
 *    receives any other frame's posts.
 *
 * The caller owns the options ref and how a change is applied (persisted or
 * not, whether the URL renavigates) — this only turns a keystroke into `apply`.
 */
import { onBeforeUnmount, onMounted, type Ref } from 'vue';
import { MESSAGE_TERMINAL_CONFIG, MESSAGE_TERMINAL_FONT_STEP } from '../events/messages.ts';
import {
    DEFAULT_TERMINAL_FONT_SIZE,
    TERMINAL_FONT_STEP_PX,
    device_font_size,
    type TerminalRenderOptions,
} from '../engine/terminal/terminal-render-options.ts';

export interface TtydFontStepOptions {
    frame: Ref<HTMLIFrameElement | null>;
    get_options: () => TerminalRenderOptions;
    /** Receives the next options after a ⌘± / ⌘0; the caller clamps, persists and pushes. */
    apply: (next: TerminalRenderOptions) => void;
    /** 1 for a page. A host that draws the frame supersampled passes its factor. */
    dpi_scale: number;
}

export interface TtydFontStep {
    push_font_size(): void;
    on_frame_load(): void;
}

export function useTtydFontStep(opts: TtydFontStepOptions): TtydFontStep {
    function push_font_size(): void {
        const win = opts.frame.value?.contentWindow;
        if (!win) return;
        win.postMessage(
            { type: MESSAGE_TERMINAL_CONFIG, font_size: device_font_size(opts.get_options().font_size, opts.dpi_scale) },
            '*',
        );
    }

    function on_message(event: MessageEvent): void {
        if (event.source !== opts.frame.value?.contentWindow) return;
        if (event.data?.type !== MESSAGE_TERMINAL_FONT_STEP) return;
        const step = Number(event.data.step);
        const current = opts.get_options();
        if (step === 0) {
            opts.apply({ ...current, font_size: DEFAULT_TERMINAL_FONT_SIZE });
        } else if (step === 1 || step === -1) {
            opts.apply({ ...current, font_size: current.font_size + step * TERMINAL_FONT_STEP_PX });
        }
    }

    onMounted(() => window.addEventListener('message', on_message));
    onBeforeUnmount(() => window.removeEventListener('message', on_message));

    return { push_font_size, on_frame_load: push_font_size };
}
