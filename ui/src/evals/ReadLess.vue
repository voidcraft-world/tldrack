<script setup lang="ts">
import { computed, ref } from 'vue';
import type { BenchFleet } from '@tldrack/schemas/bench-results.ts';

import { fmt_count, fmt_day_range, fmt_fixed, fmt_ms, fmt_share, read_less, type ReadLessPart } from './evals-model.ts';
import { use_width } from './use-width.ts';

/**
 * Panel 1. The rack's live window: of the turn ends judged on the operator's machine, how many
 * a ⚠️ put in front of the operator. One figure, then every judged turn end once in one bar.
 */
const props = defineProps<{ fleet: BenchFleet }>();

/** The rack's own three marks (`@tldrack/schemas/laya-review.ts` REVIEW_BANDS), with ⚠️ split by whether it showed. */
const PART_LABEL: Readonly<Record<ReadLessPart, string>> = {
    done: '✅ done',
    reply: '💬 reply (unsure)',
    held: '⚠️ open, never shown',
    reached: '⚠️ reached the operator',
};
/** The narrowest block that still prints its share with room either side. */
const LABEL_MIN_PX = 58;

const bar = ref<HTMLElement | null>(null);
const bar_width = use_width(bar, 760);
const view = computed(() => read_less(props.fleet));
const parts = computed(() => view.value.parts.filter((part) => part.n > 0).map((part) => ({ ...part, label: PART_LABEL[part.key], fits: part.share * bar_width.value >= LABEL_MIN_PX })));
const spoken = computed(() => parts.value.map((part) => `${part.label}: ${fmt_count(part.n)}, ${fmt_share(part.share)}`).join('. '));
</script>

<template>
    <div class="ev-read-less">
        <div class="ev-hero">
            <span class="ev-hero__figure">{{ fmt_share(view.kept_share) }}</span>
            <p class="ev-hero__caption">
                of {{ fmt_count(view.judged) }} turn ends never reached the operator. Laya judged each one on the operator's machine and showed a ⚠️ for
                {{ fmt_count(view.reached) }}.
            </p>
        </div>

        <div ref="bar" class="ev-split" role="img" :aria-label="`Every judged turn end, by verdict. ${spoken}.`">
            <div v-for="part in parts" :key="part.key" class="ev-split__part" :data-part="part.key" :style="{ flex: `${part.n} 0 0` }" :title="`${part.label}: ${fmt_count(part.n)} (${fmt_share(part.share)})`">
                <template v-if="part.fits">{{ fmt_share(part.share) }}</template>
            </div>
        </div>
        <ul class="ev-split__key">
            <li v-for="part in parts" :key="part.key">
                <span class="ev-swatch ev-swatch--block" :data-part="part.key"></span>
                {{ part.label }} <b>{{ fmt_count(part.n) }}</b>
                <template v-if="!part.fits">{{ fmt_share(part.share) }}</template>
            </li>
        </ul>

        <dl class="ev-tiles">
            <div class="ev-tile">
                <dt>⚠️ a day</dt>
                <dd>
                    <b>{{ fmt_fixed(view.per_day.reached, 1) }}</b>
                    of {{ fmt_count(view.per_day.judged) }} turn ends judged a day
                </dd>
            </div>
            <div v-if="fleet.latency" class="ev-tile">
                <dt>Verdict latency</dt>
                <dd>
                    <b>{{ fmt_ms(fleet.latency.p50_ms) }}</b>
                    median · {{ fmt_ms(fleet.latency.p90_ms) }} at p90
                </dd>
            </div>
            <div v-if="fleet.next_step && view.taken_share !== null" class="ev-tile">
                <dt>Next steps taken</dt>
                <dd>
                    <b>{{ fmt_share(view.taken_share) }}</b>
                    {{ fmt_count(fleet.next_step.taken) }} of {{ fmt_count(fleet.next_step.suggested) }} suggested
                </dd>
            </div>
            <div class="ev-tile">
                <dt>Window</dt>
                <dd>
                    <b>{{ fmt_count(fleet.days) }} days</b>
                    {{ fmt_day_range(fleet.from, fleet.to) }}
                </dd>
            </div>
        </dl>
    </div>
</template>
