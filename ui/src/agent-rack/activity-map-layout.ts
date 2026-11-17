/** The board fills its measured image viewport. Wide panes use a circuit; taller panes
 * put the current task above a tile grid. No fixed-aspect artwork or stretched text. */
import type { StationId } from './lot-map/types.ts';
export interface BoardRect { x: number; y: number; width: number; height: number }
export interface BoardTile extends BoardRect { id: StationId; side: 'left' | 'right' | 'grid'; column: number }
export interface BoardSize { width: number; height: number }
export interface BoardLayout extends BoardSize {
    mode: 'circuit' | 'grid';
    core: BoardRect;
    tiles: BoardTile[];
    margin: number;
    gap: number;
    header: number;
    footer: number;
}
const LEFT: StationId[] = ['you', 'memory', 'skills', 'hooks'];
const RIGHT: StationId[] = ['model', 'agents', 'files', 'shell', 'browser', 'web'];
const ORDER = [...LEFT, ...RIGHT];
function clamp(value: number, lo: number, hi: number): number { return Math.max(lo, Math.min(hi, value)); }

export function activity_board_layout(size: BoardSize): BoardLayout {
    const width = Number.isFinite(size.width) ? Math.max(180, Math.round(size.width)) : 720;
    const height = Number.isFinite(size.height) ? Math.max(160, Math.round(size.height)) : 292;
    const margin = clamp(width * 0.025, 10, 24);
    const header = height < 280 ? 48 : 60, footer = height < 280 ? 28 : 38;
    const gap = clamp(height * 0.025, 6, 16);
    const inner = width - margin * 2, body = height - header - footer;
    const mode = width >= 620 && width / height >= 1.35 ? 'circuit' : 'grid';
    if (mode === 'circuit') {
        const lane = clamp(width * 0.035, 20, 36);
        const left_w = inner * 0.2, core_w = inner * 0.25;
        const right_x = margin + left_w + lane * 2 + core_w;
        const tile_w = (width - margin - right_x - gap) / 2;
        const core_h = Math.min(body, clamp(body * 0.6, 68, 170));
        return { width, height, margin, gap, header, footer, mode,
            core: { x: margin + left_w + lane, y: header + (body - core_h) / 2, width: core_w, height: core_h },
            tiles: [
                ...LEFT.map((id, i) => ({ id, x: margin, y: header + i * (body + gap) / 4,
                    width: left_w, height: (body - gap * 3) / 4, side: 'left' as const, column: 0 })),
                ...RIGHT.map((id, i) => ({ id, x: right_x + i % 2 * (tile_w + gap),
                    y: header + Math.floor(i / 2) * (body + gap) / 3,
                    width: tile_w, height: (body - gap * 2) / 3, side: 'right' as const, column: i % 2 })),
            ] };
    }
    const core_h = Math.min(body * 0.28, 116);
    const grid_y = header + core_h + gap, grid_h = body - core_h - gap;
    const columns = clamp(Math.round(Math.sqrt(10 * inner / (3 * Math.max(1, grid_h)))), 2, width < 340 ? 2 : 5);
    const rows = Math.ceil(ORDER.length / columns);
    const tile_h = (grid_h - (rows - 1) * gap) / rows;
    return { width, height, margin, gap, header, footer, mode,
        core: { x: margin, y: header, width: inner, height: core_h },
        tiles: ORDER.map((id, i) => {
            const row = Math.floor(i / columns), column = i % columns;
            const in_row = Math.min(columns, ORDER.length - row * columns);
            const tile_w = (inner - gap * (in_row - 1)) / in_row;
            return { id, side: 'grid', column, x: margin + column * (tile_w + gap), y: grid_y + row * (tile_h + gap), width: tile_w, height: tile_h };
        }) };
}
