import test from 'node:test';
import assert from 'node:assert/strict';
import {
    advance_tracker,
    as_hook_state,
    background_jobs_label,
    classify_rename_status,
    classify_shutdown_status,
    count_by_display,
    derive_effective_state,
    display_state,
    estimate_session_cost,
    rack_summary_segments,
    format_bridge_sublabel,
    format_rack_summary,
    format_model_label,
    format_model_name,
    format_token_count,
    format_transition_text,
    format_usd,
    format_uptime_short,
    is_live,
    launched_by_short,
    mark_seen,
    rename_failure_message,
    new_tracker,
    UNKNOWN_MODEL_LABEL,
    parse_plugin_names,
    parse_profile_id,
    parse_profile_standing,
    parse_profiles,
    standing_reason,
    AUTO_PROFILE_ID,
    parse_launch_pick,
    parse_session_row,
    parse_stop_failure,
    parse_session_rows,
    parse_transition_entry,
    parse_usage_limits,
    resolve_launch_profile,
    PROFILE_LABEL_MAX,
    status_port_for_url,
    summarize_rack_attention,
    title_hint_state,
    ATTENTION_NOTICE_TAG,
    type BridgeIdentity,
    type SessionStatus,
    type SessionTracker,
    type SessionUsage,
    type TerminalHookState,
    type TransitionEntry,
} from '../../src/engine/terminal/terminal-status-logic.ts';

const status = (over: Partial<SessionStatus>): SessionStatus => ({
    session: 'main',
    state: 'unknown',
    age_seconds: null,
    message: null,
    title: null,
    attached: false,
    ...over,
});

test('status_port_for_url derives ttyd port + 1', () => {
    assert.equal(status_port_for_url('http://localhost:7681'), 7682);
    assert.equal(status_port_for_url('http://127.0.0.1:7681/?arg=claude'), 7682);
    assert.equal(status_port_for_url('http://localhost:8080/?arg=x'), 8081);
    assert.equal(status_port_for_url('http://localhost'), 81);
    assert.equal(status_port_for_url('not a url'), null);
});

test('is_live hides only explicit live:false rows (missing field = alive)', () => {
    assert.equal(is_live(status({ live: true })), true);
    assert.equal(is_live(status({ live: false })), false); // killed tmux session
    assert.equal(is_live(status({})), true); // pre-live bridge / legacy /status path
});

test('title_hint_state reads Claude Code OSC title prefixes', () => {
    assert.equal(title_hint_state('⠹ refactoring auth'), 'busy'); // braille spinner
    assert.equal(title_hint_state('✳ claude'), 'free');
    assert.equal(title_hint_state('✳no-space'), null);
    assert.equal(title_hint_state('plain shell title'), null);
    assert.equal(title_hint_state(null), null);
    assert.equal(title_hint_state(''), null);
});

test('derive_effective_state downgrades stale busy and applies title hints to unknowns', () => {
    const opts = { busy_stale_after_s: 600, title_hint_enabled: true };
    assert.equal(derive_effective_state(status({ state: 'busy', age_seconds: 30 }), opts), 'busy');
    assert.equal(derive_effective_state(status({ state: 'busy', age_seconds: 700 }), opts), 'unknown');
    // stale busy + working title hint → busy again via the hint
    assert.equal(
        derive_effective_state(status({ state: 'busy', age_seconds: 700, title: '⠧ working' }), opts),
        'busy',
    );
    assert.equal(derive_effective_state(status({ state: 'unknown', title: '✳ claude' }), opts), 'free');
    // hints never override a live hook state
    assert.equal(derive_effective_state(status({ state: 'attention', title: '⠧ x' }), opts), 'attention');
    // hint disabled
    assert.equal(
        derive_effective_state(status({ state: 'unknown', title: '✳ claude' }), {
            busy_stale_after_s: 600,
            title_hint_enabled: false,
        }),
        'unknown',
    );
});

const run = (
    tracker: SessionTracker,
    states: TerminalHookState[],
    opts: { debounce_polls?: number; watching?: boolean } = {},
) => {
    let fired_attention = 0;
    let fired_done = 0;
    for (const s of states) {
        const res = advance_tracker(tracker, s, {
            debounce_polls: opts.debounce_polls ?? 2,
            watching: opts.watching ?? false,
        });
        tracker = res.tracker;
        if (res.fire_attention) fired_attention += 1;
        if (res.fire_done) fired_done += 1;
    }
    return { tracker, fired_attention, fired_done };
};

