<template>
    <div class="nv-board lr" :class="{ 'lr--embedded': embedded, 'lr--light': prefs.theme === 'light' }">
        <header class="nv-head fui-panel lr-head">
            <span class="fui-corner tl"></span>
            <span class="fui-corner br"></span>

            <!-- Embedded, the host's own header already names it. -->
            <strong v-if="!embedded" class="fui-techtext lr-title">LAYA REVIEW</strong>
            <span v-if="stats" class="fui-tag">{{ stats.label || surface_label(stats.surface) }}</span>
            <span class="lr-grow"></span>
            <button
                type="button"
                class="board-btn"
                :class="{ 'is-on': prefs.explain }"
                :aria-pressed="prefs.explain"
                :title="prefs.explain ? 'Hide the explanations under every readout, card and button' : 'Explain every readout, card and button in words'"
                @click="prefs.explain = !prefs.explain"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>EXPLAIN
            </button>
            <button
                type="button"
                class="board-btn"
                :class="{ 'is-on': prefs.theme === 'light' }"
                :aria-pressed="prefs.theme === 'light'"
                :title="prefs.theme === 'light' ? 'Light mode is on: press for dark' : 'Dark mode is on: press for light'"
                @click="prefs.theme = prefs.theme === 'light' ? 'dark' : 'light'"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>LIGHT
            </button>
            <a
                v-if="embedded"
                class="board-btn lr-standalone"
                :href="laya_review_href(surface ?? undefined, view, { stratum: deal, order, session })"
                :target="LAYA_REVIEW_TARGET"
                rel="noopener"
                title="Open Laya review as its own page"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>OPEN STANDALONE ↗
            </a>
            <nav class="lr-views" aria-label="View">
                <button
                    v-for="v in VIEWS"
                    :key="v.view"
                    type="button"
                    class="board-btn"
                    :class="{ 'is-on': view === v.view }"
                    :aria-pressed="view === v.view"
                    :title="v.title"
                    @click="set_view(v.view)"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ v.label }}
                </button>
            </nav>
            <LayaUndoButton v-if="stats && view === 'deck'" class="lr-undo--head" :disabled="!can_undo" :title="undo_title" @undo="undo" />
            <p v-if="view === 'recent' || three_way" class="lr-question">Is this turn done, done with an answer for you to read, still working, or waiting on you?</p>
            <p v-else-if="stats?.question" class="lr-question">{{ stats.question }}</p>
        </header>

        <LayaRecentList
            v-if="view === 'recent'"
            :surface="surface"
            :order="order"
            :session="session"
            :explain="prefs.explain"
            :embedded="embedded"
            @surface="(s) => (surface = s)"
            @order="set_order"
            @session="set_session"
        />

        <template v-else>
            <section v-if="stats && kappas" class="lr-stats" aria-label="Progress">
                <div
                    class="fui-meter lr-meter"
                    role="img"
                    :aria-label="`${progress.done} of ${progress.total} answered`"
                    :title="`${progress.done} of ${progress.total} answered`"
                >
                    <i class="fui-meter__fill" :style="{ width: `${(progress.ratio * 100).toFixed(1)}%` }"></i>
                </div>
                <span class="lr-fig lr-wide">{{ progress.text }}</span>
                <span class="lr-fig lr-wide" :title="kappas.sonnet.title">
                    <span class="lr-label">κ SONNET</span><span class="lr-val">{{ kappas.sonnet.shown }}</span>
                </span>
                <span class="lr-fig lr-wide" :title="kappas.laya.title">
                    <span class="lr-label">κ LAYA</span><span class="lr-val">{{ kappas.laya.shown }}</span>
                </span>
                <span v-if="kappa_self" class="lr-fig lr-wide" :title="kappa_self.title">
                    <span class="lr-label">κ SELF</span><span class="lr-val">{{ kappa_self.shown }}</span>
                </span>
                <span v-if="deficit" class="fui-tag fui-tag--warn" :title="deficit.title">{{ deficit.text }}</span>
                <span v-if="short_class" class="fui-tag fui-tag--warn" :title="short_class.title">{{ short_class.text }}</span>
                <span class="lr-fig lr-narrow" :title="kappas.sonnet.title">{{ compact_stats(stats) }}</span>
                <LayaUndoButton class="lr-undo--strip" :disabled="!can_undo" :title="undo_title" @undo="undo" />
            </section>
            <ul v-if="prefs.explain && stats" class="lr-explain" aria-label="What the figures mean">
                <li v-for="line in explain_stats(stats)" :key="line">{{ line }}</li>
            </ul>

            <!-- DEAL: narrow the deck to one kind of card. The plugin deals it (`?stratum=`); SAMPLE leads because κ waits on it. -->
            <nav v-if="stats" class="lr-deal" aria-label="Deal">
                <span class="lr-label lr-deal__head">DEAL</span>
                <button
                    v-for="o in deal_choices"
                    :key="o.deal ?? 'all'"
                    type="button"
                    class="board-btn"
                    :class="{ 'is-on': o.deal === deal, 'lr-deal--sample': o.deal === 'sample' }"
                    :aria-pressed="o.deal === deal"
                    :disabled="in_flight"
                    :title="o.title"
                    @click="set_deal(o.deal)"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                    {{ o.label }}<span class="fui-count lr-count">{{ o.waiting }}</span>
                </button>
            </nav>
            <p v-if="prefs.explain && stats" class="lr-explain lr-explain--line">{{ explain_deal(stats, deal) }}</p>

            <nav v-if="show_switcher(surfaces, surface)" class="lr-switch" aria-label="Surface">
                <button
                    v-for="s in switch_surfaces"
                    :key="s.surface"
                    type="button"
                    class="board-btn"
                    :class="{ 'is-on': s.surface === surface }"
                    :aria-pressed="s.surface === surface"
                    :disabled="in_flight"
                    @click="switch_surface(s.surface)"
                >
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                    {{ s.label || surface_label(s.surface) }}<span class="fui-count lr-count">{{ s.to_review }}</span>
                </button>
            </nav>

            <LayaDownState v-if="down" :down="down" @retry="reload" />

            <div v-else-if="loading && !card && !retest" class="fui-techtext lr-loading" role="status">ASKING THE BRIDGE…</div>

            <PanelDownState v-else-if="!stats" head="NOTHING TO REVIEW">
                <template #why>{{ queue_message || 'The bridge lists no review surface' }}</template>
                <template v-if="sources_note" #how>{{ sources_note }}</template>
            </PanelDownState>

            <!-- A blind retest leads the hand (`review_data.py`: THE BLIND RETEST): the text and the answers, nothing a
                 first look showed. No reference, no score and no stratum reach this page for it. `retest` is set only
                 while one holds the screen (`screen`). -->
            <div v-else-if="retest" class="lr-play">
                <article class="fui-panel lr-card" aria-label="Retest card">
                    <span class="fui-corner tl"></span>
                    <span class="fui-corner br"></span>
                    <div class="lr-card__tags">
                        <span class="fui-tag fui-tag--warn" :title="RETEST_NOTE">RETEST · BLIND</span>
                        <span v-if="retest.truncated" class="fui-tag" :title="tail_title(retest)">TAIL</span>
                    </div>
                    <div v-if="!show_conversation" ref="text_el" class="lr-text" tabindex="0"><AgentMessage :text="retest.text" mode="full" /></div>
                    <FuiDisclosure v-model="show_conversation">Conversation · your ask, the turn, its closing</FuiDisclosure>
                    <LayaConversation :key="retest.item_id" :text="retest.text" :at="retest.at" :open="show_conversation" :turn="null" />
                </article>

                <div class="lr-side">
                    <section v-if="prefs.explain" class="fui-panel lr-verdicts" aria-label="What a retest is">
                        <p class="lr-explain lr-explain--line">{{ RETEST_NOTE }}</p>
                    </section>
                    <div class="lr-bar">
                        <div class="lr-actions" :class="{ 'lr-actions--four': retest_bar.length > 3 }" role="group" aria-label="Answer again">
                            <button
                                v-for="c in retest_bar"
                                :key="c.id"
                                type="button"
                                class="board-btn lr-act"
                                :disabled="in_flight"
                                :title="c.title"
                                @click="answer_retest(c)"
                            >
                                <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                                <span>{{ c.label }}<kbd class="fui-key">{{ c.key }}</kbd></span>
                                <span v-if="prefs.explain" class="lr-act__cap">{{ c.caption }}</span>
                            </button>
                        </div>
                        <div class="lr-status" :class="{ 'lr-status--bad': status_line && !status_line.ok }" role="status">
                            {{ status_line?.text ?? '' }}
                        </div>
                    </div>
                </div>
            </div>

            <PanelDownState v-else-if="!card && deal_empty" :head="deal_empty.head">
                <template #why>{{ deal_empty?.why }}</template>
                <template #how>
                    <span class="lr-retry">
                        <button type="button" class="board-btn" @click="set_deal(null)">
                            <span class="fui-corner tl"></span><span class="fui-corner br"></span>DEAL ALL
                        </button>
                    </span>
                </template>
            </PanelDownState>

            <PanelDownState v-else-if="!card && deal === null && stats.to_review === 0" head="ALL REVIEWED">
                <template #why>{{ stats.reviewed }} answered · {{ stats.agree }} agree · {{ stats.flip }} flip · {{ stats.skip }} skip</template>
                <template v-if="sources_note" #how>{{ sources_note }}</template>
            </PanelDownState>

            <PanelDownState v-else-if="!card" head="NO CARDS LOADED">
                <template #why>{{ deal_waiting(stats, deal) }} wait to be reviewed, but the bridge sent none</template>
                <template #how>
                    <span class="lr-retry">
                        <button type="button" class="board-btn" @click="reload">
                            <span class="fui-corner tl"></span><span class="fui-corner br"></span>RELOAD
                        </button>
                    </span>
                </template>
            </PanelDownState>

            <!-- Wide, the card reads on the left and what it is judged against, with the answers, rides beside it; narrow,
                 `.lr-side` dissolves (`display: contents`) so the answer bar can pin to the bottom of the whole play area. -->
            <div v-else class="lr-play">
                <article class="fui-panel lr-card" :class="card_head ? ['lr-card--thread', `lr-hue-${card_head.hue}`] : []" :aria-label="`${stratum.label} card`">
                    <span class="fui-corner tl"></span>
                    <span class="fui-corner br"></span>

                    <!-- The card's conversation, found by its state key (`k=`): the same head a RECENT band wears. -->
                    <LayaThreadHeader v-if="card_head" class="lr-card__head" :head="card_head">
                        <template #tags>
                            <span v-for="t in card_tags" :key="t.text" class="fui-tag" :class="{ 'fui-tag--warn': t.warn }" :title="t.title">{{ t.text }}</span>
                        </template>
                    </LayaThreadHeader>
                    <div v-else class="lr-card__tags">
                        <span v-for="t in card_tags" :key="t.text" class="fui-tag" :class="{ 'fui-tag--warn': t.warn }" :title="t.title">{{ t.text }}</span>
                    </div>

                    <!-- Rendered by AgentMessage (sanitized markdown: owned by agent-markdown.ts — see the note there), never v-html here. -->
                    <!-- A golden card has no conversation to fold: its reader is always open, on the case's own request and closing. -->
                    <div v-if="!show_conversation && !card.golden" ref="text_el" class="lr-text" tabindex="0"><AgentMessage :text="card.text" mode="full" /></div>

                    <FuiDisclosure v-if="!card.golden" v-model="show_conversation">Conversation · your ask, the turn, its closing</FuiDisclosure>
                    <LayaConversation
                        :key="card.item_id"
                        :text="card.text"
                        :at="card.decision?.at ?? null"
                        :open="show_conversation || card.golden !== null"
                        :written="card.golden ? (card.golden.request ?? '') : null"
                        :turn="card_turn_for(card)"
                        :joining="card_pending.has(card.key)"
                        @found="(f) => card_found.set(card.item_id, f)"
                    />

                    <!-- A wave: the other turn ends one answer grades, oldest first, so the operator sees what they label. -->
                    <template v-if="card.wave">
                        <FuiDisclosure v-model="show_wave">Wave · the other {{ card.wave.members.length }} turn ends this answer grades</FuiDisclosure>
                        <ol v-if="show_wave" class="lr-wave" :aria-label="`The other ${card.wave.members.length} turn ends`">
                            <li v-for="m in card.wave.members" :key="m.key" class="lr-wave__turn">
                                <span class="lr-label" :title="`${m.at} · ${stratum_label(m.stratum).label}`">LAYA {{ m.p.toFixed(2) }}</span>
                                <p class="lr-wave__text">{{ m.text_chars > m.text.length ? '…' : '' }}{{ m.text }}</p>
                            </li>
                        </ol>
                    </template>
                </article>

                <div class="lr-side">
                    <section class="fui-panel lr-verdicts" aria-label="Labels so far">
                        <div class="lr-ref">
                            <span class="lr-label" :title="card.reference.by">{{ reference_source(card) }}</span>
                            <span class="fui-tag" :class="card.reference.open ? 'fui-tag--warn' : 'fui-tag--ok'">
                                {{ verdict_word(card.surface, card.reference.open) }}
                            </span>
                            <span v-if="card.reference.why" class="lr-why">{{ card.reference.why }}</span>
                        </div>

                        <div v-if="laya" class="lr-laya" :title="laya.title">
                            <span class="lr-label">{{ laya.text }}</span>
                            <div class="fui-meter lr-laya__meter" :class="{ 'fui-meter--warn': laya.flagged }">
                                <i class="fui-meter__fill" :style="{ width: laya.fill }"></i>
                                <i class="fui-meter__mark" :style="{ left: laya.mark }"></i>
                            </div>
                        </div>

                        <ul v-if="prefs.explain" class="lr-explain lr-explain--card" aria-label="What this card shows">
                            <li v-for="line in explain_card(card, three_way)" :key="line">{{ line }}</li>
                        </ul>
                    </section>

                    <div class="lr-bar">
                        <LayaExampleButton
                            :key="card.item_id"
                            :surface="card.surface"
                            :item_key="card.key"
                            :marked="card.great_example ?? false"
                            :disabled="in_flight"
                            @saved="(marked) => { card.great_example = marked; }"
                        />
                        <!-- A four-way surface answers DONE · READ · RUNNING · NEEDS YOU (`review-deck-model.ts` `is_three_way`). -->
                        <div v-if="three_way" class="lr-actions lr-actions--four" role="group" aria-label="Answer">
                            <button
                                v-for="v in RECENT_VERDICTS"
                                :key="v.verdict"
                                type="button"
                                class="board-btn lr-act"
                                :disabled="in_flight"
                                :title="v.title"
                                @click="answer_verdict(v.verdict)"
                            >
                                <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                                <span>{{ v.label }}<kbd class="fui-key">{{ v.key }}</kbd></span>
                                <span v-if="prefs.explain" class="lr-act__cap">{{ verdict_caption(card, v.verdict) }}</span>
                            </button>
                        </div>
                        <div v-else class="lr-actions" role="group" aria-label="Answer">
                            <button
                                type="button"
                                class="board-btn lr-act"
                                :disabled="!can_answer('agree')"
                                :title="`Record ${verdict_word(card.surface, card.reference.open)} — agree with ${reference_source(card)}`"
                                @click="answer('agree')"
                            >
                                <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                                <span>AGREE<kbd class="fui-key">A</kbd></span>
                                <span v-if="prefs.explain" class="lr-act__cap">{{ answer_caption(card, 'agree') }}</span>
                            </button>
                            <button
                                type="button"
                                class="board-btn lr-act"
                                :disabled="!can_answer('flip')"
                                :title="`Record ${verdict_word(card.surface, !card.reference.open)} — against ${reference_source(card)}`"
                                @click="answer('flip')"
                            >
                                <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                                <span>{{ flip_label(card) }}<kbd class="fui-key">F</kbd></span>
                                <span v-if="prefs.explain" class="lr-act__cap">{{ answer_caption(card, 'flip') }}</span>
                            </button>
                            <button
                                type="button"
                                class="board-btn lr-act"
                                :disabled="!can_answer('skip')"
                                title="No label — the card leaves the queue"
                                @click="answer('skip')"
                            >
                                <span class="fui-corner tl"></span><span class="fui-corner br"></span>
                                <span>SKIP<kbd class="fui-key">S</kbd></span>
                                <span v-if="prefs.explain" class="lr-act__cap">{{ answer_caption(card, 'skip') }}</span>
                            </button>
                        </div>
                        <div class="lr-status" :class="{ 'lr-status--bad': status_line && !status_line.ok }" role="status">
                            {{ status_line?.text ?? '' }}
                        </div>
                    </div>
                </div>
            </div>
        </template>

        <!-- Definitions stay available without taking space from the conversation. -->
        <details class="lr-legend">
            <summary>Grading guide and keyboard shortcuts</summary>
            <p v-if="stats" class="lr-explain">{{ stats.question }} {{ explain_verdicts(stats.surface) }}</p>
            <section class="fui-callout lr-legend__body" aria-label="Review legend">
                <p>
                    <strong>This review</strong> checks one yes/no question at a time. Laya can also choose
                    between options and assign scores; this page does not review those.
                </p>
                <p v-if="view === 'recent' || three_way">
                    <strong>DONE</strong>: the turn finished its task and you can close it without reading it.
                    <strong>📖 READ</strong>: the agent is done, but its answer is for you to read (a question it
                    answered, a finding). <strong>RUNNING</strong>: work remains, but the agent is waiting on its own
                    agents or jobs. <strong>NEEDS YOU</strong>: it needs your answer, decision, permission, or help
                    with a blocker. These are your grades, not four categories the current open-work model
                    distinguishes: it reads DONE and READ as done.
                </p>
                <p v-if="view === 'recent'">
                    <strong>LAYA’s number</strong> is its score for “yes,” not a task completion percentage;
                    the threshold turns that score into a flag.
                </p>
                <p v-if="view === 'recent'">
                    <strong>EDGE</strong> ranks every verdict Laya has kept by how close its score sits to a cut, so
                    the turns one nudge from another mark come first; a tag such as <strong>0.02 FROM ⚠️</strong>
                    marks a near miss in either order. The rack draws a ⚠️ on a row that is waiting on its own jobs
                    or child sessions as ⏳, so a warning here may have reached you as ⏳.
                </p>
                <p v-if="view === 'recent'">
                    <strong>Keys:</strong> D done · B read · R running · N needs you · S skip · U undo · J/K move · Enter open ·
                    E edge.
                </p>
                <template v-if="view === 'deck'">
                    <p><strong>The kinds of card</strong> (DEAL deals one kind; ALL mixes them):</p>
                    <dl>
                        <template v-for="[key, entry] in REVIEW_STRATA" :key="key">
                            <dt>{{ entry.label }}</dt>
                            <dd>{{ entry.why }}</dd>
                        </template>
                    </dl>
                    <p>
                        <strong>Keys:</strong> {{ three_way ? 'D done · B read · R running · N needs you' : 'A agree · F flip' }} · S skip · U
                        undo. <strong>UNDO</strong> takes back your last answer and deals that card again.
                    </p>
                    <p v-if="stats?.retest">
                        <strong>RETEST · BLIND</strong>: about {{ stats.retest.percent }} in 100 cards you graded come back once,
                        {{ stats.retest.after_days }} or more days later, with only their text. Your second answer never changes
                        the first: the two are compared as <strong>κ SELF</strong>, how far you agree with yourself.
                    </p>
                </template>
                <p>
                    Your corrections improve training and evaluation; a review answer does not resume an agent
                    or complete its work. Reviews load only through your local Laya bridge plugin.
                </p>
            </section>
        </details>
    </div>
