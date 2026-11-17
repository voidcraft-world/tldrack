import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
    OFFICE_GEOMETRY,
    build_office,
    has_desk,
    office_pose,
    office_rooms,
    office_screen,
    office_summary,
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
        assert.ok(rooms[1].agents.every((a) => a.kind === 'service' && !a.lead));
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
    const fleet = (n: number, over: (i: number) => Partial<RackSession> = () => ({})): RackSession[] =>
        Array.from({ length: n }, (_, i) => agent(`agent-${String(i).padStart(2, '0')}`, over(i)));

    test('two agents in a room face each other across one desk pair', () => {
        const [first, second] = desks(build_office(fleet(2)));
        assert.equal(first.desk.x, second.desk.x);
        assert.deepEqual([first.desk.facing, second.desk.facing].sort(), [-1, 1]);
    });

    test('desks never overlap and every seat stays inside its room', () => {
        const sessions = [
            ...fleet(13),
            ...fleet(3, () => ({ repo: 'my-docs' })).map((s, i) => ({ ...s, session: `my-docs-${i}` })),
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
            ...fleet(9),
            ...['my-docs', 'billing', 'web-lab', 'help-desk'].flatMap((repo) =>
                fleet(3).map((s, i) => ({ ...s, session: `${repo}-${i}`, repo })),
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

    test('a state change moves nothing — positions change only with the rack', () => {
        const before = build_office(fleet(6));
        const after = build_office(fleet(6, (i) => ({ state: i % 2 ? 'attention' : 'free' })));
        assert.equal(after.geometry_key, before.geometry_key);
        assert.deepEqual(
            desks(after).map(({ desk }) => [desk.agent.session, desk.x, desk.z, desk.facing]),
            desks(before).map(({ desk }) => [desk.agent.session, desk.x, desk.z, desk.facing]),
        );
        assert.notEqual(build_office(fleet(7)).geometry_key, before.geometry_key);
    });

    test('a big room stacks pods rather than one endless row', () => {
        const layout = build_office(fleet(20));
        const room = layout.rooms[0];
        const columns = new Set(room.desks.map((desk) => desk.x)).size;
        assert.equal(columns, OFFICE_GEOMETRY.pod_columns);
        assert.ok(room.depth > room.width / 2, 'the room grows deep, not only wide');
    });

    test('server cabinets fit inside their room without overlap for one or many rows', () => {
        const g = OFFICE_GEOMETRY;
        for (const count of [1, 2, 4, 5, 13]) {
            const layout = build_office([
                ...fleet(3),
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
        const after = build_office([...sessions].reverse().map((row) => ({ ...row, state: 'attention', model_label: 'opus' })), 'svc-vite');
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
        const layout = build_office(fleet(3), 'agent-01');
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
    assert.equal(office_summary(layout), '3 agents · 2 want you');
    assert.equal(office_summary(build_office([agent('a', { state: 'attention' })])), '1 agent · 1 wants you');
});
