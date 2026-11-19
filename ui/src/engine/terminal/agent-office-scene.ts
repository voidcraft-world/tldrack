/**
 * The agent OFFICE in three.js: a `Group` you add to any scene, fed an
 * `OfficeLayout` (`agent-office-logic.ts` owns where everything goes and why).
 *
 * PORTABLE ON PURPOSE. Every surface is `MeshBasicMaterial` with the shading
 * BAKED into vertex colours (top face bright, sides darker), so the office
 * reads as solid boxes in a scene with no lights at all, and a host that adds
 * the group to its own scene needs no light that would relight its other
 * surfaces. The office window (`src/agent-office/`) mounts this same group.
 *
 * HUE MEANS STATE. A hue is spent only where it says something: the robots,
 * desks and floors are neutral graphite; the state hue is on the head lamp, the
 * screen and the nameplate's dot; the session's identity hue is on the
 * nameplate's name only; chrome (room outlines, labels, the focus ring) is the
 * one mint.
 *
 * MOTION, all of it in HELD STEPS, never eased (`OFFICE_MOTION`). The lamp of an
 * agent that wants you pulses (the alarm), and that agent hops behind its chair
 * with one hand up, waving: a standing robot is easy to miss across a full floor,
 * a hopping one is not. A WORKING agent types: its arms step between two held
 * angles. A worktree island rides its pool (`office-island.ts`). Nothing else
 * moves; a desk only changes place when a session comes or goes. The hop, the
 * wave and the water hold still under reduced motion (`should_animate`); the
 * raised hand and the pool stay, because they are the state, not the motion.
 */
import {
    BoxGeometry,
    BufferAttribute,
    CanvasTexture,
    Color,
    Group,
    LinearMipmapLinearFilter,
    LineBasicMaterial,
    LineLoop,
    Line,
    BufferGeometry,
    Float32BufferAttribute,
    Mesh,
    MeshBasicMaterial,
    PlaneGeometry,
    SRGBColorSpace,
    Sprite,
    SpriteMaterial,
    TorusGeometry,
    Vector3,
    Vector2,
    type PerspectiveCamera,
    type Material,
    type Object3D,
    type Raycaster,
} from 'three';
import { place_office_labels, type LabelAnchor, type LabelRect, type LabelPlacement } from '../../agent-office/office-labels.ts';
import { display_state_color, state_pulse_period_ms } from '../../config/terminal-status-flags.ts';
import { should_animate } from '../../config/fui-motion-flags.ts';
import { session_label_color } from './agent-zone-logic.ts';
import { resource_readout, type OfficeResources } from './office-resources.ts';
import { OfficeIsland, OFFICE_ISLAND_LIFT, OFFICE_WATER_LEVEL, float_phase, island_float } from './office-island.ts';
import { island_status } from './island-status.ts';
import { OFFICE_GEOMETRY, type OfficeDesk, type OfficeLayout, type OfficeRoom } from './agent-office-logic.ts';

/** The office's motion, in one place. */
export const OFFICE_MOTION = {
    /** Working agents type. False holds every arm still. */
    typing: true,
    /** One typing step; two steps make a keystroke. */
    typing_step_ms: 180,
    /** An agent that wants you hops and waves. False leaves it standing, hand up. */
    hop: true,
    /** Hops per alarm period, each this long in the air and this high (office units).
     *  The height keeps the lamp under the nameplate at the top of a hop. */
    hops: 2,
    hop_air_ms: 400,
    hop_height: 0.2,
    /** One held frame of a hop. */
    hop_step_ms: 50,
    /** One held position of the raised hand. */
    wave_step_ms: 200,
    /** Islands ride their pools and the water runs. False holds both still. */
    floating: true,
    /** One held frame of the water and the swell. */
    float_step_ms: 90,
} as const;

/** The alarm period a hop falls back to when the status flags give none. */
const HOP_PERIOD_MS = 1600;
/** The water's clock wraps here, seconds: every rate in its shader is a multiple of
 *  0.1 rad/s, so 20π is a whole number of each cycle and the wrap never shows. */
const WATER_WRAP_S = Math.PI * 20;

/** How high a waiting robot is off the floor at `now_ms`, in office units: `hops`
 *  quick hops at the head of each `period_ms`, then standing until the next. */
export function office_hop(now_ms: number, period_ms: number = HOP_PERIOD_MS): number {
    const m = OFFICE_MOTION;
    const at = Math.floor((((now_ms % period_ms) + period_ms) % period_ms) / m.hop_step_ms) * m.hop_step_ms;
    if (at >= m.hops * m.hop_air_ms) return 0;
    const u = (at % m.hop_air_ms) / m.hop_air_ms;
    return m.hop_height * 4 * u * (1 - u);
}

const PALETTE = {
    ground: 0x0b0f0e,
    floor: 0x1a1f22,
    floor_team: 0x1d2226,
    desk: 0x8a8f94,
    leg: 0x4a4f54,
    chair: 0x30353a,
    monitor: 0x23272b,
    screen_off: 0x111416,
    robot: 0xd8dde1,
    robot_dark: 0x7c8388,
    eye: 0x0e1214,
    chrome: 0x8ccdaa,
    text_dim: '#9aa6ad',
} as const;

/** Per-face shade of a box: +x, −x, +y (top), −y, +z, −z — BoxGeometry's group order. */
const FACE_SHADE = [0.68, 0.62, 1.0, 0.4, 0.84, 0.76] as const;