test('upgrades commit instantly; downgrades wait out the debounce', () => {
    let { tracker } = run(new_tracker(), ['busy']);
    assert.equal(display_state(tracker), 'busy'); // instant upgrade

    // one 'free' poll is not enough to downgrade...
    ({ tracker } = run(tracker, ['free']));
    assert.equal(display_state(tracker), 'busy');
    // ...the second commits it
    ({ tracker } = run(tracker, ['free'], { watching: true }));
    assert.equal(display_state(tracker), 'free');
});

test('flapping resets the downgrade hold', () => {
    let { tracker } = run(new_tracker(), ['busy', 'free', 'busy', 'free']);
    assert.equal(display_state(tracker), 'busy'); // never saw 2 consecutive frees
});

test('attention fires once after debounce_polls consecutive polls', () => {
    const res = run(new_tracker(), ['attention', 'attention', 'attention', 'attention']);
    assert.equal(res.fired_attention, 1);
    assert.equal(display_state(res.tracker), 'attention'); // glow was instant regardless
});

test('busy→free while unwatched raises done until mark_seen', () => {
    let { tracker, fired_done } = run(new_tracker(), ['busy', 'free', 'free'], { watching: false });
    assert.equal(fired_done, 1);
    assert.equal(display_state(tracker), 'done');
    tracker = mark_seen(tracker);
    assert.equal(display_state(tracker), 'free');
});

test('busy→free while watching never raises done', () => {
    const { tracker, fired_done } = run(new_tracker(), ['busy', 'free', 'free'], { watching: true });
    assert.equal(fired_done, 0);
    assert.equal(display_state(tracker), 'free');
});

test('a new busy period clears a lingering done highlight', () => {
    let { tracker } = run(new_tracker(), ['busy', 'free', 'free']);
    assert.equal(display_state(tracker), 'done');
    ({ tracker } = run(tracker, ['busy']));
    assert.equal(display_state(tracker), 'busy');
    assert.equal(tracker.done_pending, false);
});

test('attention→free while unwatched also raises done', () => {
    const { tracker, fired_done } = run(new_tracker(), ['attention', 'free', 'free']);
    assert.equal(fired_done, 1);
    assert.equal(display_state(tracker), 'done');
});

// ── background: the turn is over, the work is not ───────────────────────────

test('busy→background is a quiet downgrade: debounced, and it fires nothing', () => {
    let { tracker, fired_done, fired_attention } = run(new_tracker(), ['busy', 'background']);
    assert.equal(display_state(tracker), 'busy', 'one poll is not enough to downgrade');
    ({ tracker, fired_done, fired_attention } = run(tracker, ['background', 'background', 'background']));
    assert.equal(display_state(tracker), 'background');
    assert.equal(fired_done, 0, 'nothing is done while work still runs');
    assert.equal(fired_attention, 0);
    assert.equal(tracker.done_pending, false);
});

test('background→free while unwatched fires done EXACTLY once', () => {
    const { tracker, fired_done } = run(new_tracker(), ['busy', 'background', 'background', 'free', 'free', 'free', 'free']);
    assert.equal(fired_done, 1, 'busy→background said nothing, so the one done is the jobs ending');
    assert.equal(display_state(tracker), 'done');
});

test('background→free while watching never raises done', () => {
    const { tracker, fired_done } = run(new_tracker(), ['background', 'free', 'free'], { watching: true });
    assert.equal(fired_done, 0);
    assert.equal(display_state(tracker), 'free');
});

test('a background session is never shown as done while its jobs run', () => {
    // done_pending latched by an earlier finish must not survive a new background period.
    let { tracker } = run(new_tracker(), ['busy', 'free', 'free']);
    assert.equal(display_state(tracker), 'done');
    ({ tracker } = run(tracker, ['background']));
    assert.equal(display_state(tracker), 'background', 'an upgrade to background commits instantly');
    assert.equal(tracker.done_pending, false, 'and clears the stale highlight');
});

test('priority: attention > busy > background > free > unknown — upgrades instant, downgrades held', () => {
    const ladder: TerminalHookState[] = ['unknown', 'free', 'background', 'busy', 'attention'];
    for (let low = 0; low < ladder.length; low += 1) {
        for (let high = low + 1; high < ladder.length; high += 1) {
            const at_low = run(new_tracker(), [ladder[low], ladder[low]]).tracker;
            const up = run(at_low, [ladder[high]]).tracker;
            assert.equal(up.committed, ladder[high], `${ladder[low]}→${ladder[high]} is an upgrade`);
            const down = run(up, [ladder[low]]).tracker;
            assert.equal(down.committed, ladder[high], `${ladder[high]}→${ladder[low]} waits out the debounce`);
        }
    }
});

