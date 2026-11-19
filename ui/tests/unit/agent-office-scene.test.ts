import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import ts from 'typescript';
import type { Object3D } from 'three';
// The scene has constructor parameter properties; Node's default strip-only loader does not.
registerHooks({ load(url, context, next) {
    if (url.endsWith('/agent-office-scene.ts')) return { format: 'module', shortCircuit: true,
        source: ts.transpileModule(readFileSync(new URL(url), 'utf8'), {
            compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
        }).outputText };
    return next(url, context);
} });
const { AgentOfficeScene, OFFICE_MOTION, office_hop } = await import('../../src/engine/terminal/agent-office-scene.ts');
const { FUI_MOTION_FLAGS } = await import('../../src/config/fui-motion-flags.ts');
const { OFFICE_ISLAND_FLOAT, OFFICE_ISLAND_LIFT } = await import('../../src/engine/terminal/office-island.ts');
const { build_office } = await import('../../src/engine/terminal/agent-office-logic.ts');
const { Vector3, PerspectiveCamera } = await import('three');
const rows = [{ session: 'agent-a', live: true, state: 'busy' as const, repo: 'app' }, { session: 'agent-b', live: true, state: 'attention' as const, repo: 'app' }];
/** What this test reads off a three.js object, whatever its class. */
interface Drawn {
    depthTest: boolean;
    sizeAttenuation?: boolean;
    opacity: number;
    map?: { addEventListener(type: 'dispose', listener: () => void): void } | null;
}
type Probe = Object3D & { geometry?: { type?: string; parameters?: { width?: number } }; material: Drawn; isSprite?: boolean };
function objects(scene: InstanceType<typeof AgentOfficeScene>, predicate: (object: Probe) => unknown): Probe[] {
    const found: Probe[] = [];
    scene.group.traverse((object) => { if (predicate(object as Probe)) found.push(object as Probe); });
    return found;
}
/** A scene over a canvas that measures text and paints nothing, torn down after `run`.
 *  The root element is there for the motion flags, which publish themselves to it on a write. */
function with_scene(run: (scene: InstanceType<typeof AgentOfficeScene>) => void): void {
    const previous = globalThis.document;
    globalThis.document = { documentElement: { style: { setProperty() {} }, classList: { toggle() {} } },
        createElement: () => ({ width: 0, height: 0, getContext: () => ({
            measureText: (text: string) => ({ width: text.length * 24 }), fillRect() {}, fillText() {}, strokeText() {}, scale() {},
        }) }) } as unknown as Document;
    const scene = new AgentOfficeScene();
    try { run(scene); } finally { scene.dispose(); globalThis.document = previous; }
}
test('selection draws through furniture, screens grow, and floor textures are cached and released', () => {
    with_scene((scene) => {
        const layout = build_office(rows, 'agent-a');
        scene.update(layout);
        const beacons = objects(scene, object => object.geometry?.type === 'TorusGeometry');
        assert.equal(beacons.filter(object => object.visible).length, 1);
        assert.equal(beacons.find(object => object.visible)?.material.depthTest, false);
        const labels = objects(scene, object => object.isSprite);
        assert.equal(labels.length, 2);
        assert.ok(labels.every(label => label.material.sizeAttenuation && label.material.depthTest));
        const camera = new PerspectiveCamera(42, 640 / 480, 0.1, 2000);
        camera.position.set(0, 12, 16);
        camera.lookAt(0, 0, 0);
        scene.update_labels(camera, 640, 480);
        const positions = labels.map(label => label.position.clone());
        const sizes = labels.map(label => label.scale.clone());
        scene.update_labels(camera, 640, 480);
        assert.deepEqual(labels.map(label => label.position), positions);
        assert.deepEqual(labels.map(label => label.scale), sizes);
        scene.tick(0);
        scene.tick(150);
        assert.ok(labels.every(label => label.material.opacity > 0));
        const screens = objects(scene, object => object.geometry?.parameters?.width === 0.64);
        assert.ok(screens.every(screen => screen.scale.x >= 1.4 && screen.scale.y >= 1.4));
        assert.ok((scene.label_anchor('agent-a', new Vector3())?.y ?? 0) > 1);
        assert.equal(scene.label_anchor('missing', new Vector3()), null);
        const resources = { stale: false, sessions: rows.map(row => ({ session: row.session, rss: 1024 ** 3, cpu_pct: 12 })) };
        scene.update_resources(layout, resources);
        const textures = objects(scene, object => object.material?.map).map(object => object.material.map);
        scene.update_resources(layout, resources);
        assert.deepEqual(objects(scene, object => object.material?.map).map(object => object.material.map), textures);
        let disposed = 0;
        for (const texture of textures) texture?.addEventListener('dispose', () => disposed++);
        const empty = build_office([], null);
        scene.update(empty);
        scene.update_resources(empty);
        assert.ok(disposed >= textures.length);
        assert.equal(objects(scene, object => object.material?.map).length, 0);
    });
});

test('a hop is two held arcs at the head of the alarm, back on the floor between and after', () => {
    const m = OFFICE_MOTION;
    assert.equal(office_hop(0), 0);
    assert.equal(office_hop(m.hop_air_ms / 2), m.hop_height);
    assert.equal(office_hop(m.hop_air_ms), 0);
    assert.equal(office_hop(m.hop_air_ms * 1.5), m.hop_height);
    for (let t = m.hops * m.hop_air_ms; t < 1600; t += 37) assert.equal(office_hop(t), 0);
    // Held frames: nothing moves inside a step, and a period later it is the same hop.
    assert.equal(office_hop(m.hop_step_ms * 2), office_hop(m.hop_step_ms * 3 - 1));
    assert.notEqual(office_hop(m.hop_step_ms * 2), office_hop(m.hop_step_ms * 3));
    assert.equal(office_hop(130), office_hop(130 + 1600 * 7));
    for (let t = 0; t < 3200; t += 13) assert.ok(office_hop(t) >= 0 && office_hop(t) <= m.hop_height);
});