/** A box whose faces carry baked shading — reads as a solid with no lights. */
function shaded_box(width: number, height: number, depth: number, hex: number): BoxGeometry {
    const geometry = new BoxGeometry(width, height, depth);
    const base = new Color(hex);
    const colors = new Float32Array(geometry.attributes.position.count * 3);
    // 4 vertices per face, faces in FACE_SHADE order (1×1×1 segments).
    for (let face = 0; face < 6; face++) {
        const shade = FACE_SHADE[face];
        for (let v = 0; v < 4; v++) {
            const i = (face * 4 + v) * 3;
            colors[i] = base.r * shade;
            colors[i + 1] = base.g * shade;
            colors[i + 2] = base.b * shade;
        }
    }
    geometry.setAttribute('color', new BufferAttribute(colors, 3));
    return geometry;
}

/** A closed rectangle outline on the floor plane (y = 0), centred on the origin. */
function rect_outline(width: number, depth: number, material: LineBasicMaterial): LineLoop {
    const w = width / 2;
    const d = depth / 2;
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute([-w, 0, -d, w, 0, -d, w, 0, d, -w, 0, d], 3));
    return new LineLoop(geometry, material);
}

interface LabelLine {
    text: string;
    color: string;
}

/**
 * A canvas of text lines → a texture, plus its aspect (width / height).
 *
 * `fill` paints the whole canvas first — REQUIRED on an opaque material, where a
 * transparent pixel's black rgb would multiply the material colour to black.
 * `backing` is a translucent plate behind floating text, so a name stays legible
 * over a desk or a bright screen. Supersample the glyphs without changing their
 * layout; mipmaps integrate small strokes instead of skipping texels at distance,
 * and anisotropy preserves the floor inscriptions at oblique angles. Three clamps
 * the requested anisotropy to the GPU's supported limit.
 */
function text_texture(
    lines: readonly LabelLine[],
    opts: { px?: number; fill?: string; backing?: string } = {},
): { texture: CanvasTexture; aspect: number } {
    const px = opts.px ?? 44;
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('agent office: this browser gave no 2D canvas for a label');
    const font = `600 ${px}px "SF Mono", Menlo, Consolas, monospace`;
    ctx.font = font;
    const pad = Math.round(px * 0.35);
    const line_h = Math.round(px * 1.2);
    const width = Math.ceil(Math.max(...lines.map((line) => ctx.measureText(line.text).width), 1)) + pad * 2;
    const height = line_h * lines.length + pad * 2;
    const raster_scale = 2;
    canvas.width = width * raster_scale;
    canvas.height = height * raster_scale;
    ctx.scale(raster_scale, raster_scale);
    const paint = opts.fill ?? opts.backing;
    if (paint) {
        ctx.fillStyle = paint;
        ctx.fillRect(0, 0, width, height);
    }
    ctx.font = font;
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = '#050706';
    ctx.lineWidth = px * 0.1;
    lines.forEach((line, i) => {
        const y = pad + line_h * (i + 0.5);
        // Opaque monitors already have dark ink; only floating/floor text needs a halo.
        if (!opts.fill) ctx.strokeText(line.text, pad, y);
        ctx.fillStyle = line.color;
        ctx.fillText(line.text, pad, y);
    });
    const texture = new CanvasTexture(canvas);
    texture.colorSpace = SRGBColorSpace;
    texture.minFilter = LinearMipmapLinearFilter;
    texture.generateMipmaps = true;
    texture.anisotropy = 16;
    return { texture, aspect: canvas.width / canvas.height };
}

function hex_css(hex: number): string {
    return `#${hex.toString(16).padStart(6, '0')}`;
}

/** Shared geometry: one of each part for the whole office. */
function office_parts() {
    const g = OFFICE_GEOMETRY;
    return {
        desk_top: shaded_box(g.desk_width, 0.06, g.desk_depth, PALETTE.desk),
        desk_leg: shaded_box(0.06, g.desk_height - 0.06, g.desk_depth * 0.9, PALETTE.leg),
        monitor: shaded_box(0.72 * 1.5, 0.44 * 1.5, 0.05, PALETTE.monitor),
        monitor_stand: shaded_box(0.06, 0.22, 0.06, PALETTE.leg),
        seat: shaded_box(0.5, 0.08, 0.5, PALETTE.chair),
        seat_back: shaded_box(0.5, 0.5, 0.06, PALETTE.chair),
        body: shaded_box(0.42, 0.48, 0.3, PALETTE.robot),
        head: shaded_box(0.38, 0.3, 0.32, PALETTE.robot),
        arm: shaded_box(0.08, 0.3, 0.08, PALETTE.robot_dark),
        leg: shaded_box(0.1, ROBOT_LEG, 0.12, PALETTE.robot_dark),
        eye: shaded_box(0.07, 0.07, 0.02, PALETTE.eye),
        antenna: shaded_box(0.025, 0.16, 0.025, PALETTE.robot_dark),
        lamp: shaded_box(0.11, 0.11, 0.11, 0xffffff),
        screen: new PlaneGeometry(0.64, 0.36),
        hitbox: new BoxGeometry(g.desk_width, 1.9, DESK_FOOTPRINT.depth),
        server: shaded_box(g.server_width, g.server_height, g.server_depth, PALETTE.monitor),
        server_tray: shaded_box(g.server_width - 0.12, 0.18, 0.04, PALETTE.leg),
        server_vent: shaded_box(0.46, 0.035, 0.02, PALETTE.eye),
        server_hitbox: new BoxGeometry(g.server_width, g.server_height, g.server_depth + 0.1),
    };
}