</template>

<script setup lang="ts">
/**
 * The Laya REVIEW deck: `review.html`, a page of its own, and (`embedded`) a board a host application mounts inside
 * its own page.
 * One card at a time: agree with the reference label, flip it, or skip (DONE · RUNNING · NEEDS YOU on a three-way
 * surface); U takes the last answer back. A blind retest card leads the hand when one is due, and never takes the screen
 * from a card being read (`review-deck-model.ts` `screen_after`). Its RECENT view (`LayaRecentList.vue`,
 * `?view=recent`) is the other door: the newest live verdicts, graded DONE / OPEN in place. The rules (keys, merge, readouts) are
 * `review-deck-model.ts`; the wire is `decide/laya-review-client.ts`. The deck talks only to the operator's own
 * bridge: the texts it shows never leave their machine.
 *
 * Every answer awaits its POST with the buttons held: loopback answers in milliseconds, and a label the operator
 * believes was recorded must have been.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { HistoryThreads, HistoryTurn } from '@tldrack/schemas/agent-history.ts';
import type { ReviewAnswer, ReviewAnswerRequest, ReviewCard, ReviewQueue, ReviewRecentOrder, ReviewRetestCard, ReviewStats } from '@tldrack/schemas/laya-review.ts';

import PanelDownState from './PanelDownState.vue';
import FuiDisclosure from './FuiDisclosure.vue';
import { fail_text, load_review_queue, post_review_answer, post_review_retest, post_review_undo } from '../decide/laya-review-client.ts';
import { local_access_state, unlock_local_access } from '../engine/terminal/local-network-gate.ts';
import AgentMessage from './AgentMessage.vue';
import LayaDownState from './LayaDownState.vue';
import LayaExampleButton from './LayaExampleButton.vue';
import LayaConversation from './LayaConversation.vue';
import LayaRecentList from './LayaRecentList.vue';
import LayaThreadHeader from './LayaThreadHeader.vue';
import LayaUndoButton from './LayaUndoButton.vue';
import {
    LAYA_REVIEW_GO_MESSAGE,
    LAYA_REVIEW_TARGET,
    laya_review_href,
    order_from_search,
    session_from_search,
    view_from_search,
    type LayaReviewView,
    type ReviewSession,
} from './open.ts';
import { RECENT_VERDICTS, type RecentVerdict } from './recent-model.ts';
import { load_review_prefs, save_review_prefs } from './review-prefs.ts';
import { card_source, turn_head, type FoundHead, type JoinState } from './thread-model.ts';
import { NO_THREADS, freshen_threads, load_threads, merge_threads } from './turn-context.ts';
import { use_laya_view } from './view-state.ts';
import {
    PAGE,
    RETEST_NOTE,
    REVIEW_STRATA,
    answer_caption,
    card_edge,
    card_title,
    compact_stats,
    deal_options,
    deal_waiting,
    deck_down,
    deficit_readout,
    deck_kappas,
    deck_key,
    deck_progress,
    explain_card,
    explain_deal,
    explain_stats,
    explain_verdicts,
    flip_label,
    is_three_way,
    laya_readout,
    merge_cards,
    needs_refill,
    reference_source,
    retest_choices,
    retest_key,
    retest_request,
    screen_after,
    self_kappa,
    short_key,
    show_switcher,
    stratum_from_search,
    stratum_label,
    stratum_tag_text,
    surface_from_search,
    surface_label,
    tail_title,
    three_way_readout,
    trained_tag,
    undo_target,
    verdict_caption,
    verdict_key,
    verdict_request,
    verdict_word,
    wave_tag,
    type DealStratum,
    type DeckScreen,
    type RetestChoice,
    type UndoEntry,
} from './review-deck-model.ts';

// Key hints always draw on this page: it keeps no preference that turns them off.

const VIEWS: readonly { view: LayaReviewView; label: string; title: string }[] = [
    { view: 'deck', label: 'DECK', title: 'Sampled cards, one at a time — what κ and recall are measured on' },
    { view: 'recent', label: 'RECENT', title: 'The newest live verdicts from the agent rack — grade what the rack drew' },
];

/**
 * `embedded`: mounted inside a host application's page, not as its own page. The address bar is the host's, so it
 * is neither read nor written; the gate is not opened on mount (the board is there whenever the host shows it, which
 * is not the operator asking; CONNECT in the down state is); and the host's `refresh_token` (a manual or timed
 * refresh) re-reads the queue WITHOUT replacing the card on screen.
 */
