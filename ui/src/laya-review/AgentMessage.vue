<template>
    <!-- v-html (both sites) is fed ONLY by `render_agent_markdown` (a private DOMPurify allowlist, then the Trusted Types
         policy): owned by `agent-markdown.ts` — see the note there. Until it loads, the message is plain text. -->
    <div class="am" :class="{ 'am--tail': mode === 'tail', 'am--head': mode === 'head', 'am--cut': mode !== 'full' && ((mode === 'tail' && tail_cut) || box_cut) }" :style="lines_style">
        <!-- A message with pastes: its own words in order, each paste folded to one line (`paste-blocks.ts`). -->
        <div v-if="parts" ref="body_el" class="am__body am__body--parts">
            <template v-for="part in parts" :key="part.key">
                <details v-if="part.kind === 'paste' && mode === 'full'" class="am-paste" @click.stop @keydown.enter.stop>
                    <summary class="am-paste__head" :title="paste_title(part.paste)">{{ paste_label(part.paste) }}</summary>
                    <pre class="am-paste__text">{{ part.paste.text }}</pre>
                </details>
                <!-- Here a press opens the message around it, not the paste: its tip says the size and promises nothing. -->
                <div v-else-if="part.kind === 'paste'" class="am-paste__head am-paste__head--line" :title="paste_size_title(part.paste)">{{ paste_label(part.paste) }}</div>
                <div v-else-if="part.html !== null" class="am__part" v-html="part.html"></div>
                <div v-else class="am__part am__body--plain">{{ part.text }}</div>
            </template>
        </div>
        <div v-else-if="rendered" ref="body_el" class="am__body" v-html="rendered.html"></div>
        <div v-else ref="body_el" class="am__body am__body--plain">{{ plain }}</div>
    </div>
</template>

<script lang="ts">
type AgentMarkdown = typeof import('./agent-markdown.ts');

/** The renderer once its chunk has loaded, so every message after the first renders in the same tick. */
let loaded: AgentMarkdown | null = null;
let loading: Promise<AgentMarkdown> | null = null;

/** The ONLY edge to `agent-markdown.ts`, and a dynamic one: why, owned by `agent-markdown.ts` — see the note there.
 *  A failed chunk fetch clears the promise, so the next message retries; until then text stays plain. */
function load_renderer(): Promise<AgentMarkdown> {
    loading ??= import('./agent-markdown.ts').then(
        (mod) => (loaded = mod),
        (error: unknown) => {
            loading = null;
            throw error;
        },
    );
    return loading;
}
</script>

<script setup lang="ts">
/**
 * THE way the Laya review page shows an agent's message, or the operator's: rendered markdown in `full` mode, its
 * last `tail_lines` lines in `tail` mode (a top fade when anything sits above them), or its first `head_lines` lines in
 * `head` mode (the last line shown fades out when anything sits below).
 *
 * Tail mode is a bottom-anchored clip (`column-reverse`) around ONE wrapper: with the rendered blocks as direct
 * children, `column-reverse` would print them in reverse order. Both clips are a HEIGHT, never `line-clamp`: a line
 * clamp skips any child that starts its own formatting context, so a `<pre>` or a `<table>` in a message drew in full.
 *
 * A pasted document folds to one `PASTED · N lines` line: a native `<details>` in `full` mode, the bare line in `tail`
 * and `head`, where a press belongs to the row that opens the whole message. Which pastes fold (a one-line dictation
 * reads as words), and how a paste is found: owned by `paste-blocks.ts` — see the note there. `clipped`: `text` is the
 * head of a longer message (the bridge's clip), so a paste it leaves open reads as at least its size. A message without
 * a paste renders exactly as before, as one body.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import { paste_label, paste_size_title, paste_title, split_pastes, tail_segments, type PasteBlock, type PasteSegment } from './paste-blocks.ts';

const props = withDefaults(defineProps<{ text: string; mode?: 'full' | 'tail' | 'head'; tail_lines?: number; head_lines?: number; clipped?: boolean }>(), {
    mode: 'full',
    tail_lines: 6,
    head_lines: 2,
    clipped: false,
});

/** One run of a message with pastes: its text (as HTML once the renderer has loaded), or one folded paste. */
type MessagePart = { kind: 'text'; key: string; text: string; html: string | null } | { kind: 'paste'; key: string; paste: PasteBlock };