/** A standing robot's legs, and how far it steps back behind its chair to stand. */
const ROBOT_LEG = 0.8;
const STAND_BACK = 0.45;
/** The raised hand: the arm swung up past vertical and stretched to clear the head,
 *  leaning out from the body between two held angles (radians from hanging). */
const RAISED_ARM = { stretch: 1.5, angles: [Math.PI - 0.12, Math.PI - 0.55] } as const;
const ARM_LENGTH = 0.3;
/** The robot's body sits this high when seated (on the seat) — its group's y = 0. */
const SEATED_BODY_BOTTOM = 0.5;

/** One desk's floor footprint in its rig's frame — desk front edge to behind a
 *  standing robot — shared by the pick box and the focus ring so the two cannot drift. */
const DESK_FOOTPRINT = (() => {
    const g = OFFICE_GEOMETRY;
    const front = g.desk_depth / 2;
    const back = -(g.seat_offset_z + STAND_BACK + 0.2);
    return { depth: front - back, centre_z: (front + back) / 2 };
})();
type OfficeParts = ReturnType<typeof office_parts>;

/** One session's furniture: a robot at a desk, or a server cabinet facing +z. */
class OfficeRig {
    /** The desk's place on the floor. It never moves between layouts. */
    readonly root = new Group();
    /** Everything that stands on the floor, or on the island when there is one: the
     *  island lifts it and the swell moves it, and the pool stays on `root`. */
    private readonly deck = new Group();
    readonly hitbox: Mesh;
    private readonly robot = new Group();
    /** Shoulder pivots: an arm swings from its top. */
    private readonly arms: Group[] = [];
    /** The arm an agent that wants you raises, and the lamp-hued hand at its end. */
    private raised: { pivot: Group; arm: Mesh; hand: Mesh } | null = null;
    /** Where the robot stands or sits, before a hop moves it. Labels read this, so a
     *  plan made mid-hop places a name exactly where one made on the floor would. */
    private robot_y = 0;
    private waiting = false;
    private readonly phase: number;
    private readonly legs: Mesh[] = [];
    private readonly lamp_material: MeshBasicMaterial;
    private readonly screen_material: MeshBasicMaterial;
    private readonly focus_ring: LineLoop;
    private nameplate: Sprite | null = null;
    private readonly beacon: Mesh;
    private readonly label_origin = new Vector3();
    private readonly label_size = new Vector2();
    private label_lines = 1;
    private label_alpha = 1;
    private label_target_alpha = 1;
    private readonly leader = new Line(new BufferGeometry().setAttribute('position', new Float32BufferAttribute(new Float32Array(6), 3)),
        new LineBasicMaterial({ color: PALETTE.chrome, transparent: true, opacity: 0.5, depthWrite: false }));
    private island: OfficeIsland | null = null;
    private label_key = '';
    private screen_key = '';
    private state_hex = 0;
    private pulse_ms: number | null = null;
    private typing = false;
    private hovered = false;
    readonly session: string;
    readonly kind: OfficeDesk['agent']['kind'];
    private readonly floating_names: boolean;

