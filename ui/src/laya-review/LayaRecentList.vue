<template>
    <section ref="root_el" class="rl" :class="{ 'rl--embedded': embedded }" aria-label="Recent decisions">
        <!-- The ONE live region: a grade, a move to another conversation, a failure. Never behind a v-if or a Teleport, so
             a change inside it is announced (a region mounted with its text already in it is not). -->
        <p class="rl-sr" role="status" aria-live="polite">{{ announce }}</p>
        <div class="lr-stats rl-strip">
            <span class="lr-fig">{{ tally.reviewed }} / {{ tally.n }} reviewed</span>
            <details class="rl-filters">
                <summary>Filter &amp; sort<span v-if="band"> · {{ band === 'reply' ? 'Unsure' : band === 'open' ? 'Open' : 'Done' }}</span><span v-if="unanswered_only"> · To grade</span><span v-if="order === 'edge'"> · Near threshold</span></summary>
                <div class="rl-filter-options">
                    <!-- The readouts are the filters: a press narrows the list to that mark, a second press clears it. -->
                    <button
                        v-for="r in readouts"
                        :key="r.band"
                        type="button"
                        class="board-btn rl-band"
                        :class="{ 'is-on': band === r.band, 'rl-band--warn': r.warn }"
                        :aria-pressed="band === r.band"
                        :title="r.title"
                        @click="set_band(band === r.band ? null : r.band)"
                    >
                        <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                        <span aria-hidden="true">{{ r.mark }}</span> {{ r.band === 'reply' ? 'Unsure' : r.band === 'open' ? 'Open' : 'Done' }} {{ r.text }}
                    </button>
                    <button
                        type="button"
                        class="board-btn rl-band"
                        :class="{ 'is-on': unanswered_only }"
                        :aria-pressed="unanswered_only"
                        title="Only the turns you have not graded — the page fills with more of them as you grade"
                        @click="set_todo(!unanswered_only)"
                    >
                        <span class="fui-corner tl"></span><span class="fui-corner br"></span>TO GRADE
                    </button>
                    <button
                        type="button"
                        class="board-btn rl-band"
                        :class="{ 'is-on': order === 'edge' }"
                        :aria-pressed="order === 'edge'"
                        :title="order === 'edge' ? 'Nearest a cut first, over every verdict kept: press for the newest first' : 'Rank every verdict kept by how close its score sits to the ⚠️ or ✅ cut: the near misses first'"
                        @click="toggle_order"
                    >
                        <span class="fui-corner tl"></span><span class="fui-corner br"></span>EDGE<kbd class="fui-key">E</kbd>
                    </button>
                </div>
            </details>
            <span v-if="needs" class="lr-fig rl-needs" :title="needs.title">⚠ NEEDS YOU {{ needs.text }}</span>
            <!-- The readout of the narrowing, and its way out: the rack's LABEL button, or a band's rack name, narrowed it. -->
            <button
                v-if="session"
                type="button"
                class="board-btn rl-band rl-session is-on"
                :title="`Only ${session.name}'s turns${aliases.length ? ` (also named ${aliases.join(', ')})` : ''}: press for every session`"
                @click="emit('session', null)"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ session_title }} ✕
            </button>
            <span class="lr-grow"></span>
            <LayaUndoButton :disabled="in_flight || undo_stack.length === 0" title="Take back this list’s last answer" @undo="undo" />
        </div>

        <VerifierCard />

        <details v-if="session && (name_view?.named || name_error)" class="rl-name-review" open>
            <summary>Review the session name</summary>
            <LayaNameCard :view="name_view" :error="name_error" :since="session.since" @labelled="(v) => (name_view = v)" />
        </details>

        <ul v-if="explain && !down && conv" class="lr-explain rl-explain" aria-label="What this view shows">
            <li>{{ explain_order(order, unanswered_only, rows.length, pool) }}</li>
            <li v-for="line in band_lines" :key="line">{{ line }}</li>
        </ul>
        <p v-else-if="!down && conv" class="lr-explain rl-explain" :title="[explain_order(order, unanswered_only, rows.length, pool), ...band_lines].join(' ')">
            {{ order === 'edge' ? 'Nearest a cut first' : 'Newest first' }}<template v-if="join_state === 'old_bridge' || join_state === 'unavailable'"> · grouped by rack name</template>
        </p>

        <LayaDownState v-if="down" :down="down" @retry="load" />

        <div v-else-if="loading && rows.length === 0" class="fui-techtext lr-loading" role="status">ASKING THE BRIDGE…</div>

        <PanelDownState v-else-if="rows.length === 0 && session" head="NO TURN JUDGED YET">
            <template #why>
                Laya judges {{ session_title }} each time a turn ends. One that ended while the bridge restarted is
                judged when the bridge is back. Press {{ session_title }} ✕ above for every session.
            </template>
        </PanelDownState>
        <PanelDownState v-else-if="rows.length === 0" head="NO LIVE DECISIONS">
            <template #why>No turn end has been judged yet: the ledger keeps them once Laya runs on the rack</template>
        </PanelDownState>

        <PanelDownState v-else-if="!conv" head="NOTHING HERE">
            <template #why>No turn matches the filter</template>
        </PanelDownState>

        <template v-else>
            <!-- ONE conversation at a time: this bar names it and walks to the others (thread-model.ts owns why). -->
            <nav ref="nav_el" class="rl-nav" :class="`lr-hue-${head.hue}`" aria-label="Conversations">
                <button
                    type="button"
                    class="board-btn rl-step"
                    :disabled="conv_at === 0 || loading"
                    title="The previous conversation, without grading this one"
                    aria-label="Previous conversation"
                    @click="step(-1)"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>◀<kbd class="fui-key">←</kbd>
                </button>
                <LayaThreadHeader class="rl-nav__head" :head="bar_head" compact :narrowable="!!bar_head.subject" @session="narrow" />
                <button
                    ref="count_el"
                    type="button"
                    class="board-btn rl-count"
                    :class="{ 'is-on': show_list }"
                    :aria-expanded="show_list"
                    :aria-controls="pick_id"
                    :title="`Conversation ${conv_at + 1} of ${bands.length}${has_older ? ', more older ones to load' : ''}${head.meta ? ` · ${head.meta}` : ''}: press to list them`"
                    @click="show_list ? close_list(true) : (show_list = true)"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ conv_at + 1 }} / {{ bands.length }}{{ has_older ? '+' : '' }}
                </button>
                <button
                    type="button"
                    class="board-btn rl-step"
                    :disabled="!can_next || loading_older || loading"
                    :title="conv_at + 1 < bands.length ? 'The next conversation, without grading this one' : 'Load older conversations'"
                    aria-label="Next conversation"
                    @click="step(1)"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ loading_older ? '…' : '▶' }}<kbd class="fui-key">→</kbd>
                </button>

                <!-- Inside the bar, so it opens right under it wherever the bar is stuck (an in-flow list opened at the
                     conversation's top, off screen once scrolled). -->
                <ol v-if="show_list" :id="pick_id" class="rl-pick" aria-label="Every conversation on this page">
                    <li v-for="(p, i) in picks" :key="p.key">
                        <button type="button" class="rl-pick__row" :class="[`lr-hue-${p.hue}`, { 'is-on': i === conv_at }]" :aria-current="i === conv_at" @click="pick(i)">
                            <span class="rl-pick__mono" :class="{ 'rl-pick__mono--loose': p.unlinked }" aria-hidden="true">{{ p.monogram }}</span>
                            <span class="rl-pick__title">{{ p.title }}</span>
                            <span class="rl-pick__meta">{{ [p.subject, p.todo ? `${p.todo} to grade` : 'graded', p.age].filter(Boolean).join(' · ') }}</span>
                        </button>
                    </li>
                    <li v-if="has_older">
                        <button type="button" class="board-btn rl-older" :disabled="loading_older || loading" @click="void load_older()">
                            <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ loading_older ? 'LOADING…' : 'LOAD OLDER' }}
                        </button>
                    </li>
                </ol>
            </nav>

            <article ref="conv_el" class="lr-thread rl-conv" :class="`lr-hue-${head.hue}`" :aria-label="`Conversation: ${head.title}`">
                <LayaPrompt v-if="first" class="rl-first" head="YOU, FIRST" :text="first.text" :chars="first.chars" :age="first_age" />
                <ol class="rl-groups">
                    <template v-for="g in groups" :key="g.key">
                        <li v-if="g.skipped" class="rl-gap">⋯ {{ g.skipped }} more prompt{{ g.skipped === 1 ? '' : 's' }} in between, none with a verdict on this page</li>
                        <li class="rl-group">
                            <LayaPrompt v-if="g.anchor" :head="group_head(g)" :text="g.anchor.text" :chars="g.anchor.chars ?? null" :age="ts_age_text(g.anchor.ts, now)" :other="g.by === 'other'" />
                            <ol class="lr-turns">
                                <li
                                    v-for="row in g.rows"
                                    :key="row.decision_id"
                                    :data-id="row.decision_id"
                                    class="lr-turn"
                                    :class="{ 'lr-turn--on': row.decision_id === cursor_row?.decision_id }"
                                    @click="set_cursor(row.decision_id)"
                                >
                                    <!-- Every row here is an agent turn scored by Laya: neither is printed per row (a string on every
                                         row carries nothing); the score's tooltip and accessible name say whose it is. -->
                                    <div class="lr-turn__line">
                                        <span class="lr-turn__age" :title="row.at">{{ age_text(row.at, now) }}</span>
                                        <span v-if="chip_of(row)" class="lr-chip" :title="`You graded it ${chip_of(row)?.text}`">
                                            {{ chip_of(row)?.label }}<template v-if="chip_of(row)?.grade"> · <span :class="`lr-chip--${chip_of(row)?.grade}`">{{ chip_of(row)?.grade === 'right' ? 'RIGHT' : 'WRONG' }}</span></template>
                                        </span>
                                        <span v-if="drawn_note(row)" class="fui-tag" title="The rack drew this before the ✅ band existed">{{ drawn_note(row) }}</span>
                                        <span class="lr-grow"></span>
                                        <span class="lr-turn__laya" role="img" :title="score_title(row)" :aria-label="`${row_mark_words(row.band, turn_of(row)?.asked)} · open-work score ${row.p.toFixed(2)}`">
                                            {{ row_mark(row.band, turn_of(row)?.asked) }} {{ row.p.toFixed(2) }}<span v-if="edge_note(row)" class="lr-turn__edge"> · near cut {{ edge_note(row)?.cut.toFixed(2) }}</span>
                                        </span>
                                    </div>
                                    <p v-if="wakes.get(row.decision_id)" class="lr-wake" :title="wakes.get(row.decision_id)?.title">
                                        <span class="lr-wake__tag">{{ wakes.get(row.decision_id)?.tag }}</span>{{ wakes.get(row.decision_id)?.line }}
                                    </p>
                                    <div class="lr-turn__body" :class="{ 'lr-turn__body--open': opened_id === row.decision_id }">
                                        <div class="lr-turn__main">
                                            <LayaConversation
                                                v-if="opened_id === row.decision_id"
                                                part="turn"
                                                :text="row.text"
                                                :at="row.at"
                                                :open="true"
                                                :turn="turn_for(row)"
                                                :joining="joining_for(row)"
                                                :now="now"
                                                @found="(f) => found.set(row.decision_id, f)"
                                            />
                                            <!-- The LAST lines, never the first: Laya judges a turn by how it ends (the question, the "say go"). -->
                                            <AgentMessage v-else class="lr-turn__preview" :text="preview_text(row, turn_of(row))" mode="tail" :tail_lines="PREVIEW_LINES" />
                                        </div>
                                        <!-- The ONE grade group, on the cursor turn; at phone width it rides the page's bottom bar instead. -->
                                        <Teleport v-if="row.decision_id === cursor_row?.decision_id" :to="bar_el ?? 'body'" :disabled="!pinned_bar || !bar_el" defer>
                                            <div class="lr-grades" role="group" aria-label="Grade this turn">
                                                <button
                                                    v-for="v in RECENT_VERDICTS"
                                                    :key="v.verdict"
                                                    type="button"
                                                    class="board-btn lr-grade"
                                                    :class="{ 'is-on': operator_verdict(row) === v.verdict }"
                                                    :data-verdict="v.verdict"
                                                    :aria-pressed="operator_verdict(row) === v.verdict"
                                                    :disabled="in_flight || row.reference_open === null"
                                                    :title="row.reference_open === null ? 'No current card holds this text' : v.title"
                                                    @click.stop="answer(row, v.verdict)"
                                                >
                                                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                                                    <span class="lr-grade__label">{{ v.label }}<kbd class="fui-key">{{ v.key }}</kbd></span>
                                                    <span v-if="explain" class="lr-grade__cap">{{ v.caption }}</span>
                                                </button>
                                                <LayaExampleButton
                                                    :key="`${surface_name}:${row.key}`"
                                                    :surface="surface_name"
                                                    :item_key="row.key"
                                                    :marked="row.great_example ?? false"
                                                    :disabled="in_flight || row.reference_open === null"
                                                    @saved="(marked) => { rows = rows.map((r) => r.key === row.key ? { ...r, great_example: marked } : r); }"
                                                />
                                                <!-- What it says, seen; `announce` above is what assistive tech hears. -->
                                                <div class="lr-status" :class="{ 'lr-status--bad': status_line && !status_line.ok }" aria-hidden="true">{{ status_line?.text ?? '' }}</div>
                                            </div>
                                        </Teleport>
                                    </div>
                                </li>
                            </ol>
                        </li>
                    </template>
                </ol>
                <footer v-if="todo_count(conv) === 0" class="rl-end">
                    <span class="rl-end__done">✓ Every turn here is graded</span>
                    <button v-if="next_band !== null || has_older" type="button" class="board-btn rl-older" :disabled="loading_older || loading" @click="void advance()">
                        <span class="fui-corner tl"></span><span class="fui-corner br"></span>NEXT TO GRADE ▶
                    </button>
                    <span v-else class="rl-end__none">{{ end_text(behind_band !== null) }}</span>
                </footer>
            </article>
        </template>

        <div ref="bar_el" class="lr-gradebar" :class="{ 'lr-gradebar--on': pinned_bar && !!cursor_row }"></div>
        <div v-if="status_line && !cursor_row" class="lr-status" :class="{ 'lr-status--bad': !status_line.ok }" aria-hidden="true">{{ status_line.text }}</div>
    </section>
