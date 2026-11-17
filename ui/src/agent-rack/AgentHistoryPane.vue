<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
    conversation_label, highlight_parts, history_status, read_conversation, refresh_history, search_history,
    type HistoryConversation, type HistoryHit, type HistorySearch,
} from './history-client.ts';

const props = defineProps<{ bridge_origin: string }>();
const emit = defineEmits<{ (event: 'close'): void }>();
const query = ref('');
const scope = ref<number | null>(null);
const result = ref<HistorySearch | null>(null);
const conversation = ref<HistoryConversation | null>(null);
const reading = ref(false);
const searching = ref(false);
const loading = ref(false);
const error = ref('');
const detail_error = ref('');
const refreshing = ref(false);
const refresh_note = ref('');
const chosen_seq = ref<number | null>(null);
const page = ref<HTMLElement | null>(null);
let search_request: AbortController | null = null;
let detail_request: AbortController | null = null;
const lifetime = new AbortController();
let debounce: ReturnType<typeof setTimeout> | undefined;
let refresh_timer: ReturnType<typeof setTimeout> | undefined;
const indexed_at = computed(() => result.value?.indexed_at ?? conversation.value?.indexed_at);

function stamp(ts: number | null | undefined): string {
    return ts == null ? 'Time unavailable' : new Date(ts * 1000).toLocaleString();
}

function failure(exc: unknown): string {
    return exc instanceof Error ? exc.message : 'History could not be loaded.';
}

async function search(offset = 0): Promise<void> {
    clearTimeout(debounce);
    search_request?.abort();
    const request = new AbortController();
    search_request = request;
    searching.value = true;
    error.value = '';
    try {
        const next = await search_history(props.bridge_origin, query.value, offset, scope.value, request.signal);
        if (request.signal.aborted) return;
        result.value = offset && result.value ? { ...next, hits: [...result.value.hits, ...next.hits] } : next;
    } catch (exc) {
        if (!request.signal.aborted) error.value = failure(exc);
    } finally {
        if (!request.signal.aborted) searching.value = false;
    }
}

watch([query, scope], () => {
    search_request?.abort();
    result.value = null;
    reading.value = false;
    searching.value = true;
    clearTimeout(debounce);
    debounce = setTimeout(() => void search(), 250);
});

async function open_conversation(file_id: number, mode: 'around' | 'before' | 'after', seq: number): Promise<void> {
    detail_request?.abort();
    const request = new AbortController();
    detail_request = request;
    loading.value = true;
    reading.value = true;
    detail_error.value = '';
    conversation.value = null;
    chosen_seq.value = mode === 'around' ? seq : null;
    try {
        const next = await read_conversation(props.bridge_origin, file_id, { mode, seq }, request.signal);
        if (request.signal.aborted) return;
        conversation.value = next;
        await nextTick();
        if (request.signal.aborted) return;
        const target = page.value?.querySelector<HTMLElement>(`[data-seq="${seq}"]`);
        if (target) target.scrollIntoView({ block: 'center' });
        else if (page.value) page.value.scrollTop = 0;
    } catch (exc) {
        if (!request.signal.aborted) detail_error.value = failure(exc);
    } finally {
        if (!request.signal.aborted) loading.value = false;
    }
}

function choose(hit: HistoryHit): void {
    void open_conversation(hit.file_id, 'around', hit.seq);
}