    constructor(
        session: string,
        kind: OfficeDesk['agent']['kind'],
        parts: OfficeParts,
        shared: { solid: MeshBasicMaterial; chrome: LineBasicMaterial; hitbox: MeshBasicMaterial },
        floating_names: boolean,
    ) {
        this.session = session;
        this.kind = kind;
        this.floating_names = floating_names;
        this.phase = float_phase(session);
        this.root.add(this.deck);
        const g = OFFICE_GEOMETRY;
        const solid = shared.solid;
        this.screen_material = new MeshBasicMaterial({ color: PALETTE.screen_off });
        this.lamp_material = new MeshBasicMaterial({ color: 0xffffff });
        const add = (geometry: BufferGeometry, x: number, y: number, z: number, parent: Object3D = this.deck): Mesh => {
            const mesh = new Mesh(geometry, solid);
            mesh.position.set(x, y, z);
            parent.add(mesh);
            return mesh;
        };
        if (kind === 'service') {
            add(parts.server, 0, g.server_height / 2, 0);
            const front = g.server_depth / 2;
            // Stacked rack units and vents give the cabinet a server silhouette;
            // only the indicator lights and readout carry the session's state hue.
            for (let row = 0; row < 5; row++) {
                const y = 0.22 + row * 0.23;
                add(parts.server_tray, 0, y, front + 0.02);
                add(parts.server_vent, -0.08, y, front + 0.05);
                const led = new Mesh(parts.lamp, this.lamp_material);
                led.scale.set(0.45, 0.45, 0.2);
                led.position.set(0.3, y, front + 0.06);
                this.deck.add(led);
            }
        } else {
            // The desk: the agent sits on its −z side, the monitor stands on its +z half.
            add(parts.desk_top, 0, g.desk_height - 0.03, 0);
            add(parts.desk_leg, -g.desk_width / 2 + 0.05, (g.desk_height - 0.06) / 2, 0);
            add(parts.desk_leg, g.desk_width / 2 - 0.05, (g.desk_height - 0.06) / 2, 0);
            add(parts.monitor_stand, 0, g.desk_height + 0.11, 0.2);
            add(parts.monitor, 0, g.desk_height + 0.52, 0.2);
            // The chair.
            const seat_z = -g.seat_offset_z;
            add(parts.seat, 0, 0.45, seat_z);
            add(parts.seat_back, 0, 0.72, seat_z - 0.24);

            // The robot, built seated at the origin of its own group.
            add(parts.body, 0, 0.74, 0, this.robot);
            add(parts.head, 0, 1.16, 0.01, this.robot);
            add(parts.eye, -0.08, 1.18, 0.175, this.robot);
            add(parts.eye, 0.08, 1.18, 0.175, this.robot);
            add(parts.antenna, 0, 1.39, 0, this.robot);
            const lamp = new Mesh(parts.lamp, this.lamp_material);
            lamp.position.set(0, 1.5, 0);
            this.robot.add(lamp);
            for (const side of [-1, 1]) {
                // Arms hang from the shoulder: pivot at the top.
                const pivot = new Group();
                pivot.position.set(side * 0.26, 0.94, 0.02);
                const arm = new Mesh(parts.arm, solid);
                arm.position.set(0, -ARM_LENGTH / 2, 0);
                pivot.add(arm);
                this.robot.add(pivot);
                this.arms.push(pivot);
                if (side === 1) {
                    const hand = new Mesh(parts.lamp, this.lamp_material);
                    hand.position.set(0, -ARM_LENGTH * RAISED_ARM.stretch - 0.04, 0);
                    hand.name = 'raised-hand';
                    hand.visible = false;
                    pivot.add(hand);
                    this.raised = { pivot, arm, hand };
                }
                // Hung from the body, so they reach the floor once the robot stands.
                const leg = add(parts.leg, side * 0.11, SEATED_BODY_BOTTOM - ROBOT_LEG / 2, 0, this.robot);
                leg.visible = false;
                this.legs.push(leg);
            }
            this.robot.position.set(0, 0, seat_z + 0.04);
            this.robot.name = 'robot';
            this.deck.add(this.robot);
        }

        const screen = new Mesh(parts.screen, this.screen_material);
        if (kind === 'service') {
            screen.position.set(0, g.server_height - 0.3, g.server_depth / 2 + 0.01);
        } else {
            screen.position.set(0, g.desk_height + 0.52, 0.2 - 0.035);
            screen.scale.set(1.5, 1.5, 1);
            screen.rotation.y = Math.PI; // faces −z, toward the seat
        }
        this.deck.add(screen);

        const footprint = kind === 'service'
            ? { width: g.server_width + 0.2, depth: g.server_depth + 0.2, centre_z: 0 }
            : { width: g.desk_width + 0.2, ...DESK_FOOTPRINT };
        this.focus_ring = rect_outline(footprint.width, footprint.depth, shared.chrome);
        this.focus_ring.position.set(0, 0.01, footprint.centre_z);
        this.focus_ring.visible = false;
        this.deck.add(this.focus_ring);
        // A solid, always-visible halo locates selection even behind another desk/cabinet.
        this.beacon = new Mesh(new TorusGeometry(kind === 'service' ? 0.65 : 0.8, 0.055, 8, 40),
            new MeshBasicMaterial({ color: PALETTE.chrome, depthTest: false, depthWrite: false, transparent: true, opacity: 0.95 }));
        this.beacon.rotation.x = -Math.PI / 2;
        this.beacon.position.set(0, kind === 'service' ? g.server_height + 0.15 : 1.65, kind === 'service' ? 0 : -g.seat_offset_z);
        this.beacon.renderOrder = 20;
        this.beacon.visible = false;
        this.deck.add(this.beacon);

        this.hitbox = new Mesh(kind === 'service' ? parts.server_hitbox : parts.hitbox, shared.hitbox);
        this.hitbox.position.set(0, kind === 'service' ? g.server_height / 2 : 0.95, footprint.centre_z);
        this.hitbox.userData.session = session;
        this.deck.add(this.hitbox);
        this.leader.visible = false;
        this.deck.add(this.leader);
    }

    place(desk: OfficeDesk): void {
        this.root.position.set(desk.x, 0, desk.z);
        this.root.rotation.y = desk.facing === 1 ? 0 : Math.PI;
    }

