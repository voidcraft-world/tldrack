import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
    OFFICE_GEOMETRY,
    band_agents,
    build_office,
    has_desk,
    office_band,
    office_band_counts,
    office_pose,
    office_rooms,
    office_screen,
    office_summary,
    room_extent,
    type OfficeLayout,
} from '../../src/engine/terminal/agent-office-logic.ts';
import type { RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';

const LEAD_ID = '11111111-1111-4111-8111-111111111111';
const CHILD_A = '22222222-2222-4222-8222-222222222222';
const CHILD_B = '33333333-3333-4333-8333-333333333333';
const GONE_ID = '44444444-4444-4444-8444-444444444444';

function agent(session: string, over: Partial<RackSession> = {}): RackSession {
    return { session, state: 'busy', live: true, repo: 'my-app', model_label: 'opus', ...over };
}

function desks(layout: OfficeLayout) {
    return layout.rooms.flatMap((room) => room.desks.map((desk) => ({ room, desk })));
}

describe('pose and screen', () => {
    test('state outranks activity: only a busy agent works', () => {
        assert.equal(office_pose('busy', 'codebase'), 'working');
        assert.equal(office_pose('busy', 'thinking'), 'thinking');
        assert.equal(office_pose('attention', 'codebase'), 'waiting');
        assert.equal(office_pose('free', 'codebase'), 'idle');
        assert.equal(office_pose('done', 'desk'), 'idle');
        assert.equal(office_pose('background', 'tests'), 'idle');
    });

    test('the screen names the zone while busy, and is dark when the turn is over', () => {
        assert.equal(office_screen('busy', 'codebase'), 'CODE');
        assert.equal(office_screen('busy', 'tests'), 'TEST');
        assert.equal(office_screen('busy', 'thinking'), '···');
        assert.equal(office_screen('attention', 'desk'), '?');
        assert.equal(office_screen('background', 'desk'), 'BG');
        assert.equal(office_screen('free', 'codebase'), '');
    });

    test('an agent reads its activity zone off the row', () => {
        const layout = build_office([agent('a', { activity: { zone: 'aws', tool: 'Bash', detail: 'aws s3 ls' } })]);
        const { desk } = desks(layout)[0];
        assert.equal(desk.agent.pose, 'working');
        assert.equal(desk.agent.screen, 'AWS');
        assert.equal(desk.agent.tool, 'Bash');
        assert.equal(desk.agent.detail, 'aws s3 ls');
    });
});

describe('who gets a desk', () => {
    test('dead sessions and reserved services do not get agent desks, regardless of model telemetry', () => {
        assert.equal(has_desk(agent('a')), true);
        assert.equal(has_desk(agent('a', { live: false })), false);
        assert.equal(has_desk(agent('svc-vite', { model_label: null })), false);
        assert.equal(has_desk(agent('svc-docs', { model_label: 'opus' })), false);
        assert.equal(has_desk(agent('laya-train', { model_label: 'opus' })), false);
    });
});

describe('rooms', () => {
    test('services have one room separate from projects and families, even with model and lineage fields', () => {
        const rooms = office_rooms([
            agent('lead', { lineage: { id: LEAD_ID } }),
            agent('svc-vite', { model_label: null, repo: 'web' }),
            agent('svc-docs', { auto_name: 'Inherited title', lineage: { id: CHILD_A, parent_id: LEAD_ID } }),
            agent('laya-train'),
            agent('svc-closed', { live: false }),
        ]);
        assert.deepEqual(rooms.map((room) => [room.kind, room.agents.map((a) => a.session)]), [
            ['project', ['lead']],
            ['services', ['laya-train', 'svc-docs', 'svc-vite']],
        ]);
        assert.equal(rooms[1].label, 'SERVICES');
        assert.equal(rooms[1].agents[1].label, 'svc-docs');
        assert.ok(rooms[1].agents.every((a) => a.kind === 'service' && !a.lead && a.emoji === ''));
        assert.equal(rooms[0].agents[0].kind, 'agent');
    });

    test('solo agents sit in their project room, worktrees folded onto the repo', () => {
        const rooms = office_rooms([
            agent('b', { repo: null, cwd: '/Users/me/worktrees/my-app-wt-2' }),
            agent('a'),
            agent('c', { repo: 'web' }),
            agent('d', { repo: null, cwd: null }),
        ]);
        assert.deepEqual(
            rooms.map((room) => [room.label, room.kind, room.agents.map((a) => a.session)]),
            [
                ['MY-APP', 'project', ['a', 'b']],
                ['OTHER', 'project', ['d']],
                ['WEB', 'project', ['c']],
            ],
        );
    });

    test('a spawn family gets its own TEAM room, lead first', () => {
        const rooms = office_rooms([
            agent('zeta', { lineage: { id: CHILD_B, parent_id: LEAD_ID } }),
            agent('lead', { auto_name: 'Big plan', lineage: { id: LEAD_ID } }),
            agent('alpha', { lineage: { id: CHILD_A, parent_id: LEAD_ID } }),
            agent('solo'),
        ]);
        const team = rooms.find((room) => room.kind === 'team');
        assert.ok(team);
        assert.equal(team.label, 'BIG PLAN');
        assert.deepEqual(team.agents.map((a) => [a.session, a.lead]), [['lead', true], ['alpha', false], ['zeta', false]]);
        assert.deepEqual(rooms.find((room) => room.kind === 'project')?.agents.map((a) => a.session), ['solo']);
    });

    test('children whose lead closed stay a team, named after it', () => {
        const rooms = office_rooms([
            agent('kid', { lineage: { id: CHILD_A, parent_id: GONE_ID, parent_name: 'old-lead' } }),
        ]);
        assert.equal(rooms.length, 1);
        assert.equal(rooms[0].kind, 'team');
        assert.equal(rooms[0].label, 'OLD-LEAD');
        assert.equal(rooms[0].agents[0].lead, false);
    });
});

describe('layout', () => {
    const rack = (n: number, over: (i: number) => Partial<RackSession> = () => ({})): RackSession[] =>
        Array.from({ length: n }, (_, i) => agent(`agent-${String(i).padStart(2, '0')}`, over(i)));

    test('two agents in a room face each other across one desk pair', () => {
        const [first, second] = desks(build_office(rack(2)));
        assert.equal(first.desk.x, second.desk.x);
        assert.deepEqual([first.desk.facing, second.desk.facing].sort(), [-1, 1]);
    });

    test('desks never overlap and every seat stays inside its room', () => {
        const sessions = [
            ...rack(13),
            ...rack(3, () => ({ repo: 'my-docs' })).map((s, i) => ({ ...s, session: `my-docs-${i}` })),
            agent('lead', { lineage: { id: LEAD_ID } }),
            agent('kid', { lineage: { id: CHILD_A, parent_id: LEAD_ID } }),
        ];
        const layout = build_office(sessions);
        assert.equal(layout.agents, sessions.length);
        const all = desks(layout);
        const g = OFFICE_GEOMETRY;
        for (let i = 0; i < all.length; i++) {
            for (let j = i + 1; j < all.length; j++) {
                const a = all[i].desk;
                const b = all[j].desk;
                const apart = Math.abs(a.x - b.x) >= g.desk_width - 1e-9 || Math.abs(a.z - b.z) >= g.desk_depth - 1e-9;
                assert.ok(apart, `${a.agent.session} overlaps ${b.agent.session}`);
            }
        }
        for (const { room, desk } of all) {
            const seat_z = desk.z - desk.facing * g.seat_offset_z;
            for (const z of [desk.z, seat_z]) {
                assert.ok(Math.abs(z - room.z) < room.depth / 2, `${desk.agent.session} z outside ${room.label}`);
            }
            assert.ok(Math.abs(desk.x - room.x) + g.desk_width / 2 <= room.width / 2 + 1e-9, `${desk.agent.session} x outside ${room.label}`);
        }
    });

    test('rooms never overlap', () => {
        const sessions = [
            ...rack(9),
            ...['my-docs', 'billing', 'web-lab', 'help-desk'].flatMap((repo) =>
                rack(3).map((s, i) => ({ ...s, session: `${repo}-${i}`, repo })),
            ),
        ];
        const { rooms } = build_office(sessions);
        for (let i = 0; i < rooms.length; i++) {
            for (let j = i + 1; j < rooms.length; j++) {
                const a = rooms[i];
                const b = rooms[j];
                const apart =
                    Math.abs(a.x - b.x) >= (a.width + b.width) / 2 - 1e-9 ||
                    Math.abs(a.z - b.z) >= (a.depth + b.depth) / 2 - 1e-9;
                assert.ok(apart, `${a.label} overlaps ${b.label}`);
            }
        }
    });

    test('a state that stays in its band moves nothing; one that crosses a band re-seats the room', () => {
        const place = (layout: OfficeLayout) => desks(layout).map(({ desk }) => [desk.agent.session, desk.x, desk.z, desk.facing]);
        const before = build_office(rack(6));
        // busy with a tool, busy thinking, and a finished turn with work still running: all WORKING.
        const same_band = build_office(rack(6, (i) => (i % 2
            ? { state: 'background' }
            : { activity: { zone: 'thinking', tool: null, detail: null } })));
        assert.equal(same_band.geometry_key, before.geometry_key);
        assert.deepEqual(place(same_band), place(before));
        // free ↔ done: both IDLE.
        const idle = build_office(rack(6, () => ({ state: 'free' })));
        assert.equal(build_office(rack(6, (i) => ({ state: i % 2 ? 'done' : 'free' }))).geometry_key, idle.geometry_key);
        const crossed = build_office(rack(6, (i) => ({ state: i % 2 ? 'attention' : 'free' })));
        assert.notEqual(crossed.geometry_key, before.geometry_key);
        assert.notEqual(build_office(rack(7)).geometry_key, before.geometry_key);
    });

    test('a big room stacks pods rather than one endless row', () => {
        const layout = build_office(rack(20));
        const room = layout.rooms[0];
        const columns = new Set(room.desks.map((desk) => desk.x)).size;
        assert.equal(columns, OFFICE_GEOMETRY.pod_columns);
        assert.ok(room.depth > room.width / 2, 'the room grows deep, not only wide');
    });

    test('server cabinets fit inside their room without overlap for one or many rows', () => {
        const g = OFFICE_GEOMETRY;
        for (const count of [1, 2, 4, 5, 13]) {
            const layout = build_office([
                ...rack(3),
                ...Array.from({ length: count }, (_, i) => agent(`svc-${i}`)),
            ]);
            const room = layout.rooms.find((room) => room.kind === 'services')!;
            assert.equal(layout.agents, 3);
            assert.equal(layout.services, count);
            for (const cabinet of room.desks) {
                assert.equal(cabinet.facing, 1);
                assert.ok(Math.abs(cabinet.x - room.x) + g.server_width / 2 < room.width / 2);
                assert.ok(cabinet.z - g.server_depth / 2 > room.z - room.depth / 2);
                assert.ok(cabinet.z + g.server_depth / 2 < room.z + room.depth / 2 - g.label_strip);
                for (const other of room.desks) {
                    if (other === cabinet) continue;
                    assert.ok(Math.abs(cabinet.x - other.x) >= g.server_width || Math.abs(cabinet.z - other.z) >= g.server_depth);
                }
            }
            for (const other of layout.rooms.filter((r) => r !== room)) {
                assert.ok(Math.abs(room.x - other.x) >= (room.width + other.width) / 2 ||
                    Math.abs(room.z - other.z) >= (room.depth + other.depth) / 2);
            }
        }
    });

    test('service status, model telemetry, focus and input order do not move furniture', () => {
        const sessions = [agent('a'), agent('svc-vite', { model_label: null }), agent('svc-docs')];
        const before = build_office(sessions);
        // A service's state is on its cabinet: it has no band to cross.
        const after = build_office([...sessions].reverse().map((row) => ({
            ...row, state: row.session === 'a' ? row.state : 'attention', model_label: 'opus' })), 'svc-vite');
        assert.equal(after.geometry_key, before.geometry_key);
        const selected = desks(after).find(({ desk }) => desk.agent.focused)?.desk.agent;
        assert.equal(selected?.session, 'svc-vite');
        assert.equal(selected?.kind, 'service');
        assert.equal(selected?.pose, 'idle');
    });

    test('a service-only office keeps its room and counts services separately', () => {
        const layout = build_office([agent('svc-vite', { state: 'unknown', model_label: null })]);
        assert.equal(layout.rooms.length, 1);
        assert.equal(layout.agents, 0);
        assert.equal(layout.services, 1);
        assert.equal(layout.rooms[0].desks[0].agent.screen, 'SERVICE');
        assert.equal(office_summary(layout), '0 agents · 1 service');
        assert.equal(office_summary(build_office([agent('svc-vite'), agent('svc-docs'), agent('a', { state: 'attention' })])),
            '1 agent · 2 services · 1 wants you');
    });

    test('the focused agent is flagged', () => {
        const layout = build_office(rack(3), 'agent-01');
        assert.deepEqual(
            desks(layout).filter(({ desk }) => desk.agent.focused).map(({ desk }) => desk.agent.session),
            ['agent-01'],
        );
    });

    test('an empty rack is an empty office', () => {
        const layout = build_office([]);
        assert.equal(layout.rooms.length, 0);
        assert.equal(layout.agents, 0);
        assert.equal(office_summary(layout), '0 agents');
    });
});

test('summary counts the agents that want you', () => {
    const layout = build_office([agent('a', { state: 'attention' }), agent('b'), agent('c', { state: 'attention' })]);
    assert.equal(office_summary(layout), '3 agents · 1 working · 2 want you');
    assert.equal(office_summary(build_office([agent('a', { state: 'attention' })])), '1 agent · 1 wants you');
    assert.equal(office_summary(build_office([agent('a', { state: 'free' })])), '1 agent');
});

describe('bands', () => {
    const mixed = (): RackSession[] => [
        agent('a-idle', { state: 'free' }),
        agent('b-works'),
        agent('c-asks', { state: 'attention' }),
        agent('d-works', { state: 'background' }),
        agent('e-idle', { state: 'done' }),
        agent('f-idle', { state: 'unknown' }),
    ];

    test('an agent sits in the band its state names', () => {
        assert.equal(office_band({ pose: 'waiting', state: 'attention' }), 'waiting');
        assert.equal(office_band({ pose: 'working', state: 'busy' }), 'working');
        assert.equal(office_band({ pose: 'thinking', state: 'busy' }), 'working');
        assert.equal(office_band({ pose: 'idle', state: 'background' }), 'working', 'its work is still running');
        for (const state of ['free', 'done', 'unknown'] as const) assert.equal(office_band({ pose: 'idle', state }), 'idle');
    });

    test('a room is cut into NEEDS YOU, WORKING, IDLE, left to right, each holding exactly its own', () => {
        const [room] = build_office(mixed()).rooms;
        assert.deepEqual(room.bands.map((band) => [band.label, band.count]), [['NEEDS YOU', 1], ['WORKING', 2], ['IDLE', 3]]);
        for (let i = 1; i < room.bands.length; i++) assert.ok(room.bands[i].x > room.bands[i - 1].x);
        for (const desk of room.desks) {
            const band = room.bands.find((candidate) => candidate.key === office_band(desk.agent))!;
            assert.ok(Math.abs(desk.x - band.x) + OFFICE_GEOMETRY.desk_width / 2 <= band.width / 2 + 1e-9,
                `${desk.agent.session} sits outside ${band.label}`);
        }
        // Edge to edge: the bands tile the room's width with no floor left over and none shared.
        const left = room.x - room.width / 2;
        assert.ok(Math.abs(room.bands[0].x - room.bands[0].width / 2 - left) < 1e-9);
        for (let i = 1; i < room.bands.length; i++) {
            const a = room.bands[i - 1];
            const b = room.bands[i];
            assert.ok(Math.abs((a.x + a.width / 2) - (b.x - b.width / 2)) < 1e-9, `${a.label} and ${b.label} meet at the divider`);
        }
        const last = room.bands[room.bands.length - 1];
        assert.ok(Math.abs(last.x + last.width / 2 - (left + room.width)) < 1e-9);
    });

    test('an empty band takes no floor, and inside a band the order is by name', () => {
        const [room] = build_office(mixed().filter((row) => row.state !== 'attention')).rooms;
        assert.deepEqual(room.bands.map((band) => band.key), ['working', 'idle']);
        assert.deepEqual(band_agents(room.desks.map((desk) => desk.agent)).map((band) => band.agents.map((a) => a.session)),
            [['b-works', 'd-works'], ['a-idle', 'e-idle', 'f-idle']]);
        const [alone] = build_office([agent('a')]).rooms;
        assert.equal(alone.bands.length, 1);
        assert.ok(Math.abs(alone.bands[0].width - alone.width) < 1e-9, 'one band is the whole room');
    });

    test('a room is as wide as its bands and as deep as the deepest', () => {
        const g = OFFICE_GEOMETRY;
        const one = room_extent(4);
        assert.deepEqual(room_extent([4]), one);
        assert.deepEqual(room_extent([0, 4, 0]), one, 'empty bands add nothing');
        const split = room_extent([2, 2]);
        assert.ok(Math.abs(split.width - (2 * g.desk_pitch_x + g.band_gap + 2 * g.room_padding)) < 1e-9);
        assert.equal(room_extent([2, 10]).depth, room_extent(10).depth);
    });

    test('a team keeps its lead first in whichever band the lead is in', () => {
        const layout = build_office([
            agent('zz-lead', { state: 'free', lineage: { id: LEAD_ID } }),
            agent('aa-child', { state: 'free', lineage: { id: CHILD_A, parent_id: LEAD_ID } }),
            agent('bb-child', { lineage: { id: CHILD_B, parent_id: LEAD_ID } }),
        ]);
        const team = layout.rooms.find((room) => room.kind === 'team')!;
        assert.deepEqual(team.bands.map((band) => band.key), ['working', 'idle']);
        assert.deepEqual(team.desks.map((desk) => desk.agent.session), ['bb-child', 'zz-lead', 'aa-child']);
    });

    test('the whole office counts its bands, services left out', () => {
        const layout = build_office([...mixed(), agent('svc-vite', { state: 'attention' })]);
        assert.deepEqual(office_band_counts(layout), { waiting: 1, working: 2, idle: 3 });
    });
});


test('facing desks have a visible gap, including in full and stacked pods', () => {
    const g = OFFICE_GEOMETRY;
    for (const count of [2, 8, 16, 20]) {
        const layout = build_office(Array.from({ length: count }, (_, i) => agent(`agent-${String(i).padStart(2, '0')}`)));
        const all = layout.rooms.flatMap(room => room.desks);
        for (let i = 0; i < all.length - 1; i += 2) {
            const a = all[i], b = all[i + 1];
            assert.equal(a.x, b.x);
            assert.ok(Math.abs(a.z - b.z) - g.desk_depth >= 0.39);
        }
    }
});


test('activity sections stay inside each project room and update independently', () => {
    const states = ['busy', 'background', 'attention', 'free', 'done', 'unknown'] as const;
    const sessions = ['app', 'docs'].flatMap(repo => Array.from({ length: 18 }, (_, i) =>
        agent(`${repo}-${i}`, { repo, state: states[i % states.length] })));
    const layout = build_office(sessions);
    assert.equal(layout.rooms.length, 2);
    for (const room of layout.rooms) {
        assert.deepEqual(room.bands.map(band => [band.key, band.count]), [['waiting', 3], ['working', 6], ['idle', 9]]);
        for (const desk of room.desks) {
            assert.ok(desk.agent.session.startsWith(room.label.toLowerCase()));
            const band = room.bands.find(band => band.key === office_band(desk.agent))!;
            assert.ok(Math.abs(desk.x - band.x) + OFFICE_GEOMETRY.desk_width / 2 <= band.width / 2 + 1e-9);
            assert.ok(Math.abs(desk.z - band.z) + OFFICE_GEOMETRY.seat_offset_z < band.depth / 2);
        }
    }
    const after = build_office(sessions.map(row => row.repo === 'app' ? { ...row, state: 'done' as const } : row));
    assert.deepEqual(after.rooms.find(room => room.label === 'APP')!.bands.map(band => [band.key, band.count]), [['idle', 18]]);
    assert.deepEqual(after.rooms.find(room => room.label === 'DOCS')!.bands.map(band => [band.key, band.count]),
        layout.rooms.find(room => room.label === 'DOCS')!.bands.map(band => [band.key, band.count]));
    assert.deepEqual(build_office(sessions.toReversed()).rooms, layout.rooms);
});
