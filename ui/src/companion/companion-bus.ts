/**
 * The messages a rack page and a host application's main window exchange on the window bus
 * (`composables/useWindowBus.ts`), when the page runs as a satellite (`AgentRackWindow.vue`,
 * `feed: 'bus'`).
 *
 * Import-free, and the strings are a contract: the host's main window spells the same ones. A
 * host with other satellites puts more messages on the same channel; this file lists only the
 * ones a rack page sends or reads.
 */

/** What a satellite says it is in HELLO. A host may know more modes; the rack is this one. */
export type SatelliteMode = 'agent-rack';

export const COMPANION_BUS = {
    /** satellite → main: "I'm here" (payload: `{ mode }`). */
    HELLO: 'companion:hello',
    /** main → satellites: a main window (possibly reloaded) is ready; satellites say HELLO again. */
    MAIN_READY: 'companion:main-ready',
    /** main → satellites: the periodic heartbeat. Its absence lets a satellite close itself. */
    MAIN_ALIVE: 'companion:main-alive',
    /**
     * satellite → main: "are you actually gone?", sent once the heartbeat has been silent and
     * answered with an immediate MAIN_ALIVE.
     *
     * This is what makes the silence test honest. The beat is a `setInterval`, and a hidden
     * page's timers are throttled to as little as once a minute, which is the ordinary state of
     * a main window while the operator works in a satellite on top of it. Delivery of THIS
     * message is event dispatch, not a timer, so a throttled main answers at once.
     * `companion/lifecycle-logic.ts` owns the decision.
     */
    MAIN_PING: 'companion:main-ping',
    /** main → satellites: the main is going away (payload: `{ reason: 'logout' | 'unload' }`). */
    MAIN_SHUTDOWN: 'companion:main-shutdown',

    /** main → rack: the whole `RackPayload`, with `attached`. The main window is the only poller. */
    RACK_DATA: 'companion:rack-data',
    /** rack → main: `{ action, session?, value? }`, in the vocabulary of `RackAction`
     *  (`agent-rack/rack-transport.ts`). `new` carries no session: only the host knows which
     *  names are taken, dead rows included. */
    RACK_ACTION: 'companion:rack-action',
    /** rack → main: a 3 s heartbeat (payload: `{ visible }`). This, and not a Window reference,
     *  is how the host knows a rack window is open, so the state survives a reload of the main. */
    RACK_ALIVE: 'companion:rack-alive',
    /** rack → main: pagehide or beforeunload, the fast path for a window closed by hand. The
     *  RACK_ALIVE timeout is the backstop for a force-quit, where no event fires. */
    RACK_CLOSING: 'companion:rack-closing',
    /**
     * main → satellites: "beat now", the mirror of MAIN_PING, sent when a satellite's RACK_ALIVE
     * has gone quiet. Answered with HELLO and RACK_ALIVE. The satellite's beat is a
     * `setInterval` too, so a window parked BEHIND the main one looks closed. A host that
     * concluded so would open its own terminal pane again: two ttyd connections on one session.
     */
    SATELLITE_PING: 'companion:satellite-ping',
    /** main → rack: close yourself. A message and not `win.close()`: after a reload the main no
     *  longer holds the Window reference, and a script-opened window may always close itself. */
    RACK_CLOSE: 'companion:rack-close',
} as const;