    update(desk: OfficeDesk): void {
        const agent = desk.agent;
        if (agent.island) {
            if (!this.island) {
                this.island = new OfficeIsland(this.phase);
                this.deck.add(this.island.group);
                this.root.add(this.island.water);
                // Until the first tick, and for good under reduced motion: level, at rest.
                this.deck.position.y = OFFICE_ISLAND_LIFT;
            }
            this.island.update(agent.island);
        } else if (this.island) {
            this.deck.remove(this.island.group);
            this.root.remove(this.island.water);
            this.island.dispose();
            this.island = null;
            this.deck.position.y = 0;
            this.deck.rotation.set(0, 0, 0);
        }
        this.state_hex = display_state_color(agent.state);
        // The alarm only: background's DOM breathe is not a 3D motion.
        this.pulse_ms = agent.state === 'attention' ? state_pulse_period_ms(agent.state) : null;
        this.typing = this.kind === 'agent' && (agent.pose === 'working' || agent.pose === 'thinking');
        this.lamp_material.color.setHex(this.state_hex);

        const standing = agent.pose === 'waiting';
        this.waiting = standing && this.kind === 'agent';
        // Standing: up off the chair (legs on the floor) and back behind it.
        this.robot_y = standing ? ROBOT_LEG - SEATED_BODY_BOTTOM : 0;
        this.robot.position.set(0, this.robot_y, -OFFICE_GEOMETRY.seat_offset_z + (standing ? -STAND_BACK : 0.04));
        for (const leg of this.legs) leg.visible = standing;
        for (const arm of this.arms) arm.rotation.set(0, 0, 0);
        if (this.raised) {
            // The hand goes up with the state, not with the motion: it holds its first
            // angle wherever the wave does not run.
            const stretch = this.waiting ? RAISED_ARM.stretch : 1;
            this.raised.arm.scale.y = stretch;
            this.raised.arm.position.y = -(ARM_LENGTH * stretch) / 2;
            this.raised.hand.visible = this.waiting;
            if (this.waiting) this.raised.pivot.rotation.z = RAISED_ARM.angles[0];
        }

        const screen_key = `${agent.screen}|${this.state_hex}`;
        if (screen_key !== this.screen_key) {
            this.screen_key = screen_key;
            this.screen_material.map?.dispose();
            if (agent.screen) {
                // White under dark text: the material's state hue tints the white.
                const { texture } = text_texture([{ text: ` ${agent.screen} `, color: '#0b0f0e' }], { px: 64, fill: '#ffffff' });
                this.screen_material.map = texture;
                this.screen_material.color.setHex(this.state_hex);
            } else {
                this.screen_material.map = null;
                this.screen_material.color.setHex(PALETTE.screen_off);
            }
            this.screen_material.needsUpdate = true;
        }

        // The nameplate's second line is what the agent is doing — readable from
        // any side, unlike the screen, which faces only its seat.
        const doing = standing ? 'WANTS YOU' : agent.screen;
        const island = agent.island ? island_status(agent.island) : null;
        const label_key = `${agent.label}|${doing}|${this.state_hex}|${agent.lead}|${agent.focused}|${island?.label ?? ''}`;
        if (this.floating_names && label_key !== this.label_key) {
            this.label_key = label_key;
            this.set_nameplate([
                { text: `${agent.focused ? '▸ ' : agent.lead ? '★ ' : ''}${agent.label}`, color: agent.focused ? '#d8ffe6' : session_label_color(agent.session) },
                ...(doing ? [{ text: `● ${doing}`, color: hex_css(this.state_hex) }] : []),
                ...(island ? [{ text: island.label, color: island.color }] : []),
            ]);
        }
        this.focus_ring.visible = agent.focused || this.hovered;
        this.beacon.visible = agent.focused;
        if (this.kind === 'agent') this.beacon.position.y = standing ? 2.25 : 1.65;
    }

    set_hovered(hovered: boolean, focused: boolean): void {
        this.hovered = hovered;
        this.focus_ring.visible = hovered || focused;
    }

    private set_nameplate(lines: LabelLine[]): void {
        if (this.nameplate) {
            this.deck.remove(this.nameplate);
            dispose_sprite(this.nameplate);
        }
        const { texture, aspect } = text_texture(lines, { backing: 'rgba(11, 15, 14, 0.68)' });
        const sprite = new Sprite(new SpriteMaterial({ map: texture, transparent: true, depthWrite: false }));
        const height = 0.26 * lines.length;
        this.label_size.set(height * aspect, height);
        this.label_lines = lines.length;
        sprite.scale.set(this.label_size.x, height, 1);
        sprite.position.set(0, 2.1 + height / 2, this.kind === 'service' ? 0 : -OFFICE_GEOMETRY.seat_offset_z);
        this.label_origin.copy(sprite.position);
        sprite.userData.session = this.session;
        sprite.material.opacity = this.label_alpha;
        sprite.renderOrder = 2;
        this.nameplate = sprite;
        this.deck.add(sprite);
    }

    label_anchor(target: Vector3): Vector3 {
        target.set(0, this.kind === 'service' ? OFFICE_GEOMETRY.server_height : this.robot_y + 1.5,
            this.kind === 'service' ? 0 : this.robot.position.z);
        return this.deck.localToWorld(target);
    }

    project_label(camera: PerspectiveCamera, width: number, height: number): LabelAnchor | null {
        if (!this.nameplate) return null;
        const centre = this.deck.localToWorld(this.label_origin.clone());
        const depth = -centre.clone().applyMatrix4(camera.matrixWorldInverse).z;
        const ndc = centre.project(camera);
        this.nameplate.visible = depth > camera.near && ndc.z < 1;
        if (!this.nameplate.visible) this.leader.visible = false;
        if (!this.nameplate.visible || Math.abs(ndc.x) > 1.4 || Math.abs(ndc.y) > 1.4) return null;
        const world_scale = this.root.getWorldScale(new Vector3());
        const pixels = height * camera.projectionMatrix.elements[5] / (2 * depth);
        const w = this.label_size.x * world_scale.x * pixels, h = this.label_size.y * world_scale.y * pixels;
        return { session: this.session, depth, focused: this.beacon.visible, lines: this.label_lines,
            left: (ndc.x + 1) * width / 2 - w / 2, top: (1 - ndc.y) * height / 2 - h / 2, width: w, height: h };
    }