</template>

<script setup lang="ts">
import VerifierCard from './VerifierCard.vue';
/**
 * The REVIEW page's RECENT view: the newest live verdicts from the agent rack, ONE conversation at a time. ◀ ▶ (← →)
 * walk the conversations without grading; inside one, its turns read oldest first under the prompt each answered, and
 * J/K walk them. The cursor turn opens in place into its reader (`LayaConversation.vue`, its `turn` part) and carries
 * the only grade buttons on the page; grading the conversation's last open turn moves on to the next one with turns to
 * grade. `thread-model.ts` owns what a conversation is, its order and its groups, why one at a time, and where a grade
 * moves the cursor (`after_grade`); the rules are `recent-model.ts`; the wire is `decide/laya-review-client.ts`, and
 * the thread join is `turn-context.ts`. Answers go to the same operator log as the deck's (`/review/answer`), so they
 * feed the sweep and PROMOTE exactly as deck answers do.
 *
 * Message text renders through `AgentMessage.vue` (sanitized markdown: owned by `agent-markdown.ts` — see the note
 * there), never as `v-html` here.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch, type Ref } from 'vue';
import type { HistoryThreads, HistoryTurn } from '@tldrack/schemas/agent-history.ts';
import { REVIEW_RECENT_DEFAULT, type ReviewBand, type ReviewRecentOrder, type ReviewRecentRow } from '@tldrack/schemas/laya-review.ts';
import type { TopicLabelsView } from '@tldrack/schemas/topic-labels.ts';

import PanelDownState from './PanelDownState.vue';
import { fail_text, load_review_recent, post_review_answer, post_review_undo } from '../decide/laya-review-client.ts';
import { local_access_state } from '../engine/terminal/local-network-gate.ts';
import AgentMessage from './AgentMessage.vue';
import LayaExampleButton from './LayaExampleButton.vue';
import LayaConversation from './LayaConversation.vue';
import LayaDownState from './LayaDownState.vue';
import LayaNameCard from './LayaNameCard.vue';
import LayaPrompt from './LayaPrompt.vue';
import LayaThreadHeader from './LayaThreadHeader.vue';
import LayaUndoButton from './LayaUndoButton.vue';
import type { ReviewSession } from './open.ts';
import { deck_down } from './review-deck-model.ts';
import { load_topic_labels } from './topic-labels-client.ts';
import { use_laya_view } from './view-state.ts';
import {
    BAND_WORDS,
    RECENT_VERDICTS,
    age_text,
    answer_request,
    band_readouts,
    chip_text,
    drawn_note,
    edge_note,
    explain_order,
    operator_verdict,
    needs_readout,
    recent_key,
    recent_tally,
    review_title,
    row_mark,
    row_mark_words,
    scrolls_itself,
    ts_age_text,
    verdict_chip,
    visible_rows,
    with_answer,
    type RecentVerdict,
} from './recent-model.ts';
import {
    after_grade,
    after_grade_text,
    band_head,
    band_holding,
    band_title,
    end_text,
    explain_bands,
    first_in_groups,
    group_head,
    left_after_grade,
    monogram,
    next_todo_band,
    next_todo_from,
    opening_band,
    opening_row,
    pager_at,
    preview_text,
    thread_bands,
    thread_hue,
    threads_ask_of,
    todo_behind,
    todo_count,
    turn_groups,
    type AfterGrade,
    type FoundHead,
    type GradedAt,
    type JoinState,
    type ThreadBand,
    type ThreadHead,
    type TurnGroup,
} from './thread-model.ts';
import { freshen_threads, load_threads, wake_note, type WakeNote } from './turn-context.ts';

/** `order` and `session` are the deck's (it keeps them in the address bar); this list asks for them and reports a
 *  press. `explain` is the deck's EXPLAIN; `embedded` keeps the grades on the turn (a host panel fits its content,
 *  so a bottom bar would sit after every row). */
