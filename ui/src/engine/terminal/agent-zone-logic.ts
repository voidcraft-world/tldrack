/**
 * Pure logic for an agent's figure on a map of zones: which zone a session's
 * figure belongs in, how figures sharing a zone fan out, and the per-session
 * accent hue. The office reads the zone and the hue; the fan-out and the
 * scatter are for a host that stands figures on zone platforms. Deliberately
 * free of three, vue and the DOM, so it runs under `node --test`
 * (tests/unit/agent-zone-logic.test.ts). Sibling of terminal-status-logic.ts.
 */

import { fnv1a } from '../../utils/fnv1a.ts';
import type { SessionActivity, TerminalDisplayState } from './terminal-status-logic.ts';

/** 'desk' is the home position (idle / needs-you); 'thinking' is busy with no
 *  tool running (prompt submitted, Claude reasoning); the rest match the
 *  bridge classifier's zones (tldrack/activity.py ZONES). */
export type AgentZoneKey = 'desk' | 'thinking' | 'computer' | 'codebase' | 'aws' | 'web' | 'tests';

export const ACTIVITY_ZONE_KEYS = ['computer', 'codebase', 'aws', 'web', 'tests'] as const;

/**
 * Where the figure belongs this poll. State outranks activity: anything but
 * busy reads "be at the desk" — attention pulses there, free/done idle there.
 * Busy without a classified activity is the THINKING pedestal (prompt
 * submitted, no tool yet). An unrecognized zone from a newer bridge degrades
 * to 'computer' — mirroring the bridge's own unknown-tool fallback.
 */
export function target_zone(
    display: TerminalDisplayState,
    activity: SessionActivity | null | undefined,
): AgentZoneKey {
    if (display !== 'busy') return 'desk';
    const zone = activity?.zone;
    if (!zone) return 'thinking';
    return (ACTIVITY_ZONE_KEYS as readonly string[]).includes(zone) ? (zone as AgentZoneKey) : 'computer';
}

/** Centered slot index (…-1, 0, +1…) so `count` figures sharing a zone line up
 *  around its anchor instead of stacking: multiply by a gap in scene units. */
export function slot_offset(index: number, count: number): number {
    return index - (count - 1) / 2;
}

// FNV-1a: owned by `utils/fnv1a.ts` — see the note there. The module's one
// hash: hue and scatter both derive from it so everything is reload/machine-stable.

/** Stable per-session accent hue in degrees (FNV-1a over the name) — the same
 *  session is always the same color across reloads and machines. */
export function session_hue(session: string): number {
    return fnv1a(session) % 360;
}

/**
 * Deterministic landing spot inside a zone platform, as x/z offsets in
 * FRACTIONS of the platform radius. At the exact center a figure's nameplate
 * collides with the zone's own label (COMPUTER, WEB, …), so each (session,
 * zone) pair hashes to its own spot on an
 * annulus: angle uniform, radius in [min_fraction, max_fraction] biased
 * outward (sqrt = uniform over the ring's area, denser away from center).
 * Hash-derived, not random, so a figure returns to the SAME spot every poll
 * and a mover that skips an unchanged target keeps it standing still.
 */
export function zone_scatter_offset(
    session: string,
    zone: AgentZoneKey,
    min_fraction: number,
    max_fraction: number,
): { x: number; z: number } {
    // Two decorrelated streams from the one hash (swapped-order salts).
    const u_angle = fnv1a(`${session}@${zone}`) / 0x100000000;
    const u_radius = fnv1a(`${zone}@${session}`) / 0x100000000;
    const angle = u_angle * Math.PI * 2;
    const radius = min_fraction + (max_fraction - min_fraction) * Math.sqrt(u_radius);
    return { x: Math.cos(angle) * radius, z: Math.sin(angle) * radius };
}

/** Shared color for a session's NAME text: every surface that prints the name
 *  uses it, so the same session reads the same at a glance. The accent hue
 *  with lifted lightness, so text stays legible over a dark scene. A CSS string
 *  (not a three Color) keeps this module dependency-free; three parses `hsl()`
 *  natively. */
export function session_label_color(session: string): string {
    return `hsl(${session_hue(session)}, 65%, 72%)`;
}
