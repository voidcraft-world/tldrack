/**
 * `JSON.parse` for a value that came out of storage, narrowed to a plain object.
 *
 * ONE OWNER for the prologue every stored-JSON reader needs: the agent rack's
 * `parse_rect` (rack-window-geometry.ts) and `parse_stored_config`
 * (rack-remote.ts). Absent, unparseable and non-object all fail to `null` HERE,
 * because in every caller a corrupt value that escapes is worse than no value at
 * all: a NaN in a window-features string, an origin that was never validated.
 *
 * Arrays pass the object test and then fail on their (missing) properties in the
 * caller.
 *
 * No imports and no enums: its callers are run directly by `node --test` in
 * strip-only mode.
 */
export function parse_json_object(raw: string | null | undefined): Record<string, unknown> | null {
    if (!raw) return null;
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        return null;
    }
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed as Record<string, unknown>;
}
