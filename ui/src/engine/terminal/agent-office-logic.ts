/**
 * The agent OFFICE: every live agent at its own desk, desks grouped into rooms.
 *
 * WHY AN OFFICE. A map that MOVES an agent to the zone it works in answers
 * "which zone", but its busy zones crowd and their markers stack, which is
 * exactly when they need reading. Here an agent never leaves its desk: what it
 * is doing is drawn ON the desk (the screen's word, the lamp's hue, standing up
 * when it wants you). Positions therefore change only when a session appears or
 * disappears: they are STABLE, never sorted by urgency.
 *
 * ROOMS are the teams:
 *   • a spawn FAMILY (a lead and the agents it started, `session-families.ts`)
 *     gets a room of its own, the lead at the first desk;
 *   • every other agent sits in its PROJECT's room (`cohort_label_for` — worktrees
 *     fold onto their repo), the open-plan floor.
 * Reserved service sessions get a SERVICES room of server cabinets. A model label
 * is telemetry, not a role: it must never put `svc-vite` at an agent's desk.
 *
 * Pure: no three, no DOM, no clock — positions are in office units (≈ metres),
 * and the scene (`agent-office-scene.ts`) scales them. Tested by
 * `tests/unit/agent-office-logic.test.ts`.
 */
import { cohort_label_for, type RackSession, type SessionIsland } from './multi-terminal-layout.ts';
import { session_families, flatten_family, type SessionNode } from './session-families.ts';
import { is_service_session, SERVICES_COHORT_LABEL } from './service-sessions.ts';
import { target_zone, type AgentZoneKey } from './agent-zone-logic.ts';
import type { TerminalDisplayState } from './terminal-status-logic.ts';

/** How an agent sits at its desk. */
export type OfficePose =
    /** Busy, a tool running: seated, screen on with the tool's zone word. */
    | 'working'
    /** Busy, no tool yet (Claude reasoning): seated, screen on, `···`. */
    | 'thinking'
    /** Attention — it asked you something: STANDING beside the desk. */
    | 'waiting'
    /** Turn over (free / done / unknown): standing without furniture. Background keeps its desk. */
    | 'idle';

export interface OfficeAgent {
    kind: 'agent' | 'service';
    session: string;
    /** `auto_name` when the bridge has one, else the tmux name — what the rack prints. */
    label: string;
    state: TerminalDisplayState;
    pose: OfficePose;
    /** The monitor's word; '' = screen off. */
    screen: string;
    /** The running tool and its detail, for the tooltip. Null when nothing is classified. */
    tool: string | null;
    detail: string | null;
    /** The first desk of a family room. */
    lead: boolean;
    /** The rack's live pane is on this session. */
    focused: boolean;
    island?: SessionIsland;
}

export interface OfficeDesk {
    agent: OfficeAgent;
    /** Desk or server cabinet centre, office units. */
    x: number;
    z: number;
    /** +1: the agent sits on the −z side and faces +z; −1: the mirror. Desks in a
     *  pod face each other across the pod's spine. */
    facing: 1 | -1;
}

export type OfficeRoomKind = 'team' | 'project' | 'services';

export interface OfficeRoom {
    key: string;
    /** Uppercase, ready to draw. */
    label: string;
    kind: OfficeRoomKind;
    /** Room centre and full extents, office units. */
    x: number;
    z: number;
    width: number;
    depth: number;
    desks: OfficeDesk[];
}

export interface OfficeLayout {
    rooms: OfficeRoom[];
    /** Bounding box of every room, centred on the origin. */
    width: number;
    depth: number;
    agents: number;
    services: number;
    /** Changes exactly when a room or a desk MOVES — the scene rebuilds floors on it. */
    geometry_key: string;
}

