<template>
    <section v-if="open" class="lc" aria-label="This turn, in order">
        <p v-if="loading" class="lc-note" role="status">Finding the conversation…</p>
        <template v-else-if="context || written !== null">
            <p v-if="written !== null" class="lc-note">Written test case: there is no session or conversation behind it.</p>
            <template v-else-if="context && !context.ok">
                <p class="lc-note">{{ context.message }}</p>
                <button type="button" class="board-btn lc-retry" :disabled="busy" @click.stop="retry">
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ busy ? 'INDEXING…' : 'REFRESH INDEX' }}
                </button>
            </template>
            <template v-for="b in blocks" :key="b.key">
                <button
                    v-if="b.kind === 'more'"
                    type="button"
                    class="lc-more"
                    :disabled="b.loads && busy"
                    :aria-expanded="b.expanded"
                    :title="b.title"
                    @click.stop="b.press"
                >{{ b.label }}</button>
                <p v-else-if="b.kind === 'woke'" class="lr-wake" :title="b.note.title">
                    <span class="lr-wake__tag">{{ b.note.tag }}</span>{{ b.note.line }}.<template v-if="b.ask_follows"> Your last prompt:</template>
                </p>
                <article v-else class="lc-msg" :class="b.variant ? `lc-msg--${b.variant}` : ''">
                    <p class="lc-head" :title="b.title">{{ b.head }}<span v-if="b.age" class="lc-age"> · {{ b.age }}</span></p>
                    <AgentMessage :text="b.text" mode="full" />
                </article>
            </template>
            <template v-if="read_note">
                <FuiDisclosure v-model="show_read">{{ read_note }}</FuiDisclosure>
                <!-- Laya's own input, verbatim: the fitted state is not a message, so it is never rendered as one. -->
                <pre v-if="show_read" class="lc-read">{{ text }}</pre>
            </template>
        </template>
        <p v-if="error" class="lc-note" role="status">{{ error }}</p>
    </section>
</template>

<script setup lang="ts">
/**
 * One judged turn, read top to bottom: `⋯ N earlier messages` (loaded on demand), YOU ASKED (the operator's prompt
 * behind the turn, with what woke it when that was not the operator), `⋯ k messages in this turn` (the agent's
 * progress, folded), then the CLOSING message Laya scored — the ledger's full text, not the row's fitted tail — and,
 * when they differ, `▸ What Laya read`. Revealed, the earlier messages keep time order around YOU ASKED (an agent's
 * earlier answer to it draws after it, then what woke this turn). The thread's title and first message live in its head
 * (`LayaThreadHeader.vue`), so they never repeat here. No inner scroller of its own: the page scrolls, and the grade
 * controls stay pinned beside it (the deck caps its card's closing: `LayaReviewDeck.vue`).
 *
 * `part: 'turn'` is RECENT's: its conversation already prints each turn's prompt above the turn, and the turns before it,
 * so the reader draws only this turn — `⋯ k messages in this turn`, the CLOSING, `▸ What Laya read`.
 *
 * Where the messages come from (the join's link, else the needle search), and why in that order: owned by
 * `turn-context.ts` — see the note there. Every message renders through `AgentMessage.vue` (sanitized markdown: owned
 * by `agent-markdown.ts` — see the note there).
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import type { HistoryTurn } from '@tldrack/schemas/agent-history.ts';

import FuiDisclosure from './FuiDisclosure.vue';
import AgentMessage from './AgentMessage.vue';
import { ts_age_text } from './recent-model.ts';
import type { FoundHead } from './thread-model.ts';
import {
    context_source,
    laya_read_note,
    load_earlier,
    load_linked_context,
    load_turn_context,
    prepend_context,
    reader_layout,
    refresh_index,
    role_head,
    wake_note,
    type ReaderMessage,
    type TurnContext,
    type WakeNote,
} from './turn-context.ts';

/**
 * `text`: what Laya read (the row's or card's own text). `turn`: the join's answer for this verdict — undefined while
 * unasked, null when the join is out. `joining`: the join is in flight, so wait for it rather than race it.
 * `written`: a golden case's own request ('' when it has none), null for a real turn. A written case has no
 * conversation, so nothing is looked up: the reader draws its request and the closing, and says why that is all.
 */