const rendered = ref<{ html: string; cut: boolean } | null>(null);
const rendered_parts = ref<{ parts: MessagePart[]; cut: boolean } | null>(null);
const segments = computed(() => split_pastes(props.text ?? '', { clipped: props.clipped }));
const pasted = computed(() => segments.value.some((seg) => seg.kind === 'paste'));
/** The one-body text: the message, with any paste that reads as words already back in place (tags gone). */
const body_text = computed(() => segments.value.map((seg) => seg.text).join('\n\n'));
const plain = computed(() => body_text.value.trimEnd());
const tail_cut = computed(() => (rendered.value ?? rendered_parts.value)?.cut ?? false);

/** A run's part, keyed by its index in the WHOLE message, so a paste keeps its element (and its open state) when the
 *  plain parts give way to rendered ones, or a tail drops the runs above it. */
function part_of(seg: PasteSegment, index: number, html: string | null): MessagePart {
    return seg.kind === 'paste' ? { kind: 'paste', key: `p${index}`, paste: seg } : { kind: 'text', key: `t${index}`, text: seg.text.trimEnd(), html };
}

/** Null for a message without pastes (the one-body path). Until the renderer loads, every run, as plain text. */
const parts = computed<MessagePart[] | null>(() => {
    if (!pasted.value) return null;
    return rendered_parts.value?.parts ?? segments.value.map((seg, i) => part_of(seg, i, null));
});
const lines_style = computed(() => {
    if (props.mode === 'full') return undefined;
    return { '--am-lines': String(Math.max(1, props.mode === 'tail' ? props.tail_lines : props.head_lines)) };
});

/** Bumped per text: an answer for an older text never paints over a newer one. */
let seq = 0;

function paint(mod: AgentMarkdown): void {
    if (!pasted.value) {
        const { source, cut } = mod.agent_message_source(body_text.value, props.mode === 'tail' ? props.tail_lines : null);
        rendered.value = { html: mod.render_agent_markdown(source), cut };
        rendered_parts.value = null;
        return;
    }
    // The same budget `agent_message_source` gives a tail, spent from the last run back (a folded paste is one line).
    const all = segments.value;
    const { segments: shown, cut } =
        props.mode === 'tail'
            ? tail_segments(all, Math.max(1, props.tail_lines) * mod.TAIL_CHARS_PER_LINE, mod.TAIL_CHARS_PER_LINE, mod.tail_at_block)
            : { segments: all, cut: false };
    const offset = all.length - shown.length;
    rendered_parts.value = {
        parts: shown.map((seg, j) => part_of(seg, offset + j, seg.kind === 'text' ? mod.render_agent_markdown(mod.agent_message_source(seg.text, null).source) : null)),
        cut,
    };
    rendered.value = null;
}

watch(
    () => [props.text, props.mode, props.tail_lines, props.clipped] as const,
    () => {
        const mine = ++seq;
        if (loaded) {
            paint(loaded);
            return;
        }
        rendered.value = null;
        rendered_parts.value = null;
        load_renderer().then(
            (mod) => {
                if (mine === seq) paint(mod);
            },
            () => {
                // The message stays readable as plain text; a later message retries the chunk.
            },
        );
    },
    { immediate: true },
);

/** Whether the rendered (or plain) body runs past the tail or head box: CSS cannot ask, so a ResizeObserver measures. */
const body_el = ref<HTMLElement | null>(null);
const box_cut = ref(false);
let observer: ResizeObserver | null = null;