const props = withDefaults(
    defineProps<{ surface: string | null; order?: ReviewRecentOrder; session?: ReviewSession | null; explain?: boolean; embedded?: boolean }>(),
    { order: 'newest', session: null, explain: true, embedded: false },
);
const emit = defineEmits<{ surface: [surface: string | null]; order: [order: ReviewRecentOrder]; session: [session: ReviewSession | null] }>();

/** The preview's lines: the end of the closing message. */
const PREVIEW_LINES = 6;
/** Narrower than this, the grades leave the turn for the page's bottom bar (the deck's phone breakpoint). */
const NARROW_PX = 560;
/** One press walks at most this many older pages looking for a turn to grade, never the whole pool; NEXT TO GRADE
 *  looks further. */
const AUTO_PAGES = 3;

const rows = ref<ReviewRecentRow[]>([]);
const done_at = ref(0.4);
const { down, loading, in_flight, status_line } = use_laya_view(() => void load());
const band = ref<ReviewBand | null>(null);
const unanswered_only = ref(false);
const root_el = ref<HTMLElement | null>(null);
const nav_el = ref<HTMLElement | null>(null);
const count_el = ref<HTMLElement | null>(null);
const conv_el = ref<HTMLElement | null>(null);
const bar_el = ref<HTMLElement | null>(null);
const opened_id = ref<string | null>(null);
const undo_stack = ref<{ answer_id: string; key: string }[]>([]);
const now = ref(Date.now());
/** How many verdicts this page was cut from; null from a bridge that predates EDGE. */
const pool = ref<number | null>(null);
/** Where the next older page starts: the last page's `summary.next`, a PLACE in the order rather than a count, so an
 *  answer from any client or a new verdict never moves it (`review_data.py` `recent_rows` owns why). Null once
 *  nothing follows, and from a bridge that predates it. */
