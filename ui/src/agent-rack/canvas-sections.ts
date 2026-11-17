export const CANVAS_SECTION_LIMITS = {
    office: { min: 180, max: 1200 },
    details: { min: 120, max: 600 },
    document: { min: 180, max: 1200 },
} as const;
export type CanvasSection = keyof typeof CANVAS_SECTION_LIMITS;

export const DEFAULT_MAP_SHARE = 75;
export const MIN_MAP_SHARE = 15;
export const MAX_MAP_SHARE = 85;

export function clamp_map_share(value: number): number {
    return Math.max(MIN_MAP_SHARE, Math.min(MAX_MAP_SHARE, Number.isFinite(value) ? value : DEFAULT_MAP_SHARE));
}

export function parse_map_share(raw: string | null): number {
    if (!raw?.trim()) return DEFAULT_MAP_SHARE;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? clamp_map_share(value) : DEFAULT_MAP_SHARE;
}

/** Percent per CSS pixel, measured after subtracting the visible divider. */
export function map_drag_scale(row_width: number, divider_width: number): number | null {
    if (!Number.isFinite(row_width) || !Number.isFinite(divider_width) || divider_width < 0) return null;
    const available = row_width - divider_width;
    return available > 0 ? 100 / available : null;
}

export function clamp_section_height(section: CanvasSection, height: number): number {
    const { min, max } = CANVAS_SECTION_LIMITS[section];
    return Math.round(Math.max(min, Math.min(max, Number.isFinite(height) ? height : min)));
}

export function parse_section_height(section: CanvasSection, raw: string | null): number | null {
    if (!raw?.trim()) return null;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? clamp_section_height(section, value) : null;
}
