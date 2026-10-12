// Tests for web/cave-cut.js (height cut / cave view). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_cave_cut.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('./web/cave-cut.js', import.meta.url), 'utf8');
const { clipTop, cutRange, describeCut, firstVisibleHit, parseCutParam } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const bounds = { min: [-96, 3.5, -96], max: [96, 201, 0] };

test('slider range covers every block level of the bounds', () => {
  assert.deepEqual(cutRange(bounds), { min: 3, max: 200 });
  assert.deepEqual(cutRange({ min: [0, 64, 0], max: [1, 64, 1] }), { min: 64, max: 64 }, 'flat bounds stay valid');
});

test('a level keeps its own block: the plane is at its top face', () => {
  assert.equal(clipTop(40), 41);
  assert.equal(clipTop(-1), 0);
});

test('?cut= parsing accepts integers only and clamps to the range', () => {
  const range = cutRange(bounds);
  assert.equal(parseCutParam('40', range), 40);
  assert.equal(parseCutParam(' 12 ', range), 12);
  assert.equal(parseCutParam('-20', range), 3);
  assert.equal(parseCutParam('9999', range), 200);
  for (const bad of [null, undefined, '', 'abc', '4.5', '1e3', '12345', '0x10']) {
    assert.equal(parseCutParam(bad, range), null, String(bad));
  }
});

test('picking skips hits above the cut and keeps the nearest visible one', () => {
  const hits = [{ point: { y: 70 } }, { point: { y: 41.0005 } }, { point: { y: 30 } }];
  assert.equal(firstVisibleHit(hits, null), hits[0], 'no cut: nearest hit');
  assert.equal(firstVisibleHit(hits, 40), hits[1], 'top face of level 40 (y=41) is visible');
  assert.equal(firstVisibleHit(hits, 30), hits[2], 'level 30 keeps y <= 31');
  assert.equal(firstVisibleHit(hits, 20), undefined, 'everything above the cut: no hit');
  assert.equal(firstVisibleHit([{ point: { y: 31.5 } }, { point: { y: 31 } }], 30)?.point.y, 31);
  assert.equal(firstVisibleHit([], 10), undefined);
});

test('panel label', () => {
  assert.equal(describeCut(null), 'vypnuto');
  assert.equal(describeCut(52), 'bloky do Y 52');
});