    /** Conservative projected bounds include the desk, robot (standing, and at the top of a hop), and enlarged monitor. */
    project_model(camera: PerspectiveCamera, width: number, height: number): LabelRect | null {
        const service = this.kind === 'service';
        const half_width = service ? OFFICE_GEOMETRY.server_width / 2 : OFFICE_GEOMETRY.desk_width / 2;
        const back = service ? -OFFICE_GEOMETRY.server_depth / 2 : -1.5;
        const front = service ? OFFICE_GEOMETRY.server_depth / 2 : OFFICE_GEOMETRY.desk_depth / 2;
        const top = service ? OFFICE_GEOMETRY.server_height : this.robot_y + 1.6 + (this.waiting ? OFFICE_MOTION.hop_height : 0);
        let left = Infinity, right = -Infinity, bottom = -Infinity, upper = Infinity;
        const corners: Vector3[] = [];
        for (const x of [-half_width, half_width]) for (const y of [0, top]) for (const z of [back, front]) {
            corners.push(this.deck.localToWorld(new Vector3(x, y, z)));
        }
        const depths = corners.map(point => -point.clone().applyMatrix4(camera.matrixWorldInverse).z);
        if (depths.every(depth => depth <= camera.near || depth >= camera.far)) return null;
        // A model crossing the near plane can cover the viewport: disallow growth until clear.
        if (depths.some(depth => depth <= camera.near)) return { left: 0, top: 0, width, height };
        for (const corner of corners) {
            const point = corner.project(camera);
            const px = (point.x + 1) * width / 2, py = (1 - point.y) * height / 2;
            left = Math.min(left, px); right = Math.max(right, px); upper = Math.min(upper, py); bottom = Math.max(bottom, py);
        }
        return { left, top: upper, width: right - left, height: bottom - upper };
    }

    apply_label(placement: LabelPlacement, camera: PerspectiveCamera, width: number, height: number): void {
        if (!this.nameplate) return;
        const point = this.deck.localToWorld(this.label_origin.clone()).project(camera);
        point.x += placement.dx * 2 / width;
        point.y -= placement.dy * 2 / height;
        this.nameplate.position.copy(this.deck.worldToLocal(point.unproject(camera)));
        this.nameplate.scale.set(this.label_size.x * placement.scale, this.label_size.y * placement.scale, 1);
        this.label_target_alpha = placement.opacity;
        this.leader.visible = Math.hypot(placement.dx, placement.dy) > 4;
        if (this.leader.visible) {
            const start = this.deck.worldToLocal(this.label_anchor(new Vector3()));
            const end = this.nameplate.position;
            const positions = this.leader.geometry.getAttribute('position');
            positions.setXYZ(0, start.x, start.y, start.z);
            positions.setXYZ(1, end.x, end.y, end.z);
            positions.needsUpdate = true;
            this.leader.geometry.computeBoundingSphere();
        }
    }

    pick_label(): Sprite | null { return this.nameplate?.visible && this.label_alpha >= 0.5 ? this.nameplate : null; }

    tick(now_ms: number, delta_ms: number): void {
        // Like spatial portal titles, fade only material uniforms, never rebuild text to animate it.
        this.label_alpha += (this.label_target_alpha - this.label_alpha) * (1 - Math.exp(-delta_ms / 150));
        if (this.nameplate) this.nameplate.material.opacity = this.label_alpha;
        this.leader.material.opacity = this.label_alpha * 0.5;
        if (this.pulse_ms) {
            const phase = (now_ms % this.pulse_ms) / this.pulse_ms;
            const lift = 0.35 + 0.65 * (0.5 + 0.5 * Math.cos(phase * Math.PI * 2));
            this.lamp_material.color.setHex(this.state_hex).multiplyScalar(lift);
        }
        if (this.typing && OFFICE_MOTION.typing) {
            // Two held positions, alternating arms: a keystroke, not a wave.
            const step = Math.floor(now_ms / OFFICE_MOTION.typing_step_ms) % 2;
            this.arms[0].rotation.x = step === 0 ? -1.05 : -0.8;
            this.arms[1].rotation.x = step === 0 ? -0.8 : -1.05;
        }
        const animate = should_animate();
        if (this.waiting && this.raised) {
            const moving = animate && OFFICE_MOTION.hop;
            // In step with the lamp: the hop leaves the floor as the alarm peaks.
            this.robot.position.y = this.robot_y + (moving ? office_hop(now_ms, this.pulse_ms ?? undefined) : 0);
            this.raised.pivot.rotation.z = RAISED_ARM.angles[moving ? Math.floor(now_ms / OFFICE_MOTION.wave_step_ms) % 2 : 0];
        }
        if (this.island && animate && OFFICE_MOTION.floating) {
            const beat = Math.floor(now_ms / OFFICE_MOTION.float_step_ms) * OFFICE_MOTION.float_step_ms;
            const float = island_float(beat, this.phase);
            this.deck.position.y = float.lift;
            this.deck.rotation.set(float.pitch, 0, float.roll);
            this.island.tick(beat % (WATER_WRAP_S * 1000));
        }
    }

    dispose(): void {
        this.leader.geometry.dispose();
        this.leader.material.dispose();
        this.island?.dispose();
        this.lamp_material.dispose();
        this.screen_material.map?.dispose();
        this.screen_material.dispose();
        this.focus_ring.geometry.dispose();
        this.beacon.geometry.dispose();
        (this.beacon.material as Material).dispose();
        if (this.nameplate) dispose_sprite(this.nameplate);
    }
}

function dispose_sprite(sprite: Sprite): void {
    sprite.material.map?.dispose();
    sprite.material.dispose();
}

/** Keep floor text upright in screen space, including when a host rotates the office.
 * A small dead zone holds its facing near a side-on view so orbit damping cannot flicker it. */
