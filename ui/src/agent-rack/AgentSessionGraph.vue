<script setup lang="ts">
import { computed } from 'vue';
import { VueFlow, Handle, Position, useVueFlow } from '@vue-flow/core';
import type { RackSession } from '../engine/terminal/multi-terminal-layout.ts';
import { session_graph } from '../engine/terminal/session-graph.ts';
import { display_state_color_css, fresh_session_shown, session_state_emoji } from '../config/terminal-status-flags.ts';
import IslandMark from './IslandMark.vue';
import TaskGroupDisclosure from './TaskGroupDisclosure.vue';
import { groups_for_parent, type TaskGroup } from './task-groups.ts';
import '@vue-flow/core/dist/style.css';

const props = defineProps<{ sessions: RackSession[]; focused: string | null; disabled: boolean; groups?: TaskGroup[] }>();
const emit = defineEmits<{ select: [session: string]; control: [id: string, action: string] }>();
const graph = computed(() => session_graph(props.sessions, props.groups));
const { fitView, zoomIn, zoomOut } = useVueFlow();
</script>

<template>
    <section class="session-graph" aria-label="Agent session families">
        <div class="session-graph__tools">
            <button type="button" class="fui-btn" aria-label="Zoom in" @click="zoomIn()">+</button>
            <button type="button" class="fui-btn" aria-label="Zoom out" @click="zoomOut()">−</button>
            <button type="button" class="fui-btn" @click="fitView({ padding: 0.15 })">FIT</button>
        </div>
        <p v-if="sessions.length === 0" class="fui-techtext">No sessions</p>
        <VueFlow v-else :nodes="graph.nodes" :edges="graph.edges" fit-view-on-init
            :nodes-draggable="false" :nodes-connectable="false" :elements-selectable="false"
            :min-zoom="0.15" :max-zoom="2" :delete-key-code="null">
            <template #node-session="{ id, data }">
                <Handle type="target" :position="Position.Left" />
                <button type="button" class="fui-panel session-graph__node nodrag"
                    :class="{ 'session-graph__node--focused': data.session === focused }"
                    :disabled="disabled || !data.live || data.missing"
                    :title="`${data.label} · ${data.missing ? 'parent closed' : data.state}`"
                    @click="emit('select', data.session)">
                    <span :style="{ color: display_state_color_css(data.state) }">{{ session_state_emoji(data.state, data.fresh, !data.live) }}</span>
                    <span class="session-graph__label">{{ data.label }}</span>
                    <small>{{ data.missing ? 'parent closed' : !data.live ? 'session gone'
                        : fresh_session_shown(data.state, data.fresh, false) ? 'new' : data.state }}</small>
                    <IslandMark v-if="data.island" class="session-graph__island" :island="data.island" />
                </button>
                <TaskGroupDisclosure class="session-graph__group nodrag nopan"
                    :groups="groups_for_parent(groups, id)" :disabled="disabled" @control="(group_id, action) => emit('control', group_id, action)" />
                <Handle type="source" :position="Position.Right" />
            </template>
        </VueFlow>
    </section>
</template>

<style scoped>
.session-graph { position: relative; height: clamp(300px, 52vh, 650px); min-height: 300px; }
.session-graph__group { width: 240px; max-height: 180px; overflow-y: auto; }
.session-graph__tools { position: absolute; top: 8px; right: 8px; z-index: 5; display: flex; gap: 4px; }
.session-graph__node { width: 240px; min-height: 56px; display: grid; grid-template-columns: 24px 1fr; gap: 3px 6px; padding: 8px; text-align: left; color: var(--color-text-primary, #e4edf2); cursor: pointer; }
.session-graph__node--focused { border-color: var(--color-accent-green, #93ffd4); }
.session-graph__node:disabled { opacity: 0.6; cursor: default; }
.session-graph__label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.session-graph__node small { grid-column: 2; font-size: 12px; opacity: 0.7; }
.session-graph__island { grid-column: 2; }
.session-graph :deep(.vue-flow__handle) { opacity: 0; pointer-events: none; }
.session-graph :deep(.vue-flow__edge-path) { stroke: var(--color-accent-green, #93ffd4); stroke-opacity: 0.35; }
</style>
