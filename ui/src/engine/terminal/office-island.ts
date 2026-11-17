import {
    BoxGeometry, BufferGeometry, Color, Float32BufferAttribute, Group,
    Mesh, MeshBasicMaterial, type Object3D,
} from 'three';
import type { SessionIsland } from './multi-terminal-layout.ts';
import { island_status } from './island-status.ts';

/** A low, faceted worktree island under one desk and its chair. Its flat cap is
 * the rig's floor; lifting the whole rig keeps feet, furniture and picking together.
 * Fits inside the desk pitch, including the standing pose, in either facing. */
export const OFFICE_ISLAND_LIFT = 0.18;
const CENTRE_Z = -0.55;
const SEGMENTS = 12;

function island_geometry(): BufferGeometry {
    const positions: number[] = [];
    const colors: number[] = [];
    // Squared-off coastline gives the rectangular desk room, with clipped corners.
    const ring = (scale: number, y: number): number[][] => Array.from({ length: SEGMENTS }, (_, i) => {
        const angle = (i / SEGMENTS) * Math.PI * 2;
        const x = Math.cos(angle);
        const z = Math.sin(angle);
        return [Math.sign(x) * Math.pow(Math.abs(x), 0.55) * 0.94 * scale, y,
            CENTRE_Z + Math.sign(z) * Math.pow(Math.abs(z), 0.55) * 0.94 * scale];
    });
    const cap = ring(0.92, 0);
    const shore = ring(1, -0.045);
    const foot = ring(0.88, -OFFICE_ISLAND_LIFT + 0.015);
    const face = (a: number[], b: number[], c: number[], color: Color): void => {
        positions.push(...a, ...b, ...c);
        for (let i = 0; i < 3; i++) colors.push(color.r, color.g, color.b);
    };
    for (let i = 0; i < SEGMENTS; i++) {
        const j = (i + 1) % SEGMENTS;
        face([0, 0, CENTRE_Z], cap[j], cap[i], new Color(0xffffff).multiplyScalar(0.36));
        const sand = new Color(0xffffff).multiplyScalar(0.85);
        face(cap[i], cap[j], shore[i], sand);
        face(shore[i], cap[j], shore[j], sand);
        const shade = 0.16 + 0.09 * (1 + Math.cos((i / SEGMENTS) * Math.PI * 2));
        const rock = new Color(0xffffff).multiplyScalar(shade);
        face(shore[i], shore[j], foot[i], rock);
        face(foot[i], shore[j], foot[j], rock);
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
    geometry.setAttribute('color', new Float32BufferAttribute(colors, 3));
    geometry.computeBoundingSphere();
    return geometry;
}

export class OfficeIsland {
    readonly group = new Group();
    private readonly material = new MeshBasicMaterial({ vertexColors: true });
    private readonly flag_material = new MeshBasicMaterial();
    private readonly pole_material = new MeshBasicMaterial({ color: 0x9aa6ad });
    private readonly check_material = new MeshBasicMaterial({ color: 0xe4edf2 });
    private readonly check = new Group();

    constructor() {
        this.group.name = 'worktree-island';
        this.group.add(new Mesh(island_geometry(), this.material));
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
    }

    update(island: SessionIsland): void {
        const status = island_status(island);
        this.material.color.set(status.color);
        this.flag_material.color.set(status.color);
        this.check.visible = status.merged;
        this.group.userData.pr_state = island.pr_state ?? 'unknown';
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
    }
}