test('as_hook_state passes the vocabulary and names everything else unknown', () => {
    for (const state of ['busy', 'background', 'attention', 'free', 'unknown'] as const) {
        assert.equal(as_hook_state(state), state);
    }
    for (const raw of ['done', 'idle', 'shell', 'BUSY', '', null, undefined, 3, {}, 'constructor']) {
        assert.equal(as_hook_state(raw), 'unknown', `${String(raw)} is not a hook state`);
    }
});

test('background_jobs_label counts jobs, and says "background work" when it cannot', () => {
    assert.equal(background_jobs_label(1), '1 background job');
    assert.equal(background_jobs_label(3), '3 background jobs');
    assert.equal(background_jobs_label(null), 'background work');
    assert.equal(background_jobs_label(undefined), 'background work');
    assert.equal(background_jobs_label(0), 'background work');
    assert.equal(background_jobs_label(Number.NaN), 'background work');
});

test('background_jobs_label short form, for tight readouts', () => {
    assert.equal(background_jobs_label(1, 'short'), '1 bg job');
    assert.equal(background_jobs_label(4, 'short'), '4 bg jobs');
    assert.equal(background_jobs_label(null, 'short'), 'background');
    assert.equal(background_jobs_label(0, 'short'), 'background');
});

test('parse_session_row keeps fresh only as a boolean', () => {
    assert.equal(parse_session_row({ session: 'a', state: 'free', fresh: true })?.fresh, true);
    assert.equal(parse_session_row({ session: 'a', state: 'free', fresh: 'yes' })?.fresh, false);
    assert.equal(parse_session_row({ session: 'a', state: 'free' })?.fresh, undefined);
});

test('parse_session_rows validates state and background_jobs at the boundary', () => {
    const rows = parse_session_rows([
        { session: 'a', state: 'background', background_jobs: 2, age_seconds: 1 },
        { session: 'b', state: 'sleeping' },
        { session: 'c', state: 'background', background_jobs: -1 },
        { session: 'd', state: 'background', background_jobs: null },
        { session: 'e', state: 'busy' },
        { state: 'busy' },
        'not a row',
        null,
    ]);
    assert.deepEqual(rows.map((row) => [row.session, row.state, row.background_jobs]), [
        ['a', 'background', 2],
        ['b', 'unknown', undefined],
        ['c', 'background', null],
        ['d', 'background', null],
        ['e', 'busy', undefined],
    ]);
    assert.equal(rows[0].age_seconds, 1, 'everything else passes through');
    assert.deepEqual(parse_session_rows('nope'), []);
    assert.equal(parse_session_row({ session: 7 }), null);
});

test('format_uptime_short is minute-granular (indicator repaint throttle)', () => {
    assert.equal(format_uptime_short(45), 'up <1m');
    assert.equal(format_uptime_short(61), 'up 1m');
    assert.equal(format_uptime_short(3 * 60 + 20), 'up 3m');
    assert.equal(format_uptime_short(2 * 3600 + 14 * 60 + 59), 'up 2h 14m');
    assert.equal(format_uptime_short(26 * 3600), 'up 1d 2h');
    assert.equal(format_uptime_short(-5), 'up <1m'); // clock skew guard
});

test('launched_by_short maps every kind, unknown → headless', () => {
    assert.equal(launched_by_short('menubar'), 'menu bar');
    assert.equal(launched_by_short('iterm2'), 'iTerm2');
    assert.equal(launched_by_short('apple-terminal'), 'Terminal');
    assert.equal(launched_by_short('tmux'), 'tmux');
    assert.equal(launched_by_short('agent'), 'agent');
    assert.equal(launched_by_short('launchd'), 'headless');
    assert.equal(launched_by_short('something-new'), 'headless');
});

const bridge = (over: Partial<BridgeIdentity>): BridgeIdentity => ({
    pid: 12345,
    started_at: 1700000000.5,
    uptime_seconds: 2 * 3600 + 14 * 60,
    cwd: '/Users/u/code/my-app/bridge',
    tty: 'ttys003',
    mode: 'full',
    port: 7681,
    status_port: 7682,
    launched_by: { kind: 'iterm2', label: 'iTerm2 (tty ttys003)' },
    ttyd: { pid: 12346, owned: true },
    ...over,
});

