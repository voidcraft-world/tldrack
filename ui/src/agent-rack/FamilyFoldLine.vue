<script setup lang="ts">
// A folded family's one line: owned by `family-fold.ts` — see the note there.
import { computed } from 'vue';
import { family_readout_text, type FamilyFoldReadout } from './family-fold.ts';
const props = defineProps<{ readout: FamilyFoldReadout }>();
const words = computed(() => family_readout_text(props.readout));
</script>

<template>
    <div class="family-fold" :aria-label="`Folded: ${words}`">
        <span class="family-fold__words fui-techtext">{{ words }}</span>
        <template v-if="readout.percent !== null">
            <strong class="fui-techtext" title="Every task group of this parent together, weighted by workers. Milestones, not a time estimate.">{{ readout.percent }}%</strong>
            <span v-if="readout.blockers" class="fui-tag fui-tag--warn">{{ readout.blockers }} blocked</span>
            <div class="fui-meter family-fold__meter" role="progressbar" aria-label="Task groups, all together"
                :aria-valuenow="readout.percent" :aria-valuemin="0" :aria-valuemax="100">
                <i class="fui-meter__fill" :style="{ width: `${readout.percent}%` }" />
            </div>
        </template>
    </div>
</template>

<style scoped>
.family-fold { min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; padding: 3px 0; font-size: .72rem; }
.family-fold__words { white-space: nowrap; }
.family-fold__meter { flex: 1 1 80px; min-width: 60px; }
</style>