/** Office geometry, in office units. One owner: the scene reads these too. */
export const OFFICE_GEOMETRY = {
    /** Desk top. */
    desk_width: 1.6,
    desk_depth: 0.8,
    desk_height: 0.75,
    /** Centre-to-centre along a pod's row. */
    desk_pitch_x: 2.0,
    /** Desk centre's distance from the pod spine: a 0.4-unit gap separates facing tops. */
    desk_offset_z: 0.6,
    /** Where the agent sits, measured from the desk centre, away from the spine. */
    seat_offset_z: 0.8,
    /** Two facing rows of this many desks make one pod. */
    pod_columns: 4,
    /** Spine-to-spine distance between stacked pods. */
    pod_pitch_z: 4.8,
    /** Clear floor between a room's wall and its outermost desk/seat. */
    room_padding: 1.0,
    /** Extra depth along the room's front (+z) edge for its name. */
    label_strip: 1.2,
    /** Floor between neighbouring rooms. */
    room_gap: 1.4,
    /** Narrower aisle joining services to the agent rooms. */
    service_gap: 0.6,
    server_width: 0.9,
    server_height: 1.8,
    server_depth: 0.85,
    server_pitch_x: 1.6,
    server_pitch_z: 2.2,
} as const;

/** The project room for agents with no repo and no cwd. */
export const OFFICE_UNPLACED_LABEL = 'OTHER';

/** A pose for a state + activity. State outranks activity (`agent-zone-logic.ts` `target_zone`). */
export function office_pose(state: TerminalDisplayState, zone: AgentZoneKey): OfficePose {
    if (state === 'attention') return 'waiting';
    if (state !== 'busy') return 'idle';
    return zone === 'thinking' ? 'thinking' : 'working';
}

/** The monitor's word per zone. `desk` never shows: idle agents have no furniture. */
const SCREEN_WORD: Readonly<Record<AgentZoneKey, string>> = {
    desk: '',
    thinking: '···',
    computer: 'CMD',
    codebase: 'CODE',
    tests: 'TEST',
    aws: 'AWS',
    web: 'WEB',
};

/** The screen for a state + zone. A background session keeps its screen on: work is still running. */
export function office_screen(state: TerminalDisplayState, zone: AgentZoneKey): string {
    if (state === 'background') return 'BG';
    if (state === 'attention') return '?';
    return state === 'busy' ? SCREEN_WORD[zone] : '';
}

/** Does this row get a desk? Live agents only; a service shell is machinery. */
export function has_desk(entry: RackSession): boolean {
    return entry.live && !is_service_session(entry.session);
}

function to_agent(entry: RackSession, lead: boolean, focused: string | null): OfficeAgent {
    const zone = target_zone(entry.state, entry.activity);
    const service = is_service_session(entry.session);
    return {
        kind: service ? 'service' : 'agent',
        session: entry.session,
        label: service ? entry.session : entry.auto_name || entry.session,
        state: entry.state,
        pose: service ? 'idle' : office_pose(entry.state, zone),
        screen: service ? (entry.state === 'unknown' ? 'SERVICE' : entry.state.toUpperCase()) : office_screen(entry.state, zone),
        tool: entry.activity?.tool ?? null,
        detail: entry.activity?.detail ?? null,
        lead,
        focused: entry.session === focused,
        ...(!service && entry.island ? { island: entry.island } : {}),
    };
}

interface RoomDraft {
    key: string;
    label: string;
    kind: OfficeRoomKind;
    agents: OfficeAgent[];
}