const props = withDefaults(defineProps<{ embedded?: boolean; refresh_token?: number }>(), { embedded: false, refresh_token: 0 });

const view = ref<LayaReviewView>(props.embedded ? 'deck' : view_from_search(window.location.search));
const surfaces = ref<ReviewStats[]>([]);
const surface = ref<string | null>(props.embedded ? null : surface_from_search(window.location.search));
const cards = ref<ReviewCard[]>([]);
/** The blind retests held: they lead the hand one at a time, each from the moment the hand advances (`screen`). */
const retests = ref<ReviewRetestCard[]>([]);
/** Which list the screen reads, sticky between advances: owned by review-deck-model.ts `screen_after` — see the note there. */
const screen = ref<DeckScreen>('card');
const sources = ref<Record<string, string>>({});
const queue_message = ref('');
const { down, loading, in_flight, status_line } = use_laya_view(reload);
const show_conversation = ref(true);
/** A wave card's other turn ends, shown by default: one answer labels each of them. */
const show_wave = ref(true);
/** LIGHT and EXPLAIN, per device (`review-prefs.ts`); every change is stored as it happens. */
const prefs = ref(load_review_prefs());
/** DEAL: the one stratum the deck deals, or null for the plugin's mix. Standalone, the address bar keeps it. */
const deal = ref<DealStratum | null>(props.embedded ? null : stratum_from_search(window.location.search));
/** The RECENT list's order: owned here, beside DEAL, so the address bar keeps it too. */
const order = ref<ReviewRecentOrder>(props.embedded ? 'newest' : order_from_search(window.location.search));
/** The one rack session the RECENT list is narrowed to (the rack's LABEL button), kept in the address bar like `order`. */
const session = ref<ReviewSession | null>(props.embedded ? null : session_from_search(window.location.search));
const undo_stack = ref<UndoEntry[]>([]);
const text_el = ref<HTMLElement | null>(null);