const props = withDefaults(
    defineProps<{ text: string; at: string | null; open: boolean; turn?: HistoryTurn | null; joining?: boolean; now?: number; part?: 'whole' | 'turn'; written?: string | null }>(),
    { turn: undefined, joining: false, now: undefined, part: 'whole', written: null },
);
const emit = defineEmits<{ found: [found: FoundHead] }>();
const context = ref<TurnContext | null>(null);
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const show_earlier = ref(false);
const show_turn = ref(false);
const show_read = ref(false);

const linked = computed(() => (context_source(props.turn, false) === 'linked' ? props.turn ?? null : null));
const linked_tail = computed(() => linked.value?.closing_tail ?? null);
const layout = computed(() => {
    const current = context.value;
    if (!current?.ok) return null;
    const turn = linked.value;
    return reader_layout(current.messages, current.closing_seq, turn ? { request_seq: turn.request?.seq ?? null, ask: turn.ask ?? undefined } : {});
});
const closing_text = computed(() => layout.value?.closing?.text ?? linked_tail.value ?? props.text);
const read_note = computed(() => laya_read_note(props.text, layout.value?.closing?.text ?? null));
/** Messages before the turn that are loaded, on either side of the ask. */
const earlier_loaded = computed(() => (layout.value ? layout.value.earlier.length + layout.value.after_ask.length : 0));
/** Messages before the turn that are not loaded yet. */
const hidden_earlier = computed(() => {
    const current = context.value;
    const at = layout.value;
    if (!current?.ok || current.before === null || !at) return 0;
    return Math.max(0, at.earlier_count - earlier_loaded.value);
});
/** Pages a reveal may read to reach the messages before a long turn: a turn of 90 is three pages back. */
const REVEAL_PAGES = 3;

/** The reader, in order, as one list: every message draws through the same markup. */
type Block =
    | { kind: 'more'; key: string; label: string; title?: string; expanded?: boolean; loads: boolean; press: () => void }
    | { kind: 'woke'; key: string; note: WakeNote; ask_follows: boolean }
    | { kind: 'msg'; key: string; head: string; age: string | null; title?: string; variant?: 'ask' | 'closing'; text: string };

const LOAD_TITLE = 'Load the previous 30 messages';
/** "Graded" on this page means the operator's grade, so the message Laya scored never says it. */
const CLOSING_TITLE = 'The message Laya scored';
const READ_HEAD = 'CLOSING · AS LAYA READ IT';
const WRITTEN_TITLE = 'The request this test case was written with. Laya scores the closing alone';
const closing_head = computed(() => (props.part === 'turn' ? 'CLOSING' : 'AGENT · CLOSING'));