const next_cursor = ref<string | null>(null);
/** False once a bridge refused `after`, until the next load asks again (a restarted bridge takes it). */
const older_ok = ref(true);
const loading_older = ref(false);
const show_list = ref(false);
const pick_id = useId();
/** What the one live region says (`announce` in the template). */
const announce = ref('');
/** The narrowed session's NAME card, and the names it had before (its older verdicts were recorded under them). */
const name_view = ref<TopicLabelsView | null>(null);
const name_error = ref<string | null>(null);
const aliases = computed(() => name_view.value?.aliases ?? []);
const is_narrow = ref(false);
const pinned_bar = computed(() => is_narrow.value && !props.embedded);

const shown = computed(() => visible_rows(rows.value, band.value, unanswered_only.value));
const tally = computed(() => recent_tally(rows.value));
const readouts = computed(() => band_readouts(tally.value));
const needs = computed(() => needs_readout(tally.value));
const has_older = computed(() => older_ok.value && next_cursor.value !== null);

// ── conversations: the join, and the conversations drawn from it (thread-model.ts owns them) ──
const join = ref<HistoryThreads | null>(null);
const join_state = ref<JoinState>('loading');
/** What the reader's needle fallback found, per verdict, for a conversation the join did not name. */
const found = ref(new Map<string, FoundHead>());
let stop_fresh: () => void = () => {};

/** While a join is in flight the conversations keep the last one, but claim nothing about indexing until it lands. */
const bands = computed<ThreadBand[]>(() => {
    const list = thread_bands(shown.value, join.value, props.order);
    return join_state.value === 'ok' ? list : list.map((b) => ({ ...b, indexing: false }));
});
const band_of_row = computed(() => {
    const map = new Map<string, number>();
    bands.value.forEach((b, i) => b.rows.forEach((r) => map.set(r.decision_id, i)));
    return map;
});
function found_for(b: ThreadBand): FoundHead | null {
    return b.rows.map((r) => found.value.get(r.decision_id)).find(Boolean) ?? null;
}

// ── the cursor: one turn, by id; its conversation is the one shown ──
const cursor_id = ref<string | null>(null);
/** The conversation last shown: its place, its turns and its key. When the cursor's turn leaves the page (TO GRADE,
 *  a filter), the pager finds that conversation again by them (`pager_at`), and only then falls back to the place. */
let conv_last = 0;
let conv_ids: string[] = [];
let conv_key: string | null = null;
const conv_at = computed<number>(() => pager_at(bands.value, { cursor: cursor_id.value, ids: conv_ids, key: conv_key, last: conv_last }));
const conv = computed<ThreadBand | null>(() => bands.value[conv_at.value] ?? null);
const cursor_row = computed(() => {
    const b = conv.value;
    if (!b) return null;
    return b.rows.find((r) => r.decision_id === cursor_id.value) ?? opening_row(b);
});
const next_band = computed(() => next_todo_band(bands.value, conv_at.value));
const behind_band = computed(() => todo_behind(bands.value, conv_at.value));
const can_next = computed(() => conv_at.value + 1 < bands.value.length || has_older.value);

const NO_HEAD: ThreadHead = { monogram: '··', hue: 0, unlinked: true, title: 'Conversation', subject: null, meta: '', meta_title: '', first_user: null, first_clipped: false, tags: [] };
const head = computed<ThreadHead>(() => {
    const b = conv.value;
    return b ? band_head(b, { now: now.value, join_ok: join_state.value === 'ok', found: found_for(b) }) : NO_HEAD;
});
/** The bar's head: narrowed to a session, the strip's chip names it already, so the bar does not print it again. */
const bar_head = computed<ThreadHead>(() => (head.value.subject !== null && head.value.subject === props.session?.name ? { ...head.value, subject: null } : head.value));
const groups = computed<TurnGroup[]>(() => (conv.value ? turn_groups(conv.value, join.value) : []));
/** The first message prints above the turns unless a group already prints it as its prompt; `chars` when the bridge
 *  sent only its head. */
const first = computed<{ text: string; chars: number | null } | null>(() => {
    const clip = conv.value?.thread?.first_user;
    if (clip) return first_in_groups(groups.value, clip) ? null : { text: clip.text, chars: clip.chars ?? null };
    return head.value.first_user ? { text: head.value.first_user, chars: null } : null;
});
const first_age = computed(() => ts_age_text(conv.value?.thread?.first_user?.ts, now.value));
const band_lines = computed(() => explain_bands(conv.value ? [conv.value] : [], join_state.value, props.order));
const picks = computed(() => bands.value.map((b) => ({
    key: b.key,
    monogram: monogram(b),
    hue: thread_hue(b.identity),
    unlinked: b.kind === 'unlinked',
    title: band_title(b, found_for(b), now.value),
    // Narrowed to a session, every row would lead with its name: printed once, on the chip. An alias stays: it says which
    // older name these verdicts were filed under.
    subject: b.subject && b.subject !== props.session?.name ? b.subject : null,
    todo: todo_count(b),
    age: b.span ? age_text(new Date(b.span[1]).toISOString(), now.value) : null,
})));

function chip_of(row: ReviewRecentRow): { label: string; grade: 'right' | 'wrong' | null; text: string } | null {
    const chip = verdict_chip(row);
    return chip ? { ...chip, text: chip_text(chip) } : null;
}

function turn_of(row: ReviewRecentRow): HistoryTurn | null {
    return join.value?.decisions[row.decision_id] ?? null;
}
/** The reader's `turn`: undefined while the join is still out for it, null when there is none to have. */
function turn_for(row: ReviewRecentRow): HistoryTurn | null | undefined {
    return turn_of(row) ?? (join_state.value === 'loading' ? undefined : null);
}
function joining_for(row: ReviewRecentRow): boolean {
    return join_state.value === 'loading' && turn_of(row) === null;
}
/** `⟲ NOT YOUR PROMPT` on a turn the operator did not open (turn-context.ts `wake_note`), keyed by decision id; not on a
 *  rack-opened group's first turn, whose prompt head already says RACK. */
const wakes = computed(() => {
    const out = new Map<string, WakeNote>();
    for (const g of groups.value) {
        g.rows.forEach((row, at) => {
            const turn = turn_of(row);
            if (turn?.status !== 'linked' || (g.by === 'other' && at === 0)) return;
            const note = wake_note(turn.woke_by, turn.request?.text);
            if (note) out.set(row.decision_id, note);
        });
    }
    return out;
});

/** The chip names the narrowing (the rack's display name, else the session's), the bar the conversation: never the
 *  conversation's title twice. */
const session_title = computed(() => review_title(props.session?.name, name_view.value?.shown, null));
const surface_name = computed(() => props.surface ?? 'agent_open_work');

function score_title(row: ReviewRecentRow): string {
    const near = edge_note(row);
    return `Laya's score · ${BAND_WORDS[row.band]} · open-work score p ${row.p.toFixed(3)} · ✅ under ${(row.done_threshold ?? done_at.value).toFixed(2)} · ⚠️ from ${row.threshold.toFixed(2)}${near ? ` · ${near.title}` : ''} · ${row.model}${row.checkpoint ? ` · ${row.checkpoint}` : ''}`;
}