/** Which rooms there are, and who sits in each, in a stable order. */
export function office_rooms(sessions: readonly RackSession[], focused: string | null = null): RoomDraft[] {
    const seated = sessions.filter(has_desk);
    // Rank is irrelevant here — the office never orders by urgency — so every
    // node ranks alike and the forest falls back to its session-name order.
    const roots = session_families(seated, () => 0);
    const teams: RoomDraft[] = [];
    const projects = new Map<string, OfficeAgent[]>();
    const by_name = (a: OfficeAgent, b: OfficeAgent): number => a.session.localeCompare(b.session);

    for (const root of roots) {
        const members = flatten_family(root)
            .map((position) => position.node)
            .filter((node: SessionNode) => !node.missing);
        if (members.length === 0) continue;
        // A family is a team when it is more than one agent, or when its lead has
        // gone and left the children (the room keeps the lead's name).
        if (members.length > 1 || root.missing) {
            const lead_label = root.missing
                ? (root.entry.auto_name ?? 'parent closed')
                : root.entry.auto_name || root.entry.session;
            const [first, ...rest] = members;
            teams.push({
                key: `team:${root.id}`,
                label: lead_label.toUpperCase(),
                kind: 'team',
                agents: [
                    to_agent(first.entry, !root.missing, focused),
                    ...rest.map((node) => to_agent(node.entry, false, focused)).sort(by_name),
                ],
            });
            continue;
        }
        const entry = members[0].entry;
        const project = cohort_label_for(entry) ?? OFFICE_UNPLACED_LABEL;
        const list = projects.get(project);
        const agent = to_agent(entry, false, focused);
        if (list) list.push(agent);
        else projects.set(project, [agent]);
    }

    const project_rooms: RoomDraft[] = [...projects.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([project, agents]) => ({
            key: `project:${project}`,
            label: project.toUpperCase(),
            kind: 'project' as const,
            agents: agents.sort(by_name),
        }));
    teams.sort((a, b) => a.key.localeCompare(b.key));
    // The open-plan floors first: they are where most agents sit.
    const services = sessions.filter((entry) => entry.live && is_service_session(entry.session))
        .map((entry) => to_agent(entry, false, focused)).sort(by_name);
    return [...project_rooms, ...teams, ...(services.length ? [{
        key: 'services', label: SERVICES_COHORT_LABEL, kind: 'services' as const, agents: services,
    }] : [])];
}

/** Cabinets face the front aisle in compact rows, without the space a chair needs. */
function server_slots(count: number): { x: number; z: number; facing: 1 }[] {
    const g = OFFICE_GEOMETRY;
    const columns = Math.min(g.pod_columns, count);
    return Array.from({ length: count }, (_, i) => ({
        x: (i % columns - (columns - 1) / 2) * g.server_pitch_x,
        z: Math.floor(i / columns) * g.server_pitch_z,
        facing: 1,
    }));
}

function server_room_extent(count: number): { width: number; depth: number } {
    const g = OFFICE_GEOMETRY;
    const columns = Math.min(g.pod_columns, count);
    return {
        width: (columns - 1) * g.server_pitch_x + g.server_width + 2 * g.room_padding,
        depth: (Math.ceil(count / columns) - 1) * g.server_pitch_z + g.server_depth + 2 * g.room_padding + g.label_strip,
    };
}

/** Desk slots for `count` agents, relative to the room's desk-area origin. */
function desk_slots(count: number): { x: number; z: number; facing: 1 | -1 }[] {
    const g = OFFICE_GEOMETRY;
    const per_pod = g.pod_columns * 2;
    const columns = Math.min(g.pod_columns, Math.max(1, Math.ceil(count / 2)));
    const slots: { x: number; z: number; facing: 1 | -1 }[] = [];
    for (let i = 0; i < count; i++) {
        const pod = Math.floor(i / per_pod);
        const j = i % per_pod;
        const column = Math.floor(j / 2);
        // Fill a column's two facing desks before the next column, so two agents
        // face each other rather than sitting side by side.
        const side: 1 | -1 = j % 2 === 0 ? 1 : -1;
        slots.push({
            x: (column - (columns - 1) / 2) * g.desk_pitch_x,
            z: pod * g.pod_pitch_z - side * g.desk_offset_z,
            facing: side,
        });
    }
    return slots;
}

/** A room's full extents for `count` desks. */
export function room_extent(count: number): { width: number; depth: number } {
    const g = OFFICE_GEOMETRY;
    const per_pod = g.pod_columns * 2;
    const columns = Math.min(g.pod_columns, Math.max(1, Math.ceil(count / 2)));
    const pods = Math.max(1, Math.ceil(count / per_pod));
    // A pod spans seat to seat across its spine (0.5 = half a chair). A lone desk
    // is centred on its own, so it spans its seat either side.
    const pod_depth = count <= 1 ? 2 * (g.seat_offset_z + 0.5) : 2 * (g.desk_offset_z + g.seat_offset_z + 0.5);
    return {
        width: columns * g.desk_pitch_x + 2 * g.room_padding,
        depth: (pods - 1) * g.pod_pitch_z + pod_depth + 2 * g.room_padding + g.label_strip,
    };
}

/**
 * The whole office: rooms shelf-packed into rows, centred on the origin.
 *
 * The row budget grows with the square root of the total floor, so a big rack
 * reads as a block rather than one long corridor, and stays deterministic: the
 * same sessions always land in the same place.
 */
