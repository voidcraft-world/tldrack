<script setup lang="ts">
import { computed } from 'vue';
import TaskGroupBar from './TaskGroupBar.vue';
import type { TaskGroup } from './task-groups.ts';

const props = defineProps<{ groups: TaskGroup[]; disabled: boolean }>();
const emit = defineEmits<{ control: [id: string, action: string] }>();
const blocked = computed(() => props.groups.reduce((sum, group) => sum + group.blockers, 0));
</script>

<template>
    <details v-if="groups.length" class="disclosure" @click.stop @contextmenu.stop>
        <summary class="fui-techtext"><span v-if="blocked" class="fui-tag fui-tag--warn">{{ blocked }} blocked</span>
            PRs</summary>
        <TaskGroupBar v-for="group in groups" :key="group.id" :group="group" :disabled="disabled" expanded
            @control="(id, action) => emit('control', id, action)" />
    </details>
</template>

<style scoped>
.disclosure { min-width: 0; }
.disclosure summary { width: fit-content; margin-left: auto; cursor: pointer; text-transform: none; font-size: 11px; line-height: 1.6; }
.disclosure summary:focus-visible { outline: 2px solid currentColor; }
</style>