/** Item ids answered on this page: a refill fetched before an answer landed must not deal that card again. */
const answered = new Set<string>();
/** Bumped by every answer and undo, so a refill that started before one never writes older stats over newer. */
let writes = 0;
/** The refill in flight, if one is: a second ask joins it. */
let refilling: Promise<void> | null = null;
/** The last refill brought nothing new: stop asking until the hand changes for another reason. */
let dry = false;

const stats = computed(() => surfaces.value.find((s) => s.surface === surface.value) ?? null);
const card = computed(() => cards.value[0] ?? null);
/** The retest on screen, or null while a deck card (or nothing) is: a retest held behind that card is not it. */
const retest = computed(() => (screen.value === 'retest' ? (retests.value[0] ?? null) : null));
const retest_bar = computed(() => (retest.value ? retest_choices(retest.value) : []));
const three_way = computed(() => is_three_way(stats.value));
const stratum = computed(() => stratum_label(card.value?.stratum ?? ''));
const laya = computed(() => (card.value ? laya_readout(card.value) : null));
const progress = computed(() => deck_progress(stats.value));
const kappas = computed(() => (stats.value ? deck_kappas(stats.value) : null));
const deficit = computed(() => (stats.value ? deficit_readout(stats.value) : null));
const short_class = computed(() => (stats.value ? three_way_readout(stats.value) : null));
const kappa_self = computed(() => (stats.value ? self_kappa(stats.value) : null));
const deal_choices = computed(() => (stats.value ? deal_options(stats.value, deal.value) : []));
/** DEAL's own empty state: the narrowed stratum has nothing left, though other cards may. */
const deal_empty = computed(() => {
    const at = deal.value;
    const s = stats.value;
    if (at === null || s === null || deal_waiting(s, at) > 0) return null;
    return {
        head: `NO ${stratum_label(at).label} CARDS LEFT`,
        why: `${s.by_stratum[at]?.reviewed ?? 0} answered of this kind · ${deal_waiting(s, null)} other cards wait`,
    };
});
const switch_surfaces = computed(() => surfaces.value.filter((s) => s.to_review > 0 || s.surface === surface.value));
const sources_note = computed(() =>
    Object.entries(sources.value)
        .filter(([, state]) => state !== 'ok')
        .map(([name, state]) => `${name} ${state.replaceAll('_', ' ')}`)
        .join(' · '),
);