function floor_label(width: number, height: number, material: MeshBasicMaterial): Mesh<PlaneGeometry, MeshBasicMaterial> {
    const mesh = new Mesh(new PlaneGeometry(width, height), material);
    mesh.rotation.x = -Math.PI / 2;
    const right = new Vector3();
    const screen_right = new Vector3();
    mesh.onBeforeRender = (_renderer, _scene, camera) => {
        right.set(1, 0, 0);
        if (mesh.parent) right.transformDirection(mesh.parent.matrixWorld);
        screen_right.setFromMatrixColumn(camera.matrixWorld, 0).normalize();
        const alignment = right.dot(screen_right);
        if (Math.abs(alignment) <= 0.05) return;
        const rotation = alignment < 0 ? Math.PI : 0;
        if (mesh.rotation.z === rotation) return;
        mesh.rotation.z = rotation;
        mesh.updateMatrixWorld();
    };
    return mesh;
}

/** A room's floor, outline and name. */
function build_room(room: OfficeRoom, chrome: LineBasicMaterial): { group: Group; dispose: () => void } {
    const group = new Group();
    group.position.set(room.x, 0, room.z);
    const floor_geometry = new PlaneGeometry(room.width, room.depth);
    const floor_material = new MeshBasicMaterial({ color: room.kind === 'team' ? PALETTE.floor_team : PALETTE.floor });
    const floor = new Mesh(floor_geometry, floor_material);
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = 0.002;
    group.add(floor);
    const outline = rect_outline(room.width, room.depth, chrome);
    outline.position.y = 0.006;
    group.add(outline);

    // The name, painted on the floor along the room's front (+z) edge.
    const head = room.kind === 'team' ? `TEAM ${room.label}` : room.label;
    const count = `${room.desks.length}`;
    const { texture, aspect } = text_texture([{ text: `${head}  ·  ${count}`, color: hex_css(PALETTE.chrome) }], { px: 56 });
    const label_h = OFFICE_GEOMETRY.label_strip * 0.5;
    const label_w = Math.min(label_h * aspect, room.width - 0.4);
    const label_material = new MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false });
    const label = floor_label(label_w, label_w / aspect, label_material);
    label.position.set(-room.width / 2 + 0.25 + label_w / 2, 0.01, room.depth / 2 - OFFICE_GEOMETRY.label_strip / 2);
    group.add(label);

    return {
        group,
        dispose: () => {
            floor_geometry.dispose();
            floor_material.dispose();
            outline.geometry.dispose();
            label.geometry.dispose();
            texture.dispose();
            label_material.dispose();
        },
    };
}

/** One cached floor inscription; GPU texture changes only when the rounded readout does. */
class FloorReadout {
    readonly mesh = floor_label(1, 1, new MeshBasicMaterial({ transparent: true, depthWrite: false }));
    private key = '';
    private aspect = 1;
    // Over a pool it lies on: two transparent planes this close have no reliable depth order.
    constructor() { this.mesh.renderOrder = 1; }
    update(lines: string[], width: number, height: number): void {
        const key = lines.join('\n');
        if (key !== this.key) {
            this.key = key;
            this.mesh.material.map?.dispose();
            const text = text_texture(lines.map(text => ({ text, color: PALETTE.text_dim })), { px: 48 });
            this.mesh.material.map = text.texture;
            this.mesh.material.needsUpdate = true;
            this.aspect = text.aspect;
        }
        const w = Math.min(width, height * this.aspect);
        this.mesh.scale.set(w, w / this.aspect, 1);
    }
    dispose(): void {
        this.mesh.geometry.dispose();
        this.mesh.material.map?.dispose();
        this.mesh.material.dispose();
    }
}

export class AgentOfficeScene {
    /** Add this to a scene. Office units (≈ metres): scale the group to fit. */
    readonly group = new Group();
    private readonly parts = office_parts();
    private readonly shared = {
        solid: new MeshBasicMaterial({ vertexColors: true }),
        chrome: new LineBasicMaterial({ color: PALETTE.chrome, transparent: true, opacity: 0.55 }),
        hitbox: new MeshBasicMaterial({ visible: false }),
    };
    private readonly rooms_group = new Group();
    private readonly ground: Mesh;
    private rooms: { group: Group; dispose: () => void }[] = [];
    private readonly rigs = new Map<string, OfficeRig>();
    private geometry_key = '';
    private hovered: string | null = null;
    private focused: string | null = null;
    private readonly readouts = new Map<string, FloorReadout>();
    private last_tick_ms: number | null = null;
    private readonly floating_names: boolean;

    constructor(floating_names = true) {
        this.floating_names = floating_names;
        this.ground = new Mesh(new PlaneGeometry(1, 1), new MeshBasicMaterial({ color: PALETTE.ground }));
        this.ground.rotation.x = -Math.PI / 2;
        this.group.add(this.ground, this.rooms_group);
    }

