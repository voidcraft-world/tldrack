import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  EVAL_DEADLINE_ENV,
  GPU_NIGHT_WINDOW,
  LEASE_SCHEDULED_ENV,
  LOCK_DIR_ENV,
  env_value,
  legacy_env_name,
  LeaseCapacitySchema,
  MAX_CAPACITY,
  default_capacity,
  run_minutes_left,
  slot_capacity,
  slot_holds,
  window_open,
  zone_clock,
} from '../src/resource-lease.ts';

describe('the default capacity is worked out from the machine', () => {
  it('gives a 32 GB, 10-core machine 22 GB, one Claude job and 6 cores', () => {
    assert.deepEqual(default_capacity({ memory_gb: 32, cores: 10 }), { gpu: 22, subscription_claude: 1, cpu: 6 });
  });

  it('keeps a quarter of a small machine for the gpu slot and one core for the cpu slot', () => {
    assert.deepEqual(default_capacity({ memory_gb: 8, cores: 4 }), { gpu: 2, subscription_claude: 1, cpu: 1 });
    assert.deepEqual(default_capacity({ memory_gb: 16, cores: 8 }), { gpu: 6, subscription_claude: 1, cpu: 4 });
  });

  it('never leaves its bounds, whatever the machine reports', () => {
    for (const host of [{ memory_gb: 0, cores: 0 }, { memory_gb: Number.NaN, cores: -3 }, { memory_gb: 4096, cores: 1024 }]) {
      const capacity = default_capacity(host);
      assert.ok(capacity.gpu > 0 && capacity.gpu <= MAX_CAPACITY.gpu, JSON.stringify(host));
      assert.ok(capacity.cpu >= 1 && capacity.cpu <= MAX_CAPACITY.cpu, JSON.stringify(host));
      assert.ok(LeaseCapacitySchema.safeParse({ v: 1, ...capacity }).success, JSON.stringify(host));
    }
  });

  it('lets capacity.json override it, slot by slot', () => {
    const host = { memory_gb: 32, cores: 10 };
    assert.equal(slot_capacity('gpu', null, host), 22);
    assert.equal(slot_capacity('gpu', { v: 1, gpu: 12 }, host), 12);
    assert.equal(slot_capacity('cpu', { v: 1, gpu: 12 }, host), 6);
  });
});

describe('the night window runs on the machine’s clock, or a zone the caller names', () => {
  const at = new Date('2026-11-13T09:30:00Z');

  it('reads a named zone', () => {
    assert.equal(zone_clock(at, 'UTC'), '09:30');
    assert.equal(zone_clock(at, 'Asia/Tokyo'), '18:30');
  });

  it('reads the machine’s own clock without one', () => {
    const local = `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`;
    assert.equal(zone_clock(at), local);
  });

  it('bounds a scheduled run by the window’s end in that zone, and leaves a run by hand unbounded', () => {
    const scheduled = { [LEASE_SCHEDULED_ENV]: '1' };
    assert.equal(window_open(GPU_NIGHT_WINDOW, '05:00'), true);
    assert.equal(run_minutes_left(scheduled, new Date('2026-11-13T05:00:00Z'), 'UTC'), 120);
    assert.equal(run_minutes_left(scheduled, new Date('2026-11-13T09:30:00Z'), 'UTC'), 0);
    assert.equal(run_minutes_left({}, at, 'UTC'), null);
  });
});

describe('which claims hold', () => {
  it('holds in order, never letting a small ticket jump a big one', () => {
    const claims = [
      { units: 20, legacy: false },
      { units: 10, legacy: false },
      { units: 2, legacy: false },
    ];
    assert.deepEqual(slot_holds(claims, 26), [true, false, false]);
    assert.deepEqual(slot_holds([{ units: 26, legacy: true }, { units: 1, legacy: false }], 26), [true, false]);
  });
});

describe('a scheduler\'s names: the tldrack name first, then the one the lease was born with', () => {
  it('reads the new name, falls back to the old one, and treats empty as unset', () => {
    assert.equal(LEASE_SCHEDULED_ENV, 'TLDRACK_LEASE_SCHEDULED');
    assert.equal(legacy_env_name(EVAL_DEADLINE_ENV), 'VOIDCRAFT_EVAL_DEADLINE');
    assert.equal(env_value({ VOIDCRAFT_LOCK_DIR: '/x' }, LOCK_DIR_ENV), '/x');
    assert.equal(env_value({ TLDRACK_LOCK_DIR: '/new', VOIDCRAFT_LOCK_DIR: '/old' }, LOCK_DIR_ENV), '/new');
    assert.equal(env_value({ TLDRACK_LOCK_DIR: '', VOIDCRAFT_LOCK_DIR: '/old' }, LOCK_DIR_ENV), '/old');
    assert.equal(env_value({}, 'OTHER'), undefined);
  });

  it('a run started under the old names is still bounded by them', () => {
    const now = new Date(Date.UTC(2026, 0, 1, 12));
    const deadline = String(now.getTime() + 30 * 60_000);
    assert.equal(run_minutes_left({ VOIDCRAFT_EVAL_DEADLINE: deadline }, now), 30);
    assert.equal(run_minutes_left({ TLDRACK_EVAL_DEADLINE: deadline }, now), 30);
  });
});