async function update_index(): Promise<void> {
    if (refreshing.value) return;
    refreshing.value = true;
    refresh_note.value = 'Refreshing index…';
    const started = Date.now();
    try {
        const before = await history_status(props.bridge_origin, lifetime.signal);
        const previous_run = before.scheduler?.last?.started_at;
        await refresh_history(props.bridge_origin, lifetime.signal);
        async function check(): Promise<void> {
            try {
                const state = await history_status(props.bridge_origin, lifetime.signal);
                if (lifetime.signal.aborted) return;
                const scheduler = state.scheduler;
                if (scheduler?.last && scheduler.last.started_at !== previous_run && !scheduler.child_running) {
                    refreshing.value = false;
                    refresh_note.value = scheduler.last.exit === 0 ? 'Index refreshed' : 'Index refresh failed; run tldrack ledger status.';
                    await search();
                    const current = conversation.value;
                    if (current?.messages.length) {
                        void open_conversation(current.session.file_id, 'around', chosen_seq.value ?? current.messages[0].seq);
                    }
                    return;
                }
                if (Date.now() - started > 300_000) {
                    refreshing.value = false;
                    refresh_note.value = 'Refresh is taking longer; run tldrack ledger status.';
                    return;
                }
                refresh_timer = setTimeout(() => void check(), 3000);
            } catch (exc) {
                if (!lifetime.signal.aborted) {
                    refreshing.value = false;
                    refresh_note.value = failure(exc);
                }
            }
        }
        if (!lifetime.signal.aborted) refresh_timer = setTimeout(() => void check(), 1000);
    } catch (exc) {
        if (!lifetime.signal.aborted) {
            refreshing.value = false;
            refresh_note.value = failure(exc);
        }
    }
}

onMounted(() => void search());
onBeforeUnmount(() => {
    clearTimeout(debounce);
    clearTimeout(refresh_timer);
    search_request?.abort();
    detail_request?.abort();
    lifetime.abort();
});
</script>

<template>
    <section class="history fui-panel" aria-label="Conversation history">
        <form class="history__bar" role="search" @submit.prevent="search()">
            <input v-model="query" class="history__search" type="search" maxlength="300"
                placeholder="Search conversations…" aria-label="Search conversation text" />
            <button type="submit" class="history__button">Find</button>
            <button type="button" class="fui-close" aria-label="Close history" @click="emit('close')">✕</button>
        </form>
        <div class="history__meta">
            <button v-if="scope !== null" class="history__button" @click="scope = null">This conversation ×</button>
            <span v-else class="fui-techtext">All conversations</span>
            <span :title="`Indexed ${stamp(indexed_at)} · automatic refresh every 15 minutes`">Indexed {{ stamp(indexed_at) }}</span>
            <button class="history__button" :disabled="refreshing" @click="update_index">{{ refreshing ? 'Refreshing…' : 'Refresh index' }}</button>
            <span v-if="refresh_note" role="status">{{ refresh_note }}</span>
        </div>
        <div class="history__body" :class="{ 'history__body--reading': reading }">
            <div class="history__results" :aria-busy="searching">
                <p v-if="error" class="fui-callout fui-callout--warn" role="alert">{{ error }}</p>
                <p v-if="searching" role="status">Searching…</p>
                <p v-else-if="result?.hits.length === 0">{{ query.trim() ? 'No matching messages.' : 'No indexed conversations yet.' }}</p>
                <button v-for="hit in result?.hits ?? []" :key="`${hit.file_id}:${hit.seq}`"
                    class="history__hit" :class="{ 'history__hit--selected': conversation?.session.file_id === hit.file_id && chosen_seq === hit.seq }"
                    @click="choose(hit)">
                    <strong>{{ conversation_label(hit) }}</strong>
                    <small>{{ hit.profile || hit.source }} · {{ hit.kind }} · {{ stamp(hit.ts) }}</small>
                    <span class="history__snippet"><template v-for="(part, i) in highlight_parts(hit.snippet, query)" :key="i"><mark v-if="part.match">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
                </button>
                <button v-if="result?.next_offset != null" class="history__button" :disabled="searching" @click="search(result.next_offset)">More results</button>
            </div>
            <div v-if="reading" class="history__reader" :aria-busy="loading">
                <div class="history__reader-head">
                    <button class="history__button" @click="reading = false">← Results</button>
                    <strong v-if="conversation">{{ conversation_label(conversation.session) }}</strong>
                    <button v-if="conversation && scope === null" class="history__button" @click="scope = conversation.session.file_id">Search this conversation</button>
                </div>
                <p v-if="detail_error" class="fui-callout fui-callout--warn" role="alert">{{ detail_error }}</p>
                <p v-if="loading" role="status">Loading conversation…</p>
                <div v-if="conversation" ref="page" class="history__messages">
                    <p v-if="conversation.session.cwd" class="history__path">{{ conversation.session.cwd }}</p>
                    <button v-if="conversation.before !== null" class="history__button" @click="open_conversation(conversation.session.file_id, 'before', conversation.before)">Earlier messages</button>
                    <article v-for="message in conversation.messages" :key="message.seq" :data-seq="message.seq"
                        :class="{ 'history__message--selected': message.seq === chosen_seq }">
                        <header><span class="fui-techtext">{{ message.role }}</span><time>{{ stamp(message.ts) }}</time></header>
                        <div class="history__text"><template v-for="(part, i) in highlight_parts(message.text, query)" :key="i"><mark v-if="part.match">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></div>
                    </article>
                    <p v-if="conversation.messages.length === 0">This conversation has no indexed messages.</p>
                    <button v-if="conversation.after !== null" class="history__button" @click="open_conversation(conversation.session.file_id, 'after', conversation.after)">Later messages</button>
                </div>
            </div>
        </div>
        <footer class="history__limit">Indexed conversation text; tool output is excluded and messages over 20,000 characters are clipped.</footer>
    </section>
