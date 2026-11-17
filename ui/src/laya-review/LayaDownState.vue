<template>
    <PanelDownState :head="down.head">
        <template #why>{{ down.why }}</template>
        <template #how>
            <code v-if="down.how" class="lr-how">{{ down.how }}</code>
            <span class="lr-retry">
                <button v-if="down.connect" type="button" class="board-btn" @click="unlock_local_access()">
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>CONNECT
                </button>
                <button type="button" class="board-btn" @click="emit('retry')">
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>RETRY
                </button>
            </span>
        </template>
    </PanelDownState>
</template>

<script setup lang="ts">
/**
 * The REVIEW page's down state, for the deck and its RECENT list alike: one per failure kind (`deck_down`). CONNECT
 * opens the gate, and the view's own gate watcher (`view-state.ts`) does the reload.
 */
import PanelDownState from './PanelDownState.vue';
import { unlock_local_access } from '../engine/terminal/local-network-gate.ts';
import type { DeckDown } from './review-deck-model.ts';

defineProps<{ down: DeckDown }>();
const emit = defineEmits<{ retry: [] }>();
</script>
