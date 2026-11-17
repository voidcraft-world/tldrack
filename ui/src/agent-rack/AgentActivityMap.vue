<script setup lang="ts">
import lot_map_license from './lot-map/LICENSE?raw';
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import type { RackSession } from '../engine/terminal/multi-terminal-layout.ts';
import { activity_map_image, observe_activity, type ActivityMap } from './activity-map.ts';

const props = defineProps<{ agent: RackSession; stale?: boolean; compact?: boolean }>();
const map = shallowRef<ActivityMap | null>(null);
const diagram = ref<HTMLImageElement | null>(null);
const viewport = ref({ width: 720, height: 292 });
let resize: ResizeObserver | null = null;
watch(diagram, (next, previous) => {
    if (previous) resize?.unobserve(previous);
    if (next) resize?.observe(next);
});
const observed_at = ref(Date.now());
const reduced_motion = ref(false);
let preference: MediaQueryList | null = null;
function motion_changed(): void { reduced_motion.value = preference?.matches ?? false; }
watch(() => [props.agent, props.stale] as const, () => {
    if (map.value?.session !== props.agent.session) map.value = null;
    if (props.stale) return;
    observed_at.value = Date.now();
    map.value = observe_activity(map.value, props.agent, observed_at.value);
}, { immediate: true });
const source = computed(() => map.value ? activity_map_image(
    map.value, observed_at.value, !props.stale && props.agent.state === 'busy' && !reduced_motion.value, viewport.value,
) : null);
onMounted(() => {
    resize = new ResizeObserver(([entry]) => {
        const width = Math.round(entry.contentRect.width), height = Math.round(entry.contentRect.height);
        if (width > 0 && height > 0 && (width !== viewport.value.width || height !== viewport.value.height)) viewport.value = { width, height };
    });
    if (diagram.value) resize.observe(diagram.value);
    preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    motion_changed();
    preference.addEventListener('change', motion_changed);
});
onBeforeUnmount(() => { resize?.disconnect(); preference?.removeEventListener('change', motion_changed); });
</script>

<template>
    <section class="activity-map" :class="{ 'activity-map--compact': compact }" aria-label="Live agent activity map">
        <img ref="diagram" v-if="source" class="activity-map__image" :src="source" :alt="`Activity map: ${map?.label}`" />
        <p v-if="stale" class="activity-map__readout">Waiting for fresh session data</p>
        <footer class="activity-map__foot">
            <a href="https://github.com/alexeyw/lot-map" target="_blank" rel="noopener noreferrer" :title="lot_map_license">Classification: lot-map · MIT</a>
            <span title="Last activity from rack snapshots. Counts are changes observed while this view is open, not tool-call totals. Exact tool timing, errors and hooks are unavailable.">{{ compact ? 'ⓘ' : 'Observed activity ⓘ' }}</span>
        </footer>
    </section>
</template>

<style scoped>
.activity-map { display: flex; flex-direction: column; min-height: 0; min-width: 0; overflow: auto; background: #2d3035; }
.activity-map__foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 8px; font-size: 0.78rem; }
.activity-map__image { display: block; width: 100%; min-height: 0; flex: 1 1 auto; object-fit: contain; }
.activity-map__readout { margin: 0; padding: 0 8px; font-size: 0.78rem; overflow-wrap: anywhere; }
.activity-map__foot { flex-wrap: wrap; opacity: 0.8; }
.activity-map__foot a { color: inherit; }
.activity-map--compact { height: 100%; overflow: hidden; }
.activity-map--compact .activity-map__image { height: 0; flex: 1 1 0; }
.activity-map--compact .activity-map__readout { padding: 0 6px; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; }
.activity-map--compact .activity-map__foot { flex-wrap: nowrap; font-size: 0.68rem; }
</style>
