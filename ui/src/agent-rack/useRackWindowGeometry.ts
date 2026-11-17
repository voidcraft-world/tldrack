/**
 * Remembers where the operator left a rack page that runs as a window of its own.
 *
 * Sampled HERE, in the rack window, because only this window knows its own final
 * geometry; whatever opens it reads the same localStorage key back on the next
 * `open()` (same origin, so it just works).
 *
 * THE TRAP: there is no `move` event. `resize` fires only on resize, so a window
 * merely dragged to another monitor would never be recorded. Hence the poll,
 * which is cheap: it writes only when something changed.
 *
 * Clearing site data forgets the rect. That is accepted: it is a device-local
 * convenience, not data.
 */
import { onBeforeUnmount, onMounted } from 'vue';
import { STORAGE_KEY, type RackWindowRect } from './rack-window-geometry.ts';

const SAMPLE_POLL_MS = 1000;

export function useRackWindowGeometry(): void {
    let poll: ReturnType<typeof setInterval> | null = null;
    let last_written = '';

    const sample = (): void => {
        const rect: RackWindowRect = {
            x: window.screenX,
            y: window.screenY,
            w: window.outerWidth,
            h: window.outerHeight,
        };
        // Some browsers report zeroes before the first paint (and while
        // minimised). Persisting that would restore an unusable window.
        if (!rect.w || !rect.h) return;
        if (!Number.isFinite(rect.x) || !Number.isFinite(rect.y)) return;

        const serialized = JSON.stringify(rect);
        if (serialized === last_written) return;
        last_written = serialized;
        try {
            localStorage.setItem(STORAGE_KEY, serialized);
        } catch {
            // Private mode / quota. Losing the rect is not worth throwing over.
        }
    };

    onMounted(() => {
        poll = setInterval(sample, SAMPLE_POLL_MS);
        window.addEventListener('resize', sample);
        // Both, deliberately: pagehide is the reliable one in modern browsers,
        // beforeunload the one that fires in more edge cases. sample() is
        // idempotent, so running twice costs nothing.
        window.addEventListener('pagehide', sample);
        window.addEventListener('beforeunload', sample);
    });

    onBeforeUnmount(() => {
        sample();
        if (poll) {
            clearInterval(poll);
            poll = null;
        }
        window.removeEventListener('resize', sample);
        window.removeEventListener('pagehide', sample);
        window.removeEventListener('beforeunload', sample);
    });
}
