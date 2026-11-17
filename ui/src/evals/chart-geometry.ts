/**
 * The arithmetic under the EVALS charts: a linear scale, round ticks and the two path shapes.
 * Pure, so the page's charts are a few SVG elements and no chart library.
 */

export type Point = readonly [x: number, y: number];
export type Scale = (value: number) => number;

/** Maps `domain` onto `range`, straight through. A flat domain maps to the range's start. */
export function linear_scale(domain: readonly [number, number], range: readonly [number, number]): Scale {
    const span = domain[1] - domain[0];
    if (span === 0) return () => range[0];
    return (value) => range[0] + ((value - domain[0]) / span) * (range[1] - range[0]);
}

/** The smallest round number (1, 2, 2.5 or 5 times a power of ten) that is at least `value`. */
export function nice_ceiling(value: number): number {
    if (!(value > 0)) return 1;
    const power = 10 ** Math.floor(Math.log10(value));
    const lead = value / power;
    const step = [1, 2, 2.5, 5, 10].find((candidate) => candidate >= lead - 1e-9)!;
    return Number((step * power).toPrecision(12));
}

/** The top of an axis that starts at 0 and holds `value`: the first of about `count` round steps at or above it. */
export function nice_top(value: number, count = 5): number {
    if (!(value > 0)) return 1;
    const step = nice_ceiling(value / count);
    return Number((Math.ceil(value / step - 1e-9) * step).toPrecision(12));
}

/** About `count` round ticks across `[lo, hi]`, both ends included when they are round. */
export function nice_ticks(lo: number, hi: number, count = 5): number[] {
    if (!(hi > lo)) return [lo];
    const step = nice_ceiling((hi - lo) / Math.max(1, count));
    const ticks: number[] = [];
    for (let tick = Math.ceil(lo / step - 1e-9) * step; tick <= hi + step * 1e-9; tick += step) {
        ticks.push(Number(tick.toPrecision(12)));
    }
    return ticks;
}

const xy = ([x, y]: Point): string => `${round(x)},${round(y)}`;
const round = (value: number): number => Math.round(value * 100) / 100;

/** A polyline through the points. */
export function line_path(points: readonly Point[]): string {
    return points.map((point, i) => `${i === 0 ? 'M' : 'L'}${xy(point)}`).join('');
}

/**
 * A staircase through the points, left to right. `before`: a point's value holds back to the
 * point before it (the riser stands at the earlier x). `after`: it holds on to the next one.
 */
export function step_path(points: readonly Point[], hold: 'before' | 'after'): string {
    return points
        .map((point, i) => {
            if (i === 0) return `M${xy(point)}`;
            const prev = points[i - 1]!;
            const corner: Point = hold === 'before' ? [prev[0], point[1]] : [point[0], prev[1]];
            return `L${xy(corner)}L${xy(point)}`;
        })
        .join('');
}

/** A closed area between a path's points and a flat baseline at `base_y`. */
export function area_to_baseline(path: string, points: readonly Point[], base_y: number): string {
    if (points.length === 0) return '';
    return `${path}L${round(points.at(-1)![0])},${round(base_y)}L${round(points[0]![0])},${round(base_y)}Z`;
}

/** The index of the value nearest `target`; -1 on an empty list. */
export function nearest_index(values: readonly number[], target: number): number {
    let best = -1;
    let distance = Infinity;
    values.forEach((value, i) => {
        const d = Math.abs(value - target);
        if (d < distance) {
            distance = d;
            best = i;
        }
    });
    return best;
}

/** `value` held inside `[lo, hi]`. */
export function clamp(value: number, lo: number, hi: number): number {
    return Math.min(hi, Math.max(lo, value));
}

/**
 * A chart's keyboard readout, one stop per arrow key: the stop after `at`, held inside
 * `[0, last]`. Escape leaves the chart (null). Any other key is not the chart's: undefined.
 */
export function arrow_step(key: string, at: number | null, last: number): number | null | undefined {
    if (key === 'ArrowRight') return Math.min(last, (at ?? -1) + 1);
    if (key === 'ArrowLeft') return Math.max(0, (at ?? last + 1) - 1);
    return key === 'Escape' ? null : undefined;
}

/** Where a tick's label hangs on a [0, 1] axis: the two end labels stay inside the plot. */
export function unit_tick_anchor(tick: number): 'start' | 'middle' | 'end' {
    return tick === 0 ? 'start' : tick === 1 ? 'end' : 'middle';
}

/** The ticks of a [0, 1] axis. */
export const UNIT_TICKS = [0, 0.25, 0.5, 0.75, 1] as const;