/** Bumped by every load: EDGE pressed twice quickly must show the second press's page, whichever answer lands last. */
let loads = 0;
/** Set by a change of order, session or surface: the page that lands opens on ITS first conversation to grade, even
 *  though the cursor watcher re-adopted a turn of the old page meanwhile. */
let reopen = false;

/** The NAME card first, when narrowed: it names the session's former names, and the list asks for those too. A card
 *  that cannot load (an older bridge, no data directory) says so and never blocks the list. */
async function load_name(): Promise<void> {
    const session = props.session;
    if (!session) {
        name_view.value = null;
        name_error.value = null;
        return;
    }
    name_view.value = null;
    name_error.value = null;
    const got = await load_topic_labels(session);
    if (props.session !== session) return;
    name_view.value = got.ok ? got.data : null;
    name_error.value = got.ok ? null : got.error;
}

function asked_view(after: string | null = null) {
    const session = props.session ? { ...props.session, aliases: aliases.value } : null;
    return { order: props.order, todo: unanswered_only.value, session, after };
}

async function load(): Promise<void> {
    const mine = ++loads;
    loading.value = true;
    if (!props.session || name_view.value?.session !== props.session.name) await load_name();
    if (mine !== loads) return;
    const asked = asked_view();
    const got = await load_review_recent(props.surface, REVIEW_RECENT_DEFAULT, asked);
    if (mine !== loads) return;
    loading.value = false;
    now.value = Date.now();
    if (!got.ok) {
        reopen = false;
        // A plugin from before the session filter refuses `subject`: never show every session under this one's chip.
        if (got.code === 'bad_query' && asked.session) {
            emit('session', null);
            status_line.value = { ok: false, text: 'This bridge predates the per-session list: restart the bridge' };
            return;
        }
        // A bridge from before EDGE and TO GRADE's fetch refuses the new names: fall back to the plain list and say why.
        if (got.code === 'bad_query' && (asked.order !== 'newest' || asked.todo)) {
            unanswered_only.value = false;
            if (props.order !== 'newest') emit('order', 'newest');
            status_line.value = { ok: false, text: 'This bridge predates EDGE: restart the bridge to rank by the cuts' };
            if (props.order === 'newest') void load();
            return;
        }
        down.value = deck_down(got, local_access_state());
        return;
    }
    down.value = null;
    rows.value = got.data.rows;
    pool.value = got.data.summary?.pool ?? null;
    next_cursor.value = got.data.summary?.next ?? null;
    older_ok.value = true;
    if (got.data.summary) done_at.value = got.data.summary.done_at;
    void load_join(mine);
    // A new order, session or surface opens on its first conversation to grade. Otherwise (TO GRADE pressed, a refresh)
    // the cursor's turn when it is still here, else its conversation, else the first to grade.
    if (reopen) go_to(opening_band(bands.value));
    else if (cursor_id.value === null || !band_of_row.value.has(cursor_id.value)) {
        const held = band_holding(bands.value, conv_ids, conv_key);
        go_to(held >= 0 ? held : opening_band(bands.value));
    }
    reopen = false;
    if (got.data.surface !== props.surface) emit('surface', got.data.surface);
}

/** ▶ past the last conversation, the list's LOAD OLDER, or a grade with nothing ahead: the next page of verdicts after
 *  `next_cursor`, appended. True when it brought a row this page did not have. Never while a full load is out: its
 *  page replaces this one, and the cursor it would follow belongs to the page it replaces. */
async function load_older(): Promise<boolean> {
    if (!has_older.value || loading_older.value || loading.value) return false;
    const mine = loads;
    loading_older.value = true;
    try {
        // A page holding only rows already here (a text judged again moved up): walk on, a few pages at most.
        for (let pass = 0; pass < AUTO_PAGES && has_older.value; pass++) {
            const got = await load_review_recent(props.surface, REVIEW_RECENT_DEFAULT, asked_view(next_cursor.value));
            if (mine !== loads) return false;
            if (!got.ok) {
                if (got.code === 'bad_query') older_ok.value = false;
                status_line.value = { ok: false, text: got.code === 'bad_query' ? 'This bridge predates LOAD OLDER: restart the bridge' : fail_text(got) };
                return false;
            }
            next_cursor.value = got.data.summary?.next ?? null;
            pool.value = got.data.summary?.pool ?? pool.value;
            const seen = new Set(rows.value.map((r) => r.decision_id));
            const fresh = got.data.rows.filter((r) => !seen.has(r.decision_id));
            if (fresh.length) {
                rows.value = [...rows.value, ...fresh];
                void load_join(mine);
                return true;
            }
        }
        return false;
    } finally {
        loading_older.value = false;
    }
}

/** Bumped by every join: only the newest one lands. Two can be out at once (LOAD OLDER pressed while the last page's
 *  join is still out, or a grade walking on through pages), and the newer asks for every row the older did. */
let joins = 0;

/** ONE join for the page: every verdict and every raw conversation on it (`threads_urls` splits a long ask). While it
 *  is out, the conversations keep the last join (a fresh page shares most rows with the one before); a failed one
 *  falls back to rack name and time. A superseded join is dropped and never starts a freshen of its own. */
async function load_join(mine: number): Promise<void> {
    const seq = ++joins;
    stop_fresh();
    stop_fresh = () => {};
    const ask = threads_ask_of(rows.value);
    join_state.value = 'loading';
    const got = await load_threads(ask);
    if (seq !== joins || mine !== loads) return;
    if (!got.ok) {
        join.value = null;
        join_state.value = got.reason;
        return;
    }
    join.value = got.data;
    join_state.value = 'ok';
    stop_fresh = freshen_threads(ask, got.data, (data) => {
        if (seq === joins && mine === loads) join.value = data;
    });
}

/** A band press narrows the page; the pager stays on the conversation shown while any of its turns is still in view
 *  (`pager_at`), on its first such turn. */
function set_band(next: ReviewBand | null): void {
    band.value = next;
}

function set_todo(next: boolean): void {
    unanswered_only.value = next;
    void load();
}

function toggle_order(): void {
    emit('order', props.order === 'edge' ? 'newest' : 'edge');
}

/** The rack name in the bar: narrow the page to that session. The rack's LABEL button knows when tmux made it; a row
 *  does not, so this narrowing reads every conversation the name held, one at a time. */
function narrow(name: string): void {
    if (props.session?.name !== name) emit('session', { name, since: null });
}

function set_cursor(id: string): void {
    cursor_id.value = id;
}

/** Show conversation `i`, on its first turn to grade. */
function go_to(i: number): void {
    const target = bands.value[i];
    if (show_list.value) close_list(!!nav_el.value?.contains(document.activeElement));
    if (!target) return;
    conv_last = i;
    cursor_id.value = opening_row(target)?.decision_id ?? null;
}