const blocks = computed<Block[]>(() => {
    const current = context.value;
    const at = layout.value;
    if (props.written !== null) {
        const closing: Block = { kind: 'msg', key: 'closing', head: READ_HEAD, title: CLOSING_TITLE, age: null, variant: 'closing', text: props.text };
        return props.written ? [{ kind: 'msg', key: 'ask', head: 'REQUEST · AS WRITTEN', title: WRITTEN_TITLE, age: null, variant: 'ask', text: props.written }, closing] : [closing];
    }
    if (!current) return [];
    if (!current.ok || !at) {
        // Not found: the closing still reads — the agent's own tail when linked, else what Laya read.
        const tail = linked_tail.value;
        return [{ kind: 'msg', key: 'closing', head: tail ? closing_head.value : READ_HEAD, title: CLOSING_TITLE, age: null, variant: 'closing', text: tail ?? props.text }];
    }
    const turn = linked.value;
    // The turn's opener heads by its turn's origin, not its role: owned by turn-context.ts `role_head` — see the note there.
    const msg = (m: ReaderMessage): Block => ({
        kind: 'msg', key: `m${m.seq}`, age: age_of(m.ts), text: m.text,
        head: role_head(m.role, turn && m.seq === turn.request?.seq ? turn.woke_by : null),
    });
    const out: Block[] = [];
    const ask: Block | null = at.ask
        ? { kind: 'msg', key: 'ask', head: 'YOU ASKED', title: 'The request this turn answered', age: age_of(at.ask.ts), variant: 'ask', text: at.ask.text }
        : null;
    const woke = (ask_follows: boolean): Block[] => {
        const note = wake_note(turn?.woke_by, turn?.request?.text);
        return note === null ? [] : [{ kind: 'woke', key: 'woke', note, ask_follows }];
    };
    // part 'turn': the conversation around it prints the prompt and the turns before, so only this turn's own draw here.
    const whole = props.part !== 'turn';
    if (whole && show_earlier.value) {
        // In time order: what came before the ask, the ask, the agent's earlier answers to it, then what woke this turn.
        if (hidden_earlier.value > 0) {
            out.push({ kind: 'more', key: 'earlier', label: busy.value ? 'Loading…' : `⋯ ${count_of(hidden_earlier.value, 'earlier message')}`, title: LOAD_TITLE, loads: true, press: () => void earlier() });
        }
        out.push(...at.earlier.map(msg), ...(ask ? [ask] : []), ...at.after_ask.map(msg), ...woke(false));
    } else if (whole) {
        if (at.earlier_count > 0) {
            out.push({ kind: 'more', key: 'reveal', label: `⋯ ${count_of(at.earlier_count, 'earlier message')}`, loads: false, press: () => void reveal_earlier() });
        }
        out.push(...woke(ask !== null), ...(ask ? [ask] : []));
    }
    if (at.in_turn_count > 0) {
        out.push({
            kind: 'more', key: 'turn', expanded: show_turn.value, loads: false,
            label: `${show_turn.value ? '▾' : '⋯'} ${count_of(at.in_turn_count, 'message')} in this turn`,
            press: () => (show_turn.value = !show_turn.value),
        });
    }
    if (show_turn.value) {
        const missing = at.in_turn_count - at.in_turn.length;
        if (missing > 0) {
            out.push({ kind: 'more', key: 'turn-rest', label: busy.value ? 'Loading…' : `⋯ ${count_of(missing, 'more message')} in this turn`, title: LOAD_TITLE, loads: true, press: () => void earlier() });
        }
        out.push(...at.in_turn.map(msg));
    }
    // part 'turn': the turn's own line right above already says Agent and its age.
    out.push({ kind: 'msg', key: 'closing', head: closing_head.value, title: CLOSING_TITLE, age: whole ? age_of(at.closing?.ts) : null, variant: 'closing', text: closing_text.value });
    return out;
});

function age_of(ts: number | null | undefined): string | null {
    return ts_age_text(ts, props.now ?? Date.now());
}

function count_of(n: number, noun: string): string {
    return `${n.toLocaleString('en-US')} ${noun}${n === 1 ? '' : 's'}`;
}

let generation = 0;
/** Waiting on the join: the watch on `joining` loads once it lands. */
let waiting = false;

async function load(): Promise<void> {
    if (props.written !== null) return;
    const source = context_source(props.turn, props.joining);
    waiting = source === 'wait';
    if (waiting) {
        loading.value = true;
        return;
    }
    const token = ++generation;
    loading.value = true;
    error.value = '';
    const found = source === 'linked' && props.turn ? await load_linked_context(props.turn) : await load_turn_context(props.text, props.at);
    if (token !== generation) return;
    context.value = found;
    loading.value = false;
    if (source === 'needle' && found.ok) emit('found', { title: found.title, first_user: found.first_user?.text ?? null });
}

async function reveal_earlier(): Promise<void> {
    show_earlier.value = true;
    const token = generation;
    for (let page = 0; page < REVEAL_PAGES && token === generation && !earlier_loaded.value && hidden_earlier.value > 0; page++) {
        if (!(await earlier())) return;
    }
}

