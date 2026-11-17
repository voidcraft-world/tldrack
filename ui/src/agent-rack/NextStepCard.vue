<script setup lang="ts">
/**
 * The suggested next step for the OPENED (focused) row: the action, why, the evidence, and
 * [Do it] [Dismiss]. A `none` draws its reason alone and nothing to press.
 *
 * It owns no behaviour. [Do it] emits `do`, and the rack presses the row's own control through
 * `next-step-view.ts::press_next_step`, so the control's arm and refusals still run; the button's
 * label is that control's (`KILL?`, `PR?`) once armed. Placement (the canvas pane where one is
 * mounted, under the row anywhere else) is the rack's (`AgentRackComponent.vue`, the `<Teleport>`).
 */
import type { NextStep } from '@tldrack/schemas/next-step.ts';
import type { CardPress } from './next-step-view.ts';

defineProps<{ step: NextStep; press: CardPress | null }>();
const emit = defineEmits<{ (event: 'do'): void; (event: 'dismiss'): void }>();
</script>

<template>
    <section
        :class="['next', `next--${step.kind}`]"
        :aria-label="step.kind === 'suggest' ? `Suggested next step: ${step.label}, ${step.reason}` : `No suggested click: ${step.reason}`"
        @click.stop
    >
        <template v-if="step.kind === 'suggest'">
            <header class="next__head">
                <span class="next__action fui-techtext">→ {{ step.label }}</span>
                <span class="next__reason">{{ step.reason }}</span>
                <span
                    class="next__source"
                    :title="step.source === 'model' ? `Suggested by ${step.model_version}; the quote is checked word for word against the agent's message` : `Decided by a rule (${step.model_version}) from the session's state`"
                >{{ step.source === 'model' ? 'AI' : 'RULE' }}</span>
            </header>
            <p v-if="step.evidence" class="next__evidence" :title="step.source === 'model' ? 'Quoted from the agent’s closing message' : 'The runtime fact that decided it'">
                <mark v-if="step.source === 'model'" class="next__quote">{{ step.evidence }}</mark>
                <span v-else class="next__fact">{{ step.evidence }}</span>
            </p>
            <div class="next__acts">
                <button
                    v-if="press"
                    type="button"
                    :class="['next__do', { 'next__do--armed': press.armed }]"
                    :disabled="press.disabled"
                    :title="press.title"
                    @click.stop="emit('do')"
                >{{ press.label }}</button>
                <button
                    type="button"
                    class="next__dismiss"
                    title="Hide this suggestion until the session moves on"
                    @click.stop="emit('dismiss')"
                >DISMISS</button>
            </div>
        </template>
        <p v-else class="next__none" :title="`No suggested click: ${step.reason}`">
            <span class="next__glyph" aria-hidden="true">◌</span>{{ step.reason }}
        </p>
    </section>
</template>

<style scoped>
/* Graphite chrome, mint for the one action: hue on this card is never a session STATE (the rack's
   hue contract), and the armed amber is the same one every armed control in the rack wears. */
.next {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
    padding: 6px 8px;
    border-left: 2px solid rgba(147, 255, 212, 0.55);
    background: var(--rack-raised, #383c42);
    color: var(--rack-text, #edf0f3);
    font-size: 12.5px;
    cursor: default;
}
.next--none {
    border-left-color: rgba(181, 189, 198, 0.4);
    background: transparent;
    padding: 2px 8px;
}
.next__head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
}
.next__action {
    flex: 0 0 auto;
    color: var(--color-accent-green, #93ffd4);
    font-size: 0.78rem;
    letter-spacing: 0.1em;
}
.next__reason {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.next__source {
    flex: 0 0 auto;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.68rem;
    letter-spacing: 0.12em;
    color: var(--rack-muted, #b5bdc6);
}
.next__evidence {
    margin: 0;
    min-width: 0;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    line-height: 1.4;
}
.next__quote {
    background: rgba(147, 255, 212, 0.16);
    color: var(--rack-text, #edf0f3);
    padding: 0 2px;
    border-radius: 2px;
}
.next__quote::before { content: '“'; }
.next__quote::after { content: '”'; }
.next__fact {
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.78rem;
    color: var(--rack-muted, #b5bdc6);
}
.next__acts {
    display: flex;
    gap: 8px;
}
.next__acts button {
    min-height: 28px;
    padding: 0 12px;
    border-radius: 2px;
    font: inherit;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    cursor: pointer;
    background: transparent;
}
.next__do {
    color: #10151a;
    background: var(--color-accent-green, #93ffd4) !important;
    border: 1px solid var(--color-accent-green, #93ffd4);
}
.next__do:hover:not(:disabled) { filter: brightness(1.08); }
/* The control's own arm, in the rack's armed amber — the second press fires it. */
.next__do--armed {
    color: #f8c146;
    background: rgba(248, 193, 70, 0.14) !important;
    border-color: rgba(248, 193, 70, 0.55);
}
.next__do:disabled {
    cursor: not-allowed;
    opacity: 0.45;
}
.next__dismiss {
    color: var(--rack-muted, #b5bdc6);
    border: 1px solid rgba(181, 189, 198, 0.3);
}
.next__dismiss:hover { background: rgba(181, 189, 198, 0.12); }
.next__none {
    margin: 0;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--rack-muted, #b5bdc6);
}
.next__glyph { margin-right: 6px; opacity: 0.7; }
@media (hover: none) {
    .next__acts button { min-height: 44px; }
}
</style>
