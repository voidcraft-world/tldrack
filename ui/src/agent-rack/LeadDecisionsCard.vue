<script setup lang="ts">
/**
 * The DECISIONS card: the opened lead's open decisions, one line each — the title, then
 * `BLOCKS S2` or `ASSUMING <what the lead is doing meanwhile>` — and the operator's answers: KEEP
 * (non-blocking only), one button per option or ANSWER in words, and DEFER. What each verb does to
 * the run: owned by `bridge/tldrack/lead/runs.py` — see the note there.
 *
 * It owns no transport. A press emits one `DecisionRequest`; the rack sends it through its payload
 * owner, which adds the decision secret (`lead-client.ts`). KEEP and an option ARM first (`KEEP?`):
 * an answer can contradict the lead's assumption and reopen the steps built on it, so the second
 * press is the one that answers. ANSWER's field and DEFER's presets are already a second press.
 * Placement (the canvas pane where one is mounted, under the row anywhere else) is the rack's
 * (`AgentRackComponent.vue`, the `<Teleport>`).
 */
import { computed, nextTick, ref, watch } from 'vue';
import { useArmedAction } from '../composables/useArmedAction.ts';
import {
    decision_choices,
    decision_stance,
    defer_presets,
    type Decision,
    type DecisionChoices,
    type DecisionRequest,
    type DecisionStance,
    type DeferPreset,
    type LeadRun,
} from './lead-client.ts';

const props = defineProps<{ run: LeadRun; decisions: Decision[]; frozen: boolean }>();
const emit = defineEmits<{ (event: 'decide', request: DecisionRequest): void }>();

/** The rack's arm window (KILL?, PR?): long enough to read the label change, short enough to forget. */
const ARM_MS = 4000;
const arm = useArmedAction(ARM_MS);

interface CardLine {
    decision: Decision;
    stance: DecisionStance;
    choices: DecisionChoices;
}

const lines = computed<CardLine[]>(() => props.decisions.map((decision) => ({
    decision,
    stance: decision_stance(decision),
    choices: decision_choices(decision),
})));

/** The one disclosure open on the card: a decision's ANSWER field or its DEFER presets. */
const open = ref<{ key: string; kind: 'answer' | 'defer' } | null>(null);
const answer_text = ref('');
const presets = ref<DeferPreset[]>([]);
let answer_field: HTMLInputElement | null = null;

function keep_key(decision: Decision): string {
    return `${decision.key}\u0000keep`;
}
function option_key(decision: Decision, index: number): string {
    return `${decision.key}\u0000option\u0000${index}`;
}

// A decision answered elsewhere (another surface, the phone) leaves the card: its arm and its open
// field must not outlive it, or the next decision's button would fire on one press.
watch(lines, (next) => {
    arm.retain(next.flatMap(({ decision, choices }) => [
        keep_key(decision),
        ...choices.options.map((_, index) => option_key(decision, index)),
    ]));
    if (open.value && !next.some(({ decision }) => decision.key === open.value!.key)) open.value = null;
});

function is_open(decision: Decision, kind: 'answer' | 'defer'): boolean {
    return open.value?.key === decision.key && open.value.kind === kind;
}

function toggle(decision: Decision, kind: 'answer' | 'defer'): void {
    arm.disarm();
    if (is_open(decision, kind)) {
        open.value = null;
        return;
    }
    open.value = { key: decision.key, kind };
    if (kind === 'defer') {
        presets.value = defer_presets(new Date());
        return;
    }
    answer_text.value = '';
    // The press asked to type, so this focus is the request. A field never focuses itself only because a card opened.
    void nextTick(() => answer_field?.focus());
}

function set_answer_field(el: unknown): void {
    answer_field = el instanceof HTMLInputElement ? el : null;
}

function press(arm_key: string, request: DecisionRequest): void {
    if (props.frozen) return;
    open.value = null;
    arm.press(arm_key, () => emit('decide', request));
}

function keep(decision: Decision): void {
    press(keep_key(decision), { verb: 'answer', key: decision.key, kind: 'keep' });
}

