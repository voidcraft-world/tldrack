<template>
    <div role="alert">
        <PanelDownState head="THE BRIDGE IS NOT ANSWERING">
            <template #why>{{ failure.error }}</template>
            <template #how>
                <code v-if="failure.hint" class="lr-how">{{ failure.hint }}</code>
                <span class="lr-retry">
                    <button type="button" class="board-btn" @click="emit('retry')">
                        <span class="fui-corner tl"></span><span class="fui-corner br"></span>RETRY
                    </button>
                </span>
            </template>
        </PanelDownState>
    </div>
</template>

<script setup lang="ts">
/**
 * The page's one "bridge is down" banner, above whichever tab is open (`page-bridge.ts` owns
 * when it shows). The callout itself: owned by `laya-review/PanelDownState.vue` — see the note there.
 */
import PanelDownState from '../laya-review/PanelDownState.vue';
import type { BridgeFailure } from './bridge-call.ts';

defineProps<{ failure: BridgeFailure }>();
const emit = defineEmits<{ retry: [] }>();
</script>
