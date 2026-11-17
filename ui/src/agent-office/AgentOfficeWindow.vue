<script setup lang="ts">
/** The standalone office's channel lifecycle; rendering is owned by AgentOfficeView.vue. */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import AgentOfficeView from './AgentOfficeView.vue';
import { build_office, office_summary } from '../engine/terminal/agent-office-logic.ts';
import {
    OFFICE_CHANNEL,
    OFFICE_HEARTBEAT_MS,
    OFFICE_STALE_MS,
    parse_office_message,
    type OfficeFleet,
} from '../engine/terminal/agent-office-link.ts';

const fleet = shallowRef<OfficeFleet | null>(null);
const fleet_at = ref(0);
const now = ref(Date.now());
const stale = computed(() => fleet.value !== null && now.value - fleet_at.value > OFFICE_STALE_MS);
let channel: BroadcastChannel | null = null;
let heartbeat = 0;
let clock = 0;

function select(session: string): void {
    channel?.postMessage({ type: 'office:select', session });
}

watch(fleet, (next) => {
    document.title = `Office · ${next ? office_summary(build_office(next.sessions, next.focused)) : 'waiting for the rack'}`;
}, { immediate: true });

onMounted(() => {
    if (typeof BroadcastChannel !== 'undefined') {
        channel = new BroadcastChannel(OFFICE_CHANNEL);
        channel.onmessage = (event: MessageEvent) => {
            const message = parse_office_message(event.data);
            if (message?.type !== 'office:fleet') return;
            fleet.value = message.fleet;
            fleet_at.value = Date.now();
        };
        const hello = (): void => channel?.postMessage({ type: 'office:hello' });
        hello();
        heartbeat = window.setInterval(hello, OFFICE_HEARTBEAT_MS);
    }
    clock = window.setInterval(() => { now.value = Date.now(); }, 1000);
});

onBeforeUnmount(() => {
    window.clearInterval(heartbeat);
    window.clearInterval(clock);
    channel?.close();
});
</script>

<template>
    <AgentOfficeView :fleet="fleet" :stale="stale" @select="select" />
</template>
