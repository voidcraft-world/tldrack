/**
 * Mount the rack. Two functions, one per place it runs, so a page entry is one call and a host
 * application's own page is the same call with its additions.
 *
 * - `mount_rack_window`: the whole page, list beside the terminal (`AgentRackWindow.vue`).
 *   `index.html` mounts it on the bridge feed. A host that pops the rack out of its own main
 *   window mounts it with `feed: 'bus'`.
 * - `mount_rack_frame`: the list alone, for a host that embeds it in a frame and owns the
 *   terminal itself. It talks to `window.parent` (`rack-transport.ts`
 *   `create_iframe_transport`).
 *
 * Both take `settings`: a render function for the rack's settings tray, where a host puts its
 * own controls. This repository passes none.
 *
 * Deliberately no component library: the rack is rows and a few bracketed buttons, the shared
 * classes cover it, and the bundle stays small. No login either: the page talks only to a
 * bridge, whose guards are the gate.
 */
// First, before any module that reads storage as it loads: owned by storage/migrate-keys.ts.
import '../storage/migrate-keys.ts';
import '@fontsource/roboto/latin-400.css';
import '../styles/boot.ts';

import { createApp, h, ref, type App, type VNodeChild } from 'vue';
import AgentRackComponent from './AgentRackComponent.vue';
import AgentRackWindow from './AgentRackWindow.vue';
import { create_iframe_transport } from './rack-transport.ts';
import { with_launch_directories } from './launch-directories-client.ts';
import { bridge_origin } from '../config/bridge-origin.ts';
import { useWindowBus } from '../composables/useWindowBus.ts';
import {
    RENDER_OPTIONS_STORAGE_KEY,
    load_render_options,
    serialize_render_options,
    type TerminalRenderOptions,
} from '../engine/terminal/terminal-render-options.ts';

/** What a host renders in the settings tray. `bridge_origin` is set on a page that talks HTTP. */
export type RackSettingsSlot = (props: { open: boolean; bridge_origin?: string }) => VNodeChild;

export interface RackWindowOptions {
    /** `AgentRackWindow.vue` owns the two meanings. Default `bridge`. */
    feed?: 'bridge' | 'bus';
    settings?: RackSettingsSlot;
}

export function mount_rack_window(target: string | Element, options: RackWindowOptions = {}): App {
    const feed = options.feed ?? 'bridge';
    // Before ANY bus traffic: a host's main window reads the role of whoever posts, and takes a
    // window still labelled `main` for a second copy of itself.
    if (feed === 'bus') useWindowBus().set_role('companion');
    const app = createApp({
        render: () => h(AgentRackWindow, { feed }, options.settings ? { settings: options.settings } : {}),
    });
    app.mount(target);
    return app;
}

export function mount_rack_frame(target: string | Element, options: { settings?: RackSettingsSlot } = {}): App {
    const transport = with_launch_directories(create_iframe_transport(), (path) => `${bridge_origin()}${path}`);
    // A render-function ROOT, not `createApp(AgentRackComponent, props)`: root props are a plain
    // static object, which Vue neither tracks nor unwraps, so the FONT and RENDER controls would
    // freeze at their first value. This wrapper owns the state and re-renders on change.
    const app = createApp({
        setup() {
            // This page does NOT own the terminal: the frame beside it is the host's. A change is
            // sent home (`render`), and the host applies and persists it.
            const render_options = ref<TerminalRenderOptions>(load_render_options());
            /** The last value sent home, so the host's write-back is told apart from a change
             *  that started somewhere else. */
            let last_sent = '';

            // The `storage` event keeps the slider honest: a same-origin write is delivered to
            // every OTHER document, so the host saving after a ⌘± keypress (which the terminal
            // forwards to the host, not to this page) lands here as an update.
            //
            // The echo guard matters during a DRAG. Every slider tick sends, the host saves, and
            // the event comes back asynchronously; a late echo would snap the handle back to a
            // position the operator had already dragged past. Compared as EFFECTIVE options
            // (`serialize_render_options`), never as the raw `event.newValue`: storage holds only
            // the touched fields, so the raw value can never equal the full set that was sent.
            window.addEventListener('storage', (event) => {
                if (event.key !== RENDER_OPTIONS_STORAGE_KEY) return;
                const incoming = load_render_options();
                if (serialize_render_options(incoming) === last_sent) return;
                render_options.value = incoming;
            });

            return () =>
                h(AgentRackComponent, {
                    transport,
                    render_options: render_options.value,
                    'onUpdate:render_options': (next: TerminalRenderOptions) => {
                        render_options.value = next; // optimistic, so the slider tracks the drag
                        last_sent = serialize_render_options(next);
                        transport.send('render', undefined, JSON.stringify(next));
                    },
                }, options.settings ? { settings: options.settings } : {});
        },
    });
    app.mount(target);
    return app;
}