test('format_bridge_sublabel composes pid · uptime · launcher', () => {
    assert.equal(format_bridge_sublabel(bridge({})), 'pid 12345 · up 2h 14m · iTerm2');
    assert.equal(
        format_bridge_sublabel(bridge({ launched_by: { kind: 'menubar', label: 'whatever' }, uptime_seconds: 30 })),
        'pid 12345 · up <1m · menu bar',
    );
});

test('classify_shutdown_status maps each failure to its own next step', () => {
    assert.equal(classify_shutdown_status(200), 'ok');
    assert.equal(classify_shutdown_status(404), 'unsupported'); // pre-shutdown bridge
    assert.equal(classify_shutdown_status(403), 'forbidden'); // origin denied
    assert.equal(classify_shutdown_status(500), 'error');
    assert.equal(classify_shutdown_status(0), 'error');
});

const usage = (over: Partial<SessionUsage> = {}): SessionUsage => ({
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    ...over,
});

test('format_model_name maps current model ids to friendly names', () => {
    assert.equal(format_model_name('claude-opus-4-8'), 'Opus 4.8');
    assert.equal(format_model_name('claude-opus-4-7'), 'Opus 4.7');
    assert.equal(format_model_name('claude-sonnet-5'), 'Sonnet 5');
    assert.equal(format_model_name('claude-sonnet-4-6'), 'Sonnet 4.6');
    assert.equal(format_model_name('claude-haiku-4-5'), 'Haiku 4.5');
    assert.equal(format_model_name('claude-fable-5'), 'Fable 5');
});

test('format_model_name strips [1m]/date suffixes and handles unknowns', () => {
    assert.equal(format_model_name('claude-opus-4-8[1m]'), 'Opus 4.8');
    assert.equal(format_model_name('claude-haiku-4-5-20251001'), 'Haiku 4.5');
    assert.equal(format_model_name(null), null);
    assert.equal(format_model_name(''), null);
    assert.equal(format_model_name('gpt-4o'), 'gpt-4o'); // unknown shape → raw id, not dropped
});

test('estimate_session_cost prices per family and folds cache tiers', () => {
    // 1M output tokens on Opus @ $25/1M → $25.00 exactly.
    assert.equal(estimate_session_cost('claude-opus-4-8', usage({ output_tokens: 1_000_000 })), 25);
    // 1M input on Sonnet @ $3/1M → $3.00.
    assert.equal(estimate_session_cost('claude-sonnet-5', usage({ input_tokens: 1_000_000 })), 3);
    // Cache reads on Opus are 0.1× input: 1M @ $5×0.1 → $0.50.
    assert.equal(estimate_session_cost('claude-opus-4-8', usage({ cache_read_input_tokens: 1_000_000 })), 0.5);
    // Cache writes on Opus are 1.25× input: 1M @ $5×1.25 → $6.25.
    assert.equal(estimate_session_cost('claude-opus-4-8', usage({ cache_creation_input_tokens: 1_000_000 })), 6.25);
    assert.equal(estimate_session_cost(null, usage()), null);
    assert.equal(estimate_session_cost('claude-opus-4-8', null), null);
    assert.equal(estimate_session_cost('gpt-4o', usage({ output_tokens: 100 })), null);
});

test('format_usd is compact with a sub-cent floor', () => {
    assert.equal(format_usd(1.234), '$1.23');
    assert.equal(format_usd(0.05), '$0.05');
    assert.equal(format_usd(0.004), '<$0.01');
    assert.equal(format_usd(0), '$0.00');
    assert.equal(format_usd(123.4), '$123');
});

test('format_token_count is compact', () => {
    assert.equal(format_token_count(950), '950');
    assert.equal(format_token_count(4200), '4.2k');
    assert.equal(format_token_count(22_000), '22k');
    assert.equal(format_token_count(1_700_000), '1.7M');
});

test('format_model_label assembles model · cost · tokens', () => {
    const u = usage({ output_tokens: 200_000 }); // 200k out @ $25/1M = $5.00 exactly
    assert.equal(format_model_label('claude-opus-4-8', u), 'Opus 4.8 · ~$5.00 · 200k out');
    assert.equal(format_model_label('claude-opus-4-8', u, { show_cost: false, show_tokens: true }), 'Opus 4.8 · 200k out');
    assert.equal(format_model_label('claude-opus-4-8', u, { show_cost: true, show_tokens: false }), 'Opus 4.8 · ~$5.00');
    assert.equal(format_model_label('claude-opus-4-8', null), 'Opus 4.8'); // no usage → model only
});