export function build_office(sessions: readonly RackSession[], focused: string | null = null): OfficeLayout {
    const g = OFFICE_GEOMETRY;
    const drafts = office_rooms(sessions, focused);
    const sized = drafts.map((draft) => ({
        draft,
        ...(draft.kind === 'services' ? server_room_extent(draft.agents.length) : room_extent(draft.agents.length)),
    }));
    const area = sized.reduce((sum, room) => sum + room.width * room.depth, 0);
    const widest = sized.reduce((max, room) => Math.max(max, room.width), 0);
    const row_budget = Math.max(widest, Math.sqrt(area) * 1.6);

    // Shelf pack: left to right, wrapping to a new row past the budget.
    type Placed = { draft: RoomDraft; width: number; depth: number; left: number; top: number };
    const placed: Placed[] = [];
    let cursor_x = 0;
    let row_top = 0;
    let row_depth = 0;
    for (const room of sized) {
        const service = room.draft.kind === 'services';
        const gap = service ? g.service_gap : g.room_gap;
        let left = cursor_x > 0 ? cursor_x + gap : 0;
        if (left > 0 && left + room.width > row_budget) {
            row_top += row_depth + gap;
            left = 0;
            row_depth = 0;
        }
        // Services come last: tuck below only the rooms in their horizontal span,
        // rather than leaving empty floor for the deepest room elsewhere on the shelf.
        const top = service ? placed.reduce((edge, other) => {
            const overlaps = left < other.left + other.width + gap - 1e-9 &&
                left + room.width + gap > other.left + 1e-9;
            return overlaps ? Math.max(edge, other.top + other.depth + gap) : edge;
        }, 0) : row_top;
        placed.push({ ...room, left, top });
        cursor_x = left + room.width;
        row_depth = Math.max(row_depth, room.depth);
    }
    const total_width = placed.reduce((max, room) => Math.max(max, room.left + room.width), 0);
    const total_depth = placed.reduce((max, room) => Math.max(max, room.top + room.depth), 0);

    const rooms: OfficeRoom[] = placed.map(({ draft, width, depth, left, top }) => {
        const x = left + width / 2 - total_width / 2;
        const z = top + depth / 2 - total_depth / 2;
        // Desks sit in the room minus its label strip (the +z edge).
        const desk_area_z = z - g.label_strip / 2;
        const slots = draft.kind === 'services' ? server_slots(draft.agents.length) : desk_slots(draft.agents.length);
        // Centre the desk cluster in the desk area (every room has ≥ 1 desk).
        const z0 = desk_area_z - (Math.min(...slots.map((s) => s.z)) + Math.max(...slots.map((s) => s.z))) / 2;
        return {
            key: draft.key,
            label: draft.label,
            kind: draft.kind,
            x,
            z,
            width,
            depth,
            desks: draft.agents.map((agent, i) => ({
                agent,
                x: x + slots[i].x,
                z: z0 + slots[i].z,
                facing: slots[i].facing,
            })),
        };
    });

    const geometry_key = rooms
        .map((room) => `${room.key}@${room.x.toFixed(2)},${room.z.toFixed(2)}:${room.desks.map((d) => d.agent.session).join(',')}`)
        .join('|');
    return {
        rooms,
        width: total_width,
        depth: total_depth,
        agents: rooms.filter((room) => room.kind !== 'services').reduce((sum, room) => sum + room.desks.length, 0),
        services: rooms.find((room) => room.kind === 'services')?.desks.length ?? 0,
        geometry_key,
    };
}

/** The one-line readout for the window's head: `7 agents · 2 want you`. */
export function office_summary(layout: OfficeLayout): string {
    const waiting = layout.rooms.reduce(
        (sum, room) => sum + room.desks.filter((desk) => desk.agent.pose === 'waiting').length,
        0,
    );
    const agents = `${layout.agents} ${layout.agents === 1 ? 'agent' : 'agents'}`;
    const services = layout.services ? ` · ${layout.services} ${layout.services === 1 ? 'service' : 'services'}` : '';
    return `${agents}${services}${waiting > 0 ? ` · ${waiting} want${waiting === 1 ? 's' : ''} you` : ''}`;
}