function pick(decision: Decision, option: string, index: number): void {
    press(option_key(decision, index), { verb: 'answer', key: decision.key, kind: 'option', answer: option });
}

function send_answer(decision: Decision): void {
    const answer = answer_text.value.trim();
    if (!answer || props.frozen) return;
    open.value = null;
    emit('decide', { verb: 'answer', key: decision.key, kind: 'text', answer });
}

/** Re-read at the press: presets opened at 17:59 must not send a 18:00 that has just passed. */
function defer(decision: Decision, preset: DeferPreset): void {
    if (props.frozen) return;
    const fresh = defer_presets(new Date()).find((row) => row.key === preset.key) ?? preset;
    open.value = null;
    emit('decide', { verb: 'defer', key: decision.key, until: fresh.until });
}

function stance_title(line: CardLine): string {
    const { decision, stance } = line;
    if (stance.blocking) {
        return decision.steps.length > 0
            ? `Blocking: steps ${decision.steps.join(', ')} are parked until you answer`
            : 'Blocking: its steps are parked until you answer';
    }
    return `Not blocking: the lead goes on assuming this${decision.steps.length > 0 ? ` (steps ${decision.steps.join(', ')})` : ''}. KEEP if it holds`;
}
</script>

<template>
    <section
        class="lead"
        :aria-label="`LEAD ${run.id}: ${decisions.length} ${decisions.length === 1 ? 'decision' : 'decisions'} waiting on you`"
        @click.stop
    >
        <header class="lead__head fui-techtext" :title="run.title || run.id">
            DECISIONS · {{ run.id }}
        </header>
        <div v-for="line in lines" :key="line.decision.key" class="lead__item">
            <div class="lead__line">
                <span class="lead__title" :title="line.decision.detail || line.decision.title">{{ line.decision.title }}</span>
                <span
                    :class="['lead__stance', { 'lead__stance--blocks': line.stance.blocking }]"
                    :title="stance_title(line)"
                >{{ line.stance.text }}</span>
            </div>
            <div class="lead__acts">
                <button
                    v-if="line.choices.keep"
                    type="button"
                    :class="['lead__btn', { 'lead__btn--armed': arm.armed.value === keep_key(line.decision) }]"
                    :disabled="frozen"
                    :title="arm.armed.value === keep_key(line.decision) ? 'Press again: the assumption holds' : 'The assumption holds — the lead goes on as it is'"
                    @click.stop="keep(line.decision)"
                >{{ arm.armed.value === keep_key(line.decision) ? 'KEEP?' : 'KEEP' }}</button>
                <button
                    v-for="(option, index) in line.choices.options"
                    :key="index"
                    type="button"
                    :class="['lead__btn', 'lead__btn--option', { 'lead__btn--armed': arm.armed.value === option_key(line.decision, index) }]"
                    :disabled="frozen"
                    :title="arm.armed.value === option_key(line.decision, index) ? `Press again to answer: ${option}` : `Answer: ${option}`"
                    @click.stop="pick(line.decision, option, index)"
                >{{ arm.armed.value === option_key(line.decision, index) ? `${option}?` : option }}</button>
                <button
                    v-if="line.choices.text"
                    type="button"
                    class="lead__btn"
                    :disabled="frozen"
                    :aria-expanded="is_open(line.decision, 'answer')"
                    title="Answer in your own words"
                    @click.stop="toggle(line.decision, 'answer')"
                >ANSWER</button>
                <button
                    type="button"
                    class="lead__btn lead__btn--quiet"
                    :disabled="frozen"
                    :aria-expanded="is_open(line.decision, 'defer')"
                    title="Hide it for a while — a blocking decision keeps its steps parked meanwhile"
                    @click.stop="toggle(line.decision, 'defer')"
                >DEFER</button>
            </div>
            <form v-if="is_open(line.decision, 'answer')" class="lead__answer" @submit.prevent="send_answer(line.decision)">
                <input
                    :ref="set_answer_field"
                    v-model="answer_text"
                    type="text"
                    maxlength="2000"
                    autocomplete="off"
                    :aria-label="`Your answer: ${line.decision.title}`"
                    placeholder="Your answer…"
                    @keydown.esc.stop="open = null"
                />
                <button type="submit" class="lead__btn lead__btn--send" :disabled="frozen || !answer_text.trim()">SEND</button>
            </form>
            <div v-if="is_open(line.decision, 'defer')" class="lead__defer" role="group" :aria-label="`Defer: ${line.decision.title}`">
                <button
                    v-for="preset in presets"
                    :key="preset.key"
                    type="button"
                    class="lead__btn"
                    :disabled="frozen"
                    :title="preset.title"
                    @click.stop="defer(line.decision, preset)"
                >{{ preset.label }}</button>
            </div>
        </div>
    </section>