test('format_model_label falls back to UNKNOWN_MODEL_LABEL', () => {
    assert.equal(format_model_label(null, null), UNKNOWN_MODEL_LABEL);
    assert.equal(format_model_label(undefined, usage()), UNKNOWN_MODEL_LABEL);
    // Pinned deliberately: this string is user-visible on every rack row that
    // has no model, and it must not drift back into claiming a disconnection.
    // A plain shell pane gets a row too, so it must not claim a Claude either.
    assert.equal(UNKNOWN_MODEL_LABEL, 'model unknown');
});

test('parse_usage_limits accepts partial windows and rejects garbage', () => {
    const full = parse_usage_limits({
        five_hour: { used_percentage: 24.4, resets_at: 1790000000 },
        seven_day: { used_percentage: 41, resets_at: 1790400000 },
        age_seconds: 4.2,
    });
    assert.deepEqual(full, {
        age_seconds: 4.2,
        five_hour: { used_percentage: 24.4, resets_at: 1790000000 },
        seven_day: { used_percentage: 41, resets_at: 1790400000 },
    });
    const partial = parse_usage_limits({ five_hour: { used_percentage: 7 }, age_seconds: 0 });
    assert.deepEqual(partial, { age_seconds: 0, five_hour: { used_percentage: 7, resets_at: null } });
    assert.equal(parse_usage_limits(undefined), null);
    assert.equal(parse_usage_limits('nope'), null);
    assert.equal(parse_usage_limits({ age_seconds: 'old' }), null);
    // age but no valid window → treated as absent.
    assert.equal(parse_usage_limits({ age_seconds: 1 }), null);
    assert.equal(parse_usage_limits({ age_seconds: 1, five_hour: { used_percentage: 'lots' } }), null);
});

const ZERO = { busy: 0, background: 0, attention: 0, done: 0, free: 0, unknown: 0 };

test('count_by_display tallies every display state', () => {
    const rows = (['busy', 'busy', 'background', 'attention', 'done', 'free', 'unknown'] as const).map((display) => ({
        display,
    }));
    assert.deepEqual(count_by_display(rows), { busy: 2, background: 1, attention: 1, done: 1, free: 1, unknown: 1 });
    assert.deepEqual(count_by_display([]), ZERO);
});

test('count_by_display counts a word outside the vocabulary as unknown, never NaN', () => {
    const rows = [{ display: 'hibernating' }, { display: 'constructor' }] as unknown as { display: 'busy' }[];
    assert.deepEqual(count_by_display(rows), { ...ZERO, unknown: 2 });
});

test('format_rack_summary: needs you → busy → background → done → free, zeros dropped', () => {
    assert.equal(
        format_rack_summary({ busy: 2, background: 4, attention: 1, done: 1, free: 3, unknown: 0 }),
        '1 needs you · 2 busy · 4 background · 1 awaiting review · 3 free',
    );
    assert.equal(format_rack_summary({ ...ZERO, free: 2, unknown: 1 }), '2 free');
    assert.equal(format_rack_summary({ ...ZERO, unknown: 3 }), '3 unknown');
    assert.equal(format_rack_summary(ZERO), '');
});

test('rack_summary_segments carries the state each segment is dotted with', () => {
    assert.deepEqual(rack_summary_segments({ ...ZERO, background: 1, free: 2 }), [
        { state: 'background', text: '1 background' },
        { state: 'free', text: '2 free' },
    ]);
});

test('parse_transition_entry validates the ring-buffer row shape', () => {
    const entry = parse_transition_entry({
        ts: 1234.5,
        session: 'main',
        from_state: 'busy',
        to_state: 'free',
        event: 'Stop',
        activity: { zone: 'tests', tool: 'Bash', detail: 'bash: pytest' },
    });
    assert.deepEqual(entry, {
        ts: 1234.5,
        session: 'main',
        from_state: 'busy',
        to_state: 'free',
        event: 'Stop',
        activity: { zone: 'tests', tool: 'Bash', detail: 'bash: pytest' },
    });
    // Malformed activity degrades to null; malformed core fields reject the row.
    assert.equal(parse_transition_entry({ ts: 1, session: 's', from_state: 'a', to_state: 'b', activity: 'x' })?.activity, null);
    assert.equal(parse_transition_entry(null), null);
    assert.equal(parse_transition_entry({ ts: 'now', session: 's', from_state: 'a', to_state: 'b' }), null);
    assert.equal(parse_transition_entry({ ts: 1, session: 's', from_state: 'a' }), null);
});

