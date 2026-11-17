import test from 'node:test';
import assert from 'node:assert/strict';
import { artifact_viewer_url, parse_canvas_artifacts } from '../../src/agent-rack/canvas-artifacts.ts';

const file = {
    id: 'a'.repeat(24), revision: 'b'.repeat(64), name: 'diagram.html', path: 'output/diagram.html', bytes: 120,
};
const shelf = { key: '7-1760000000', artifacts: [file], error: null };

test('shelf messages accept bounded metadata and strip untrusted extra fields', () => {
    assert.deepEqual(parse_canvas_artifacts({ ...shelf, artifacts: [{ ...file, url: 'https://example.test/' }] }), shelf);
    assert.deepEqual(parse_canvas_artifacts({ key: null, artifacts: [], error: null }), { key: null, artifacts: [], error: null });
    assert.equal(parse_canvas_artifacts({ ...shelf, key: null }), null);
    assert.equal(parse_canvas_artifacts(undefined), null);
});

test('shelf boundary rejects paths disguised as IDs, oversized payloads and corrupt rows', () => {
    for (const key of ['../private', '7-1760000000/other', '7-1?x=2']) {
        assert.equal(parse_canvas_artifacts({ ...shelf, key }), null);
    }
    for (const invalid of [{ id: '../escape' }, { revision: 'bad' }, { bytes: -1 }, { bytes: 9000000 }, { name: '' }, { path: 'x'.repeat(2049) }]) {
        assert.equal(parse_canvas_artifacts({ ...shelf, artifacts: [{ ...file, ...invalid }] }), null);
    }
    assert.equal(parse_canvas_artifacts({ ...shelf, artifacts: Array(33).fill(file) }), null);
});

test('viewer URLs stay on the selected bridge and include a revision for refreshed files', () => {
    const url = new URL(artifact_viewer_url('http://127.0.0.1:7791/', shelf.key, file));
    assert.equal(url.origin, 'http://127.0.0.1:7791');
    assert.equal(url.pathname, '/plugin/agent-canvas/artifact');
    assert.equal(url.searchParams.get('key'), shelf.key);
    assert.equal(url.searchParams.get('id'), file.id);
    assert.equal(url.searchParams.get('revision'), file.revision);
});