</template>

<style scoped>
.history { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--rack-base, #181a1d); color: var(--fui-text, #e4e7eb); font-family: Roboto, sans-serif; font-size: 0.85rem; }
.history__bar, .history__meta, .history__reader-head { display: flex; align-items: center; gap: 8px; padding: 8px; border-bottom: 1px solid var(--rack-border, #373a40); }
.history__meta { flex-wrap: wrap; color: var(--fui-muted, #adb2bc); font-size: 0.78rem; }
.history__search { flex: 1; min-width: 0; padding: 8px; color: inherit; background: transparent; border: 1px solid var(--rack-border, #373a40); font: inherit; }
.history__button { padding: 4px 8px; min-height: 28px; color: inherit; background: transparent; border: 1px solid var(--rack-border, #373a40); font: inherit; cursor: pointer; }
.history__button:hover, .history__hit:hover { background: #ffffff0d; }
.history__button:disabled { opacity: 0.5; cursor: default; }
.history__body { display: flex; flex: 1; min-height: 0; }
.history__results { flex: 1; min-width: 0; overflow: auto; }
.history__body--reading .history__results { flex: 0 0 32%; }
.history__hit { display: flex; flex-direction: column; gap: 5px; width: 100%; text-align: left; padding: 12px; border: 0; border-bottom: 1px solid var(--rack-border, #373a40); color: inherit; background: transparent; font: inherit; cursor: pointer; overflow-wrap: anywhere; }
.history__hit small, article header, .history__path { color: var(--fui-muted, #adb2bc); font-size: 0.78rem; }
.history__reader { flex: 1; display: flex; flex-direction: column; min-width: 0; border-left: 1px solid var(--rack-border, #373a40); }
.history__reader-head { flex-wrap: wrap; }
.history__reader-head strong { flex: 1; overflow-wrap: anywhere; }
.history__messages { overflow: auto; padding: 12px; }
.history__path { overflow-wrap: anywhere; }
article { padding: 12px 8px; border-bottom: 1px solid var(--rack-border, #373a40); }
article header { display: flex; gap: 12px; margin-bottom: 8px; }
.history__text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; user-select: text; }
.history__hit--selected, .history__message--selected { box-shadow: inset 2px 0 var(--fui-accent, #a9bacd); }
mark { background: #f8c14640; color: inherit; }
.history__limit { padding: 6px 8px; font-size: 0.78rem; color: var(--fui-muted, #adb2bc); border-top: 1px solid var(--rack-border, #373a40); }
@media (max-width: 900px) { .history__body--reading .history__results { display: none; } }
@media (pointer: coarse) { .history__button, .history .fui-close { min-height: 44px; min-width: 44px; } }
</style>
