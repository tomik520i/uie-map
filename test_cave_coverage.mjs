// Tests for web/cave-coverage.js (pure).  node --test test_cave_coverage.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createCaveCoverage, CAVE_COVERAGE_TEXT as TEXT, MAX_COVERAGE_CHUNKS } from './web/cave-coverage.js';

const chunk = (x, z, file = `chunks/${x}_${z}.json`) => ({ x, z, file });
const manifest = (geometry, blocks) => ({
  chunks: geometry.map(([x, z]) => chunk(x, z)),
  ...(blocks ? { blockInfo: { format: 'UIE_BLOCKS1', chunks: blocks.map(([x, z]) => chunk(x, z, `blocks/${x}_${z}.json`)) } } : {}),
});
const state = (coverage, x, z, cut = 30) => coverage.describe({ x, z, cut }).state;

test('cut off: hidden, no message, whatever the position', () => {
  const coverage = createCaveCoverage(manifest([[0, 0]], [[0, 0]]));
  for (const cut of [null, undefined, NaN, Infinity, '30', true]) {
    assert.deepEqual(coverage.describe({ x: 1, z: 1, cut }), { visible: false, state: 'off', chunk: null, text: null });
  }
  assert.deepEqual(coverage.describe(), { visible: false, state: 'off', chunk: null, text: null });
});

test('inside and outside texts at the view target, sparse holes are outside', () => {
  // LIVE-like 2x2 chunk block with a hole at (7,7) inside the bounding box.
  const cells = [[6, 6], [7, 6], [6, 7], [7, 7], [8, 8]];
  const coverage = createCaveCoverage(manifest(cells.filter(([x, z]) => !(x === 7 && z === 7)),
    cells.filter(([x, z]) => !(x === 7 && z === 7))));
  assert.equal(coverage.count, 4);
  assert.deepEqual(coverage.describe({ x: 100, z: 100, cut: 20 }),
    { visible: true, state: 'inside', chunk: [6, 6], text: TEXT.inside });
  assert.equal(TEXT.inside, 'Řez podzemím: oblast s detailními daty.');
  assert.deepEqual(coverage.describe({ x: 7 * 16 + 1, z: 7 * 16 + 1, cut: 20 }),
    { visible: true, state: 'outside', chunk: [7, 7], text: TEXT.outside }, 'a hole inside the bounds is outside');
  assert.equal(TEXT.outside, 'Mimo detailní oblast: zde je dostupný pouze povrch, podzemí není vykreslené.');
  assert.equal(state(coverage, 8 * 16, 8 * 16), 'inside', 'an isolated chunk counts');
  assert.equal(state(coverage, 9 * 16, 8 * 16), 'outside');
});

test('exact and negative chunk boundaries use floor(x / 16)', () => {
  const coverage = createCaveCoverage(manifest([[0, 0], [-1, -1], [1, 0]], [[0, 0], [-1, -1], [1, 0]]));
  const at = (x, z) => coverage.describe({ x, z, cut: 0 }).chunk;
  assert.deepEqual(at(0, 0), [0, 0]);
  assert.deepEqual(at(-0, -0), [0, 0], 'no -0 chunk');
  assert.deepEqual(at(15.999, 15.999), [0, 0]);
  assert.deepEqual(at(16, 0), [1, 0]);
  assert.deepEqual(at(-0.001, -0.001), [-1, -1]);
  assert.deepEqual(at(-16, -16), [-1, -1]);
  assert.deepEqual(at(-16.001, -16), [-2, -1]);
  assert.equal(state(coverage, -0.5, -0.5), 'inside');
  assert.equal(state(coverage, -16.001, -0.5), 'outside');
  assert.equal(state(coverage, 31.999, 0), 'inside');
  assert.equal(state(coverage, 32, 0), 'outside');
});

test('only geometry AND block data is inside; geometry alone is labelled, block data alone is outside', () => {
  const coverage = createCaveCoverage(manifest([[0, 0], [1, 0]], [[0, 0], [5, 5]]));
  assert.equal(coverage.count, 1);
  assert.equal(coverage.geometryCount, 2);
  assert.equal(state(coverage, 1, 1), 'inside');
  assert.deepEqual(coverage.describe({ x: 17, z: 1, cut: 5 }),
    { visible: true, state: 'geometry-only', chunk: [1, 0], text: TEXT.geometryOnly });
  assert.equal(state(coverage, 5 * 16, 5 * 16), 'outside', 'block data without geometry is not coverage');
  assert.equal(coverage.has(0, 0), true);
  assert.equal(coverage.has(1, 0), false);
  assert.equal(coverage.has(5, 5), false);
  const noBlocks = createCaveCoverage(manifest([[0, 0]]));
  assert.equal(noBlocks.count, 0);
  assert.equal(state(noBlocks, 1, 1), 'geometry-only');
  const wrongFormat = createCaveCoverage({ chunks: [chunk(0, 0)], blockInfo: { format: 'OTHER', chunks: [chunk(0, 0)] } });
  assert.equal(wrongFormat.count, 0);
});

test('invalid view target is unknown, never inside', () => {
  const coverage = createCaveCoverage(manifest([[0, 0]], [[0, 0]]));
  for (const [x, z] of [[NaN, 0], [0, Infinity], ['1', 1], [null, 1], [undefined, 1], [30_000_001, 0], [0, -30_000_001]]) {
    assert.deepEqual(coverage.describe({ x, z, cut: 10 }),
      { visible: true, state: 'unknown', chunk: null, text: TEXT.unknown }, `${x},${z}`);
  }
  assert.equal(state(coverage, 30_000_000, 0), 'outside', 'the world border itself is a valid position');
});

test('invalid or oversized manifests never claim coverage', () => {
  for (const bad of [null, undefined, 42, 'manifest', {}, { chunks: 'x' }, { chunks: null }]) {
    const coverage = createCaveCoverage(bad);
    assert.equal(coverage.valid, false);
    assert.equal(coverage.count, 0);
    assert.equal(state(coverage, 1, 1), 'outside');
  }
  const many = Array.from({ length: MAX_COVERAGE_CHUNKS + 1 }, (_, i) => [i % 200, Math.floor(i / 200)]);
  const oversized = createCaveCoverage(manifest(many, many));
  assert.equal(oversized.valid, false, 'an inventory over the limit is refused, not truncated');
  assert.equal(oversized.count, 0);
  assert.equal(state(oversized, 1, 1), 'outside');
  const atLimit = Array.from({ length: MAX_COVERAGE_CHUNKS }, (_, i) => [i % 96, Math.floor(i / 96)]);
  assert.equal(createCaveCoverage(manifest(atLimit, atLimit)).count, MAX_COVERAGE_CHUNKS);
});

test('malformed entries create no coverage and do not break valid ones', () => {
  const entries = [chunk(0, 0), null, 'x', chunk(1.5, 0), chunk('2', 0), chunk(true, 0), chunk(3, NaN),
    chunk(2_000_000, 0), { z: 4 }, chunk(Number.MAX_SAFE_INTEGER + 1, 0), chunk(0, 0)];
  const coverage = createCaveCoverage({ chunks: entries, blockInfo: { format: 'UIE_BLOCKS1', chunks: entries } });
  assert.equal(coverage.valid, true);
  assert.equal(coverage.count, 1, 'only (0,0), duplicates counted once');
  assert.equal(coverage.geometryCount, 1);
  assert.equal(state(coverage, 1.5 * 16, 1), 'outside');
  assert.equal(coverage.has(2_000_000, 0), false);
  assert.equal(coverage.has('0', 0), false);
});
