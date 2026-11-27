import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { build_office } from '../../src/engine/terminal/agent-office-logic.ts';
import {
    BOARD_COLORS,
    board_lines,
    office_accounts,
    office_machines,
    parse_office_board,
    type OfficeBoardData,
} from '../../src/engine/terminal/office-board.ts';
import { device_rows, parse_devices } from '../../src/engine/terminal/device-readout.ts';
import { parse_profiles } from '../../src/engine/terminal/terminal-status-logic.ts';
import { display_state_color_css } from '../../src/config/terminal-status-flags.ts';
import type { RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';

// Every value here is invented.
const GIB = 1024 ** 3;
const agent = (session: string, state: RackSession['state'] = 'busy'): RackSession => ({ session, state, live: true, repo: 'my-app' });
const profiles = () => parse_profiles([
    { id: 'one', label: 'ONE', kind: 'claude', usage: { five_hour: { used_percentage: 12 }, seven_day: { used_percentage: 67.4 }, age_seconds: 5 } },
    { id: 'two', label: 'TWO', kind: 'claude', usage: { seven_day: { used_percentage: 91 }, age_seconds: 5 } },
    { id: 'three', label: 'THREE', kind: 'claude', usage: { seven_day: { used_percentage: 41 }, age_seconds: 9000 },
        standing: { state: 'out', error: 'billing_error', message: 'Your plan has ended', since: 1 } },
    { id: 'shell', label: 'SHELL', kind: 'shell' },
])!;
const devices = () => device_rows(parse_devices({ sample_s: 2, devices: [
    { name: 'local', local: true, state: 'up', cores: 10, cpu_busy: 2.2, mem_used: 29 * GIB, mem_total: 32 * GIB, age_s: 1 },
    { name: 'build-box', local: false, state: 'down', error: 'ssh: connect timed out' },
] })!);
const board = (): OfficeBoardData => ({ accounts: office_accounts(profiles()), machines: office_machines(devices()) });

describe('what the rack projects', () => {
    test('an account is a line when it reported usage or is out; a shell is not', () => {
        assert.deepEqual(office_accounts(profiles()), [
            { label: 'ONE', five_hour: 12, week: 67, out: null },
            { label: 'TWO', five_hour: null, week: 91, out: null },
            { label: 'THREE', five_hour: null, week: 41, out: 'plan ended' },
        ]);
    });

    test('a machine carries its meters as whole percentages, and its status when it has no meters', () => {
        const [local, remote] = office_machines(devices());
        assert.deepEqual([local.label, local.tone, local.status, local.cpu, local.ram], ['LOCAL', 'ok', null, 22, 91]);
        assert.deepEqual([remote.tone, remote.status, remote.cpu, remote.ram], ['bad', 'OFFLINE', null, null]);
    });

    test('a relayed board survives the wire, and a malformed one is dropped by the row', () => {
        assert.deepEqual(parse_office_board(JSON.parse(JSON.stringify(board()))), board());
        assert.equal(parse_office_board(null), undefined);
        assert.equal(parse_office_board({ accounts: [], machines: [] }), undefined);
        assert.deepEqual(parse_office_board({ accounts: [{ label: '', week: 5 }, { label: 'OK', week: 250, five_hour: 'x', out: 7 }, 'nope'], machines: 'nope' }),
            { accounts: [{ label: 'OK', five_hour: null, week: 100, out: null }], machines: [] });
        assert.equal(parse_office_board({ machines: [{ label: 'm', tone: 'purple', cpu: -3 }] })!.machines[0].tone, 'wait');
    });
});

describe('the lines', () => {
    const layout = () => build_office([agent('a'), agent('b'), agent('c', 'attention'), agent('d', 'free'), agent('svc-vite')]);

    test('the three counts always show, each in its state hue while it holds someone', () => {
        const lines = board_lines(layout(), undefined);
        assert.deepEqual(lines.map((line) => line.text), ['RACK · 4 AGENTS · 1 SERVICE', '  2 WORKING', '  1 NEEDS YOU', '  1 IDLE']);
        assert.equal(lines[1].color, display_state_color_css('busy'));
        assert.equal(lines[2].color, display_state_color_css('attention'));
        const quiet = board_lines(build_office([agent('a', 'free')]), undefined);
        assert.deepEqual(quiet.map((line) => line.text), ['RACK · 1 AGENT', '  0 WORKING', '  0 NEED YOU', '  1 IDLE']);
        assert.ok(quiet.slice(1).every((line) => line.color === BOARD_COLORS.dim));
    });

    test('an account near its cap is amber, one that is out says why in red, in aligned columns', () => {
        const lines = board_lines(layout(), board());
        const at = lines.findIndex((line) => line.text === 'ACCOUNTS');
        assert.deepEqual(lines.slice(at + 1, at + 4), [
            { text: 'ONE    5H  12%  WK  67%', color: BOARD_COLORS.text },
            { text: 'TWO             WK  91%', color: BOARD_COLORS.warn },
            { text: 'THREE  OUT · PLAN ENDED', color: BOARD_COLORS.bad },
        ]);
    });

    test('a machine shows its meters, or the one word that replaces them', () => {
        const lines = board_lines(layout(), board());
        const at = lines.findIndex((line) => line.text === 'MACHINES');
        assert.deepEqual(lines.slice(at + 1), [
            { text: 'LOCAL      CPU  22%  RAM  91%', color: BOARD_COLORS.warn },
            { text: 'BUILD-BOX  OFFLINE', color: BOARD_COLORS.bad },
        ]);
    });

    test('a rack that stopped answering greys its accounts and machines and says so; the counts stay', () => {
        const lines = board_lines(layout(), board(), true);
        assert.ok(lines.some((line) => line.text === 'ACCOUNTS · STALE'));
        assert.ok(lines.some((line) => line.text === 'MACHINES · STALE'));
        assert.equal(lines[1].color, display_state_color_css('busy'));
        const body = lines.filter((line) => line.color !== BOARD_COLORS.head).slice(3);
        assert.ok(body.every((line) => line.color === BOARD_COLORS.dim));
    });
});
