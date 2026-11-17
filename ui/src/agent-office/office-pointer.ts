type OfficePointer = Pick<PointerEvent, 'pointerId' | 'pointerType' | 'clientX' | 'clientY' | 'button'>;
const CLICK_SLOP_PX = 5;

/** Classifies taps and one-finger strip scrolling; OrbitControls owns camera gestures. */
export function office_pointer_gesture(scroll_touch: boolean) {
    const pointers = new Map<number, { y: number }>();
    let press: { id: number; x: number; y: number; moved: boolean } | null = null;
    let multi = false;

    function down(event: OfficePointer): void {
        if (pointers.size === 0) {
            multi = false;
            press = event.button === 0
                ? { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false } : null;
        }
        pointers.set(event.pointerId, { y: event.clientY });
        if (pointers.size > 1) { multi = true; press = null; }
    }
    function moved(event: OfficePointer): void {
        if (press?.id === event.pointerId && Math.hypot(event.clientX - press.x, event.clientY - press.y) > CLICK_SLOP_PX) {
            press.moved = true;
        }
    }
    function move(event: OfficePointer): number {
        const previous = pointers.get(event.pointerId);
        if (!previous) return 0;
        const delta = previous.y - event.clientY;
        previous.y = event.clientY;
        moved(event);
        return scroll_touch && event.pointerType === 'touch' && !multi && pointers.size === 1 && press?.moved ? delta : 0;
    }
    function up(event: OfficePointer): boolean {
        moved(event);
        const tap = !multi && press?.id === event.pointerId && !press.moved && pointers.size === 1;
        pointers.delete(event.pointerId);
        press = null;
        if (pointers.size === 0) multi = false;
        return tap;
    }
    function cancel(pointer_id?: number): void {
        press = null;
        if (pointer_id === undefined) pointers.clear();
        else pointers.delete(pointer_id);
        multi = pointers.size > 0;
    }
    return { down, move, up, cancel, pressed: () => pointers.size > 0 };
}
