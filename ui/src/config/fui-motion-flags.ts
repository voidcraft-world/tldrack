/**
 * The FUI motion vocabulary: the timings of the DOM primitives, published as CSS
 * custom properties on `<html>` and consumed by `styles/fui-panel.css`.
 *
 * The flags are a plain object. A page or a host sets a field before mount (or
 * later, through `FUI_MOTION_FLAGS`) and the stylesheet follows. Nothing is stored.
 *
 * The look is **discrete motion, not more motion**. A smooth opacity ramp reads as
 * light dimming, which is an organic gesture, and a system does not dim, it
 * switches. So every ramp here is quantized into a handful of held states: cut,
 * hold, cut, hold. Tuning the *amount* of animation is the wrong axis; continuous
 * against discrete is the axis.
 *
 *   ENGAGE  a surface/marker ARRIVES     3 held states
 *   DROP    a surface/marker LEAVES      2 states, ~half of ENGAGE
 *   LATCH   a readout's VALUE CHANGED    snap on, step off
 *   RELAY   a CHANNEL switched (accent)  one held 50% mix, then land
 *   FIRE    a control TOOK INPUT         LATCH's control-side twin
 *   WORK    a control is STILL RUNNING   3-position scanner, loops till done
 *
 * The first five are TRANSITIONS and each is over inside 140ms. WORK is the one
 * sustained state, and the one exception to "nothing idles": the loop IS the
 * state. It also keeps running under `enabled: false`, because a frozen progress
 * indicator communicates nothing (WCAG 2.2.2). See `styles/fui-panel.css` § WORK.
 *
 * WHEN FLAGS ARE READ:
 *   - The `*_ms` fields become CSS custom properties, which are read when each
 *     animation or transition is CREATED. An edit therefore lands on the next
 *     ENGAGE, not on one already running.
 *   - `enabled` / `force_reduced_motion` re-publish `.fui-motion-off` on write,
 *     so those apply live.
 *
 * NOT tunable, on purpose: the step COUNTS (3 for ENGAGE, 2 for DROP/RELAY).
 * Those are design decisions, and `steps(var(--n))` is not reliably supported
 * anyway.
 */
const FLAGS = {
    /**
     * Master switch for the whole vocabulary. false = hard cuts and no bracket
     * draw. A host with a motion preference of its own writes it here.
     */
    enabled: true,
    /** Honor the OS `prefers-reduced-motion` query. false = ignore it (testing only). */
    respect_reduced_motion: true,

    // ── DOM timings (published as CSS custom properties) ─────────────────────
    /** ENGAGE draw time for one `.fui-corner` L-stroke. 0 = brackets appear fully formed. */
    corner_draw_ms: 150,
    /**
     * Clockwise TL → TR → BR → BL offset between the four corners. 0 = all four
     * engage together, which reads as one object appearing rather than a frame
     * assembling — noticeably less mechanical, and the first thing to A/B.
     */
    corner_stagger_ms: 30,
    /** ENGAGE (`.fui-engage` enter). 0 = surfaces hard-pop in. */
    engage_in_ms: 120,
    /**
     * DROP (`.fui-engage` leave). Kept at roughly HALF of `engage_in_ms` — the
     * asymmetry is the point: a system steps through an acquire when putting a
     * readout up, and just drops it when taking one down.
     */
    engage_out_ms: 90,
    /** RELAY: the held mid-mix when `--fui-accent` switches. 0 = the accent snaps. */
    relay_ms: 140,
    /** LATCH: flash-to-rest decay when a readout's value changes. 0 = values change silently. */
    latch_ms: 120,
    /**
     * FIRE: flash-to-rest when a control takes input, by click OR by keyboard
     * accelerator. 0 = a control never acknowledges a press.
     *
     * Kept SHORT on the frequency law: a press happens more often than anything
     * else in the vocabulary, so the budget is smaller than ENGAGE's, not larger.
     */
    fire_ms: 130,
    /**
     * FIRE's third channel: how far a bracketed button's two corner brackets kick
     * OUTWARD on the leading step before snapping home.
     *
     * 0 = flash + squash only, brackets hold still. It is the one part of FIRE
     * that is decoration and not feedback.
     *
     * Published WITH a `px` unit even at 0, for the same reason
     * `--fui-corner-draw-stagger-ms` carries `ms` at 0: it is consumed inside
     * `translate()`, and a bare `0` is a `<number>`, not a `<length>`, which
     * invalidates the whole declaration instead of zeroing it.
     */
    fire_kick_px: 3,
    /**
     * WORK: one full sweep of the running-scanner bar, in ms.
     *
     * The ONLY sustained verb, so it is tuned on the opposite law to FIRE. FIRE
     * is short because it fires constantly; WORK is slow because it is read, not
     * glimpsed. An operator watching a 10-second `git worktree remove` needs the
     * bar to look deliberate, and three held positions across 780ms is roughly a
     * heartbeat per step. Much faster reads as an error.
     *
     * NOT gated by `enabled`. Every other timing here goes inert when motion is
     * turned off; this one must not, because the frozen result is a static bar
     * that says "stuck", not "working".
     */
    work_ms: 780,

    /** Force the reduced-motion path without touching OS settings. false = respect OS + preference. */
    force_reduced_motion: false,
};

