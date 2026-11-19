import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import {
  DECIDE_SURFACE_NAME,
  LAYA_FINE_TUNE_PREFIX,
  LAYA_LEGACY_FINE_TUNE_PREFIX,
  LAYA_LANDING_SURFACES,
  LAYA_LEDGER_KEEP_TEXT,
  LAYA_LEDGER_RACK_SURFACES,
} from '../src/decide.ts';
import { DEFAULT_CARD_REPO, GITHUB_REPO } from '../src/laya-review.ts';
import { NEXT_STEP_ACTIONS } from '../src/next-step.ts';
import { ENV_PREFIX, LEASE_SCHEDULED_ENV, LEGACY_ENV_PREFIX } from '../src/resource-lease.ts';

/**
 * A constant this package shares with a Python writer is a twin, and a twin drifts silently unless something reads
 * both. These read the Python source as text (no interpreter, no import), so the pin runs wherever `node --test` does.
 */

const ROOT = new URL('../../', import.meta.url);
const python = (path: string) => readFileSync(new URL(path, ROOT), 'utf8');

/** `NAME = ("a", "b")` or `NAME: tuple[str, ...] = ("a", "b")` → ['a', 'b']. */
function tuple(source: string, name: string): string[] {
  const match = new RegExp(`^${name}(?::[^=\\n]+)?\\s*=\\s*\\(([^)]*)\\)`, 'm').exec(source);
  assert.ok(match, `${name} is not a tuple literal any more: update this pin`);
  return [...match[1]!.matchAll(/"([^"]*)"/g)].map((m) => m[1]!);
}

/** `NAME = "value"` → 'value'. */
function string(source: string, name: string): string {
  const match = new RegExp(`^${name}(?::[^=\\n]+)?\\s*=\\s*"([^"]*)"`, 'm').exec(source);
  assert.ok(match, `${name} is not a string literal any more: update this pin`);
  return match[1]!;
}

describe('the Python twins', () => {
  it('next-step actions: bridge/tldrack/next_step.py ACTIONS', () => {
    assert.deepEqual(tuple(python('bridge/tldrack/next_step.py'), 'ACTIONS'), [...NEXT_STEP_ACTIONS]);
  });

  it('ledger surface sets: plugins/laya/laya_bridge/ledger.py KEEP_TEXT and RACK_SURFACES', () => {
    const ledger = python('plugins/laya/laya_bridge/ledger.py');
    assert.deepEqual(tuple(ledger, 'KEEP_TEXT'), [...LAYA_LEDGER_KEEP_TEXT]);
    assert.deepEqual(tuple(ledger, 'RACK_SURFACES'), [...LAYA_LEDGER_RACK_SURFACES]);
  });

  it('landing surfaces: plugins/laya/laya_bridge/landing.py SURFACES', () => {
    assert.deepEqual(tuple(python('plugins/laya/laya_bridge/landing.py'), 'SURFACES'), [...LAYA_LANDING_SURFACES]);
  });

  it('surface names and the fine-tune prefix: plugins/laya/laya_bridge/paths.py', () => {
    const paths = python('plugins/laya/laya_bridge/paths.py');
    const surface = /^SURFACE_NAME\s*=\s*re\.compile\(r"([^"]+)"\)/m.exec(paths);
    assert.ok(surface, 'SURFACE_NAME is not a re.compile literal any more: update this pin');
    assert.equal(`^${surface[1]}$`, DECIDE_SURFACE_NAME.source);
    assert.equal(string(paths, 'FINE_TUNE_PREFIX'), LAYA_FINE_TUNE_PREFIX);
    assert.equal(string(paths, 'LEGACY_FINE_TUNE_PREFIX'), LAYA_LEGACY_FINE_TUNE_PREFIX);
  });

  it('the verifier card repository: plugins/laya/laya_bridge/card_files.py', () => {
    const card = python('plugins/laya/laya_bridge/card_files.py');
    assert.equal(string(card, 'DEFAULT_REPO'), DEFAULT_CARD_REPO);
    const shape = /^REPO_SHAPE\s*=\s*re\.compile\(r"([^"]+)"\)/m.exec(card);
    assert.ok(shape, 'REPO_SHAPE is not a re.compile literal any more: update this pin');
    assert.equal(`^${shape[1]}$`, GITHUB_REPO.source.replace('\\/', '/'));
  });

  it('a scheduler\'s names: plugins/laya/laya_bridge/env_names.py and lab/laya_lab/lease.py', () => {
    const names = python('plugins/laya/laya_bridge/env_names.py');
    assert.equal(string(names, 'PREFIX'), ENV_PREFIX);
    assert.equal(string(names, 'LEGACY_PREFIX'), LEGACY_ENV_PREFIX);
    assert.equal(string(python('lab/laya_lab/lease.py'), 'LEASE_SCHEDULED_ENV'), LEASE_SCHEDULED_ENV);
  });
});
