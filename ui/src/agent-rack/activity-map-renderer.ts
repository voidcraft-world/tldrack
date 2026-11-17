/** TLDRack's activity board. Classification stays with lot-map; the rectangular layout,
 * current-tool connection, context gauge and observation trail are drawn here.
 * This is an SVG image document: escaped text only, no scripts or external resources.
 */
import { display_state_color_css } from '../config/terminal-status-flags.ts';
import { escape_html } from '../utils/trusted-html.ts';
import type { ActivityMap } from './activity-map.ts';
import { activity_board_layout, type BoardSize, type BoardTile } from './activity-map-layout.ts';
import type { StationId } from './lot-map/types.ts';

const STATIONS: Record<StationId, { label: string; glyph: string }> = {
    you: { label: 'You', glyph: 'U' }, memory: { label: 'Memory', glyph: 'M' },
    skills: { label: 'Skills', glyph: '/' }, hooks: { label: 'Hooks', glyph: '↪' },
    model: { label: 'Model', glyph: 'AI' }, agents: { label: 'Agents', glyph: '&' },
    files: { label: 'Files', glyph: '{}' }, shell: { label: 'Shell', glyph: '>_' },
    browser: { label: 'Browser', glyph: 'UI' }, web: { label: 'Network', glyph: '↗' },
};
const INK = '#e4e6e9';
const MUTED = '#b2b5bc';
const EDGE = '#585c64';
function clip(text: string, length: number): string {
    const characters = Array.from(text);
    return escape_html(characters.length > length ? `${characters.slice(0, length - 1).join('')}…` : text);
}
function number(value: number | undefined): number {
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}
function count_text(count: number): string { return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count); }
function bracket(x: number, y: number, w: number, h: number, color: string): string {
    return `<path d="M${x} ${y + 8}v-8h8 M${x + w - 8} ${y}h8v8 M${x + w} ${y + h - 8}v8h-8 M${x + 8} ${y + h}h-8v-8" fill="none" stroke="${color}" stroke-width="1.5"/>`;
}
function station_tile(tile: BoardTile, map: ActivityMap, accent: string): string {
    const { id, x, y, width, height } = tile;
    const active = map.active === id;
    const { label, glyph } = STATIONS[id];
    const count = number(map.board.counts[id]);
    const color = active ? accent : count ? INK : MUTED;
    const font = Math.max(6, Math.min(16, height * 0.42, width / 9));
    const show_icon = width >= 140 && height >= 28;
    const label_x = x + (show_icon ? 40 : 10);
    const baseline = y + height / 2 + font * 0.35;
    const count_width = count ? count_text(count).length * Math.min(10, font) * 0.62 + 12 : 0;
    const name_limit = Math.max(2, Math.floor((width - (show_icon ? 40 : 10) - count_width - 8) / (font * 0.62)));
    return `<g data-station="${id}" data-active="${active}">
      <title>${label}: ${count} observed changes${active ? ' · current activity' : ''}</title>
      <rect x="${x}" y="${y}" width="${width}" height="${height}" rx="2" fill="${active ? accent : '#383c42'}" fill-opacity="${active ? '.10' : '.7'}" stroke="${active ? accent : EDGE}"/>
      ${active ? bracket(x, y, width, height, accent) : ''}
      ${show_icon ? `<text x="${x + 12}" y="${baseline}" font-size="${font}" fill="${color}">${escape_html(glyph)}</text>` : ''}
      <text x="${label_x}" y="${baseline}" font-size="${font}" fill="${color}">${clip(label, name_limit)}</text>
      ${count ? `<text x="${x + width - 9}" y="${y + height / 2 + Math.min(10, font) * 0.35}" font-size="${Math.min(10, font)}" fill="${MUTED}" text-anchor="end">${count_text(count)}</text>` : ''}
    </g>`;
}