function measure(): void {
    const body = body_el.value;
    const box = body?.parentElement;
    box_cut.value = props.mode !== 'full' && !!body && !!box && body.offsetHeight > box.clientHeight + 1;
}

watch(
    [body_el, () => props.mode] as const,
    ([el, mode], previous) => {
        const old_el = previous?.[0];
        if (old_el && observer) observer.unobserve(old_el);
        if (!el || mode === 'full' || typeof ResizeObserver === 'undefined') {
            box_cut.value = false;
            return;
        }
        observer ??= new ResizeObserver(measure);
        observer.observe(el);
        measure();
    },
    { flush: 'post' },
);

onBeforeUnmount(() => {
    seq++;
    observer?.disconnect();
    observer = null;
});
</script>

<style scoped>
/* Prose for an agent's message, from the page's tokens only (`laya-review.css`), so LIGHT and dark both read. The
   rendered HTML is v-html, so every rule below reaches it through :deep(). Compact on purpose: a message sits in a
   row, a card or a reader, never on a page of its own. */
.am {
    min-width: 0;
    color: var(--lr-ink);
    font-size: var(--am-font-size, 0.93rem);
    line-height: 1.6;
    overflow-wrap: anywhere;
}
.am__body--plain {
    white-space: pre-wrap;
}

/* A message with pastes: its runs stack with a paragraph's gap, each part's own first and last margins dropped. */
.am__body--parts > * + * {
    margin-top: 0.55em;
}
.am__part > :deep(:first-child) {
    margin-top: 0;
}
.am__part > :deep(:last-child) {
    margin-bottom: 0;
}