test('format_transition_text renders time, change, and detail', () => {
    const entry: TransitionEntry = {
        ts: 1790000000,
        session: 'deploy',
        from_state: 'busy',
        to_state: 'free',
        event: 'Stop',
        activity: { zone: 'tests', tool: 'Bash', detail: 'bash: pytest' },
    };
    const text = format_transition_text(entry);
    // Hour depends on the local timezone — assert structure, not the clock.
    assert.match(text, /^\d{2}:\d{2}  deploy {2}busy→free · bash: pytest$/);
    const same_state = format_transition_text({ ...entry, from_state: 'busy', to_state: 'busy', activity: null });
    assert.match(same_state, /^\d{2}:\d{2}  deploy {2}busy$/);
});

// ── coalesced rack notifications ──────────────────────────────────────────

test('nothing outstanding renders no notice — the caller\'s cue to retract', () => {
    assert.equal(summarize_rack_attention([]), null);
});

test('a lone blocked agent is NAMED and keeps its message verbatim', () => {
    const notice = summarize_rack_attention([{ session: 'api', message: 'allow Bash?' }]);
    assert.ok(notice);
    assert.equal(notice.title, 'tldrack — agent needs input');
    assert.equal(notice.body, 'api — allow Bash?');
});

test('a lone blocked agent with no message still says what it wants', () => {
    const notice = summarize_rack_attention([{ session: 'api', message: null }]);
    assert.equal(notice?.body, 'api needs input');
});

test('several blocked agents COALESCE into one counted summary, not N banners', () => {
    const notice = summarize_rack_attention([
        { session: 'api', message: 'allow Bash?' },
        { session: 'web', message: 'allow Write?' },
        { session: 'infra', message: null },
    ]);
    assert.ok(notice);
    assert.equal(notice.title, '3 agents need you');
    // Names only — three messages would neither fit nor be read.
    assert.equal(notice.body, 'api, web, infra');
});

test('every attention notice shares ONE tag so renders replace instead of stack', () => {
    const one = summarize_rack_attention([{ session: 'api', message: null }]);
    const many = summarize_rack_attention([
        { session: 'api', message: null },
        { session: 'web', message: null },
    ]);
    assert.equal(one?.tag, ATTENTION_NOTICE_TAG);
    assert.equal(many?.tag, ATTENTION_NOTICE_TAG);
});

test('the key ignores ordering — a reshuffled set must not re-alert', () => {
    const a = summarize_rack_attention([
        { session: 'web', message: null },
        { session: 'api', message: null },
    ]);
    const b = summarize_rack_attention([
        { session: 'api', message: 'now with a message' },
        { session: 'web', message: null },
    ]);
    assert.equal(a?.key, b?.key);
});

test('the key CHANGES when a new agent joins the outstanding set', () => {
    const before = summarize_rack_attention([{ session: 'api', message: null }]);
    const after = summarize_rack_attention([
        { session: 'api', message: null },
        { session: 'web', message: null },
    ]);
    assert.notEqual(before?.key, after?.key);
});

// ── rename ──────────────────────────────────────────────────────────────────
// POST /rename-session has two failures a user hits by accident and can act on,
// so they must not collapse into a generic "rename failed".

test('classify_rename_status separates taken and gone from the shared vocabulary', () => {
    assert.equal(classify_rename_status(200), 'ok');
    assert.equal(classify_rename_status(409), 'taken');
    assert.equal(classify_rename_status(410), 'gone');
});

test('classify_rename_status keeps 404 meaning "this bridge has no such route"', () => {
    // The bridge answers 410, NOT 404, for a session that vanished — precisely
    // so 404 keeps its one meaning across every route and maps to the
    // restart-your-bridge advice.
    assert.equal(classify_rename_status(404), 'unsupported');
    assert.equal(classify_rename_status(403), 'forbidden');
    assert.equal(classify_rename_status(500), 'error');
});

test('every rename failure names the session and suggests a next step', () => {
    const results = ['taken', 'gone', 'unsupported', 'forbidden', 'unreachable', 'error'] as const;
    for (const result of results) {
        const message = rename_failure_message('vc-1', 'backend', result);
        assert.ok(message.length > 0, `${result} produced no message`);
        // 'taken' is the one case that is about the NEW name, not the old one.
        const subject = result === 'taken' ? 'backend' : 'vc-1';
        assert.ok(message.includes(subject), `${result} message omits '${subject}': ${message}`);
    }
});