export function draw_activity_map(map: ActivityMap, animate: boolean, size: BoardSize = { width: 720, height: 292 }): string {
    const layout = activity_board_layout(size);
    const { width, height, core, tiles: nodes, margin, gap, mode } = layout;
    const accent = display_state_color_css(map.state);
    const active = nodes.find(tile => tile.id === map.active);
    let link = '';
    if (active && mode === 'circuit') {
        const start_x = active.side === 'left' ? core.x : core.x + core.width;
        const start_y = core.y + core.height / 2;
        const end_x = active.side === 'left' ? active.x + active.width : active.x;
        const end_y = active.y + active.height / 2;
        const first_right = nodes.find(tile => tile.side === 'right')!;
        const bend = (start_x + end_x) / 2;
        const path = active.side === 'right' && active.column === 1
            ? `M${start_x} ${start_y}H${(start_x + first_right.x) / 2}V${active.y - gap / 2}H${active.x + active.width / 2}V${active.y}`
            : `M${start_x} ${start_y}H${bend}V${end_y}H${end_x}`;
        link = `<path d="${path}" fill="none" stroke="${accent}" stroke-opacity=".7" stroke-width="1.5"/>`;
        if (animate) link += `<circle r="2.5" fill="${accent}"><animateMotion dur="1.8s" repeatCount="indefinite" path="${path}"/></circle>`;
    }
    const context = typeof map.board.context === 'number' && Number.isFinite(map.board.context)
        ? Math.round(Math.max(0, Math.min(100, map.board.context))) : null;
    const gauge_w = Math.min(150, width * 0.28), gauge_x = width - margin - gauge_w;
    const title_limit = Math.max(8, Math.floor((gauge_x - margin - 16) / 8));
    const short_core = core.height < 65;
    const core_font = Math.min(20, Math.max(12, core.height * 0.22));
    const core_label_y = short_core ? core.y + core.height / 2 + core_font * 0.35 : core.y + core.height * 0.54;
    const core_limit = Math.max(6, Math.floor((core.width - 30) / (core_font * 0.55)));
    const history: StationId[] = [];
    for (const entry of map.board.entries) {
        if (history.at(-1) !== entry.station) history.push(entry.station);
    }
    const trail_count = Math.max(1, Math.min(6, Math.floor((width - margin * 2 - 50) / 76)));
    const trail = history.slice(-trail_count);
    const trail_y = height - layout.footer / 2 + 4;
    const trail_width = (width - margin * 2 - 50) / trail_count;
    const title = map.name || map.session;
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${clip(title, 80)} activity">
      <title>${clip(title, 80)} · ${escape_html(map.label)}. Counts and trail show observed changes, not tool-call totals.</title>
      <style>text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace} .name{font-family:system-ui,sans-serif;font-weight:600}</style>
      <rect width="${width}" height="${height}" fill="#2d3035"/>
      ${bracket(1, 1, width - 2, height - 2, EDGE)}
      <text x="${margin}" y="25" class="name" fill="${INK}" font-size="16">${clip(title, title_limit)}</text>
      <text x="${margin}" y="42" fill="${MUTED}" font-size="11">${clip(map.board.model ?? 'Model unavailable', title_limit + 3)}</text>
      <text x="${gauge_x}" y="23" fill="${MUTED}" font-size="10">CTX ${context === null ? '—' : `${context}% USED`}</text>
      <rect x="${gauge_x}" y="33" width="${gauge_w}" height="4" fill="${EDGE}"/>
      ${context === null ? '' : `<rect x="${gauge_x}" y="33" width="${gauge_w * context / 100}" height="4" fill="${context > 85 ? '#e6b86a' : '#93d6b2'}"/>`}
      ${link}
      <rect x="${core.x}" y="${core.y}" width="${core.width}" height="${core.height}" fill="#363a40" stroke="${EDGE}"/>
      ${bracket(core.x, core.y, core.width, core.height, accent)}
      ${short_core ? '' : `<rect x="${core.x + 15}" y="${core.y + 14}" width="5" height="5" fill="${accent}"/>
      <text x="${core.x + 28}" y="${core.y + 20}" font-size="10" fill="${accent}">${map.active ? 'CURRENT' : 'SESSION'}</text>`}
      <text x="${core.x + 15}" y="${core_label_y}" class="name" font-size="${core_font}" fill="${INK}">${clip(map.label, core_limit)}</text>
      ${short_core ? '' : `<text x="${core.x + 15}" y="${core.y + core.height - 16}" font-size="11" fill="${MUTED}">${map.active ? STATIONS[map.active].label : 'No current tool'}</text>`}
      ${nodes.map(tile => station_tile(tile, map, accent)).join('')}
      <path d="M${margin} ${height - layout.footer}H${width - margin}" stroke="${EDGE}"/>
      <text x="${margin}" y="${trail_y}" fill="${MUTED}" font-size="10">TRAIL</text>
      ${trail.length ? trail.map((id, i) => `<text x="${margin + 50 + i * trail_width}" y="${trail_y}" font-size="11" fill="${i === trail.length - 1 ? INK : MUTED}">${STATIONS[id].label}${i < trail.length - 1 ? ' ›' : ''}</text>`).join('')
        : `<text x="${margin + 50}" y="${trail_y}" fill="${MUTED}" font-size="11">No observations yet</text>`}
    </svg>`;
}