/* A folded paste: one quiet mono line, the operator's words around it at full ink. */
.am-paste__head {
    display: inline-block;
    max-width: 100%;
    padding: 0 8px;
    border: 1px solid var(--lr-well-line);
    border-radius: 2px;
    color: var(--lr-ink-2);
    font: 0.72rem/1.6 var(--fui-mono);
    letter-spacing: 0.04em;
}
summary.am-paste__head {
    list-style: none;
    cursor: pointer;
    user-select: none;
}
summary.am-paste__head::-webkit-details-marker {
    display: none;
}
summary.am-paste__head::before {
    content: '▸ ';
}
.am-paste[open] > summary.am-paste__head::before {
    content: '▾ ';
}
summary.am-paste__head:hover {
    color: var(--lr-ink);
    border-color: var(--lr-rule);
}
summary.am-paste__head:focus-visible {
    outline: 2px solid var(--fui-accent);
    outline-offset: 1px;
}
/* Opened: the paste verbatim, in a well that scrolls inside itself. `.am` raises it over the markdown `pre` rule. */
.am .am-paste__text {
    max-height: 24rem;
    margin: 0.35em 0 0;
    padding: 8px 10px;
    overflow: auto;
    overscroll-behavior: contain;
    border: 1px solid var(--lr-well-line);
    border-radius: 3px;
    background: var(--lr-well);
    color: var(--lr-ink-2);
    font: 0.8rem/1.5 var(--fui-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
}

/* Tail: the last lines, anchored to the bottom; the overflow falls off the top. */
.am--tail {
    display: flex;
    flex-direction: column-reverse;
    max-height: calc(var(--am-lines, 6) * 1.6em);
    overflow: hidden;
}
.am--tail.am--cut {
    -webkit-mask-image: linear-gradient(to bottom, transparent, #000 1.6em);
    mask-image: linear-gradient(to bottom, transparent, #000 1.6em);
}

/* Head: the first lines, from the top. Cut, the lines above the last stay whole and the last fades out to the right,
   where an ellipsis would sit (a fade down a single line would leave it half-read). */
.am--head {
    max-height: calc(var(--am-lines, 2) * 1.6em);
    overflow: hidden;
}
.am--head.am--cut {
    -webkit-mask:
        linear-gradient(#000, #000) top / 100% calc(100% - 1.6em) no-repeat,
        linear-gradient(to left, transparent, #000 6em) bottom / 100% 1.6em no-repeat;
    mask:
        linear-gradient(#000, #000) top / 100% calc(100% - 1.6em) no-repeat,
        linear-gradient(to left, transparent, #000 6em) bottom / 100% 1.6em no-repeat;
}

.am__body > :deep(:first-child) {
    margin-top: 0;
}
.am__body > :deep(:last-child) {
    margin-bottom: 0;
}
.am :deep(p) {
    margin: 0 0 0.55em;
}
.am :deep(:is(h1, h2, h3, h4, h5, h6)) {
    margin: 0.75em 0 0.25em;
    color: var(--lr-ink);
    font-size: 1em;
    font-weight: 700;
    line-height: 1.35;
}
.am :deep(h1) {
    font-size: 1.05rem;
}
.am :deep(h2) {
    font-size: 1rem;
}
.am :deep(strong) {
    font-weight: 700;
}
.am :deep(del),
.am :deep(s) {
    color: var(--lr-ink-2);
}
.am :deep(:is(ul, ol)) {
    margin: 0.2em 0 0.6em;
    padding-left: 1.4em;
}
.am :deep(li) {
    margin: 0.15em 0;
}
.am :deep(li > :is(ul, ol)) {
    margin: 0.1em 0 0.2em;
}
.am :deep(li > p) {
    margin: 0.2em 0;
}
/* A task-list item: the glyph is the bullet. */
.am :deep(li:has(> .vc-task-box:first-child, > p:first-child > .vc-task-box:first-child)) {
    list-style: none;
}
.am :deep(.vc-task-box) {
    margin-right: 0.3em;
    color: var(--lr-ink-2);
}
.am :deep(a[href]) {
    color: var(--fui-accent);
    text-decoration: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 2px;
}
.am :deep(a[href]:hover) {
    text-decoration-thickness: 2px;
}
.am :deep(a[href]:focus-visible) {
    outline: 2px solid var(--fui-accent);
    outline-offset: 1px;
}
/* Inline code: a chip. */
.am :deep(code) {
    padding: 0 0.3em;
    border: 1px solid var(--lr-well-line);
    border-radius: 3px;
    background: var(--lr-tint);
    font-family: var(--fui-mono);
    font-size: 0.84em;
}
/* A code block: a well that scrolls sideways inside itself, never the page. */
.am :deep(pre) {
    max-width: 100%;
    margin: 0.4em 0 0.7em;
    padding: 8px 10px;
    overflow-x: auto;
    border: 1px solid var(--lr-well-line);
    border-radius: 3px;
    background: var(--lr-well);
    line-height: 1.45;
}
.am :deep(pre code) {
    padding: 0;
    border: 0;
    background: transparent;
    font-size: 0.82em;
    white-space: pre;
    overflow-wrap: normal;
}
.am :deep(blockquote) {
    margin: 0.4em 0 0.7em;
    padding: 0 0 0 0.8em;
    border-left: 3px solid var(--lr-rule);
    color: var(--lr-ink-2);
}
.am :deep(hr) {
    margin: 0.7em 0;
    border: 0;
    border-top: 1px solid var(--lr-well-line);
}
/* A table scrolls inside itself; a cell breaks only a word longer than the column could ever be. */
.am :deep(table) {
    display: block;
    max-width: 100%;
    margin: 0.4em 0 0.7em;
    overflow-x: auto;
    border-collapse: collapse;
    font-size: 0.92em;
}
.am :deep(:is(th, td)) {
    padding: 3px 8px;
    border: 1px solid var(--lr-well-line);
    overflow-wrap: break-word;
    vertical-align: top;
}
.am :deep(th) {
    background: var(--lr-tint);
    font-weight: 700;
}
.am :deep(th:not([align])) {
    text-align: left;
}
</style>