/** A row of the counter's list: that conversation, and focus back on the counter (the list it was in is gone). */
function pick(i: number): void {
    status_line.value = null;
    go_to(i);
    void nextTick(() => count_el.value?.focus());
}

function close_list(refocus: boolean): void {
    show_list.value = false;
    if (refocus) void nextTick(() => count_el.value?.focus());
}

/** ◀ ▶ and ← →: the neighbouring conversation, graded or not; past the last, the next page of older ones (its turns of
 *  the conversation shown join it, so this loads on until a new conversation arrives, a few pages at most). */
async function step(delta: -1 | 1): Promise<void> {
    if (loading.value) return;
    status_line.value = null;
    if (delta < 0) {
        if (conv_at.value > 0) go_to(conv_at.value - 1);
        return;
    }
    for (let pages = 0; conv_at.value + 1 >= bands.value.length; pages++) {
        if (pages >= AUTO_PAGES || !(await load_older())) return;
    }
    go_to(conv_at.value + 1);
}

/** Whether the status line holds a failure (read through a call: TypeScript would keep the `null` assigned before an
 *  await, though a failed page load sets it meanwhile). */
function load_said_why(): boolean {
    return status_line.value?.ok === false;
}

/** NEXT TO GRADE: the conversation shown when an older page added turns to grade to it, else the next one that has
 *  them, loading older pages while none is left on this one. */
async function advance(): Promise<void> {
    status_line.value = null;
    for (let pages = 0; ; pages++) {
        const to = next_todo_from(bands.value, conv_at.value);
        if (to !== null) {
            go_to(to);
            return;
        }
        if (pages >= AUTO_PAGES || !(await load_older())) break;
    }
    if (load_said_why()) return; // a page that failed to load said why
    status_line.value = { ok: true, text: has_older.value ? 'Nothing to grade in the next pages: press again to look further' : end_text(behind_band.value !== null) };
}

/** The cursor turn into view. It opens in place and grows down, so one that starts low on the screen comes up under the
 *  sticky bar, where its reader has room, and one already near the top stays put (its prompt above stays in view). The
 *  turn's scroll margins keep it clear of both sticky bars (`laya-review.css`). */
async function scroll_to(id: string | null): Promise<void> {
    if (id === null) return;
    await nextTick();
    const el = conv_el.value?.querySelector<HTMLElement>(`[data-id="${id}"]`);
    if (!el) return;
    if (props.embedded) {
        el.scrollIntoView({ block: 'nearest' });
        return;
    }
    const top = el.getBoundingClientRect().top;
    const under = nav_el.value?.getBoundingClientRect().bottom ?? 0;
    const above = pinned_bar.value ? (bar_el.value?.getBoundingClientRect().height ?? 0) : 0;
    const low = top > under + (window.innerHeight - above - under) * 0.4;
    el.scrollIntoView({ block: low || top < under ? 'start' : 'nearest' });
}

function toggle(row: ReviewRecentRow): void {
    opened_id.value = opened_id.value === row.decision_id ? null : row.decision_id;
}

/** One verdict on one row. A changed mind undoes the live answer first: the log keeps both, as the deck's UNDO does.
 *  Then the cursor moves on (`move_after_answer`), and a grade pressed from the keyboard keeps its focus. */
async function answer(row: ReviewRecentRow, verdict: RecentVerdict): Promise<void> {
    if (in_flight.value || operator_verdict(row) === verdict) return;
    // The group is drawn on the cursor turn, so moving the cursor re-creates it: focus would fall to <body>.
    const refocus = (document.activeElement as HTMLElement | null)?.closest('.lr-grades') ? verdict : null;
    in_flight.value = true;
    status_line.value = null;
    try {
        if (row.operator) {
            const undone = await post_review_undo({ surface: surface_name.value, answer_id: row.operator.answer_id });
            if (!undone.ok && undone.code !== 'already_undone' && undone.code !== 'unknown_answer') {
                status_line.value = { ok: false, text: fail_text(undone) };
                return;
            }
            rows.value = with_answer(rows.value, row.key, null);
        }
        let reference = row.reference_open;
        for (let attempt = 0; attempt < 2; attempt++) {
            const request = answer_request(row, surface_name.value, verdict, reference);
            if (!request) return;
            const got = await post_review_answer(request);
            if (got.ok && got.data.answer) {
                const ref = got.data.answer;
                // Who the conversation was, taken before the answer moves anything (TO GRADE takes the turn out).
                const was = conv.value;
                const graded: GradedAt = { ids: was?.rows.map((r) => r.decision_id) ?? [], key: was?.key ?? null, row, at: conv_at.value };
                rows.value = with_answer(rows.value, row.key, ref);
                undo_stack.value = [...undo_stack.value, { answer_id: ref.answer_id, key: row.key }];
                const chip = verdict_chip({ band: row.band, operator: ref });
                await move_after_answer(graded, chip ? `Graded ${chip_text(chip)}` : 'Graded');
                return;
            }
            if (!got.ok && got.code === 'reference_changed' && got.body?.card) {
                // The verdict is the operator's own words; only its agree/flip encoding moves with the reference.
                reference = got.body.card.reference.open;
                continue;
            }
            // Answered elsewhere (the deck, another tab, the phone): shown as it is. The next page needs no correction
            // for it: it starts at a place, not a count (`next_cursor`).
            if (!got.ok && got.code === 'already_answered' && got.body?.answer) {
                rows.value = with_answer(rows.value, row.key, got.body.answer);
            }
            status_line.value = { ok: false, text: got.ok ? got.data.message : fail_text(got) };
            return;
        }
    } finally {
        in_flight.value = false;
        if (refocus) void restore_focus(refocus);
    }
}

/** The same grade button on the group's new place, once it has mounted (the Teleport is `defer`). */
async function restore_focus(verdict: RecentVerdict): Promise<void> {
    await nextTick();
    const host = pinned_bar.value ? bar_el.value : conv_el.value;
    host?.querySelector<HTMLButtonElement>(`.lr-grades [data-verdict="${verdict}"]`)?.focus({ preventScroll: true });
}

/** Where the cursor goes after an answer (`thread-model.ts` `after_grade` owns the rule and its words): the
 *  conversation's next open turn, else the next conversation with one, loading older pages while none is ahead (their
 *  turns of THIS conversation join it, so it is checked first each time); else what is left behind it. */
async function move_after_answer(graded: GradedAt, text: string): Promise<void> {
    let step: AfterGrade | null = after_grade(bands.value, graded);
    for (let pages = 0; step === null && pages < AUTO_PAGES && (await load_older()); pages++) step = after_grade(bands.value, graded);
    const failed = step === null && status_line.value?.ok === false ? status_line.value.text : null;
    const more = step === null && has_older.value;
    step ??= left_after_grade(bands.value, graded);
    if (step.kind === 'same') cursor_id.value = step.row.decision_id;
    else if (step.kind === 'next' || (step.kind === 'behind' && !step.held)) go_to(step.band);
    // `held`: the graded conversation stays shown, and its footer says what is left. A page that failed to load says so.
    if (failed !== null) status_line.value = { ok: false, text: `${text}. ${failed}` };
    else status_line.value = { ok: true, text: more ? `${text}. Nothing to grade in the next pages yet: NEXT TO GRADE ▶ looks further` : after_grade_text(text, step) };
}

