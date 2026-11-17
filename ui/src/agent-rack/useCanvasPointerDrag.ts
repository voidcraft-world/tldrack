import { onBeforeUnmount } from 'vue';

/** Shared pointer capture for the section heights and the map/artifact divider. */
export function use_canvas_pointer_drag(
    axis: 'x' | 'y',
    start: () => { value: number; scale: number } | null,
    change: (value: number) => void,
    persist: () => void,
    active: (value: boolean) => void,
) {
    let drag: { at: number; value: number; scale: number } | null = null;
    const coordinate = (event: PointerEvent): number => axis === 'x' ? event.clientX : event.clientY;
    function begin(event: PointerEvent): void {
        if (event.button !== 0) return;
        const initial = start();
        if (!initial) return;
        drag = { at: coordinate(event), ...initial };
        (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
        active(true);
        event.preventDefault();
    }
    function move(event: PointerEvent): void {
        if (drag) change(drag.value + (coordinate(event) - drag.at) * drag.scale);
    }
    function end(): void {
        if (!drag) return;
        drag = null;
        persist();
        active(false);
    }
    onBeforeUnmount(end);
    return { begin, move, end };
}
