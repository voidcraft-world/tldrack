/**
 * A side rail sized to a whole DEVICE pixel, so the `<iframe>` beside it starts
 * on the pixel grid.
 *
 * Half a CSS pixel of offset is enough to make Chrome resample an entire xterm
 * canvas, which reads as blurry text next to a native terminal at the same
 * size. So the rail's width is snapped in JS — `round(w · dpr) / dpr`, floored
 * at `min_px` BEFORE the snap (a CSS `min-width` would win afterwards and put
 * the fraction back) — and re-measured when the window resizes.
 *
 * DPR changes with no event when the window is dragged between displays; the
 * only signal is a media query that stops matching. It is re-armed each time
 * because the query is pinned to the ratio that was current when it was made
 * (the same trick xterm uses internally for its glyph atlas).
 *
 * The rack page's list rail uses it (`agent-rack/AgentRackWindow.vue`).
 */
import { computed, onBeforeUnmount, onMounted, ref, type ComputedRef, type Ref } from 'vue';

export interface DevicePixelRail {
    /** The snapped width in CSS px. */
    px: Ref<number>;
    /** `${px}px`, for an inline `flex-basis` / `width`. */
    width: ComputedRef<string>;
    measure(): void;
}

export function useDevicePixelRail(min_px: number, wanted: () => number): DevicePixelRail {
    const px = ref<number>(min_px);
    const width = computed<string>(() => `${px.value}px`);

    function measure(): void {
        const dpr = window.devicePixelRatio || 1;
        px.value = Math.round(Math.max(min_px, wanted()) * dpr) / dpr;
    }

    let dpr_query: MediaQueryList | null = null;
    function watch_dpr(): void {
        dpr_query?.removeEventListener('change', on_dpr_change);
        dpr_query = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
        dpr_query.addEventListener('change', on_dpr_change);
    }
    function on_dpr_change(): void {
        measure();
        watch_dpr();
    }

    onMounted(() => {
        window.addEventListener('resize', measure);
        measure();
        watch_dpr();
    });
    onBeforeUnmount(() => {
        window.removeEventListener('resize', measure);
        dpr_query?.removeEventListener('change', on_dpr_change);
        dpr_query = null;
    });

    return { px, width, measure };
}