async function undo(): Promise<void> {
    const top = undo_stack.value.at(-1);
    if (!top || in_flight.value) return;
    in_flight.value = true;
    try {
        const got = await post_review_undo({ surface: surface_name.value, answer_id: top.answer_id });
        if (got.ok || got.code === 'already_undone' || got.code === 'unknown_answer') {
            undo_stack.value = undo_stack.value.slice(0, -1);
            rows.value = with_answer(rows.value, top.key, null);
            const back = rows.value.find((r) => r.key === top.key && band_of_row.value.has(r.decision_id));
            if (back) cursor_id.value = back.decision_id;
            status_line.value = { ok: true, text: 'Answer taken back' };
        } else {
            status_line.value = { ok: false, text: fail_text(got) };
        }
    } finally {
        in_flight.value = false;
    }
}

/** The last element pressed: a click into a code block that takes no focus leaves the keys on <body>, and this says the
 *  reader is in it. Cleared whenever the cursor moves. */
let last_press: Element | null = null;

function on_key(event: KeyboardEvent): void {
    if (event.code === 'Escape' && show_list.value) {
        event.preventDefault();
        close_list(true);
        return;
    }
    const action = recent_key(event);
    if (action === null || down.value !== null) return;
    if (event.target === document.body && scrolls_itself(last_press, event.code)) return;
    if (action === 'edge') {
        event.preventDefault();
        toggle_order();
        return;
    }
    const b = conv.value;
    const row = cursor_row.value;
    if (b === null || row === null) return;
    event.preventDefault();
    const at = b.rows.findIndex((r) => r.decision_id === row.decision_id);
    if (action === 'next') cursor_id.value = b.rows[Math.min(b.rows.length - 1, at + 1)].decision_id;
    else if (action === 'prev') cursor_id.value = b.rows[Math.max(0, at - 1)].decision_id;
    else if (action === 'next_conv') void step(1);
    else if (action === 'prev_conv') void step(-1);
    else if (action === 'expand') toggle(row);
    else if (action === 'undo') void undo();
    else void answer(row, action);
}

/** A press outside the bar closes the counter's list. */
function on_press(event: PointerEvent): void {
    last_press = event.target instanceof Element ? event.target : null;
    if (show_list.value && nav_el.value && !nav_el.value.contains(event.target as Node | null)) show_list.value = false;
}

// Watch the selected decision, not just its place: the first result, a re-group that moved it, and a row that left the
// page must all open the right one. A cursor whose row left adopts the turn the conversation now opens on.
watch(() => cursor_row.value?.decision_id, (id) => {
    if (id && id !== cursor_id.value) cursor_id.value = id;
    last_press = null;
    opened_id.value = id ?? null;
    void scroll_to(id ?? null);
}, { immediate: true });
// Remember the conversation shown, for `pager_at` when its cursor turn leaves the page.
watch(conv, (b) => {
    if (!b) return;
    conv_last = conv_at.value;
    conv_ids = b.rows.map((r) => r.decision_id);
    conv_key = b.key;
});
// A new conversation starts at its top: the bar is sticky, so bring the conversation's start up under it.
watch(() => conv.value?.key, async (key, before) => {
    if (key === undefined || before === undefined || key === before) return;
    await nextTick();
    const el = conv_el.value;
    if (el && el.getBoundingClientRect().top < (nav_el.value?.getBoundingClientRect().bottom ?? 0)) el.scrollIntoView({ block: 'start' });
    void scroll_to(cursor_row.value?.decision_id ?? null);
});
// What the live region says: the status, then which conversation this is when that changed with it (one watcher on
// both, so a grade that moved on says both, in that order). A cleared status empties it, so the same words said
// twice (two DONE grades in a row) are still heard.
watch([() => conv.value?.key, status_line], ([key, line], [before]) => {
    const where = key !== undefined && key !== before
        ? `Conversation ${conv_at.value + 1} of ${bands.value.length}${has_older.value ? '+' : ''}: ${head.value.title}`
        : null;
    announce.value = [line?.text, where].filter(Boolean).join('. ');
});
// The list opens with focus on the conversation shown, scrolled to it.
watch(show_list, async (open) => {
    if (!open) return;
    await nextTick();
    const row = nav_el.value?.querySelector<HTMLElement>('.rl-pick [aria-current="true"]');
    const list = row?.closest<HTMLElement>('.rl-pick');
    if (!row || !list) return;
    list.scrollTop = Math.max(0, row.offsetTop - list.clientHeight / 2);
    row.focus({ preventScroll: true });
});
watch(() => [props.surface, props.order, props.session?.name, props.session?.since], () => {
    reopen = true;
    cursor_id.value = null;
    void load();
});

/** `el`'s rendered height into `--<name>` on the root, for what sticks or scrolls beside it: the pager bar's (the grade
 *  column, a turn scrolled to) and the pinned grade bar's (a turn scrolled to; 0 while it is hidden). */
function size_into(el_ref: Ref<HTMLElement | null>, name: string): void {
    let observer: ResizeObserver | null = null;
    watch(el_ref, (el) => {
        observer?.disconnect();
        if (!el || typeof ResizeObserver === 'undefined') return;
        observer = new ResizeObserver(([entry]) => root_el.value?.style.setProperty(name, `${Math.ceil(entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height)}px`));
        observer.observe(el);
    });
    onBeforeUnmount(() => observer?.disconnect());
}
size_into(nav_el, '--lr-sticky-top');
size_into(bar_el, '--lr-gradebar-h');

let clock: number | undefined;
let sizer: ResizeObserver | null = null;

onMounted(() => {
    window.addEventListener('keydown', on_key);
    window.addEventListener('pointerdown', on_press, true);
    // The ages read "4m"; a minute tick keeps them honest without refetching rows under the cursor.
    clock = window.setInterval(() => (now.value = Date.now()), 60_000);
    if (root_el.value && typeof ResizeObserver !== 'undefined') {
        sizer = new ResizeObserver(([entry]) => (is_narrow.value = entry.contentRect.width < NARROW_PX));
        sizer.observe(root_el.value);
    }
    void load();
});

onBeforeUnmount(() => {
    window.removeEventListener('keydown', on_key);
    window.removeEventListener('pointerdown', on_press, true);
    window.clearInterval(clock);
    sizer?.disconnect();
    stop_fresh();
});
</script>