const can_undo = computed(() => !in_flight.value && undo_target(undo_stack.value, stats.value) !== null);
const undo_title = computed(() =>
    undo_stack.value.length > 0 ? 'Take back this page’s last answer' : 'Take back the last answer recorded for this surface',
);

function can_answer(kind: ReviewAnswer): boolean {
    return !in_flight.value && retest.value === null && card.value !== null && card.value.answers.includes(kind);
}

// ── the card's conversation (the thread join: turn-context.ts owns it) ───────

/** The join's answers for the cards dealt so far, by state key and by decision id; a card is asked about once. */
const card_join = ref<HistoryThreads>(NO_THREADS);
const card_join_state = ref<JoinState>('loading');
/** Keys whose ask is still out: their reader waits for it rather than racing it with a text search. */
const card_pending = ref(new Set<string>());
/** What the reader's needle fallback found, per card, for one the join did not resolve. */
const card_found = ref(new Map<string, FoundHead>());
const card_asked = new Set<string>();
let stop_card_fresh: () => void = () => {};

/** The card's turn: its decision's link when it has one (that exact verdict), else its state key's; unlinked, a
 *  `pending` answer beats a final `missing` (a fresh verdict whose text no older one had). Undefined while asked and
 *  unanswered, null when the join has nothing for it. */
function card_turn_for(c: ReviewCard): HistoryTurn | null | undefined {
    const by_decision = c.decision ? card_join.value.decisions[c.decision.decision_id] : undefined;
    const by_key = card_join.value.keys[c.key];
    const pick = (status: HistoryTurn['status']) => [by_decision, by_key].find((t) => t?.status === status);
    const got = pick('linked') ?? pick('pending') ?? by_key ?? by_decision;
    if (got) return got;
    return card_pending.value.has(c.key) ? undefined : null;
}

const card_tags = computed(() => {
    const c = card.value;
    if (!c) return [];
    const near = card_edge(c);
    return [
        { text: stratum_tag_text(c), title: card_title(c), warn: false },
        ...(c.wave ? [{ ...wave_tag(c.wave), warn: true }] : []),
        ...(near ? [{ ...near, warn: false }] : []),
        ...(trained_tag(c) ? [{ text: 'TRAINED', title: 'The served fine-tune trained on this text, so its score here is not a held-out measurement', warn: true }] : []),
        ...(c.truncated ? [{ text: 'TAIL', title: tail_title(c), warn: false }] : []),
    ];
});

/** A head only when there is something to name: a written test case with no conversation keeps the bare tags. */
const card_head = computed(() => {
    const c = card.value;
    if (!c) return null;
    const turn = card_turn_for(c);
    const found = card_found.value.get(c.item_id) ?? null;
    const source = card_source(c.key, turn, card_join.value);
    if (!source.thread && !found?.title && !source.indexing) return null;
    // A card the text search named is not "not linked": it has no rack-name group for the tag to describe.
    return turn_head(source, turn, c.decision?.at ?? null, { now: Date.now(), join_ok: card_join_state.value === 'ok' && !found, found });
});

/** Ask the join about every card not asked yet: one call per deal or refill, not one per card. */
async function join_cards(): Promise<void> {
    const fresh = cards.value.filter((c) => !card_asked.has(c.key));
    if (!fresh.length) return;
    for (const c of fresh) card_asked.add(c.key);
    const keys = fresh.map((c) => c.key);
    const ask = { keys, decisions: fresh.flatMap((c) => (c.decision ? [c.decision.decision_id] : [])) };
    card_pending.value = new Set([...card_pending.value, ...keys]);
    const got = await load_threads(ask);
    card_pending.value = new Set([...card_pending.value].filter((k) => !keys.includes(k)));
    if (!got.ok) {
        card_join_state.value = got.reason;
        return;
    }
    card_join.value = merge_threads(card_join.value, got.data);
    card_join_state.value = 'ok';
    stop_card_fresh();
    stop_card_fresh = freshen_threads(ask, got.data, (data) => (card_join.value = merge_threads(card_join.value, data)));
}
watch(() => cards.value.map((c) => c.key).join(','), () => void join_cards());

// ── the queue ───────────────────────────────────────────────────────────────

function take_queue(queue: ReviewQueue): void {
    surfaces.value = queue.surfaces;
    surface.value = queue.surface;
    sources.value = queue.sources;
    queue_message.value = queue.message;
    keep_address(queue.surface, view.value);
}

/** Standalone, the address bar keeps the surface, the view, DEAL and the RECENT order, so a bookmark reopens exactly this page. */
function keep_address(at: string | null, shown: LayaReviewView): void {
    if (!props.embedded) {
        window.history.replaceState(null, '', laya_review_href(at ?? undefined, shown, { stratum: deal.value, order: order.value, session: session.value }));
    }
}

function set_view(next: LayaReviewView): void {
    if (next === view.value || in_flight.value) return;
    view.value = next;
    status_line.value = null;
    keep_address(surface.value, next);
}

function set_order(next: ReviewRecentOrder): void {
    if (next === order.value) return;
    order.value = next;
    keep_address(surface.value, view.value);
}

/** RECENT's narrowing, kept in the address bar: a band's rack name sets it (no `since`: a row does not know when tmux
 *  made the session), its ✕ clears it, and the rack's LABEL button opens the page on one (`on_go`). */
