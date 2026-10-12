import test from 'node:test';
import assert from 'node:assert/strict';
import { validHiresSource } from './web/hires-source.mjs';

const HASH = 'a'.repeat(64);
const TILES = { '0,0': HASH, '1,0': 'b'.repeat(64), '2,0': 'c'.repeat(64) };
const complete = () => ({
  legacyIndexSha256: HASH, producerFinishedAt: 1_780_000_000,
  planComplete: true, plannedJobs: 3, notScheduled: 0,
  failedJobs: 0, failedTiles: 0, staleTiles: [],
});
const partial = () => ({ ...complete(), planComplete: false, notScheduled: 1,
  failedJobs: 1, failedTiles: 2, staleTiles: ['0,0', '1,0'] });

test('accepts complete and partial producer snapshots', () => {
  assert.equal(validHiresSource(complete(), TILES), true);
  assert.equal(validHiresSource(partial(), TILES), true);
  assert.equal(validHiresSource({ ...partial(), staleTiles: [] }, TILES), true);
});

test('requires exact source keys and object shapes', () => {
  const source = complete();
  for (const key of Object.keys(source)) {
    const changed = { ...source };
    delete changed[key];
    assert.equal(validHiresSource(changed, TILES), false, key);
  }
  assert.equal(validHiresSource({ ...source, extra: true }, TILES), false);
  assert.equal(validHiresSource([], TILES), false);
  assert.equal(validHiresSource(new Date(), TILES), false);
  assert.equal(validHiresSource(source, []), false);
  assert.equal(validHiresSource(source, null), false);
});

test('rejects invalid hashes, times, booleans and counts', () => {
  for (const hash of ['A'.repeat(64), 'a'.repeat(63), 123, null]) {
    assert.equal(validHiresSource({ ...complete(), legacyIndexSha256: hash }, TILES), false);
  }
  for (const when of [0, -1, 0.5, 1e-7, Infinity, -Infinity, NaN,
    Number.MAX_SAFE_INTEGER + 1, '1780000000', null]) {
    assert.equal(validHiresSource({ ...complete(), producerFinishedAt: when }, TILES), false);
  }
  assert.equal(validHiresSource({ ...complete(), planComplete: 1 }, TILES), false);
  for (const key of ['plannedJobs', 'notScheduled', 'failedJobs', 'failedTiles']) {
    for (const value of [-1, 0.5, Number.MAX_SAFE_INTEGER + 1, true, '1']) {
      assert.equal(validHiresSource({ ...complete(), [key]: value }, TILES), false, `${key}: ${value}`);
    }
  }
  assert.equal(validHiresSource({ ...partial(), notScheduled: 4 }, TILES), false);
  assert.equal(validHiresSource({ ...partial(), failedJobs: 4 }, TILES), false);
});

test('complete plan cannot contain pending work, errors or stale tiles', () => {
  for (const field of ['notScheduled', 'failedJobs', 'failedTiles']) {
    assert.equal(validHiresSource({ ...complete(), [field]: 1 }, TILES), false, field);
  }
  assert.equal(validHiresSource({ ...complete(), staleTiles: ['0,0'] }, TILES), false);
});

test('stale tiles must be sorted unique canonical in-world own keys', () => {
  for (const staleTiles of [
    ['1,0', '0,0'], ['0,0', '0,0'], ['3,0'], ['00,0'], ['-0,0'],
    ['+1,0'], ['468751,0'], [42], '0,0', Array(100_001).fill('0,0'),
  ]) {
    const tiles = { ...TILES };
    for (const key of Array.isArray(staleTiles) ? staleTiles : []) {
      if (typeof key === 'string' && key !== '3,0') tiles[key] = HASH;
    }
    assert.equal(validHiresSource({ ...partial(), staleTiles }, tiles), false);
  }
  const inherited = Object.create({ '0,0': HASH });
  assert.equal(validHiresSource({ ...partial(), staleTiles: ['0,0'] }, inherited), false);
});