test('an agent that wants you hops with a hand up, and its nameplate is planned as if it stood still', () => {
    with_scene((scene) => {
        scene.update(build_office(rows, null));
        const named = (name: string) => objects(scene, object => object.name === name);
        const robots = named('robot');
        const waiting = robots.find(robot => robot.position.y > 0)!;
        const seated = robots.find(robot => robot.position.y === 0)!;
        const floor = waiting.position.y;
        const hands = named('raised-hand');
        assert.equal(hands.length, 2);
        assert.equal(hands.filter(hand => hand.visible).length, 1);
        const hand = hands.find(hand => hand.visible)!;
        assert.ok(waiting.getObjectById(hand.id), 'the raised hand is the waiting robot\'s');
        // Up past the shoulder, and it waves between two held angles.
        const pivot = hand.parent!;
        scene.tick(0);
        const first = pivot.rotation.z;
        scene.tick(OFFICE_MOTION.wave_step_ms);
        assert.notEqual(pivot.rotation.z, first);
        assert.ok(Math.cos(first) < -0.8 && Math.cos(pivot.rotation.z) < -0.8);

        scene.tick(OFFICE_MOTION.hop_air_ms / 2);
        assert.equal(waiting.position.y, floor + OFFICE_MOTION.hop_height);
        assert.equal(seated.position.y, 0);
        const camera = new PerspectiveCamera(42, 640 / 480, 0.1, 2000);
        camera.position.set(0, 12, 16);
        camera.lookAt(0, 0, 0);
        scene.update_labels(camera, 640, 480);
        const labels = objects(scene, object => object.isSprite);
        const mid_hop = labels.map(label => label.position.clone());
        const anchor = scene.label_anchor('agent-b', new Vector3())!.clone();
        scene.tick(1000);
        assert.equal(waiting.position.y, floor);
        scene.update_labels(camera, 640, 480);
        assert.deepEqual(labels.map(label => label.position), mid_hop);
        assert.deepEqual(scene.label_anchor('agent-b', new Vector3()), anchor);

        // Reduced motion: it stands, hand still up.
        FUI_MOTION_FLAGS.force_reduced_motion = true;
        try {
            scene.tick(OFFICE_MOTION.hop_air_ms / 2);
            assert.equal(waiting.position.y, floor);
            assert.equal(pivot.rotation.z, first);
            assert.equal(hand.visible, true);
        } finally { FUI_MOTION_FLAGS.force_reduced_motion = false; }

        // The question answered: it sits back down, arm at its side.
        scene.update(build_office(rows.map(row => ({ ...row, state: 'busy' as const })), null));
        scene.tick(OFFICE_MOTION.hop_air_ms / 2);
        assert.equal(waiting.position.y, 0);
        assert.equal(hands.filter(item => item.visible).length, 0);
        assert.equal(pivot.rotation.z, 0);
    });
});

test('an island lifts and rocks the desk on a pool that stays put, and lets go of both with the worktree', () => {
    with_scene((scene) => {
        const island = { path: '/worktrees/app/nav', label: 'fix/nav', title: 'Worktree: fix/nav' };
        const afloat = [{ ...rows[0], island }, rows[1]];
        scene.update(build_office(afloat, null));
        const waters = () => objects(scene, object => object.name === 'worktree-water');
        const hulls = () => objects(scene, object => object.name === 'worktree-island');
        assert.equal(waters().length, 1);
        assert.equal(hulls().length, 1);
        const water = waters()[0];
        const deck = hulls()[0].parent!;
        // The pool hangs off the desk's fixed place; the hull, the robot and the pick box ride the deck.
        assert.equal(water.parent, deck.parent);
        assert.ok(deck.getObjectByName('robot'));
        assert.equal(deck.position.y, OFFICE_ISLAND_LIFT);
        const pool_y = water.getWorldPosition(new Vector3()).y;
        const heights = new Set<number>();
        for (let t = 0; t <= OFFICE_ISLAND_FLOAT.period_ms; t += OFFICE_MOTION.float_step_ms) {
            scene.tick(t);
            heights.add(deck.position.y);
            assert.ok(Math.abs(deck.position.y - OFFICE_ISLAND_LIFT) <= OFFICE_ISLAND_FLOAT.rise + 1e-9);
            assert.equal(water.getWorldPosition(new Vector3()).y, pool_y);
        }
        assert.ok(heights.size > 10, 'the deck rides the swell');
        // The other desk stands on the floor, untouched.
        const grounded = objects(scene, object => object.name === 'robot').map(robot => robot.parent!).find(parent => parent !== deck)!;
        assert.equal(grounded.position.y, 0);

        FUI_MOTION_FLAGS.force_reduced_motion = true;
        try {
            const held = deck.position.y;
            scene.tick(OFFICE_ISLAND_FLOAT.period_ms * 1.25);
            assert.equal(deck.position.y, held);
        } finally { FUI_MOTION_FLAGS.force_reduced_motion = false; }

        scene.update(build_office(rows, null));
        assert.equal(waters().length, 0);
        assert.equal(hulls().length, 0);
        assert.equal(deck.position.y, 0);
        assert.deepEqual([deck.rotation.x, deck.rotation.z], [0, 0]);
    });
});