function set_session(next: ReviewSession | null): void {
    session.value = next;
    keep_address(surface.value, view.value);
}

/** The rack's LABEL button on a page already up (`open.ts` `open_laya_review`): move to its address in place, no reload. */
function on_go(event: MessageEvent): void {
    const data = event.data as { type?: unknown; href?: unknown } | null;
    if (event.origin !== window.location.origin || data?.type !== LAYA_REVIEW_GO_MESSAGE || typeof data.href !== 'string') return;
    const search = new URL(data.href, window.location.href).search;
    status_line.value = null;
    view.value = view_from_search(search);
    order.value = order_from_search(search);
    session.value = session_from_search(search);
    keep_address(surface.value, view.value);
}

/** DEAL one stratum, or (null) the plugin's mix: a fresh hand from the plugin, never the old one filtered here. */
function set_deal(next: DealStratum | null): void {
    if (next === deal.value || in_flight.value) return;
    deal.value = next;
    cards.value = [];
    retests.value = [];
    status_line.value = null;
    keep_address(surface.value, view.value);
    void load(surface.value);
}

function put_stats(next: ReviewStats): void {
    surfaces.value = surfaces.value.map((s) => (s.surface === next.surface ? next : s));
}

/** Bumped by every load: DEAL pressed twice quickly must show the second press's hand, whichever answer lands last. */
let loads = 0;

async function load(target: string | null): Promise<void> {
    const mine = ++loads;
    loading.value = true;
    const got = await load_review_queue(target, PAGE, deal.value);
    if (mine !== loads) return;
    loading.value = false;
    if (!got.ok) {
        // A bookmarked surface or stratum the bridge no longer serves: fall back to the mix on the surface with the most work.
        if ((target !== null || deal.value !== null) && got.code === 'bad_query') {
            deal.value = null;
            return load(null);
        }
        down.value = deck_down(got, local_access_state());
        return;
    }
    down.value = null;
    take_queue(got.data);
    cards.value = merge_cards([], got.data.cards, answered);
    retests.value = merge_cards([], got.data.retests ?? [], answered);
    settle_screen(true); // a fresh hand: the one read that may lead with a retest
    dry = false;
}

/** `advance`: a fresh hand was dealt, or the item on screen just left. Anything else keeps the screen where it is. */
function settle_screen(advance: boolean): void {
    screen.value = screen_after(screen.value, { retests: retests.value.length, cards: cards.value.length }, advance);
}

/** `force`: a re-read the host asked for (`refresh_token`), whatever the hand holds; it never marks the queue dry. */
function refill(force = false): Promise<void> {
    if (refilling) return refilling;
    if (!force && !needs_refill(cards.value.length, stats.value, dry, deal.value)) return Promise.resolve();
    refilling = read_more(force).finally(() => {
        refilling = null;
    });
    return refilling;
}

/** One refill's read. What it fetches goes in BEHIND the item on screen, retests included (`settle_screen`). */
async function read_more(force: boolean): Promise<void> {
    const at = surface.value;
    const narrowed = deal.value;
    const since = writes;
    const got = await load_review_queue(at, PAGE, narrowed);
    if (!got.ok) {
        status_line.value = { ok: false, text: fail_text(got) };
        return;
    }
    if (got.data.surface !== surface.value || deal.value !== narrowed) return; // switched meanwhile
    const before = cards.value.length;
    cards.value = merge_cards(cards.value, got.data.cards, answered);
    retests.value = merge_cards(retests.value, got.data.retests ?? [], answered);
    settle_screen(false);
    if (cards.value.length > before) dry = false;
    else if (!force) dry = true;
    if (since === writes) surfaces.value = got.data.surfaces;
}

/** A deck card or a retest card leaves the hand: their item ids never collide (`review_data.py` `retest_card`). The
 *  hand advances only when the one that left was the one on screen. */
function drop(item_id: string): void {
    const shown = (retest.value ?? card.value)?.item_id;
    answered.add(item_id);
    cards.value = cards.value.filter((c) => c.item_id !== item_id);
    retests.value = retests.value.filter((c) => c.item_id !== item_id);
    settle_screen(shown === item_id);
}

// ── answers ─────────────────────────────────────────────────────────────────

function answer(kind: ReviewAnswer): Promise<void> {
    const current = card.value;
    if (!current || !can_answer(kind)) return Promise.resolve();
    // A wave card names the riders it showed: the plugin records them only if they are still exactly the wave.
    const wave = current.wave ? { wave: current.wave.members.map((m) => m.key) } : {};
    return submit(current, { surface: current.surface, key: current.key, answer: kind, reference_open: current.reference.open, ...wave });
}

/** A three-way press (`review-deck-model.ts` `verdict_request` maps it onto the card's reference). */
function answer_verdict(verdict: RecentVerdict): Promise<void> {
    const current = card.value;
    if (!current || in_flight.value || retest.value !== null) return Promise.resolve();
    return submit(current, verdict_request(current, verdict));
}

/** One held press, an answer's or a retest's: the buttons wait on its POST (the header note owns why), `settle` says
 *  what the answer did to the hand, and the hand refills. `writes` moves BEFORE `settle` puts the answer's stats, in
 *  the same turn as it, so a refill already in flight sees the bump and never writes its older stats over them. */
async function press<T>(post: () => Promise<T>, settle: (got: T) => void): Promise<void> {
    in_flight.value = true;
    status_line.value = null;
    try {
        const got = await post();
        writes += 1;
        settle(got);
    } finally {
        in_flight.value = false;
    }
    void refill();
}

function submit(current: ReviewCard, request: ReviewAnswerRequest): Promise<void> {
    return press(() => post_review_answer(request), (got) => {
        if (got.ok) {
            drop(current.item_id);
            const done = got.data.answer;
            if (done) {
                const entry = { answer_id: done.answer_id, surface: current.surface, item_id: current.item_id, wave: Boolean(done.wave_id) };
                undo_stack.value = [...undo_stack.value, entry];
            }
            if (got.data.stats) put_stats(got.data.stats);
            if (current.wave) status_line.value = { ok: true, text: `Graded ×${current.wave.n}` };
        } else if (got.code === 'already_answered' || got.code === 'unknown_item') {
            drop(current.item_id);
            if (got.body?.stats) put_stats(got.body.stats);
            status_line.value = { ok: false, text: got.error };
        } else if ((got.code === 'reference_changed' || got.code === 'wave_changed') && got.body?.card) {
            // The label AGREE would have recorded, or the wave it grades, moved under the operator: deal the fresh card.
            const fresh = got.body.card;
            cards.value = [fresh, ...cards.value.filter((c) => c.item_id !== current.item_id && c.item_id !== fresh.item_id)];
            status_line.value = { ok: false, text: got.error };
        } else {
            status_line.value = { ok: false, text: fail_text(got) };
        }
    });
}

