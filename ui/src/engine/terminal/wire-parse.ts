/**
 * The two tolerant-parse primitives every tldrack envelope is read
 * with: "is this an object" and "is this a non-empty string".
 *
 * They are here and not private to each parser because a plugin ships
 * independently of the pages, so every parser in this directory has to answer
 * the same two questions the same way. A copy that treated `[]` as a record, or
 * `''` as a name, would degrade one panel differently from its neighbour for
 * the same wire body.
 *
 * The NUMBER coercions stay per module on purpose: one parser floors and clamps
 * a count to ≥ 0, another trusts any finite number, a third carries an
 * epoch/ISO union. Those are different questions wearing one name.
 *
 * Dependency-free like its callers, so `node --test` runs them directly.
 */

/** A plain object, or null for anything else — an array included. */
export function as_record(value: unknown): Record<string, unknown> | null {
    return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

/** A non-empty string, or null. An empty string is treated as absent. */
export function as_string(value: unknown): string | null {
    return typeof value === 'string' && value.length > 0 ? value : null;
}
