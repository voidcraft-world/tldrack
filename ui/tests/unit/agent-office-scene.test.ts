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
const { AgentOfficeScene } = await import('../../src/engine/terminal/agent-office-scene.ts');
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
test('selection draws through furniture, screens grow, and floor textures are cached and released', () => {
    const previous = globalThis.document;
    globalThis.document = { createElement: () => ({ width: 0, height: 0, getContext: () => ({
        measureText: (text: string) => ({ width: text.length * 24 }), fillRect() {}, fillText() {}, strokeText() {}, scale() {},
    }) }) } as unknown as Document;
    const scene = new AgentOfficeScene();
    try {
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
    } finally { scene.dispose(); globalThis.document = previous; }
});
