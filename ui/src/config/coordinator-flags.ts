/**
 * Tunables for a surface that shows the coordinator plugin's findings: which parked agent
 * sessions are still waiting on the operator (`plugins/coordinator/` owns the audit).
 *
 * No page of this app draws that surface yet; `engine/terminal/coordinator-logic.ts` parses the
 * plugin's wire and a host application reads both. A surface is gated on the plugin answering,
 * not on a flag: a bridge without the plugin grows no coordinator UI, so there is nothing here
 * to switch off.
 */
export const COORDINATOR_FLAGS = {
    /** The bridge port the plugin mounts on (`config/terminal-status-flags.ts` `DEFAULT_STATUS_PORT`). */
    status_port: 7682,
    /** Plugin route prefix on the status port — /state, /audit, /dismiss, /restore. */
    route_base: '/plugin/coordinator',

    /**
     * Rows the panel shows after ranking (`rank_findings`). Deliberately small:
     * the point is "the next thing to walk back to", not an inbox, and a scan of a
     * busy machine finds far more than a narrow rail can list. Overflow is
     * reported as a `+N MORE` count, never dropped silently. 0 or less = no cap.
     */
    max_findings: 5,

    /**
     * Show severity-4 findings — the reflexive hand-offs (`/code-review`, "verify
     * visually", "before you deploy"). Off by default because they are the bulk of
     * the corpus and almost none of them are real work; the panel offers a toggle,
     * and this is only its starting position.
     */
    show_routine: false,

    /**
     * Kick an audit automatically when the surface first opens. Off by default:
     * an audit walks every transcript on disk, so it stays a button press.
     */
    auto_audit_on_enter: false,

    /**
     * How long a row's RESUME button stays armed (amber `CONFIRM?`) before it
     * disarms itself. RESUME spawns a real agent, and one stray click in a long
     * list must not start one, so it is a two-step arm, never a `window.confirm`.
     */
    resume_arm_timeout_ms: 4000,

    /** Log the parsed view on each fetch / action. */
    debug_log: false,
};
