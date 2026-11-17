/**
 * The one rule for a valid tmux session name, shared by every surface that lets
 * the operator name a terminal (the rack's rename, `+ NEW`).
 *
 * Mirrors the bridge's canonical guard (bridge/tldrack/snapshot.py
 * `SESSION_NAME_RE`). Each trust boundary re-validates on purpose: this is the
 * page's copy, not a cross-language single source. Within the pages it must
 * live in exactly one place, so the regex and the message shown can never
 * drift between call sites.
 *
 * Dependency-free so it runs under `node --test`.
 */

/** A tmux session name: one or more letters, digits, `-` or `_`. */
export const SESSION_NAME_RE = /^[A-Za-z0-9_-]+$/;

/** The warning shown when a name fails {@link SESSION_NAME_RE}. */
export const SESSION_NAME_WARNING = 'Session names must match [A-Za-z0-9_-]+';

/** True when `name` is a valid tmux session name. */
export function is_valid_session_name(name: string): boolean {
    return SESSION_NAME_RE.test(name);
}

/**
 * The prefix that makes a ttyd `?arg=` ATTACH-ONLY instead of attach-or-create.
 *
 * `ttyd -a` hands `?arg=` to `sh -c` as `$1`, and the bridge's script runs
 * `tmux new -A` for a bare name — attach-or-CREATE. So a ttyd URL is not a
 * *view* of a session, it is a *constructor* for one: navigating to
 * `?arg=<name>` is what makes it exist. That was deliberate (the rack's `+ NEW`
 * had no other mechanism; a bridge without `POST /spawn` still has none) but it
 * means anything that SAVES or REPLAYS such a URL resurrects a session the
 * operator killed: a surface that persists `?arg=<name>` re-creates that
 * session on every load, however often it is killed.
 *
 * Unambiguous because {@link SESSION_NAME_RE} excludes `:` — and tmux rejects
 * it in a session name anyway. The bridge half is `ttyd_session_script()`.
 */
export const ATTACH_ONLY_PREFIX = 'attach:';

/** The `?arg=` value of a surface that must attach, never create. */
export function attach_only_arg(session: string): string {
    return `${ATTACH_ONLY_PREFIX}${session}`;
}
