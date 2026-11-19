import {
    BoxGeometry, BufferGeometry, Color, Float32BufferAttribute, Group,
    Mesh, MeshBasicMaterial, ShaderMaterial, Vector2, Vector4, type Object3D,
} from 'three';
import type { SessionIsland } from './multi-terminal-layout.ts';
import { island_status } from './island-status.ts';

/**
 * A worktree island: a hull that FLOATS in its own pool, under one desk and its chair.
 *
 * WHY WATER. A worktree is a checkout cut off from the main one, and a slab that is
 * only a different colour of floor says "highlighted desk", not "somewhere else".
 * Water around the hull, and the hull riding on it, is the separation drawn.
 *
 * TWO PARTS, TWO PARENTS. `group` is the hull and its flag: it goes on the rig's deck,
 * so the desk, the robot and the pick box ride the same swell. `water` is the pool: it
 * goes on the rig's fixed anchor, because a pool that rose with its island would be a
 * tray. The flat cap is the deck's floor (`OFFICE_ISLAND_LIFT`).
 *
 * THE HUE STAYS THE STATE'S. The hull and flag carry the PR hue (`island-status.ts`);
 * the water is scenery, a teal that is none of the state hues. Unlit like every
 * office surface: a shader, but it reads no light.
 */
export const OFFICE_ISLAND_LIFT = 0.22;
/** The pool's surface above the floor: over the room's outline and floor text. */
export const OFFICE_WATER_LEVEL = 0.03;
/** The swell. `rise` keeps the keel under the surface at the top of it (`island_float`). */
export const OFFICE_ISLAND_FLOAT = { period_ms: 5200, rise: 0.022, lean: 0.011 } as const;

/** Floor rectangles in the rig's frame (x, z). The hull holds the desk, the chair and
 *  the standing robot. The pool spills past it on three sides, stops short of the facing
 *  desk's legs and the next desk's, and reaches back under the desk's floor readout,
 *  which then lies on the water (`agent-office-scene.ts` `update_resources`). Two
 *  neighbouring pools overlap into one channel. */
const HULL = { x: 0.84, z_front: 0.38, z_back: -1.52, radius: 0.2 } as const;
const POOL = { x: 1.16, z_front: 0.42, z_back: -2.14, radius: 0.26 } as const;
/** The walkable cap is the hull's outline drawn in by this much: the beach. */
const BEACH = 0.045;
const BEACH_DROP = 0.04;
/** How far the keel reaches under the deck: below the surface at any point of the swell. */
export const OFFICE_KEEL_DEPTH = OFFICE_ISLAND_LIFT + 0.06;
const SEGMENTS = 24;

function centre_z(box: { z_front: number; z_back: number }): number {
    return (box.z_front + box.z_back) / 2;
}
function half_z(box: { z_front: number; z_back: number }): number {
    return (box.z_front - box.z_back) / 2;
}

/** Where the deck rides at `now_ms`: its height over the floor and its lean, radians.
 *  `phase` de-syncs neighbours: a row of islands rising together reads as a lift. */
export function island_float(now_ms: number, phase: number): { lift: number; pitch: number; roll: number } {
    const t = (now_ms / OFFICE_ISLAND_FLOAT.period_ms) * Math.PI * 2 + phase;
    return {
        lift: OFFICE_ISLAND_LIFT + OFFICE_ISLAND_FLOAT.rise * Math.sin(t),
        pitch: OFFICE_ISLAND_FLOAT.lean * Math.sin(t * 0.6 + phase),
        roll: OFFICE_ISLAND_FLOAT.lean * Math.cos(t * 0.45 + phase * 2),
    };
}

/** A stable phase for a session, 0..2π: the same island always rides the same swell. */
export function float_phase(session: string): number {
    let hash = 0;
    for (let i = 0; i < session.length; i++) hash = (hash * 31 + session.charCodeAt(i)) >>> 0;
    return (hash % 6283) / 1000;
}

