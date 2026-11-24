/**
 * Which tmux sessions are machinery, by name prefix. `engine/terminal/service-sessions.ts` owns
 * what the rack does with them; this file owns the list, because the list is the host's.
 *
 * A prefix is a contract with whatever names the session: a plugin finds its session again by
 * that name. The defaults are the names this repository's own plugins use. A host that runs more
 * machinery in tmux (a dev server, a deploy pane, a nightly job) adds its prefixes here before
 * the rack mounts:
 *
 *     SERVICE_SESSION_FLAGS.job_prefixes.push('deploy-', 'nightly-');
 *
 * An agent session never takes one of these names: the rename box refuses every prefix listed
 * here (`engine/terminal/session-name.ts`), and the bridge refuses the two defaults below at its
 * own doors (`bridge/tldrack/service_names.py`, whose test holds the two default lists equal).
 *
 * Import-free, like every module the layout loads under bare `node --test`.
 */
export const SERVICE_SESSION_FLAGS = {
    /** Long-lived services a supervisor plugin keeps up: `svc-<key>` (the `dev-services` wire). */
    service_prefixes: ['svc-'],
    /** One-shot jobs a plugin starts in a pane the operator can watch: `laya-<verb>` (`plugins/laya`). */
    job_prefixes: ['laya-'],
};