test('a taken name tells the user to pick another, not that something broke', () => {
    const message = rename_failure_message('vc-1', 'backend', 'taken');
    assert.ok(message.includes('already exists'));
    assert.ok(!message.toLowerCase().includes('failed'));
});

// ── launch profiles ─────────────────────────────────────────────────────────

test('parse_plugin_names reads the names off the plugins object, and null is "this bridge did not say"', () => {
    assert.deepEqual(parse_plugin_names({ worktree: { routes: [] }, devices: {}, 'BAD NAME': {} }), ['devices', 'worktree']);
    assert.deepEqual(parse_plugin_names({}), []);
    for (const absent of [undefined, null, 'devices', ['devices']]) assert.equal(parse_plugin_names(absent), null);
});

test('a profile carries its account standing: OUT when the bridge says so, null for anything else', () => {
    const out = { state: 'out', error: 'billing_error', message: ' Your plan has ended ', since: 1795118400 };
    const [lapsed, fine, older] = parse_profiles([
        { id: 'a', kind: 'claude', standing: out },
        { id: 'b', kind: 'claude', standing: null },
        { id: 'c', kind: 'claude' },
    ])!;
    assert.deepEqual(lapsed.standing, { state: 'out', error: 'billing_error', message: 'Your plan has ended', since: 1795118400 });
    assert.equal(fine.standing, null);
    assert.equal(older.standing, null, 'a bridge that does not say reads as fine');
    for (const not_out of [{ state: 'ok' }, 'out', [], 7]) assert.equal(parse_profile_standing(not_out), null);
    assert.deepEqual(parse_profile_standing({ state: 'out', error: 'Bad Token!', since: -1 }),
        { state: 'out', error: 'unknown', message: null, since: null });
    assert.equal(standing_reason(lapsed.standing!), 'plan ended');
    assert.equal(standing_reason({ ...lapsed.standing!, error: 'authentication_failed' }), 'signed out');
    assert.equal(standing_reason({ ...lapsed.standing!, error: 'unknown' }), 'account out');
});

test('parse_profiles is null when the key is absent — an older bridge, not an empty one', () => {
    assert.equal(parse_profiles(undefined), null);
    assert.equal(parse_profiles({ id: 'x' }), null);
    assert.deepEqual(parse_profiles([]), []);
});

test('parse_profiles is TOTAL: malformed entries are dropped, never coerced', () => {
    const parsed = parse_profiles([
        { id: 'claude-a', label: 'A', kind: 'claude', default: true, hooks_ok: false, cli_found: true,
          usage: { five_hour: { used_percentage: 42 }, age_seconds: 3 } },
        { id: 'codex-a', label: 'CODEX A', kind: 'codex', default: true, hooks_ok: true, model_label: 'gpt-6-codex' },
        { id: 'claude-a', label: 'DUP', kind: 'claude' }, // duplicate id
        { id: 'Has Space', label: 'x', kind: 'claude' }, // bad id
        { id: 'pi', label: 'PI', kind: 'pi' }, // unknown kind
        null,
        'shell',
    ]);
    assert.ok(parsed);
    assert.deepEqual(parsed.map((p) => p.id), ['claude-a', 'codex-a']);
    const [a, codex] = parsed;
    assert.equal(a.default, true);
    assert.equal(codex.default, false, 'at most one default survives');
    assert.equal(a.hooks_ok, false);
    assert.equal(a.usage?.five_hour?.used_percentage, 42);
    assert.equal(codex.model_label, 'gpt-6-codex');
    assert.equal(codex.cli_found, true, 'absent cli_found counts as found');
    assert.equal(codex.usage, null);
});

test('parse_profiles clips a long label and derives one from the id when missing', () => {
    const parsed = parse_profiles([
        { id: 'a', label: 'A-VERY-LONG-LABEL', kind: 'shell' },
        { id: 'sh', kind: 'shell', hooks_ok: 'yes' },
    ]);
    assert.ok(parsed);
    assert.equal(parsed[0].label.length, PROFILE_LABEL_MAX);
    assert.equal(parsed[1].label, 'SH');
    assert.equal(parsed[1].hooks_ok, null);
});