<style scoped>
/* A button here is sized and tinted through its `--lr-btn-*` tokens, a list of turns indented through
   `--lr-turns-indent`, and the turns and grade bar are styled once for the deck too: owned by laya-review.css — see the
   note there. What is RECENT's alone (the pager bar, its list, the prompt groups) is here. */
.rl {
    display: flex;
    flex-direction: column;
    gap: 12px;
}
/* The live region: heard, not seen. */
/* jscpd:ignore-start: the standard visually-hidden recipe, verbatim. */
.rl-sr {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
    border: 0;
}
/* jscpd:ignore-end */
.rl-filters { max-width: 100%; }
.rl-filters summary { cursor: pointer; padding: 8px 0; color: var(--lr-ink-2); }
.rl-filter-options { display: flex; flex-wrap: wrap; gap: 6px; padding: 6px 0; }
.rl-strip {
    gap: 8px 10px;
}
.rl-session { max-width: 100%; white-space: normal; overflow-wrap: anywhere; text-align: left; }
.rl-band {
    flex: none;
}
.rl-explain {
    display: block;
}
.rl-band--warn {
    --lr-btn-ink: var(--lr-amber);
}
.rl-name-review summary { cursor: pointer; padding: 8px 0; color: var(--lr-ink-2); }
.rl-needs {
    color: var(--lr-amber);
}

/* The pager bar: sticky, opaque, so the conversation scrolls under it and its name never leaves the screen. */
.rl-nav {
    position: sticky;
    top: 0;
    z-index: 3;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    border: 1px solid var(--lr-well-line);
    border-left: 3px solid var(--lr-thread, var(--lr-rule));
    border-radius: 4px;
    background: var(--lr-bg);
}
/* Two classes: the head's own `.lth--compact` padding is a peer rule from another chunk (laya-review.css's header). */
.rl-nav > .rl-nav__head {
    min-width: 0;
    padding: 4px 0;
}
.rl-step,
.rl-count {
    --lr-btn-h: 40px;
    --lr-btn-pad: 4px 12px;
    font-variant-numeric: tabular-nums;
}
.rl-step .fui-key {
    margin-left: 6px;
}

/* The counter's list: every conversation on the page, one row each. It opens right under the bar, over the
   conversation, wherever the bar is stuck; in the embedded board, where nothing sticks (the panel fits its content), it
   takes its place in the bar's flow instead. Its height cap is a pixel token there (`--lr-list-max`, LayaReviewDeck.vue). */
.rl-pick {
    grid-column: 1 / -1;
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: var(--lr-list-max, min(50vh, 420px));
    margin: 0;
    padding: 4px;
    overflow: auto;
    overscroll-behavior: contain;
    list-style: none;
    border: 1px solid var(--lr-well-line);
    border-radius: 4px;
    background: var(--lr-panel);
}
.rl:not(.rl--embedded) .rl-pick {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    z-index: 3;
    box-shadow: var(--lr-shadow);
}
.rl-pick__row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    column-gap: 10px;
    width: 100%;
    min-height: 44px;
    padding: 6px 8px;
    border: 0;
    border-left: 3px solid transparent;
    border-radius: 3px;
    background: none;
    color: var(--lr-ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
}
.rl-pick__row:hover {
    background: var(--lr-tint);
}
.rl-pick__row:focus-visible {
    outline: 2px solid var(--fui-accent);
    outline-offset: -2px;
}
.rl-pick__row.is-on {
    border-left-color: var(--lr-thread, var(--fui-accent));
    background: var(--lr-on-fill);
}
.rl-pick__mono {
    grid-row: span 2;
    align-self: center;
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border: 1.5px solid var(--lr-thread, var(--lr-rule));
    border-radius: 50%;
    color: var(--lr-thread, var(--lr-ink-2));
    font: 700 0.68rem/1 var(--fui-mono);
}
.rl-pick__mono--loose {
    border-style: dashed;
}
.rl-pick__title {
    overflow: hidden;
    font-size: 0.9rem;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.rl-pick__meta {
    color: var(--lr-ink-3);
    font: 0.74rem/1.4 var(--fui-mono);
}
.rl-older {
    align-self: flex-start;
    margin: 4px;
}

/* One conversation: the first message, then each prompt with the turns that answered it, indented under it. */
.rl-conv {
    gap: 14px;
    padding: 12px 14px 14px;
    scroll-margin-top: calc(var(--lr-sticky-top, 0px) + 8px);
}
.rl-groups {
    display: flex;
    flex-direction: column;
    gap: 16px;
    margin: 0;
    padding: 0;
    list-style: none;
}
.rl-group {
    --lr-turns-indent: 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
}
.rl-group > .lr-turns {
    border-left: 2px solid var(--lr-well-line);
}
.rl-gap {
    padding: 2px 0 2px 12px;
    border-left: 2px dotted var(--lr-well-line);
    color: var(--lr-ink-3);
    font-size: 0.82rem;
}
.rl-end {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--lr-well-line);
}
.rl-end__done {
    color: var(--lr-good);
    font-size: 0.85rem;
    font-weight: 700;
}
.rl-end__none {
    color: var(--lr-ink-3);
    font-size: 0.85rem;
}

/* Phone width: the bar stays ONE strip, so the conversation keeps the screen, and its controls are touch-sized. The title takes one
   line and the tags and rack name the next; the counts ride the counter's tooltip, and the list prints the rest. */
@container (max-width: 559px) {
    .rl-nav {
        gap: 6px;
        padding: 4px 6px;
    }
    .rl-step,
    .rl-count {
        --lr-btn-h: 44px;
        --lr-btn-pad: 4px 8px;
        min-width: 44px;
        justify-content: center;
    }
    .rl-step .fui-key {
        display: none;
    }
    .rl-older {
        --lr-btn-h: 44px;
    }
    .rl-nav > .rl-nav__head {
        column-gap: 8px;
        padding: 2px 0;
    }
    .rl-nav__head :deep(.lth-title) {
        flex: 1 1 100%;
        min-width: 0;
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
    }
    /* Clipped sideways only (`clip`, not `hidden`), so the rack name's 44px touch box still reaches past the row. */
    .rl-nav__head :deep(.lth-sub) {
        display: flex;
        flex-wrap: nowrap;
        align-items: center;
        gap: 0 8px;
        min-width: 0;
        overflow-x: clip;
    }
    .rl-nav__head :deep(.lth-sub > .lth-tag) {
        flex: none;
        white-space: nowrap;
    }
    .rl-nav__head :deep(.lth-meta) {
        min-width: 0;
    }
    .rl-nav__head :deep(button.lth-rack) {
        display: block;
        max-width: 100%;
        overflow: hidden;
        line-height: 44px;
        white-space: nowrap;
        text-overflow: ellipsis;
    }
    .rl-nav__head :deep(.lth-meta__rest) {
        display: none;
    }
    .rl-conv {
        padding: 10px;
    }
    .rl-group {
        --lr-turns-indent: 4px;
    }
}
</style>
