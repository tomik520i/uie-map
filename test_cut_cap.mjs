// Tests for web/cut-cap.js (closed height cut). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_cut_cap.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const load = (path) => import(`data:text/javascript;base64,${Buffer.from(readFileSync(new URL(path, import.meta.url), 'utf8')).toString('base64')}`);
const cap = await load('./web/cut-cap.js');
const { blockShape } = await load('./web/walk-physics.js');

// 3 x 2 cropped area; level 10: x0 stone, x1 air (cave), x2 stone on row z0; row z1: water, water, lava.
function chunk() {
  const width = 3, depth = 2;
  const ids = new Uint16Array(width * depth * 256);
  const metas = new Uint8Array(ids.length);
  const put = (x, y, z, id) => { ids[(z * width + x) * 256 + y] = id; };
  for (let x = 0; x < 3; x += 1) for (let z = 0; z < 2; z += 1) for (let y = 0; y < 10; y += 1) put(x, y, z, 1);
  put(0, 10, 0, 1); put(2, 10, 0, 1); put(1, 10, 0, 31);
  put(0, 10, 1, 9); put(1, 10, 1, 9); put(2, 10, 1, 11);
  const registry = new Map([[0, 'minecraft:air'], [1, 'minecraft:stone'], [9, 'minecraft:water'], [11, 'minecraft:lava'],
    [31, 'minecraft:tallgrass']]);
  return { x: 32, z: -16, width, depth, registry, ids, metas };
}

test('classes: solid, water, lava, and plants/air as none', () => {
  const c = chunk();
  const classes = cap.capClasses(c, blockShape);
  const at = (x, y, z) => classes[(z * c.width + x) * 256 + y];
  assert.equal(at(0, 10, 0), cap.CAP_SOLID);
  assert.equal(at(1, 10, 0), cap.CAP_NONE, 'tall grass is not cut rock');
  assert.equal(at(0, 10, 1), cap.CAP_WATER);
  assert.equal(at(2, 10, 1), cap.CAP_LAVA);
  assert.equal(at(1, 11, 0), cap.CAP_NONE);
});

test('quads merge equal runs and leave cave cells open', () => {
  const c = chunk();
  const classes = cap.capClasses(c, blockShape);
  assert.deepEqual(cap.capQuads(classes, c.width, c.depth, 10), [
    { x0: 0, z0: 0, x1: 1, z1: 1, code: cap.CAP_SOLID },
    { x0: 2, z0: 0, x1: 3, z1: 1, code: cap.CAP_SOLID },
    { x0: 0, z0: 1, x1: 2, z1: 2, code: cap.CAP_WATER },
    { x0: 2, z0: 1, x1: 3, z1: 2, code: cap.CAP_LAVA },
  ]);
  assert.deepEqual(cap.capQuads(classes, c.width, c.depth, 5), [{ x0: 0, z0: 0, x1: 3, z1: 2, code: cap.CAP_SOLID }],
    'a full solid level is one rectangle');
  assert.deepEqual(cap.capQuads(classes, c.width, c.depth, 200), []);
  assert.deepEqual(cap.capQuads(classes, c.width, c.depth, 256), []);
});

test('arrays place upward quads in world coordinates with the code colour', () => {
  const { positions, colors, indices } = cap.capArrays(32, -16, [{ x0: 0, z0: 0, x1: 3, z1: 2, code: cap.CAP_LAVA }], 10.998);
  assert.deepEqual([...positions].map((v) => Math.round(v * 1000) / 1000),
    [32, 10.998, -16, 32, 10.998, -14, 35, 10.998, -14, 35, 10.998, -16]);
  assert.deepEqual([...colors.slice(0, 3)].map((v) => Math.round(v * 100) / 100), [0.92, 0.42, 0.08]);
  assert.deepEqual([...indices], [0, 1, 2, 0, 2, 3]);
  // Normal of (p0, p1, p2) points up.
  const [a, b, c] = [0, 1, 2].map((i) => positions.slice(i * 3, i * 3 + 3));
  const u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
  const v = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
  assert.ok(u[2] * v[0] - u[0] * v[2] > 0, 'counter-clockwise from above');
});