test('parse_profile_id mirrors the bridge token rule', () => {
    assert.equal(parse_profile_id('claude-a'), 'claude-a');
    for (const bad of ['', 'UPPER', '-lead', 'a b', 'a:b', 'x'.repeat(33), 3, null]) {
        assert.equal(parse_profile_id(bad), null, String(bad));
    }
});

test('resolve_launch_profile: the pick, else the default, else the first launchable', () => {
    const profiles = parse_profiles([
        { id: 'a', label: 'A', kind: 'claude', default: true },
        { id: 'codex-a', label: 'CODEX A', kind: 'codex', cli_found: false },
        { id: 'sh', label: 'SH', kind: 'shell' },
    ])!;
    assert.equal(resolve_launch_profile(profiles, 'sh'), 'sh');
    assert.equal(resolve_launch_profile(profiles, 'gone'), 'a');
    assert.equal(resolve_launch_profile(profiles, 'codex-a'), 'a', 'a pick whose CLI is missing is not launchable');
    assert.equal(resolve_launch_profile(profiles, null), 'a');
    assert.equal(resolve_launch_profile(profiles.slice(1), null), 'sh');
    assert.equal(resolve_launch_profile([], 'a'), null);
});

test('resolve_launch_profile: AUTO is the default where the bridge can pick, and a manual pick still wins', () => {
    const profiles = parse_profiles([
        { id: 'a', label: 'A', kind: 'claude', default: true },
        { id: 'third', label: 'THIRD', kind: 'claude' },
    ])!;
    const pick = parse_launch_pick({ id: 'third', label: 'THIRD', reason: 'THIRD · 90% left · idle', spent: false });
    assert.equal(resolve_launch_profile(profiles, null, pick), AUTO_PROFILE_ID);
    assert.equal(resolve_launch_profile(profiles, AUTO_PROFILE_ID, pick), AUTO_PROFILE_ID);
    assert.equal(resolve_launch_profile(profiles, 'gone', pick), AUTO_PROFILE_ID);
    assert.equal(resolve_launch_profile(profiles, 'a', pick), 'a');
    // No pick (an older bridge), or nothing it can launch: AUTO is not offered.
    assert.equal(resolve_launch_profile(profiles, AUTO_PROFILE_ID, null), 'a');
    const nothing = parse_launch_pick({ id: null, label: null, reason: 'no agent CLI found', spent: false });
    assert.equal(resolve_launch_profile(profiles, AUTO_PROFILE_ID, nothing), 'a');
});

test('parse_launch_pick keeps the display fields and refuses a malformed id', () => {
    assert.deepEqual(
        parse_launch_pick({ id: 'codex-a', label: 'CODEX A', reason: 'CODEX A · 53% left · idle', spent: true, ranking: [] }),
        { id: 'codex-a', label: 'CODEX A', reason: 'CODEX A · 53% left · idle', spent: true },
    );
    assert.deepEqual(parse_launch_pick({ id: null }), { id: null, label: null, reason: '', spent: false });
    for (const bad of [undefined, null, [], 'codex-a', { id: 'Bad Id' }, { id: 7 }]) {
        assert.equal(parse_launch_pick(bad), null, JSON.stringify(bad));
    }
});

test('parse_stop_failure keeps the bridge tokens and drops anything else', () => {
    const wire = { error: 'rate_limit', limit: 'five_hour', resets_at: 1700003600, message: 'limit · resets 3:00pm' };
    assert.deepEqual(parse_stop_failure(wire), wire);
    assert.equal(parse_stop_failure({ ...wire, error: 'Rate Limit' }), null);
    assert.equal(parse_stop_failure(null), null);
    assert.deepEqual(parse_stop_failure({ error: 'overloaded', limit: 7, resets_at: -1, message: '' }),
        { error: 'overloaded', limit: null, resets_at: null, message: null });
    const row = parse_session_row({ session: 'vc-1', state: 'free', stop_failure: wire });
    assert.deepEqual(row?.stop_failure, wire);
    assert.equal(parse_session_row({ session: 'vc-1', state: 'free', stop_failure: 'cut' })?.stop_failure, null);
});


test('first-prompt label is validated without changing the session key', () => {
    const row = parse_session_row({ session: 'vc-1', auto_name: 'Fix rack resizing' });
    assert.equal(row?.session, 'vc-1');
    assert.equal(row?.auto_name, 'Fix rack resizing');
    for (const auto_name of [12, {}, 'x'.repeat(57), 'bad\nname', '']) {
        assert.equal(parse_session_row({ session: 'vc-1', auto_name })?.auto_name, undefined);
    }
});
