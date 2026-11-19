/**
 * Tunables of the rack: one live terminal plus a list of every session.
 *
 * WHY ONE PANE. Every live pane is a frame with its own renderer and its own socket, so N live
 * panes are N of each. The rack pays for ONE pane; every other session is a row.
 *
 * A plain object, so a host or a console can change a value before the rack reads it. A host
 * that draws the rack somewhere else (as an object in a scene, in a window it opens) keeps the
 * flags of that surface in its own code.
 */
export const MULTI_TERMINAL_FLAGS = {
    /** Sessions a surface that CANNOT scroll lists before collapsing the rest into `+N MORE`.
     *  Overflow is always explicit; the count is never silently truncated. */
    max_sessions: 8,
    /** What a payload owner sends as `RackPayload.max_sessions`. The DOM rack no longer reads it:
     *  its list scrolls, so it lists every session (`AgentRackComponent.vue` `rows`). A cap there
     *  hid the agents past it behind a `+N MORE` row with nothing to click. Kept for a host that
     *  draws the payload on a surface with a longer budget than `max_sessions`. */
    scroll_max_sessions: 32,
    /** Idle cadence of the rack's worktree-plugin `/state` poll, ms. A create in flight drops it
     *  to `CREATE_POLL_INTERVAL_MS` (`agent-rack/worktree-client.ts`). */
    worktree_poll_ms: 5000,
    /** After the live pane's frame fires `load`, how long its row's name box stays locked: xterm
     *  grabs focus on connect, just after `load`. Why: `agent-rack/pane-settle.ts`. */
    pane_settle_grace_ms: 800,
    /** The longest a row stays locked waiting for that `load`, ms. A missed event must never
     *  strand a name box. */
    pane_settle_cap_ms: 8000,
    /** The list's share of the page's width until the operator drags the split
     *  (`AgentRackWindow.vue`). */
    pop_out_rack_ratio: 0.28,
};
