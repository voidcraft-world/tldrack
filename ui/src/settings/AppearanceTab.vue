<template>
    <section class="fui-panel st-card" aria-labelledby="st-terminal-title">
        <div class="st-card__head">
            <h2 id="st-terminal-title" class="fui-techtext st-card__title">TERMINAL</h2>
        </div>
        <p class="st-card__about">
            How the rack’s terminal is drawn in this browser. The rack’s own ⚙ tray writes the same settings, so either one sets both.
            A rack that is already open reads them when its page loads: reload it to pick up a change made here.
        </p>

        <ul class="st-rows">
            <li class="st-row">
                <div class="st-row__body">
                    <label class="st-row__title" for="st-font-size">Font size</label>
                    <p class="st-row__text">In CSS pixels. In the terminal, ⌘/Ctrl with + or − steps it too.</p>
                </div>
                <div class="st-font">
                    <button type="button" class="board-btn" title="Smaller" aria-label="Smaller font" :disabled="options.font_size <= MIN_TERMINAL_FONT_SIZE" @click="step(-1)">
                        <span class="fui-corner tl"></span><span class="fui-corner br"></span>A−
                    </button>
                    <input
                        id="st-font-size"
                        class="st-range"
                        type="range"
                        :min="MIN_TERMINAL_FONT_SIZE"
                        :max="MAX_TERMINAL_FONT_SIZE"
                        step="1"
                        :value="options.font_size"
                        @input="apply({ font_size: clamp_font_size(($event.target as HTMLInputElement).value) })"
                    />
                    <button type="button" class="board-btn" title="Larger" aria-label="Larger font" :disabled="options.font_size >= MAX_TERMINAL_FONT_SIZE" @click="step(1)">
                        <span class="fui-corner tl"></span><span class="fui-corner br"></span>A+
                    </button>
                    <output class="st-readout" for="st-font-size">{{ options.font_size }}px</output>
                </div>
            </li>
            <li class="st-row">
                <div class="st-row__body">
                    <label class="st-row__title" for="st-renderer">Renderer</label>
                    <p class="st-row__text">
                        webgl is drawn on the GPU and is the fastest under heavy output. canvas draws on the CPU. dom uses the browser’s own text
                        rendering: the lightest on a GPU, and the slowest when an agent prints a lot.
                    </p>
                </div>
                <select id="st-renderer" class="st-select" :value="options.renderer" @change="set_renderer(($event.target as HTMLSelectElement).value)">
                    <option v-for="name in TERMINAL_RENDERERS" :key="name" :value="name">{{ name === 'webgl' ? 'webgl (GPU)' : name }}</option>
                </select>
            </li>
            <li class="st-row">
                <div class="st-row__body">
                    <label class="st-row__title" for="st-family">Typeface</label>
                    <p class="st-row__text">The fonts a Mac has. A family this machine lacks falls through to the next one in its stack.</p>
                </div>
                <!-- Bound to the STACK, so a stored value the menu does not offer leaves the control blank
                     and never claims to be the default (the rack's FAMILY menu keeps the same rule). -->
                <select id="st-family" class="st-select" :value="options.font_family" @change="apply({ font_family: ($event.target as HTMLSelectElement).value })">
                    <option v-for="font in TERMINAL_FONT_FAMILIES" :key="font.stack" :value="font.stack">{{ font.label }}</option>
                </select>
            </li>
            <li class="st-row">
                <div class="st-row__body">
                    <label class="st-row__title" for="st-theme">Theme</label>
                    <p class="st-row__text">The terminal’s colours, all sixteen ANSI slots included.</p>
                </div>
                <select id="st-theme" class="st-select" :value="options.theme" @change="set_theme(($event.target as HTMLSelectElement).value)">
                    <option v-for="name in TERMINAL_THEME_IDS" :key="name" :value="name">{{ name.toUpperCase() }}</option>
                </select>
            </li>
        </ul>

        <figure class="st-preview">
            <figcaption class="st-row__text">Preview, at this size, typeface and theme</figcaption>
            <pre class="st-preview__screen" :style="screen_style"><span :style="{ color: theme.green }">$</span> tldrack up
<span :style="{ color: theme.green }">✓</span> bridge   127.0.0.1:7682
<span :style="{ color: theme.yellow }">!</span> hooks    <span :style="{ color: theme.brightBlack }">out of date for 1 account</span>
<span :style="{ color: theme.red }">✗</span> laya     <span :style="{ color: theme.cyan }">not installed</span>
<span :style="{ color: theme.blue }">agent</span> <span :style="{ color: theme.magenta }">wt-settings</span> is waiting on you<span class="st-preview__cursor" :style="{ background: theme.cursor }"> </span></pre>
        </figure>
    </section>
</template>

<script setup lang="ts">
/**
 * APPEARANCE: the terminal's font size, renderer, typeface and theme.
 *
 * ONE STORE. These are the rack's render options, read and written through the same module the
 * rack's ⚙ tray uses, so there is no second copy to drift: owned by
 * `engine/terminal/terminal-render-options.ts` — see the note there (what is stored, and why
 * only the touched fields). This tab never reaches the bridge: it works with the bridge down.
 *
 * A change made in another tab (the rack's tray, a second settings page) arrives as a `storage`
 * event, and the controls follow it.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import {
    MAX_TERMINAL_FONT_SIZE,
    MIN_TERMINAL_FONT_SIZE,
    RENDER_OPTIONS_STORAGE_KEY,
    TERMINAL_FONT_FAMILIES,
    TERMINAL_FONT_STEP_PX,
    TERMINAL_RENDERERS,
    TERMINAL_THEMES,
    TERMINAL_THEME_IDS,
    clamp_font_size,
    clamp_render_options,
    load_render_options,
    save_render_options,
    type TerminalRenderOptions,
} from '../engine/terminal/terminal-render-options.ts';

const options = ref<TerminalRenderOptions>(load_render_options());

/** Every edit goes through the clamp, then the store: a value the terminal could not draw is never saved. */
function apply(patch: Partial<TerminalRenderOptions>): void {
    const next = clamp_render_options({ ...options.value, ...patch });
    options.value = next;
    save_render_options(next);
}

function step(direction: -1 | 1): void {
    apply({ font_size: clamp_font_size(options.value.font_size + direction * TERMINAL_FONT_STEP_PX) });
}

// A `<select>` hands back a string: the clamp is what decides whether it names a renderer or a theme.
function set_renderer(value: string): void {
    apply({ renderer: clamp_render_options({ ...options.value, renderer: value }).renderer });
}
function set_theme(value: string): void {
    apply({ theme: clamp_render_options({ ...options.value, theme: value }).theme });
}

const theme = computed(() => TERMINAL_THEMES[options.value.theme]);
const screen_style = computed(() => ({
    background: theme.value.background,
    color: theme.value.foreground,
    fontFamily: options.value.font_family,
    fontWeight: String(options.value.font_weight),
    fontSize: `${options.value.font_size}px`,
}));

/** `key === null` is a whole-storage clear, which is a change too. */
function on_storage(event: StorageEvent): void {
    if (event.key === RENDER_OPTIONS_STORAGE_KEY || event.key === null) options.value = load_render_options();
}
onMounted(() => window.addEventListener('storage', on_storage));
onBeforeUnmount(() => window.removeEventListener('storage', on_storage));
</script>
