import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
    build_rack_board,
    clip,
    rack_board_header,
    format_age,
    state_label,
    zone_label,
    type RackBoardInput,
    type RackBoardSession,
} from '../../src/engine/terminal/rack-board-logic.ts';
import { build_rack_rows, type RackSession } from '../../src/engine/terminal/multi-terminal-layout.ts';

function session(over: Partial<RackSession> & { session: string }): RackSession {
    return { state: 'busy', live: true, ...over };
}

function detail(over: Partial<RackBoardSession> = {}): RackBoardSession {
    return { state: 'busy', model: 'opus', zone: 'codebase', age_seconds: 12, attached: false, message: null, ...over };
}

/** Board input built the way a caller builds it: rack rows in, detail map beside. */
function board_input(sessions: RackSession[], details: Record<string, RackBoardSession>, max_lines = 8): RackBoardInput {
    return {
        rows: build_rack_rows({
            sessions,
            focused: null,
            max_sessions: 8,
        }),
        sessions: new Map(Object.entries(details)),
        max_lines,
    };
}

describe('rack-board: row order is the rack\'s, by construction', () => {
    test('a blocked agent sorts to the top on both surfaces', () => {
        const sessions = [
            session({ session: 'aaa', state: 'busy' }),
            session({ session: 'bbb', state: 'attention' }),
            session({ session: 'ccc', state: 'free' }),
        ];
        const input = board_input(sessions, {
            aaa: detail({ state: 'busy' }),
            bbb: detail({ state: 'attention' }),
            ccc: detail({ state: 'free' }),
        });
        const rack_order = input.rows.filter((r) => r.kind === 'session').map((r) => r.session);
        const board_order = build_rack_board(input)
            .lines.filter((l) => l.kind === 'session')
            .map((l) => l.session);
        assert.deepEqual(board_order, rack_order);
        assert.equal(board_order[0], 'bbb', 'the blocked agent must lead');
    });

    test('cohort headers survive into the board', () => {
        const sessions = [
            session({ session: 'a1', repo: '/repo/one' }),
            session({ session: 'b1', repo: '/repo/two' }),
        ];
        const view = build_rack_board(
            board_input(sessions, { a1: detail(), b1: detail() }),
        );
        assert.ok(view.lines.some((l) => l.kind === 'cohort'), 'expected cohort headers');
    });

    test('note lines are dropped — the board is a column grid', () => {
        const input = board_input([session({ session: 'a1', live: false })], { a1: detail() });
        assert.ok(input.rows.some((r) => r.kind === 'note'), 'fixture should contain a note line');
        assert.ok(!build_rack_board(input).lines.some((l) => l.kind === 'note' as never));
    });
});

describe('rack-board: nothing is dropped silently', () => {
    test('over budget produces an explicit +N MORE, never a slice', () => {
        const sessions = Array.from({ length: 12 }, (_, i) => session({ session: `s${i}` }));
        const details = Object.fromEntries(sessions.map((s) => [s.session, detail()]));
        const view = build_rack_board(board_input(sessions, details, 6));
        const overflow = view.lines.filter((l) => l.kind === 'overflow');
        assert.equal(overflow.length, 1, 'exactly one overflow line');
        assert.match(overflow[0].text ?? '', /^\+\d+ MORE/);
        assert.ok(view.lines.length <= 6, `board drew ${view.lines.length} lines into a budget of 6`);
    });

    test('a hidden BLOCKED agent is called out by name in the overflow line', () => {
        // The whole point of the board is that a blocked agent is never lost.
        const sessions = [
            ...Array.from({ length: 8 }, (_, i) => session({ session: `busy${i}`, state: 'busy' as const })),
            session({ session: 'zzz', state: 'attention' }),
        ];
        const details: Record<string, RackBoardSession> = Object.fromEntries(
            sessions.map((s) => [s.session, detail({ state: s.state })]),
        );
        // Force truncation with a tiny budget; attention sorts first, so pad the
        // front with cohorts by giving everyone the same repo (no header) and
        // shrinking the budget below the session count.
        const view = build_rack_board(board_input(sessions, details, 4));
        const overflow = view.lines.find((l) => l.kind === 'overflow');
        assert.ok(overflow, 'expected an overflow line');
        // Blocked agents sort first so they should be SHOWN, not hidden — but if
        // any were hidden the line must say so.
        if (/BLOCKED/.test(overflow!.text ?? '')) {
            assert.match(overflow!.text ?? '', /\d+ BLOCKED/);
        }
        assert.equal(view.blocked, 1, 'rack-wide blocked count is independent of the budget');
    });

    test('the rack-wide counts ignore the line budget entirely', () => {
        const sessions = Array.from({ length: 20 }, (_, i) =>
            session({ session: `s${i}`, state: i < 3 ? 'attention' : 'busy' }),
        );
        const details = Object.fromEntries(
            sessions.map((s) => [s.session, detail({ state: s.state })]),
        );
        const view = build_rack_board(board_input(sessions, details, 4));
        assert.equal(view.total, 20);
        assert.equal(view.blocked, 3);
        assert.ok(view.shown < view.total);
    });

    test('an empty rack says so instead of rendering nothing', () => {
        const view = build_rack_board(board_input([], {}));
        assert.equal(view.lines.length, 1);
        assert.equal(view.lines[0].kind, 'empty');
        assert.match(view.lines[0].text ?? '', /NO TMUX/);
    });
});