    /** Apply a layout. Floors rebuild only when a room or desk MOVED; desks update in place. */
    update(layout: OfficeLayout): void {
        if (layout.geometry_key !== this.geometry_key) {
            this.geometry_key = layout.geometry_key;
            for (const room of this.rooms) {
                this.rooms_group.remove(room.group);
                room.dispose();
            }
            this.rooms = layout.rooms.map((room) => build_room(room, this.shared.chrome));
            for (const room of this.rooms) this.rooms_group.add(room.group);
            const margin = 6;
            this.ground.scale.set(Math.max(layout.width, 1) + margin * 2, Math.max(layout.depth, 1) + margin * 2, 1);
        }

        const seen = new Set<string>();
        this.focused = null;
        for (const room of layout.rooms) {
            for (const desk of room.desks) {
                const session = desk.agent.session;
                seen.add(session);
                if (desk.agent.focused) this.focused = session;
                let rig = this.rigs.get(session);
                if (!rig) {
                    rig = new OfficeRig(session, desk.agent.kind, this.parts, this.shared, this.floating_names);
                    this.rigs.set(session, rig);
                    this.group.add(rig.root);
                }
                rig.place(desk);
                rig.update(desk);
            }
        }
        for (const [session, rig] of [...this.rigs]) {
            if (seen.has(session)) continue;
            this.group.remove(rig.root);
            rig.dispose();
            this.rigs.delete(session);
        }
    }

    label_anchor(session: string, target: Vector3): Vector3 | null {
        return this.rigs.get(session)?.label_anchor(target) ?? null;
    }

    /** Re-plan only on camera/layout changes; sprites still follow perspective on every frame. */
    update_labels(camera: PerspectiveCamera, width: number, height: number): void {
        if (width <= 0 || height <= 0) return;
        camera.updateMatrixWorld();
        this.group.updateMatrixWorld(true);
        const anchors: LabelAnchor[] = [], models: LabelRect[] = [];
        for (const rig of this.rigs.values()) {
            const anchor = rig.project_label(camera, width, height);
            if (anchor) anchors.push(anchor);
            const model = rig.project_model(camera, width, height);
            if (model) models.push(model);
        }
        // The one HTML control floats above the scene; don't move a title under it.
        models.push({ left: width - 66, top: 0, width: 66, height: 50 });
        for (const placement of place_office_labels(anchors, width, height, models)) {
            this.rigs.get(placement.session)?.apply_label(placement, camera, width, height);
        }
    }

    update_resources(layout: OfficeLayout, resources?: OfficeResources): void {
        const seen = new Set<string>();
        const rows = new Map(resources?.sessions.map(row => [row.session, row]) ?? []);
        const paint = (key: string, lines: string[], x: number, z: number, width: number, height: number, y = 0.018): void => {
            seen.add(key);
            let readout = this.readouts.get(key);
            if (!readout) { readout = new FloorReadout(); this.readouts.set(key, readout); this.group.add(readout.mesh); }
            readout.update(lines, width, height);
            readout.mesh.position.set(x, y, z);
        };
        const all = layout.rooms.flatMap(room => room.desks);
        if (resources) {
            for (const room of layout.rooms) {
                for (const desk of room.desks) {
                    const row = rows.get(desk.agent.session);
                    // An island's pool reaches under this spot (`office-island.ts` `OFFICE_POOL`): its readout lies on the water.
                    paint(`desk:${desk.agent.session}`, resource_readout(row ? [row] : [], 1, resources.stale).split(' · '),
                        desk.x, desk.z - desk.facing * (desk.agent.kind === 'service' ? -0.85 : 1.8), 1.85, 0.6,
                        desk.agent.island ? OFFICE_WATER_LEVEL + 0.006 : undefined);
                }
                const room_rows = room.desks.flatMap(desk => rows.get(desk.agent.session) ?? []);
                paint(`room:${room.key}`, [resource_readout(room_rows, room.desks.length, resources.stale)],
                    room.x, room.z + room.depth / 2 - 0.14, room.width - 0.4, 0.3);
            }
            if (all.length) paint('total', [`ALL SECTIONS · ${resource_readout(all.flatMap(desk => rows.get(desk.agent.session) ?? []), all.length, resources.stale)}`],
                0, layout.depth / 2 + 0.7, Math.max(8, layout.width), 0.6);
        }
        for (const [key, readout] of this.readouts) {
            if (seen.has(key)) continue;
            this.group.remove(readout.mesh); readout.dispose(); this.readouts.delete(key);
        }
    }

    /** The session under a ray, or null. */
    pick(raycaster: Raycaster): string | null {
        const hitboxes = [...this.rigs.values()].flatMap((rig) => {
            const label = rig.pick_label();
            return label ? [rig.hitbox, label] : [rig.hitbox];
        });
        const hit = raycaster.intersectObjects(hitboxes, false)[0];
        return (hit?.object.userData.session as string | undefined) ?? null;
    }

    set_hovered(session: string | null): void {
        if (session === this.hovered) return;
        const previous = this.hovered ? this.rigs.get(this.hovered) : undefined;
        this.hovered = session;
        previous?.set_hovered(false, previous.session === this.focused);
        const next = session ? this.rigs.get(session) : undefined;
        next?.set_hovered(true, next.session === this.focused);
    }

    /** Advance the pulse and the typing. Call once a frame. */
    tick(now_ms: number): void {
        const delta_ms = this.last_tick_ms === null ? 16 : Math.min(100, Math.max(0, now_ms - this.last_tick_ms));
        this.last_tick_ms = now_ms;
        for (const rig of this.rigs.values()) rig.tick(now_ms, delta_ms);
    }

    dispose(): void {
        for (const readout of this.readouts.values()) readout.dispose();
        this.readouts.clear();
        for (const room of this.rooms) room.dispose();
        for (const rig of this.rigs.values()) rig.dispose();
        this.rigs.clear();
        this.ground.geometry.dispose();
        (this.ground.material as Material).dispose();
        for (const geometry of Object.values(this.parts)) geometry.dispose();
        for (const material of Object.values(this.shared)) material.dispose();
    }
}