/** One page further back; true when it loaded. */
async function earlier(): Promise<boolean> {
    const current = context.value;
    if (busy.value || !current?.ok || current.before === null) return false;
    const token = generation;
    busy.value = true;
    error.value = '';
    const page = await load_earlier(current.file_id, current.messages[0]?.seq ?? current.before);
    if (token !== generation) return false;
    busy.value = false;
    if (!page.ok) {
        error.value = page.message;
        return false;
    }
    const merged = prepend_context(current, page);
    context.value = merged;
    if (!linked.value) emit('found', { title: merged.title, first_user: merged.first_user?.text ?? null });
    return merged !== current;
}

async function retry(): Promise<void> {
    const token = generation;
    busy.value = true;
    const started = await refresh_index();
    if (token !== generation) return;
    if (started) {
        await new Promise((resolve) => setTimeout(resolve, 4_000));
        if (token !== generation) return;
        busy.value = false;
        await load();
        return;
    } else error.value = 'The bridge could not refresh the session index. Try again.';
    busy.value = false;
}

function reset(): void {
    generation++;
    waiting = false;
    context.value = null;
    loading.value = false;
    busy.value = false;
    error.value = '';
    show_earlier.value = false;
    show_turn.value = false;
    show_read.value = false;
}

/** A turn is the same turn while its text, time and link are: anything else starts over. */
const identity = computed(() => {
    const turn = linked.value;
    return `${props.at}\u0000${props.text}\u0000${turn ? `${turn.file_id}:${turn.closing_seq}` : ''}`;
});
watch(identity, () => {
    reset();
    if (props.open) void load();
});
// A reader that waited on the join loads once it lands, linked or not (a link that landed has already restarted it).
watch(() => props.joining, (now_joining) => {
    if (!now_joining && waiting) void load();
});
watch(() => props.open, (wanted) => {
    if (wanted && !context.value && !loading.value) void load();
}, { immediate: true });
onBeforeUnmount(() => { generation++; });
</script>

<style scoped>
/* Tokens: owned by laya-review.css — see the note there. */
.lc {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
    font-size: 0.92rem;
    line-height: 1.55;
}
.lc p {
    margin: 0;
}
.lc-note {
    color: var(--lr-ink-3);
    font-size: 0.85rem;
}
.lc-retry {
    align-self: flex-start;
}
/* A fold: a quiet dashed control, so it never reads as a grade. */
.lc-more {
    align-self: flex-start;
    min-height: 28px;
    padding: 3px 10px;
    border: 1px dashed var(--lr-well-line);
    border-radius: 3px;
    background: none;
    color: var(--lr-ink-2);
    font: 0.82rem/1.4 var(--lr-sans);
    cursor: pointer;
}
.lc-more:hover:not(:disabled) {
    border-color: var(--fui-accent);
    color: var(--lr-ink);
}
.lc-more:focus-visible {
    outline: 2px solid var(--fui-accent);
    outline-offset: 2px;
}
.lc-more:disabled {
    opacity: 0.6;
    cursor: default;
}
.lc-msg {
    min-width: 0;
    padding: 6px 10px;
    overflow-wrap: anywhere;
}
/* One line, then the message: who, then how long ago. */
.lc-head {
    margin-bottom: 4px;
    color: var(--lr-ink-3);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    line-height: 1.6;
}
.lc-age {
    font-family: var(--fui-mono);
    font-weight: 400;
    letter-spacing: 0;
}
.lc-msg--ask {
    border-radius: 3px;
    background: var(--lr-tint);
}
.lc-msg--closing {
    padding: 8px 12px;
    border-left: 3px solid var(--fui-accent);
    border-radius: 0 3px 3px 0;
    background: var(--lr-well);
}
.lc-read {
    margin: 0;
    padding: 8px 10px;
    border: 1px dashed var(--lr-well-line);
    border-radius: 3px;
    color: var(--lr-ink-2);
    font: 0.8rem/1.5 var(--fui-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
}
</style>
