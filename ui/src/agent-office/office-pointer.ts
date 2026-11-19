type OfficePointer = Pick<PointerEvent, 'pointerId' | 'pointerType' | 'clientX' | 'clientY' | 'button'>
    & Partial<Pick<PointerEvent, 'ctrlKey' | 'timeStamp'>>;
const CLICK_SLOP_PX = 5;
/** A finger held still this long asks for the menu: touch has no second button. */
export const LONG_PRESS_MS = 500;

/** What a release was: `select` picks the agent under it, `menu` opens its actions
 *  (the second button, ctrl-click, or a long press), null was a camera gesture. */
export type OfficeTap = 'select' | 'menu' | null;

/** Classifies taps and one-finger strip scrolling; OrbitControls owns camera gestures.
 *  The second button PANS when it drags, so a menu is decided here, on release, and
 *  never on the browser's `contextmenu` event, which fires on the press. */
export function office_pointer_gesture(scroll_touch: boolean) {
    const pointers = new Map<number, { y: number }>();
    let press: { id: number; x: number; y: number; moved: boolean; menu: boolean; at: number } | null = null;
    let multi = false;

    function down(event: OfficePointer): void {
        if (pointers.size === 0) {
            multi = false;
            press = event.button === 0 || event.button === 2
                ? { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false,
                    menu: event.button === 2 || event.ctrlKey === true, at: event.timeStamp ?? 0 } : null;
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
    function up(event: OfficePointer): OfficeTap {
        moved(event);
        const held = press && !multi && press.id === event.pointerId && !press.moved && pointers.size === 1 ? press : null;
        pointers.delete(event.pointerId);
        press = null;
        if (pointers.size === 0) multi = false;
        if (!held) return null;
        const long_press = event.pointerType === 'touch' && (event.timeStamp ?? 0) - held.at >= LONG_PRESS_MS;
        return held.menu || long_press ? 'menu' : 'select';
    }
    function cancel(pointer_id?: number): void {
        press = null;
        if (pointer_id === undefined) pointers.clear();
        else pointers.delete(pointer_id);
        multi = pointers.size > 0;
    }
    return { down, move, up, cancel, pressed: () => pointers.size > 0 };
}