/** A retest's answer: recorded beside the label it re-tests, never over it (`review_log.py`: RETEST). */
function answer_retest(choice: RetestChoice): Promise<void> {
    const current = retest.value;
    if (!current || in_flight.value) return Promise.resolve();
    return press(() => post_review_retest(retest_request(current, choice)), (got) => {
        if (got.ok) {
            drop(current.item_id);
            const done = got.data.retest;
            if (done) undo_stack.value = [...undo_stack.value, { answer_id: done.answer_id, surface: current.surface, item_id: current.item_id, retest: true }];
            if (got.data.stats) put_stats(got.data.stats);
            return;
        }
        // Stale, already retested or not due: the label moved, or another tab answered it, so this card is spent.
        const spent = got.code === 'retest_stale' || got.code === 'already_retested' || got.code === 'not_due';
        if (spent) drop(current.item_id);
        status_line.value = { ok: false, text: spent ? got.error : fail_text(got) };
    });
}

async function undo(): Promise<void> {
    const target = undo_target(undo_stack.value, stats.value);
    if (!target || in_flight.value) return;
    in_flight.value = true;
    status_line.value = null;
    try {
        const got = await post_review_undo(target);
        writes += 1;
        const top = undo_stack.value.at(-1);
        // Undone now, or already gone: either way this page's entry is spent.
        if (top && top.answer_id === target.answer_id && (got.ok || got.code === 'already_undone' || got.code === 'unknown_answer')) {
            undo_stack.value = undo_stack.value.slice(0, -1);
            answered.delete(top.item_id);
        }
        if (got.ok) {
            if (got.data.stats) put_stats(got.data.stats);
            const back = got.data.card;
            if (back) {
                answered.delete(back.item_id);
                if (back.surface === surface.value) {
                    cards.value = [back, ...cards.value.filter((c) => c.item_id !== back.item_id)];
                    screen.value = 'card'; // the operator asked for it back: it is shown ahead of a retest still held
                }
            }
            dry = false;
            const key = back?.key ?? got.data.answer?.key; // the card dealt back: a wave's lead, whichever row was named
            const many = /×\d+$/.exec(got.data.message)?.[0];
            status_line.value = { ok: true, text: key ? `Back: ${short_key(key)}${many ? ` ${many}` : ''}` : got.data.message };
            if (top?.retest) await retest_back(top.item_id);
        } else {
            if (got.body?.stats) put_stats(got.body.stats);
            status_line.value = { ok: false, text: fail_text(got) };
        }
    } finally {
        in_flight.value = false;
    }
}

/**
 * An undone retest is not a deck card: it comes back with the next queue read, not in the undo's answer. That read is
 * awaited inside the undo, with the buttons still held (`in_flight`), so no key lands on another item in between; a
 * refill that started before the undo cannot hold it, so it is let finish first. The operator asked for it back, so it
 * takes the screen like a card dealt back does.
 */
async function retest_back(item_id: string): Promise<void> {
    if (refilling) await refilling;
    await refill(true);
    const back = retests.value.find((c) => c.item_id === item_id);
    if (!back) return;
    retests.value = [back, ...retests.value.filter((c) => c.item_id !== item_id)];
    screen.value = 'retest';
}

function switch_surface(next: string): void {
    if (next === surface.value || in_flight.value) return;
    cards.value = [];
    retests.value = [];
    status_line.value = null;
    void load(next);
}

// ── down states ─────────────────────────────────────────────────────────────

function reload(): void {
    status_line.value = null;
    void load(surface.value);
}

// The host's refresh (embedded only): a down or never-loaded deck loads again; a live one merges the fresh
// queue under the card on screen, so a timer never swaps what the operator is reading.
watch(
    () => props.refresh_token,
    () => {
        if (loading.value || in_flight.value) return;
        if (down.value || !stats.value) reload();
        else void refill(true);
    },
);

// ── keys ────────────────────────────────────────────────────────────────────

function on_key(event: KeyboardEvent): void {
    if (view.value !== 'deck' || down.value !== null) return; // RECENT owns its own keys (`recent-model.ts` recent_key)
    if (retest.value) {
        const choice = retest_key(event, retest_bar.value);
        if (choice === null) return;
        event.preventDefault();
        if (choice === 'undo') void undo();
        else void answer_retest(choice);
        return;
    }
    if (three_way.value) {
        const verdict = verdict_key(event);
        if (verdict === null) return;
        event.preventDefault();
        if (verdict === 'undo') void undo();
        else void answer_verdict(verdict);
        return;
    }
    const action = deck_key(event);
    if (action === null) return;
    event.preventDefault();
    if (action === 'undo') void undo();
    else void answer(action);
}

// LIGHT and EXPLAIN: stored on every change. Standalone, the page around the deck (`<html>`, past its max width) follows
// LIGHT too (laya-review.css `data-lr-theme`); embedded, the host page around the board is not this deck's to paint.
watch(
    prefs,
    (next) => {
        save_review_prefs(next);
        if (!props.embedded) document.documentElement.dataset.lrTheme = next.theme;
    },
    { deep: true, immediate: true },
);

// A new card starts at its first line, not where the last one was scrolled to.
watch(
    () => retest.value?.item_id ?? card.value?.item_id,
    async () => {
        await nextTick();
        if (text_el.value) text_el.value.scrollTop = 0;
    },
);

onMounted(() => {
    // This page IS a local-tools surface: opening it is the operator asking, so it opens the gate itself (the
    // gate: owned by engine/terminal/local-network-gate.ts — see the note there). CONNECT stays for a gate shut since;
    // the reload after it is `use_laya_view`'s. Embedded, it is not asked: see `embedded` above.
    if (!props.embedded) unlock_local_access();
    if (!props.embedded) window.addEventListener('message', on_go);
    window.addEventListener('keydown', on_key);
    void load(surface.value);
});

onBeforeUnmount(() => {
    window.removeEventListener('message', on_go);
    window.removeEventListener('keydown', on_key);
    stop_card_fresh();
});
</script>

<style scoped>
/* A rule the page skin could outrank starts `.lr.`, and a button is sized through its `--lr-btn-*` tokens: owned by
   laya-review.css — see the note there. */
.lr {
    container-type: inline-size;
    max-width: 1280px;
    margin: 0 auto;
    padding: clamp(10px, 2vw, 22px);
}

/* Embedded in a host panel whose height FITS its content: a viewport unit there grows with every fit and never
   settles, so the page's `min-height: 100vh`, the reading caps and RECENT's conversation list (`--lr-list-max`,
   `LayaRecentList.vue`) go to pixels. The host's panel draws the glass and the padding. */
