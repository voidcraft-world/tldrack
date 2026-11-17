/**
 * The write-or-`--check` half of every `export-*.ts` that generates a JSON twin
 * of a `schemas/src/*.ts` source of truth.
 *
 * ONE owner for the contract every check reads (the schemas tests, a plugin's
 * twin test): `--check` exits 1 on a missing or stale output and prints the
 * command that fixes it, a plain run rewrites it. One copy of that block, so the
 * exit codes and the wording cannot drift between generators.
 *
 * Each generator keeps what is actually its own: the payload, the header prose
 * saying why ITS staleness bites, and the one-line summary of what it wrote.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/** This file lives in `schemas/scripts/`, so the repo root is two levels up. */
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

export interface GeneratedJsonSpec {
    /** Bracketed prefix on every line this prints, e.g. `taxonomy`. */
    label: string;
    /** Repo-relative path of the generated JSON. */
    out: string;
    /** Repo-relative `schemas/src/*.ts` the payload is derived from. */
    source: string;
    /** Repo-relative path of the calling generator, for the `Run:` hint. */
    script: string;
    /** Everything after the `_generated` / `_source` header keys, in key order. */
    payload: Record<string, unknown>;
    /** Parenthesised detail on the `wrote` line; evaluated only when writing. */
    summary: () => string;
}

/**
 * Serializes `spec.payload` behind the standard `_generated` / `_source` header
 * and hands it to `emit_generated_text`.
 */
export function emit_generated_json(spec: GeneratedJsonSpec): void {
    const serialized =
        JSON.stringify(
            {
                _generated: `DO NOT EDIT — regenerate via ${spec.script}`,
                _source: spec.source,
                ...spec.payload,
            },
            null,
            2,
        ) + '\n';
    emit_generated_text({ ...spec, text: serialized });
}

export interface GeneratedTextSpec extends Omit<GeneratedJsonSpec, 'payload'> {
    /** The whole file, header included — a non-JSON twin writes its own. */
    text: string;
}

/**
 * Either compares `spec.text` against the committed file (`--check`, exit 1
 * when missing or stale) or writes it. For twins that are not JSON (a skill's
 * generated reference) — the same contract, so one check reads both.
 */
export function emit_generated_text(spec: GeneratedTextSpec): void {
    const out_path = resolve(REPO_ROOT, spec.out);
    const run_hint = `Run: node --experimental-strip-types ${spec.script}`;

    if (process.argv.includes('--check')) {
        let current = '';
        try {
            current = readFileSync(out_path, 'utf8');
        } catch {
            console.error(`[${spec.label}] MISSING: ${out_path}\n${run_hint}`);
            process.exit(1);
        }
        if (current !== spec.text) {
            console.error(
                `[${spec.label}] STALE: ${spec.out} does not match ${spec.source}\n${run_hint}`,
            );
            process.exit(1);
        }
        console.log(`[${spec.label}] up to date`);
    } else {
        writeFileSync(out_path, spec.text);
        console.log(`[${spec.label}] wrote ${out_path} (${spec.summary()})`);
    }
}
