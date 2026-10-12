// Tests for web/overview-select.js. Node built-ins only.  Run from work/:  node --test uie-map/test_overview_select.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('./web/overview-select.js', import.meta.url), 'utf8');
const { OverviewError, levelForDistance, selectOverviewTiles, tileRect, validateOverviewIndex, wholeWorldDistance } =
  await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function index() {
  // Level 0: a 4x4 block of regions -2..1; parents follow floor division.
  const l0 = [];
  for (let z = -2; z < 2; z += 1) for (let x = -2; x < 2; x += 1) l0.push([x, z]);
  const parent = (list) => [...new Set(list.map(([x, z]) => `${Math.floor(x / 2)},${Math.floor(z / 2)}`))].map((s) => s.split(',').map(Number));
  const l1 = parent(l0);
  const l2 = parent(l1);
  return { format: 'UIE_OVERVIEW1', tileSize: 512, levels: 3, tiles: { 0: l0, 1: l1, 2: l2 },
    blockBounds: { min: [-1024, -1024], max: [1024, 1024] } };
}

test('index validation', () => {
  const parsed = validateOverviewIndex(index());
  assert.equal(parsed.levels, 3);
  assert.ok(parsed.tiles.get(0).has('-2,-2'));
  assert.deepEqual([...parsed.tiles.get(2)], ['-1,-1', '0,-1', '-1,0', '0,0']);
  for (const [label, change] of [['format', (d) => { d.format = 'X'; }], ['tile', (d) => { d.tiles[0].push([1.5, 0]); }],
    ['level missing', (d) => { delete d.tiles[2]; }], ['bounds', (d) => { d.blockBounds.max = [-2000, 0]; }]]) {
    const doc = index();
    change(doc);
    assert.throws(() => validateOverviewIndex(doc), OverviewError, label);
  }
});

test('level grows with distance and is clamped', () => {
  assert.equal(levelForDistance(100, 3), 0);
  assert.equal(levelForDistance(1199, 3), 0);
  assert.equal(levelForDistance(1200, 3), 1);
  assert.equal(levelForDistance(2400, 3), 2);
  assert.equal(levelForDistance(1e7, 3), 2);
});

test('tile rectangles in blocks, including negative tiles', () => {
  assert.deepEqual(tileRect(0, -1, 2), { x0: -512, z0: 1024, size: 512 });
  assert.deepEqual(tileRect(2, -1, 0), { x0: -2048, z0: 0, size: 2048 });
});

test('selection: nearest existing tiles of the chosen level first', () => {
  const parsed = validateOverviewIndex(index());
  const near = selectOverviewTiles(parsed, 10, 10, 100);
  assert.equal(near[0].key, '0/0_0', 'tile under the target first');
  assert.ok(near.every((t) => t.level === 0));
  assert.ok(near.length <= 16 && near.every((t) => parsed.tiles.get(0).has(`${t.tx},${t.tz}`)));
  const far = selectOverviewTiles(parsed, 0, 0, 5000);
  assert.ok(far.every((t) => t.level === 2) && far.length === 4);
  assert.equal(selectOverviewTiles(parsed, 50000, 50000, 100).length, 0, 'nothing outside the world');
  assert.ok(selectOverviewTiles(parsed, 0, 0, 900, 3).length <= 3, 'maxTiles respected');
});

test('whole world distance covers the bounds', () => {
  assert.equal(wholeWorldDistance(validateOverviewIndex(index())), 2048 * 0.95);
});