.lr.lr--embedded {
    min-height: 0;
    max-width: none;
    padding: 0;
    background: none;
    --lr-reading-max: 560px;
    --lr-reading-max-narrow: 560px;
    --lr-list-max: 420px;
}
/* Embedded and LIGHT, the board paints its own page: the host's panel around it stays dark. */
.lr.lr--embedded.lr--light {
    padding: 12px;
    border-radius: 3px;
    background: var(--lr-bg);
}

.lr-legend summary { cursor: pointer; padding: 10px 0; color: var(--lr-ink-2); }
.lr-head {
    row-gap: 10px;
}
.lr-title {
    font-size: 1rem;
}
.lr-question {
    flex-basis: 100%;
    margin: 0;
    color: var(--lr-ink);
    font-size: 1rem;
    line-height: 1.55;
}
/* DEAL: one row of stratum buttons; SAMPLE carries the accent so the κ door is found first. */
.lr-deal {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
}
.lr-deal__head {
    margin-right: 4px;
}
.lr-deal .board-btn {
    flex: none;
}
.lr-deal .lr-deal--sample:not(.is-on) {
    border-color: var(--fui-accent);
}
.lr-explain--line {
    display: block;
}
.lr-explain--card {
    padding: 10px 0 0;
    border-top: 1px solid var(--lr-well-line);
}

.lr-undo--head {
    display: none;
}
.lr-views {
    display: inline-flex;
    gap: 6px;
}
.lr-standalone {
    text-decoration: none;
    white-space: nowrap;
}
.lr-legend__body {
    margin-top: 4px;
    padding: 14px 18px;
    max-height: 560px;
    overflow: auto;
}
.lr-legend__body p,
.lr-legend__body dl {
    margin: 0 0 12px;
}
.lr-legend__body p:last-child {
    margin-bottom: 0;
}
.lr-legend__body dt {
    margin-top: 10px;
    font-size: 0.8rem;
    font-weight: 700;
    letter-spacing: 0.04em;
}
.lr-legend__body dd {
    margin: 0;
    color: var(--lr-ink-2);
}

.lr-narrow {
    display: none;
}
.lr-label {
    color: var(--lr-ink-2);
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
}
.lr-val {
    color: var(--lr-ink);
    font-weight: 700;
}

.lr-switch {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding-bottom: 2px;
}
.lr-switch .board-btn {
    flex: none;
}
.lr-count {
    margin-left: 6px;
}

/* The play area. Narrow: card, its labels, then the answers, one column. Wide: see the container query below. */
.lr-play {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
}
.lr-side {
    display: contents;
}
.lr-card {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px 18px;
    min-width: 0;
}
.lr-card__tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}
/* The card draws the panel; its head sits inside the card's own padding. */
.lr-card > .lr-card__head {
    padding: 0;
}
.lr-text {
    max-height: var(--lr-reading-max, 56vh);
    overflow: auto;
    overflow-wrap: anywhere;
    color: var(--lr-ink);
    font-size: 1rem;
    line-height: 1.6;
    padding: 12px 14px;
    border-radius: 4px;
    background: var(--lr-well);
    border: 1px solid var(--lr-well-line);
}
.lr-text:focus-visible {
    outline: 2px solid var(--fui-accent);
    outline-offset: 1px;
}
/* The reader's closing keeps the card text's reading cap, so the answers stay in reach: the embedded board fits its
   content, and an uncapped ledger message would push AGREE / FLIP below it and resize the panel on every card. */
.lr-card :deep(.lc-msg--closing) {
    max-height: var(--lr-reading-max, 56vh);
    overflow-y: auto;
}
.lr-verdicts {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 14px 16px;
    min-width: 0;
}
.lr-ref,
.lr-laya {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 10px;
    min-width: 0;
}
.lr-wave {
    display: grid;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
}
.lr-wave__turn {
    display: flex;
    gap: 10px;
    align-items: baseline;
    padding: 6px 10px;
    border-radius: 4px;
    background: var(--lr-well);
    border: 1px solid var(--lr-well-line);
}
.lr-wave__text {
    flex: 1 1 auto;
    min-width: 0;
    margin: 0;
    color: var(--lr-ink-2);
    font-size: 0.86rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
}
.lr-why {
    flex: 1 1 220px;
    min-width: 0;
    color: var(--lr-ink-2);
    font-size: 0.92rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
}
.lr-laya__meter {
    flex: 1 1 160px;
    max-width: 320px;
}

.lr-bar {
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.lr-actions {
    display: flex;
    gap: 10px;
}
.lr-act {
    --lr-btn-h: 46px;
    --lr-btn-pad: 10px 20px;
    --lr-btn-fs: 0.9rem;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
}
/* EXPLAIN's caption under an answer: the label it records, in sentence case, not the button's caps. */
.lr-act__cap {
    color: var(--lr-ink-2);
    font-size: 0.8rem;
    font-weight: 400;
    letter-spacing: 0;
    text-transform: none;
}

/* Wide: the card takes the width and reads tall; its labels and the answers ride beside it, stacked, and stay in
   view while the text scrolls. */
@container (min-width: 1000px) {
    .lr-play {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 360px;
        gap: 16px;
        align-items: start;
    }
    .lr-side {
        position: sticky;
        top: 12px;
        display: flex;
        flex-direction: column;
        gap: 12px;
    }
    .lr-text,
    .lr-card :deep(.lc-msg--closing) {
        max-height: var(--lr-reading-max, 70vh);
    }
    .lr-actions {
        flex-direction: column;
    }
    .lr-act {
        --lr-btn-pad: 10px 16px;
        align-items: flex-start;
        text-align: left;
    }
    .lr-legend__body dl {
        display: grid;
        grid-template-columns: max-content minmax(0, 1fr);
        gap: 6px 16px;
    }
    .lr-legend__body dt {
        margin-top: 0;
    }
}

/* Phone width: one column, the action bar pinned under the thumb, UNDO up in the header, the strip folded to one readout. */
@container (max-width: 559px) {
    .lr-undo--head {
        display: inline-flex;
    }
    .lr-undo--strip,
    .lr-wide {
        display: none;
    }
    .lr-narrow {
        display: inline-flex;
    }
    .lr-text,
    .lr-card :deep(.lc-msg--closing) {
        max-height: var(--lr-reading-max-narrow, 46vh);
    }
    .lr-bar {
        position: sticky;
        bottom: 0;
        padding: 8px 0 10px;
        background: var(--lr-bg);
    }
    .lr-actions {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 8px;
    }
    /* Four answers (the three-way grade and SKIP) are two rows of two: four across would cut NEEDS YOU on a phone. */
    .lr-actions--four {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    /* Five answers on two columns: the odd one out (SKIP) takes the whole last row. */
    .lr-actions--four > :last-child:nth-child(odd) {
        grid-column: 1 / -1;
    }
    .lr-act {
        --lr-btn-h: 48px;
        --lr-btn-pad: 8px 6px;
    }
}
</style>
