import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import { AGENT_CANVAS_BLOCK_SCHEMAS, AGENT_CANVAS_QUESTION_TYPES } from '../src/agent-canvas.ts';

/**
 * The agent-canvas plugin is stdlib Python and reads a generated JSON twin of `src/agent-canvas.ts`; agents read a
 * generated block reference. Both are committed, so a block added here without a regeneration would be a block the
 * plugin refuses and no agent is told about. Why a generated twin: owned by `scripts/generated-json.ts` — see the
 * note there.
 */
const ROOT = fileURLToPath(new URL('../../', import.meta.url));

describe('agent canvas', () => {
  it('both generated twins match the registry', () => {
    const run = spawnSync(process.execPath, ['--experimental-strip-types', 'schemas/scripts/export-agent-canvas.ts', '--check'],
      { cwd: ROOT, encoding: 'utf8' });
    assert.equal(run.status, 0, `${run.stdout}${run.stderr}`);
  });

  it('every question type is a registered block', () => {
    for (const type of AGENT_CANVAS_QUESTION_TYPES) assert.ok(type in AGENT_CANVAS_BLOCK_SCHEMAS, type);
  });
});