type FuiMotionFlags = typeof FLAGS;

// ── Reduced motion: the one shared source of truth ───────────────────────────
//
// One `matchMedia` query with a LISTENER. A site that reads the query once at
// module load never sees the OS setting change mid-session; the listener below
// is what makes that work.

const _mql: MediaQueryList | null =
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
        ? window.matchMedia('(prefers-reduced-motion: reduce)')
        : null;

let _os_reduced = _mql?.matches ?? false;

_mql?.addEventListener('change', (e) => {
    _os_reduced = e.matches;
    apply_fui_motion_flags();
});

/**
 * Should any FUI verb play? The OR of the opt-outs: the OS setting, `enabled`,
 * and the forced override.
 *
 * This is the single gate: the CSS reads it indirectly through the
 * `.fui-motion-off` class, and a host that animates with the same vocabulary
 * calls it directly.
 */
export function should_animate(): boolean {
    if (!FLAGS.enabled) return false;
    if (FLAGS.force_reduced_motion) return false;
    if (FLAGS.respect_reduced_motion && _os_reduced) return false;
    return true;
}

/** The OS query alone, for callers that must distinguish it from the preference. */
export function os_prefers_reduced_motion(): boolean {
    return _os_reduced;
}

/**
 * Push the current flags onto `<html>` as CSS custom properties, and toggle the
 * `.fui-motion-off` kill class.
 *
 * Durations are published WITH a `ms` unit even at 0. `--fui-corner-draw-stagger-ms`
 * is multiplied inside a `calc()` in the stylesheet, and `calc(0 * 3)` is a
 * `<number>`, not a `<time>` — which invalidates the whole `animation-delay`
 * declaration and silently un-staggers the brackets instead of un-delaying them.
 */
export function apply_fui_motion_flags(): void {
    if (typeof document === 'undefined') return;
    const root = document.documentElement;
    const s = root.style;
    s.setProperty('--fui-corner-draw-ms', `${FLAGS.corner_draw_ms}ms`);
    s.setProperty('--fui-corner-draw-stagger-ms', `${FLAGS.corner_stagger_ms}ms`);
    s.setProperty('--fui-engage-in-ms', `${FLAGS.engage_in_ms}ms`);
    s.setProperty('--fui-engage-out-ms', `${FLAGS.engage_out_ms}ms`);
    s.setProperty('--fui-relay-ms', `${FLAGS.relay_ms}ms`);
    s.setProperty('--fui-latch-ms', `${FLAGS.latch_ms}ms`);
    s.setProperty('--fui-fire-ms', `${FLAGS.fire_ms}ms`);
    // NOT gated by `enabled`: see the field note. With motion turned off the
    // progress bar still moves, and only that.
    s.setProperty('--fui-work-ms', `${FLAGS.work_ms}ms`);
    // `px` even at 0 — see the note on `fire_kick_px`. This one is consumed by
    // `translate()` rather than `calc()`, but the failure is identical: a bare
    // `0` is a `<number>`, the declaration is invalid, and the brackets keep
    // whatever the fallback was instead of holding still.
    s.setProperty('--fui-fire-kick', `${FLAGS.fire_kick_px}px`);
    root.classList.toggle('fui-motion-off', !should_animate());
}

/**
 * The exported handle. Writing a field re-publishes the CSS vars, so
 * `FUI_MOTION_FLAGS.corner_draw_ms = 600` lands with no reload, and flipping
 * `enabled` / `force_reduced_motion` immediately adds or removes the
 * `.fui-motion-off` class.
 */
export const FUI_MOTION_FLAGS = new Proxy(FLAGS, {
    set(target, key, value): boolean {
        (target as Record<string | symbol, unknown>)[key] = value;
        apply_fui_motion_flags();
        return true;
    },
}) as FuiMotionFlags;

// Publish once at import, BEFORE Vue mounts anything. That ordering is what
// makes the `.fui-motion-off` class sufficient on its own: every verb in
// fui-panel.css is mount-triggered, so no `.fui-corner` can exist before the
// class has been decided, and the stylesheet needs no duplicate `@media` block.
apply_fui_motion_flags();