function hull_geometry(): BufferGeometry {
    const positions: number[] = [];
    const colors: number[] = [];
    const cz = centre_z(HULL);
    // A rounded rectangle walked by angle: straight sides between four corner arcs.
    const ring = (inset: number, y: number, squeeze = 1): number[][] => Array.from({ length: SEGMENTS }, (_, i) => {
        const angle = (i / SEGMENTS) * Math.PI * 2;
        const cos = Math.cos(angle);
        const sin = Math.sin(angle);
        const hx = (HULL.x - inset) * squeeze;
        const hz = (half_z(HULL) - inset) * squeeze;
        const radius = Math.min(HULL.radius, hx, hz);
        const corner = (unit: number): number => (Math.abs(unit) < 1e-9 ? 0 : Math.sign(unit));
        return [corner(cos) * (hx - radius) + radius * cos, y, cz + corner(sin) * (hz - radius) + radius * sin];
    });
    const cap = ring(BEACH, 0);
    const shore = ring(0, -BEACH_DROP);
    const keel = ring(0, -OFFICE_KEEL_DEPTH, 0.9);
    const face = (a: number[], b: number[], c: number[], color: Color): void => {
        positions.push(...a, ...b, ...c);
        for (let i = 0; i < 3; i++) colors.push(color.r, color.g, color.b);
    };
    for (let i = 0; i < SEGMENTS; i++) {
        const j = (i + 1) % SEGMENTS;
        face([0, 0, cz], cap[j], cap[i], new Color(0xffffff).multiplyScalar(0.36));
        const sand = new Color(0xffffff).multiplyScalar(0.85);
        face(cap[i], cap[j], shore[i], sand);
        face(shore[i], cap[j], shore[j], sand);
        const shade = 0.16 + 0.09 * (1 + Math.cos((i / SEGMENTS) * Math.PI * 2));
        const rock = new Color(0xffffff).multiplyScalar(shade);
        face(shore[i], shore[j], keel[i], rock);
        face(keel[i], shore[j], keel[j], rock);
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
    geometry.setAttribute('color', new Float32BufferAttribute(colors, 3));
    geometry.computeBoundingSphere();
    return geometry;
}

/** One quad over the pool; the shader cuts its rounded bank. */
function pool_geometry(): BufferGeometry {
    const { x, z_front, z_back } = POOL;
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute([
        -x, 0, z_back, -x, 0, z_front, x, 0, z_front,
        -x, 0, z_back, x, 0, z_front, x, 0, z_back,
    ], 3));
    geometry.computeBoundingSphere();
    return geometry;
}

const WATER_VERTEX = /* glsl */ `
varying vec2 v_floor;
void main() {
    v_floor = position.xz;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`;

/** Drawn, not simulated: a pale shallows round the hull falling off to deep water, a
 *  crisp foam line that laps at the waterline, thin rings leaving the hull and dying
 *  before the bank, and broken glints on the open water. Every edge is hard, like the
 *  office's boxes; a soft glow here read as a light under the desk, not as water.
 *  `u_time` is seconds and may stand still. */
const WATER_FRAGMENT = /* glsl */ `
uniform float u_time;
uniform float u_phase;
uniform vec3 u_deep;
uniform vec3 u_shallow;
uniform vec3 u_foam;
uniform vec4 u_pool;
uniform vec4 u_hull;
uniform vec2 u_radius;
varying vec2 v_floor;
float rounded_box(vec2 point, vec2 half_size, float radius) {
    vec2 q = abs(point) - half_size + radius;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - radius;
}
void main() {
    float bank = rounded_box(v_floor - u_pool.xy, u_pool.zw, u_radius.x);
    float shore = rounded_box(v_floor - u_hull.xy, u_hull.zw, u_radius.y);
    float chop = sin(v_floor.x * 19.0 + u_time * 1.1 + u_phase) * sin(v_floor.y * 17.0 - u_time * 0.7);
    vec3 color = mix(u_shallow, u_deep, smoothstep(0.03, 0.4, shore));
    // The bank is shallow too: a paler lip just inside the pool's edge.
    color = mix(color, u_shallow, smoothstep(-0.07, -0.015, bank) * 0.55);
    float rings = smoothstep(0.82, 0.97, sin(shore * 27.0 - u_time * 1.7 + u_phase))
        * (1.0 - smoothstep(0.1, 0.46, shore)) * smoothstep(-0.03, -0.1, bank);
    float lines = smoothstep(0.9, 0.99, sin(v_floor.y * 15.0 + sin(v_floor.x * 4.0 + u_time * 0.8 + u_phase) * 1.4 - u_time * 0.6));
    float breaks = smoothstep(0.15, 0.6, sin(v_floor.x * 5.3 + v_floor.y * 2.1 + u_phase + u_time * 0.3));
    float glints = lines * breaks * smoothstep(0.12, 0.3, shore) * smoothstep(-0.04, -0.12, bank);
    color = mix(color, u_foam, rings * 0.5 + glints * 0.24);
    float foam = 1.0 - smoothstep(0.03, 0.042, shore + chop * 0.012 + 0.008 * sin(u_time * 1.3 + u_phase));
    color = mix(color, u_foam, foam);
    gl_FragColor = vec4(color, 1.0 - smoothstep(-0.012, 0.0, bank));
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
}`;

