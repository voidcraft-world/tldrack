import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { REPO } from '../../scripts/evals-export-source.mjs';

/**
 * Every results file names the commit it was measured at (`commit`, written by `laya-lab bench`), and
 * the EVALS page publishes it as "reproduce at this commit". A commit outside this repository's history
 * is a reproduce line that leads nowhere.
 *
 * A RELEASE gate, not an edit-time test: the first public push publishes ONE commit built from the
 * reviewed tree, so every commit of the local history leaves the public history (its objects may still
 * sit in a local clone, so this asks for ANCESTRY, not existence). From that push until the bench is
 * re-run at a public commit, every results file fails this; failing every unrelated edit for it would
 * teach agents to ignore red. So it runs where `TLDRACK_REQUIRE_CITED_COMMITS=1`: the release job sets
 * it for a final tag (not a release candidate), with the full history checked out. Re-running the bench
 * needs the operator's own labels, so no test can do it.
 */
const ROOT = fileURLToPath(REPO);
const RESULTS = new URL('bench/results/', REPO);
const required = process.env.TLDRACK_REQUIRE_CITED_COMMITS === '1';
const has_git = existsSync(new URL('.git', REPO));
const skip_reason = !required ? 'a release gate: TLDRACK_REQUIRE_CITED_COMMITS=1 runs it'
    : !has_git ? 'no git history here (an installed package)' : false;

function cited_commits(): Array<[string, string]> {
    return readdirSync(RESULTS)
        .filter((name) => name.endsWith('.json'))
        .flatMap((name) => {
            const commit = JSON.parse(readFileSync(new URL(name, RESULTS), 'utf8')).commit;
            return typeof commit === 'string' ? [[name, commit] as [string, string]] : [];
        });
}

test('every results file cites a commit in this history', { skip: skip_reason }, () => {
    const shallow = execFileSync('git', ['-C', ROOT, 'rev-parse', '--is-shallow-repository'], { encoding: 'utf8' }).trim();
    assert.equal(shallow, 'false', 'a shallow clone cannot answer: check out the full history (fetch-depth: 0)');
    const missing = cited_commits().filter(([, commit]) => {
        try {
            execFileSync('git', ['-C', ROOT, 'merge-base', '--is-ancestor', commit, 'HEAD'], { stdio: 'ignore' });
            return false;
        } catch {
            return true;
        }
    });
    assert.deepEqual(missing, [], 'a results file cites a commit outside this history: re-run the bench at a public commit (`uv run --project lab laya-lab bench`) and re-export the EVALS page');
});

test('a cited commit is a full 40-character id', () => {
    for (const [name, commit] of cited_commits()) assert.match(commit, /^[0-9a-f]{40}$/, name);
});
