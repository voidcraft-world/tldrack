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
 * No DOM, so it runs under `node --test`.
 */
import { is_service_session } from './service-sessions.ts';

/** A tmux session name: one or more letters, digits, `-` or `_`. */
export const SESSION_NAME_RE = /^[A-Za-z0-9_-]+$/;

/** The warning shown when a name fails {@link SESSION_NAME_RE}. */
export const SESSION_NAME_WARNING = 'Session names must match [A-Za-z0-9_-]+';

/** True when `name` is a valid tmux session name. */
export function is_valid_session_name(name: string): boolean {
    return SESSION_NAME_RE.test(name);
}

/** The warning shown when a name would file an agent among the services. */
export const RESERVED_NAME_WARNING = 'That prefix is kept for services and jobs: the row would move to SERVICES';

/**
 * Why the operator may not give a session this name, or null when they may. A machinery prefix
 * is refused because the rack files a session by its name, and a service row offers no rename
 * back. The bridge refuses its own plugins' prefixes again (`bridge/tldrack/service_names.py`
 * owns why); a host's extra prefixes are known only here, so this check is the one that covers
 * them. `current` is the session being renamed: one that already carries such a name may keep
 * its prefix, since refusing would change nothing about where it is filed.
 */
export function session_name_error(name: string, current?: string): string | null {
    if (!is_valid_session_name(name)) return SESSION_NAME_WARNING;
    if (is_service_session(name) && !(current !== undefined && is_service_session(current))) return RESERVED_NAME_WARNING;
    return null;
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
