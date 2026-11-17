/** Perspective nameplates, with LOCAL breathing room. No pixel-size floor or screen-edge cards.
 * A label may grow/nudge only into space clear of the projected furniture AND nearer labels.
 * Where no such space exists it keeps its original 3D size; the farther overlap fades.
 */
export interface LabelRect { left: number; top: number; width: number; height: number }
export interface LabelAnchor extends LabelRect {
    session: string;
    /** Camera-space depth: smaller is nearer. */
    depth: number;
    focused: boolean;
    lines: number;
}
export interface LabelPlacement extends LabelRect {
    session: string;
    scale: number;
    dx: number;
    dy: number;
    opacity: number;
}
function overlap_area(a: LabelRect, b: LabelRect): number {
    return Math.max(0, Math.min(a.left + a.width, b.left + b.width) - Math.max(a.left, b.left))
        * Math.max(0, Math.min(a.top + a.height, b.top + b.height) - Math.max(a.top, b.top));
}
function padded(rect: LabelRect, pad: number): LabelRect {
    return { left: rect.left - pad, top: rect.top - pad, width: rect.width + pad * 2, height: rect.height + pad * 2 };
}
function label_rect(anchor: LabelAnchor, scale: number, dx = 0, dy = 0): LabelRect {
    return { left: anchor.left + (anchor.width * (1 - scale)) / 2 + dx,
        top: anchor.top + (anchor.height * (1 - scale)) / 2 + dy, width: anchor.width * scale, height: anchor.height * scale };
}

export function place_office_labels(anchors: readonly LabelAnchor[], width: number, height: number,
    models: readonly LabelRect[] = []): LabelPlacement[] {
    if (width <= 0 || height <= 0) return [];
    const placed: LabelPlacement[] = [];
    const ordered = [...anchors].sort((a, b) => a.depth - b.depth || a.session.localeCompare(b.session));
    for (const anchor of ordered) {
        // A cap prevents close-up titles filling the scene; distance still controls all smaller sizes.
        const cap = Math.min(1.5, anchor.lines * 22 / Math.max(1, anchor.height), width * 0.45 / Math.max(1, anchor.width));
        const natural = Math.min(1, cap);
        const radius = Math.min(40, Math.max(12, anchor.height * 1.4));
        const offsets = [[0, 0], [-radius, 0], [radius, 0], [0, -radius], [-radius, -radius / 2], [radius, -radius / 2]];
        let chosen: LabelPlacement | null = null;
        const clear = (box: LabelRect): boolean => box.left >= 4 && box.top >= 4 && box.left + box.width <= width - 4
            && box.top + box.height <= height - 4
            && !models.some(model => overlap_area(box, padded(model, 3)) > 0)
            && !placed.some(label => overlap_area(box, padded(label, 3)) > 0);
        for (const scale of [...new Set([cap, Math.min(cap, 1.25), natural])]) {
            for (const [dx, dy] of offsets) {
                const box = label_rect(anchor, scale, dx, dy);
                if (!clear(box)) continue;
                chosen = { ...box, session: anchor.session, scale, dx, dy, opacity: 1 };
                break;
            }
            if (chosen) break;
        }
        if (!chosen) chosen = { ...label_rect(anchor, natural), session: anchor.session, scale: natural, dx: 0, dy: 0, opacity: 1 };
        const area = Math.max(1, chosen.width * chosen.height);
        const cover = Math.min(1, placed.reduce((sum, label) => sum + overlap_area(chosen!, label), 0) / area);
        // Only farther overlapping names recede. The nearest name stays at full strength.
        chosen.opacity = 1 - Math.min(1, cover * 2) * 0.72;
        placed.push(chosen);
    }
    return placed;
}