</template>

<style scoped>
/* Graphite chrome: the only hue on this card is BLOCKS' amber, and the armed amber every armed
   control in the rack wears — both mean "look at this". A decision is not a session STATE. */
.lead {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
    padding: 6px 8px;
    border-left: 2px solid rgba(244, 247, 255, 0.28);
    background: var(--rack-raised, #383c42);
    color: var(--rack-text, #edf0f3);
    font-size: 12.5px;
    cursor: default;
}
.lead__head {
    font-size: 0.68rem;
    letter-spacing: 0.12em;
    color: var(--rack-muted, #b5bdc6);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.lead__item {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
}
.lead__item + .lead__item {
    padding-top: 6px;
    border-top: 1px solid rgba(244, 247, 255, 0.08);
}
.lead__line {
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
}
.lead__title {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.lead__stance {
    flex: 1 1 0;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    color: var(--rack-muted, #b5bdc6);
}
.lead__stance--blocks {
    flex: 0 0 auto;
    color: #f8c146;
    letter-spacing: 0.1em;
}
.lead__acts,
.lead__defer {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}
.lead__btn {
    min-height: 28px;
    max-width: 16em;
    padding: 0 10px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    border-radius: 2px;
    border: 1px solid rgba(244, 247, 255, 0.32);
    background: transparent;
    color: var(--rack-text, #edf0f3);
    font: inherit;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.72rem;
    letter-spacing: 0.1em;
    cursor: pointer;
}
.lead__btn:hover:not(:disabled) { background: rgba(255, 255, 255, 0.08); }
.lead__btn:focus-visible { outline: 1px solid #42ffa8; outline-offset: 2px; }
.lead__btn:disabled { cursor: not-allowed; opacity: 0.45; }
/* An option is the lead's own words: sentence case, not a mono verb. */
.lead__btn--option {
    font-family: inherit;
    font-size: 0.78rem;
    letter-spacing: 0;
}
.lead__btn--quiet {
    color: var(--rack-muted, #b5bdc6);
    border-color: rgba(181, 189, 198, 0.3);
}
.lead__btn--armed {
    color: #f8c146;
    background: rgba(248, 193, 70, 0.14);
    border-color: rgba(248, 193, 70, 0.55);
}
.lead__answer {
    display: flex;
    gap: 6px;
    min-width: 0;
}
.lead__answer input {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 28px;
    padding: 0 8px;
    border-radius: 2px;
    border: 1px solid rgba(244, 247, 255, 0.28);
    background: rgba(0, 0, 0, 0.25);
    color: inherit;
    font: inherit;
}
.lead__answer input:focus-visible { outline: 1px solid #42ffa8; outline-offset: 1px; }
/* SEND is the card's one bright action, in the rack's create accent. */
.lead__btn--send {
    color: #10151a;
    background: var(--color-accent-green, #93ffd4);
    border-color: var(--color-accent-green, #93ffd4);
}
.lead__btn--send:hover:not(:disabled) { background: var(--color-accent-green, #93ffd4); filter: brightness(1.08); }
@media (hover: none) {
    .lead__btn,
    .lead__answer input { min-height: 44px; }
}
</style>
