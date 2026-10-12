// Tests for web/lod-distance.js. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_lod_distance.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('./web/lod-distance.js', import.meta.url), 'utf8');
const { selectDistanceLod } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const chunk = (cx, cz) => ({ id: `${cx},${cz}`, minX: cx * 16, maxX: cx * 16 + 16, minZ: cz * 16, maxZ: cz * 16 + 16 });
const grid = (x0, x1, z0, z1) => {
  const out = [];
  for (let cz = z0; cz <= z1; cz += 1) for (let cx = x0; cx <= x1; cx += 1) out.push(chunk(cx, cz));
  return out;
};
const select = (tiles, x, z, previousDetailIds = [], enterRadius = 8, exitRadius = 24) =>
  selectDistanceLod({ tiles, focus: { x, z }, enterRadius, exitRadius, previousDetailIds });
const byId = (rows) => Object.fromEntries(rows.map((r) => [r.id, r]));
const detailIds = (rows) => rows.filter((r) => r.level === 'detail').map((r) => r.id).sort();

test('distance is horizontal point-to-rectangle, zero inside and on edges, also at negative coordinates', () => {
  const tiles = [chunk(-2, -3), chunk(0, 0)];       // -32..-16 x -48..-32 and 0..16 x 0..16
  const rows = byId(select(tiles, -20, -40));
  assert.equal(rows['-2,-3'].distance, 0, 'inside');
  assert.equal(byId(select(tiles, -32, -40))['-2,-3'].distance, 0, 'on the west edge');
  assert.equal(byId(select(tiles, -16, -32))['-2,-3'].distance, 0, 'on a corner');
  assert.equal(byId(select(tiles, -36, -40))['-2,-3'].distance, 4, 'straight west of the tile');
  assert.equal(byId(select(tiles, -35, -52))['-2,-3'].distance, 5, 'diagonal to a corner (3-4-5)');
  assert.equal(rows['0,0'].distance, Math.hypot(20, 40));
});

test('Euclidean diagonal boundaries: corner distance decides, not the axis offsets', () => {
  const tiles = [chunk(0, 0)];
  // 6/8 from the corner: distance exactly 10.
  assert.equal(select(tiles, -6, -8, [], 10, 20)[0].level, 'detail', 'at enterRadius is inclusive');
  assert.equal(select(tiles, -6.01, -8, [], 10, 20)[0].level, 'coarse', 'just beyond');
  // Each axis offset (8) is below the radius, but the corner distance (11.31) is not.
  assert.equal(select(tiles, -8, -8, [], 10, 20)[0].level, 'coarse');
});

test('hysteresis: enter at enterRadius, stay up to exitRadius, leave beyond it, over repeated moves', () => {
  const tiles = [chunk(0, 0)];
  const path = [[-30, 8], [-9, 8], [-8, 8], [-20, 8], [-24, 8], [-24.5, 8], [-20, 8], [-8, 8]];
  const expected = ['coarse', 'coarse', 'detail', 'detail', 'detail', 'coarse', 'coarse', 'detail'];
  let previous = [];
  path.forEach(([x, z], i) => {
    const rows = select(tiles, x, z, previous);
    assert.equal(rows[0].level, expected[i], `step ${i} at x=${x}`);
    previous = detailIds(rows);
  });
});

test('previousDetailIds is the previous desired selection; unknown ids are ignored', () => {
  const tiles = grid(-1, 1, -1, 1);
  const rows = select(tiles, 40, 8, ['0,0', 'gone,99']);   // 0,0 is 24 away: retained; 'gone,99' ignored
  assert.equal(byId(rows)['0,0'].level, 'detail');
  assert.equal(rows.length, 9);
  assert.ok(!rows.some((r) => r.id === 'gone,99'));
  // Removed tiles disappear from the output; added tiles enter only by enterRadius.
  const changed = select([...grid(0, 1, 0, 0), chunk(5, 5)], 8, 8, ['-1,0', '0,0']);
  assert.deepEqual(changed.map((r) => r.id), ['0,0', '1,0', '5,5']);
  assert.equal(byId(changed)['5,5'].level, 'coarse');
  assert.equal(byId(changed)['0,0'].level, 'detail');
});

test('nearest first with stable input-order ties', () => {
  const tiles = [chunk(2, 0), chunk(-1, 0), chunk(1, 0), chunk(0, 0), chunk(0, 1)];
  // Focus in the middle of 0,0: -1,0, 1,0 and 0,1 are all 8 away and keep their input order.
  const rows = select(tiles, 8, 8);
  assert.deepEqual(rows.map((r) => r.id), ['0,0', '-1,0', '1,0', '0,1', '2,0']);
  assert.deepEqual(rows.map((r) => r.distance), [0, 8, 8, 8, 24]);
  assert.deepEqual(select(tiles, 8, 8), rows, 'deterministic');
  const equal = [chunk(5, 0), chunk(4, 0)];                        // both 0 away: input order wins
  assert.deepEqual(select(equal, 80, 8).map((r) => r.id), ['5,0', '4,0']);
});

test('inputs are not mutated', () => {
  const tiles = grid(-1, 0, -1, 0);
  const previous = ['0,0'];
  const focus = { x: 3, z: 4 };
  const snapshot = JSON.stringify({ tiles, previous, focus });
  selectDistanceLod({ tiles, focus, enterRadius: 1, exitRadius: 2, previousDetailIds: previous });
  assert.equal(JSON.stringify({ tiles, previous, focus }), snapshot);
});

test('malformed input is refused', () => {
  const tiles = [chunk(0, 0)];
  const ok = { tiles, focus: { x: 0, z: 0 }, enterRadius: 1, exitRadius: 2 };
  const bad = [
    { tiles: 'x' }, { tiles: [null] }, { tiles: [{ ...chunk(0, 0), id: '' }] }, { tiles: [{ ...chunk(0, 0), id: 7 }] },
    { tiles: [chunk(0, 0), chunk(0, 0)] }, { tiles: [{ ...chunk(0, 0), maxX: NaN }] },
    { tiles: [{ ...chunk(0, 0), minX: -Infinity }] }, { tiles: [{ ...chunk(0, 0), maxX: 0 }] },
    { tiles: [{ ...chunk(0, 0), maxZ: -1 }] }, { tiles: [{ ...chunk(0, 0), minZ: '0' }] },
    { focus: null }, { focus: { x: 0 } }, { focus: { x: NaN, z: 0 } }, { focus: { x: 0, z: Infinity } },
    { enterRadius: -1 }, { enterRadius: 2, exitRadius: 2 }, { enterRadius: 3 }, { exitRadius: Infinity },
    { enterRadius: NaN }, { enterRadius: '1' },
    { previousDetailIds: 'a' }, { previousDetailIds: ['a', 'a'] }, { previousDetailIds: [1] }, { previousDetailIds: [''] },
  ];
  for (const change of bad) {
    assert.throws(() => selectDistanceLod({ ...ok, ...change }), /must|needs/, JSON.stringify(change));
  }
  assert.throws(() => selectDistanceLod(), TypeError);
  assert.deepEqual(selectDistanceLod({ ...ok, tiles: [] }), []);
});