export class OfficeIsland {
    /** The hull and flag. Lives on the rig's deck. */
    readonly group = new Group();
    /** The pool. Lives on the rig's anchor, so it never rides the swell. */
    readonly water: Mesh<BufferGeometry, ShaderMaterial>;
    private readonly material = new MeshBasicMaterial({ vertexColors: true });
    private readonly flag_material = new MeshBasicMaterial();
    private readonly pole_material = new MeshBasicMaterial({ color: 0x9aa6ad });
    private readonly check_material = new MeshBasicMaterial({ color: 0xe4edf2 });
    private readonly check = new Group();

    constructor(phase = 0) {
        this.group.name = 'worktree-island';
        this.group.add(new Mesh(hull_geometry(), this.material));
        const pole = new Mesh(new BoxGeometry(0.025, 0.48, 0.025), this.pole_material);
        pole.position.set(0.67, 0.24, -1.12);
        const flag = new Mesh(new BoxGeometry(0.26, 0.18, 0.025), this.flag_material);
        flag.position.set(0.54, 0.39, -1.12);
        // Two strokes on both sides of the flag: readable from either desk facing.
        for (const z of [-0.018, 0.018]) {
            const short = new Mesh(new BoxGeometry(0.035, 0.065, 0.006), this.check_material);
            short.rotation.z = Math.PI / 4;
            short.position.set(-0.04, -0.025, z);
            const long = new Mesh(new BoxGeometry(0.035, 0.12, 0.006), this.check_material);
            long.rotation.z = -Math.PI / 4;
            long.position.set(0.017, -0.005, z);
            this.check.add(short, long);
        }
        this.check.position.copy(flag.position);
        this.group.add(pole, flag, this.check);

        this.water = new Mesh(pool_geometry(), new ShaderMaterial({
            vertexShader: WATER_VERTEX,
            fragmentShader: WATER_FRAGMENT,
            transparent: true,
            depthWrite: false,
            uniforms: {
                u_time: { value: 0 },
                u_phase: { value: phase },
                u_deep: { value: new Color(0x0d4a66) },
                u_shallow: { value: new Color(0x2f93a8) },
                u_foam: { value: new Color(0xe2f4f8) },
                u_pool: { value: new Vector4(0, centre_z(POOL), POOL.x, half_z(POOL)) },
                u_hull: { value: new Vector4(0, centre_z(HULL), HULL.x, half_z(HULL)) },
                u_radius: { value: new Vector2(POOL.radius, HULL.radius) },
            },
        }));
        this.water.name = 'worktree-water';
        this.water.position.y = OFFICE_WATER_LEVEL;
    }

    update(island: SessionIsland): void {
        const status = island_status(island);
        this.material.color.set(status.color);
        this.flag_material.color.set(status.color);
        this.check.visible = status.merged;
        this.group.userData.pr_state = island.pr_state ?? 'unknown';
    }

    /** Move the water. Not called under reduced motion: the pool then holds one still frame. */
    tick(now_ms: number): void {
        this.water.material.uniforms.u_time.value = now_ms / 1000;
    }

    dispose(): void {
        this.group.traverse((object: Object3D) => {
            if (object instanceof Mesh) object.geometry.dispose();
        });
        this.material.dispose();
        this.flag_material.dispose();
        this.pole_material.dispose();
        this.check_material.dispose();
        this.group.clear();
        this.water.geometry.dispose();
        this.water.material.dispose();
    }
}
