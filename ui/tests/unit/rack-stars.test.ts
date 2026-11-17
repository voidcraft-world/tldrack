import test from 'node:test';
import assert from 'node:assert/strict';
import { parse_session_row, type PortSnapshotRow } from '../../src/engine/terminal/terminal-status-logic.ts';
import { to_rack_session } from '../../src/engine/terminal/rack-view.ts';
import { build_rack_rows, type RackRow } from '../../src/engine/terminal/multi-terminal-layout.ts';
import { search_rack_rows, starred_rack_rows } from '../../src/agent-rack/rack-search.ts';
import {
    parse_star_value,
    set_star,
    star_filter_title,
    star_known,
    star_title,
    star_value,
    starred_count,
} from '../../src/agent-rack/star-client.ts';
import { closed_rows_view, parse_closed } from '../../src/agent-rack/closed-client.ts';
import { relock_local_access, unlock_local_access } from '../../src/engine/terminal/local-network-gate.ts';

function snapshot_row(overrides: Partial<PortSnapshotRow> & { session: string }): PortSnapshotRow {
    return { state: 'free', display: 'free', age_seconds: 1, message: null, title: null, attached: false, live: true, ...overrides };
}
const heading = (text: string): RackRow => ({ kind: 'cohort', text, dim: false, focused: false, actions: [] });
const session = (name: string, extra: Partial<RackRow> = {}): RackRow => ({
    kind: 'session', session: name, text: name, dim: false, focused: false, actions: [], ...extra,
});

test('parse_session_row keeps a boolean star and drops anything else', () => {
    assert.equal(parse_session_row({ session: 'a', state: 'free', starred: true })?.starred, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', starred: false })?.starred, false);
    assert.equal(parse_session_row({ session: 'a', state: 'free', starred: 'yes' })?.starred, undefined);
    assert.equal(parse_session_row({ session: 'a', state: 'free' })?.starred, undefined, 'an older bridge sends none');
});

test('the star rides to the rack row, and its absence says the bridge keeps none', () => {
    const rows = build_rack_rows({
        sessions: [
            to_rack_session(snapshot_row({ session: 'a', starred: true })),
            to_rack_session(snapshot_row({ session: 'b', starred: false })),
            to_rack_session(snapshot_row({ session: 'c' })),
        ],
        focused: null,
        max_sessions: 8,
    });
    const by_name = new Map(rows.filter((row) => row.kind === 'session').map((row) => [row.session, row]));
    assert.equal(by_name.get('a')?.starred, true);
    assert.equal(by_name.get('b')?.starred, false);
    assert.equal(star_known(by_name.get('b')!), true);
    assert.equal(star_known(by_name.get('c')!), false, 'no field: draw no control');
    assert.equal(starred_count(rows), 1);
});

test('the starred filter keeps a starred child with its family and heading, and nothing else', () => {
    const parent = session('parent', { node_id: 'p', starred: false });
    const child = session('target', { node_id: 'c', ancestors: ['p'], starred: true, complete: true });
    const rows = [heading('first'), session('other', { starred: false }), heading('second'), parent, child];
    assert.deepEqual(starred_rack_rows(rows), [rows[2], parent, child]);
    assert.deepEqual(starred_rack_rows([heading('first'), session('other')]), []);
    // FIND then searches inside the starred sessions.
    const two = [heading('repo'), session('nav-fix', { starred: true }), session('nav-other'), session('billing', { starred: true })];
    assert.deepEqual(search_rack_rows(starred_rack_rows(two), 'nav'), [two[0], two[1]]);
});

test('the action carries the state asked for, never a toggle', () => {
    assert.equal(parse_star_value(star_value(true)), true);
    assert.equal(parse_star_value(star_value(false)), false);
    for (const bad of [undefined, null, '', 'toggle', 'true', 1, true]) assert.equal(parse_star_value(bad), null);
});

test('the words: what a press does, and what the filter shows', () => {
    assert.match(star_title(false), /^Star this session/);
    assert.match(star_title(true), /clear the star/);
    assert.equal(star_filter_title(1, false), 'Show only the 1 starred session');
    assert.match(star_filter_title(3, true), /^Showing only the 3 starred sessions/);
});

test('set_star posts the session and the state, and a refusal or a dead bridge is false', async (t) => {
    // A loopback call is refused until this device opened its gate (`local-network-gate.ts`).
    unlock_local_access();
    t.after(relock_local_access);
    const calls: { url: string; body: unknown }[] = [];
    let answer: () => Response = () => new Response('{}', { status: 200 });
    t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), body: JSON.parse(String(init?.body)) });
        return answer();
    });
    const url = (path: string): string => `http://127.0.0.1:7682${path}`;
    assert.equal(await set_star(url, 'vc-2', true), true);
    assert.deepEqual(calls, [{ url: 'http://127.0.0.1:7682/session-star', body: { session: 'vc-2', starred: true } }]);
    answer = () => new Response('{}', { status: 404 });
    assert.equal(await set_star(url, 'vc-2', false), false, 'an older bridge has no such route');
    answer = () => { throw new TypeError('fetch failed'); };
    assert.equal(await set_star(url, 'vc-2', true), false);
});

test('a closed row says it closed starred, and an older bridge says nothing', () => {
    const base = { id: 'a1b2c3d4e5f6', session: 'vc-3', label: null, cwd: '/w/app', profile: 'claude-a', kind: 'claude',
        how: 'killed', closed_at: 1_800_000_000, resumable: true, ok: true };
    const parsed = parse_closed({ sessions: [{ ...base, starred: true }, { ...base, id: 'b1b2c3d4e5f6' }] });
    assert.deepEqual(parsed?.map((row) => row.starred), [true, undefined]);
    const [starred, plain] = closed_rows_view(parsed ?? [], 1_800_000_060_000);
    assert.equal(starred.starred, true);
    assert.match(starred.title, /starred/);
    assert.equal(plain.starred, false);
    assert.doesNotMatch(plain.title, /starred/);
});