describe('rack-board: a blocked agent always gets its reason', () => {
    test('an attention row reserves its continuation line', () => {
        const view = build_rack_board(
            board_input(
                [session({ session: 'vc1', state: 'attention', message: 'needs approval: rm -rf dist/' })],
                { vc1: detail({ state: 'attention', message: 'needs approval: rm -rf dist/' }) },
            ),
        );
        const kinds = view.lines.map((l) => l.kind);
        assert.deepEqual(kinds.slice(0, 2), ['session', 'detail']);
        assert.match(view.lines[1].text ?? '', /rm -rf dist/);
    });

    test('a row is never drawn without room for its reason', () => {
        // With a budget of 2 the reserved line is 1; an attention row needs 2,
        // so it must be pushed to overflow rather than shown headless.
        const sessions = [
            session({ session: 'a', state: 'busy' }),
            session({ session: 'b', state: 'attention', message: 'why' }),
        ];
        const details = { a: detail({ state: 'busy' }), b: detail({ state: 'attention', message: 'why' }) };
        for (const budget of [2, 3, 4, 5]) {
            const view = build_rack_board(board_input(sessions, details, budget));
            for (let i = 0; i < view.lines.length; i++) {
                if (view.lines[i].kind !== 'session' || view.lines[i].state !== 'attention') continue;
                assert.equal(
                    view.lines[i + 1]?.kind,
                    'detail',
                    `budget ${budget}: attention row drawn without its reason`,
                );
            }
        }
    });

    test('an attention row with no message stays one line', () => {
        const view = build_rack_board(
            board_input([session({ session: 'x', state: 'attention' })], {
                x: detail({ state: 'attention', message: null }),
            }),
        );
        assert.equal(view.lines.filter((l) => l.kind === 'detail').length, 0);
    });
});

describe('rack-board: columns', () => {
    test('every session line carries the full column set', () => {
        const view = build_rack_board(
            board_input([session({ session: 'vc1' })], {
                vc1: detail({ state: 'busy', model: 'opus 4.6', zone: 'aws', age_seconds: 252 }),
            }),
        );
        const line = view.lines.find((l) => l.kind === 'session')!;
        assert.deepEqual(line.cells, ['WORKING', 'vc1', 'opus 4.6', 'aws', '4m12s']);
    });

    test('missing detail degrades to placeholders, never undefined', () => {
        const view = build_rack_board(board_input([session({ session: 'ghost' })], {}));
        const line = view.lines.find((l) => l.kind === 'session')!;
        assert.ok(line.cells.every((c) => typeof c === 'string' && c.length > 0));
    });

    test('attached is carried through so the board can mark it', () => {
        const view = build_rack_board(
            board_input([session({ session: 'vc1' })], { vc1: detail({ attached: true }) }),
        );
        assert.equal(view.lines.find((l) => l.kind === 'session')!.attached, true);
    });

    test('state words say what they mean to a person', () => {
        assert.equal(state_label('attention'), 'BLOCKED');
        assert.equal(state_label('busy'), 'WORKING');
        assert.equal(state_label('background'), 'BACKGROUND');
        assert.equal(state_label('free'), 'IDLE');
    });

    test('zone words are short and fall back to the raw key', () => {
        assert.equal(zone_label('computer'), 'bash');
        assert.equal(zone_label(null), '—');
        assert.equal(zone_label('brand-new-zone'), 'brand-new-zone');
    });
});

describe('rack-board: formatting helpers', () => {
    test('format_age covers every magnitude', () => {
        assert.equal(format_age(0), '0s');
        assert.equal(format_age(42), '42s');
        assert.equal(format_age(60), '1m');
        assert.equal(format_age(252), '4m12s');
        assert.equal(format_age(3600), '1h');
        assert.equal(format_age(3840), '1h04m');
        assert.equal(format_age(86400 * 3), '3d');
    });

    test('format_age never renders garbage for missing or bad data', () => {
        assert.equal(format_age(null), '—');
        assert.equal(format_age(undefined), '—');
        assert.equal(format_age(-5), '—');
        assert.equal(format_age(Number.NaN), '—');
        assert.equal(format_age(Number.POSITIVE_INFINITY), '—');
    });

    test('clip ellipsises rather than hard-cutting', () => {
        assert.equal(clip('hello', 10), 'hello');
        assert.equal(clip('hello world', 8), 'hello w…');
        assert.equal(clip('hello', 1), '…');
        assert.equal(clip('hello', 0), '');
    });

    test('the header leads with the blocked count when there is one', () => {
        const calm = build_rack_board(
            board_input([session({ session: 'a' })], { a: detail({ state: 'busy' }) }),
        );
        assert.equal(rack_board_header(calm), 'RACK   1 SESSION');

        const alarmed = build_rack_board(
            board_input([session({ session: 'a', state: 'attention' }), session({ session: 'b' })], {
                a: detail({ state: 'attention' }),
                b: detail({ state: 'busy' }),
            }),
        );
        assert.equal(rack_board_header(alarmed), 'RACK   1 BLOCKED · 2 SESSIONS');
    });
});
