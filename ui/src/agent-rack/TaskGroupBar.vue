<script setup lang="ts">
import { ref } from 'vue';
import { worker_stage_word, type TaskGroup } from './task-groups.ts';
const props = defineProps<{ group: TaskGroup; disabled?: boolean }>();
const emit = defineEmits<{ control: [id: string, action: string] }>();
const armed = ref<string | null>(null);
function control(action: string) {
    if (action !== 'pause' && armed.value !== action) { armed.value = action; return; }
    armed.value = null;
    emit('control', props.group.id, action);
}
</script>

<template>
    <div class="task-group" @click.stop @contextmenu.stop>
        <div class="task-group__readout fui-techtext">
            <span :title="group.label">{{ group.label }}</span>
            <strong :title="`${group.ready}/${group.total} workers reported validation; merged PRs count as delivered. Milestones, not a time estimate.`">{{ group.percent }}%</strong>
            <span>PR {{ group.opened }}/{{ group.total }}</span>
            <span v-if="group.blockers" class="fui-tag fui-tag--warn">{{ group.blockers }} blocked</span>
            <span v-else-if="!group.sealed" class="fui-tag">launching</span>
            <span v-else-if="group.phase === 'opening'" class="fui-tag">opening PRs</span>
        </div>
        <div class="fui-meter" role="progressbar" :aria-label="`${group.label} milestones`" :aria-valuenow="group.percent" :aria-valuemin="0" :aria-valuemax="100">
            <i class="fui-meter__fill" :style="{ width: `${group.percent}%` }" />
        </div>
        <details>
            <summary class="fui-techtext">{{ group.auto_pr ? 'Auto PR on' : 'Auto PR paused' }} · workers</summary>
            <div v-for="worker in group.workers" :key="worker.branch" class="task-group__worker">
                <span>{{ worker.session }} · {{ worker_stage_word(worker) }}</span>
                <a v-if="worker.pr_url" :href="worker.pr_url" target="_blank" rel="noopener noreferrer">PR ↗</a>
                <span v-if="worker.error || worker.note">{{ worker.error || worker.note }}</span>
            </div>
            <div class="task-group__actions">
                <button v-if="group.phase !== 'complete'" type="button" class="fui-btn" :disabled="disabled"
                    @click="control(group.auto_pr ? 'pause' : 'resume')">{{ group.auto_pr ? 'Pause auto PR' : armed === 'resume' ? 'Resume auto PR?' : 'Resume auto PR' }}</button>
                <button v-if="group.workers.some(w => w.error)" type="button" class="fui-btn" :disabled="disabled"
                    @click="control('retry')">{{ armed === 'retry' ? 'Retry PRs?' : 'Retry PRs' }}</button>
            </div>
        </details>
    </div>
</template>

<style scoped>
.task-group { min-width: 0; padding: 5px 0; display: grid; gap: 4px; }
.task-group__readout { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: .72rem; }
.task-group__readout > span:first-child { overflow: hidden; text-overflow: ellipsis; max-width: 180px; white-space: nowrap; }
.task-group summary { cursor: pointer; font-size: .7rem; }
.task-group__worker { display: flex; flex-wrap: wrap; gap: 5px 10px; padding-top: 5px; font-size: .8rem; overflow-wrap: anywhere; }
.task-group__worker > span:last-child { flex-basis: 100%; }
.task-group__actions { display: flex; gap: 6px; margin-top: 6px; }
</style>
