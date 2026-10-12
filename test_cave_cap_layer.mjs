// Tests for web/cave-cap-layer.js with real UIC1 tiles (cave-cap-tile.js) and real cut-cap merging.
//   node --test test_cave_cap_layer.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { setImmediate as tick } from 'node:timers/promises';
import { createCaveCapLayer } from './web/cave-cap-layer.js';
import { decodeCaveCapTile } from './web/cave-cap-tile.js';

const flush = async () => { for (let i = 0; i < 5; i += 1) await tick(); };
const FULL_QUAD_BYTES = 2 * (12 * 4 + 12 * 4 + 6 * 4);   // one merged 64 x 64 quad: 240 B accounted

// A UIC1 tile whose every column has class classOf(y) at height y.
function tileBuffer(x, z, classOf = (y) => (y < 100 ? 1 : 0), present = 0xffff) {
  const buffer = new ArrayBuffer(262168);
  const view = new DataView(buffer);
  const bytes = new Uint8Array(buffer);
  bytes.set([85, 73, 67, 49, 1]);
  view.setInt32(8, x, true); view.setInt32(12, z, true);
  view.setUint16(16, 64, true); view.setUint16(18, 64, true); view.setUint16(20, 256, true); view.setUint16(22, present, true);
  const column = new Uint8Array(64);
  for (let y = 0; y < 256; y += 1) column[y >> 2] |= (classOf(y) & 3) << ((y & 3) * 2);
  const data = new Uint8Array(buffer, 24);
  for (let c = 0; c < 4096; c += 1) data.set(column, c * 64);
  return buffer;
}

const entry = (id, origin) => ({ pairSha256: id, expected: { geometrySha256: id, atlasSha256: 'atlas',
  rendererIdentity: 'renderer', capIdentity: 'rules', origin } });

function harness({ tiles, deferred = false, attachResult = () => true, ...options }) {
  const log = { loads: [], attach: [], drop: [], errors: [] };
  const pending = [];
  const layer = createCaveCapLayer({
    ...options,
    load: ({ pairSha256 }) => {
      log.loads.push(pairSha256);
      const result = { tile: decodeCaveCapTile(tiles[pairSha256].slice(0)) };
      if (!deferred) return Promise.resolve(result);
      return new Promise((resolve) => pending.push({ id: pairSha256, resolve: () => resolve(result) }));
    },
    attach: (key, arrays) => { log.attach.push({ key, arrays }); return attachResult(key, arrays); },
    drop: (key) => log.drop.push(key),
    onError: (key, error) => log.errors.push([key, error.message]),
  });
  return { layer, log, pending };
}

const quadsOf = ({ positions }) => {
  const out = [];
  for (let q = 0; q < positions.length / 12; q += 1) {
    const xs = [0, 3, 6, 9].map((k) => positions[q * 12 + k]);
    const zs = [2, 5, 8, 11].map((k) => positions[q * 12 + k]);
    out.push({ x0: Math.min(...xs), x1: Math.max(...xs), z0: Math.min(...zs), z1: Math.max(...zs), y: positions[q * 12 + 1] });
  }
  return out;
};

test('a cap is built at level + 0.998 and level changes rebuild without refetching', async () => {
  const { layer, log } = harness({ tiles: { a: tileBuffer(0, 0) } });
  const entries = new Map([['a', entry('a', [0, 0])]]);
  layer.update({ entries, level: 50 });
  await flush();
  assert.deepEqual(log.loads, ['a']);
  assert.equal(log.attach.length, 1);
  const [quad] = quadsOf(log.attach[0].arrays);
  assert.deepEqual(quad, { x0: 0, x1: 64, z0: 0, z1: 64, y: Math.fround(50.998) });
  assert.deepEqual([layer.snapshot().meshes, layer.snapshot().meshBytes], [1, FULL_QUAD_BYTES]);
  layer.update({ entries, level: 60 });
  assert.equal(log.attach.length, 2, 'rebuilt synchronously');
  assert.equal(quadsOf(log.attach[1].arrays)[0].y, Math.fround(60.998));
  layer.update({ entries, level: 150 });                       // air: no cap needed
  assert.deepEqual(log.drop, ['a']);
  assert.deepEqual([layer.snapshot().meshes, layer.snapshot().meshBytes], [0, 0]);
  layer.update({ entries, level: 40 });
  assert.equal(log.attach.length, 3);
  await flush();
  assert.deepEqual(log.loads, ['a'], 'no refetch for level changes');
  layer.dispose();
});

test('unchanged updates do no mesh work', async () => {
  const { layer, log } = harness({ tiles: { a: tileBuffer(0, 0) } });
  const entries = new Map([['a', entry('a', [0, 0])]]);
  layer.update({ entries, level: 50, maskedChunks: new Set(['9,9']) });
  await flush();
  const before = [log.attach.length, log.drop.length, log.loads.length];
  for (let i = 0; i < 5; i += 1) {
    layer.update({ entries: new Map([['a', entry('a', [0, 0])]]), level: 50, maskedChunks: new Set(['9,9']) });
  }
  await flush();
  assert.deepEqual([log.attach.length, log.drop.length, log.loads.length], before);
  layer.dispose();
});

test('chunks covered by detail geometry get no cap, also at negative origins', async () => {
  const { layer, log } = harness({ tiles: { a: tileBuffer(0, 0), n: tileBuffer(-64, -64) } });
  layer.update({ entries: new Map([['a', entry('a', [0, 0])], ['n', entry('n', [-64, -64])]]), level: 10,
    maskedChunks: new Set(['0,0', '-1,-1']) });
  await flush();
  const caps = new Map(log.attach.map(({ key, arrays }) => [key, quadsOf(arrays)]));
  const covers = (quads, x, z) => quads.some((q) => q.x0 <= x && x < q.x1 && q.z0 <= z && z < q.z1);
  assert.equal(covers(caps.get('a'), 8, 8), false, 'chunk 0,0 masked');
  assert.equal(covers(caps.get('a'), 20, 8), true);
  assert.equal(covers(caps.get('a'), 8, 20), true);
  assert.equal(covers(caps.get('n'), -8, -8), false, 'chunk -1,-1 masked');
  assert.equal(covers(caps.get('n'), -24, -8), true);
  assert.equal(caps.get('a').length, 2, 'remaining cells merged into rectangles');
  const masked = new Set();
  for (let cz = 0; cz < 4; cz += 1) for (let cx = 0; cx < 4; cx += 1) masked.add(`${cx},${cz}`);
  layer.update({ entries: new Map([['a', entry('a', [0, 0])], ['n', entry('n', [-64, -64])]]), level: 10,
    maskedChunks: masked });
  assert.deepEqual(log.drop, ['a'], 'a fully masked tile needs no mesh');
  assert.equal(layer.snapshot().meshes, 1);
  layer.dispose();
});

test('identity replacement drops the old cap synchronously before the replacement loads', async () => {
  const { layer, log } = harness({ tiles: { g1: tileBuffer(0, 0), g2: tileBuffer(0, 0, (y) => (y < 20 ? 2 : 0)) } });
  layer.update({ entries: new Map([['k', entry('g1', [0, 0])]]), level: 10 });
  await flush();
  assert.equal(log.attach.length, 1);
  layer.update({ entries: new Map([['k', entry('g2', [0, 0])]]), level: 10 });
  assert.deepEqual(log.drop, ['k'], 'dropped before any await');
  assert.equal(layer.snapshot().meshes, 0);
  await flush();
  assert.deepEqual(log.loads, ['g1', 'g2']);
  assert.equal(log.attach.length, 2);
  assert.equal(layer.snapshot().meshes, 1);
  layer.dispose();
});

test('mesh budget keeps the nearest caps and counts the omitted ones', async () => {
  const tiles = { a: tileBuffer(0, 0), b: tileBuffer(64, 0), c: tileBuffer(128, 0) };
  const { layer, log, pending } = harness({ tiles, deferred: true, concurrency: 3, meshBudgetBytes: 2 * FULL_QUAD_BYTES + 20 });
  layer.update({ entries: new Map([['a', entry('a', [0, 0])], ['b', entry('b', [64, 0])], ['c', entry('c', [128, 0])]]),
    level: 50 });
  await flush();
  const resolve = (id) => pending.find((p) => p.id === id).resolve();
  resolve('c'); await flush();
  resolve('a'); await flush();
  assert.deepEqual([...log.attach.map((a) => a.key)], ['c', 'a']);
  resolve('b'); await flush();                                  // nearer than c: c's cap gives way
  const snap = layer.snapshot();
  assert.deepEqual([snap.meshes, snap.meshBytes, snap.budgetLimited, snap.resident], [2, 2 * FULL_QUAD_BYTES, 1, 3]);
  assert.deepEqual(log.drop, ['c']);
  assert.ok(snap.meshBytes <= snap.meshBudgetBytes);
  layer.update({ entries: new Map([['b', entry('b', [64, 0])], ['c', entry('c', [128, 0])]]), level: 50 });
  assert.equal(layer.snapshot().budgetLimited, 0, 'a left: the limited cap fits again without refetch');
  assert.equal(layer.snapshot().meshes, 2);
  assert.equal(log.loads.length, 3);
  layer.dispose();
});

test('late responses after a window change, cut off or dispose never attach', async () => {
  const { layer, log, pending } = harness({ tiles: { a: tileBuffer(0, 0), b: tileBuffer(64, 0) }, deferred: true });
  layer.update({ entries: new Map([['a', entry('a', [0, 0])]]), level: 50 });
  await flush();
  layer.update({ entries: new Map([['a', entry('a', [0, 0])]]), level: null });
  assert.deepEqual([layer.snapshot().wanted, layer.snapshot().resident], [0, 0], 'cut off empties the window');
  pending[0].resolve(); await flush();
  assert.equal(log.attach.length, 0);
  layer.update({ entries: new Map([['b', entry('b', [64, 0])]]), level: 50 });
  await flush();
  layer.dispose();
  pending[1].resolve(); await flush();
  assert.equal(log.attach.length, 0);
  assert.deepEqual(layer.snapshot().loading, 0);
});

test('cut off and dispose drop every cap and release the counters', async () => {
  const { layer, log } = harness({ tiles: { a: tileBuffer(0, 0), b: tileBuffer(64, 0) } });
  const entries = new Map([['a', entry('a', [0, 0])], ['b', entry('b', [64, 0])]]);
  layer.update({ entries, level: 50 });
  await flush();
  assert.equal(layer.snapshot().meshes, 2);
  layer.update({ entries, level: null });
  assert.deepEqual(log.drop.sort(), ['a', 'b']);
  assert.deepEqual([layer.snapshot().meshes, layer.snapshot().meshBytes, layer.snapshot().bytes], [0, 0, 0]);
  layer.update({ entries, level: 50 });
  await flush();
  layer.dispose();
  const snap = layer.snapshot();
  assert.deepEqual([snap.meshes, snap.meshBytes, snap.resident, snap.bytes, snap.budgetLimited, snap.level], [0, 0, 0, 0, 0, null]);
  assert.equal(log.drop.length, 4);
  layer.update({ entries, level: 50 });
  await flush();
  assert.equal(log.attach.length, 4, 'a disposed layer attaches nothing');
});

// Fluids by height: water below 10, lava 10..19, cube 20..29, air/partial above.
const fluids = (y) => (y < 10 ? 2 : y < 20 ? 3 : y < 30 ? 1 : 0);

test('classAt reads only the resident tile of the current window, without loads or bytes', async () => {
  const { layer, log } = harness({ tiles: { a: tileBuffer(0, 0, fluids), n: tileBuffer(-64, -64, fluids, 0xfffe) } });
  const entries = new Map([['a', entry('a', [0, 0])], ['n', entry('n', [-64, -64])]]);
  assert.equal(layer.classAt('a', 0, 5, 0), undefined, 'not resident yet');
  layer.update({ entries, level: 50 });
  assert.equal(layer.classAt('a', 0, 5, 0), undefined, 'still loading');
  await flush();
  const before = [log.loads.length, layer.snapshot().bytes, layer.snapshot().resident];
  for (let i = 0; i < 1000; i += 1) {
    assert.equal(layer.classAt('a', i % 64, 5, (i * 7) % 64), 2);
    assert.equal(layer.classAt('a', 63, 15, 63), 3);
  }
  assert.deepEqual([layer.classAt('a', 1, 25, 1), layer.classAt('a', 1, 40, 1), layer.classAt('a', 0, 255, 0)], [1, 0, 0]);
  assert.equal(layer.classAt('n', 0, 5, 0), undefined, 'chunk 0 absent (present mask 0xfffe): unknown, never air');
  assert.equal(layer.classAt('n', 16, 5, 0), 2);
  assert.deepEqual([log.loads.length, layer.snapshot().bytes, layer.snapshot().resident], before, 'queries never fetch');
  for (const [lx, y, lz] of [[64, 5, 0], [-1, 5, 0], [0, 256, 0], [0, -1, 0], [0.5, 5, 0], [0, '5', 0], [NaN, 5, 0]]) {
    assert.equal(layer.classAt('a', lx, y, lz), undefined, `invalid ${lx},${y},${lz} never throws`);
  }
  assert.equal(layer.classAt('missing', 0, 5, 0), undefined);
  layer.dispose();
});

test('classAt follows window replacement, cut off and dispose synchronously', async () => {
  const { layer, pending } = harness({ tiles: { g1: tileBuffer(0, 0, fluids), g2: tileBuffer(0, 0, () => 3) }, deferred: true });
  layer.update({ entries: new Map([['k', entry('g1', [0, 0])]]), level: 50 });
  await flush(); pending.shift().resolve(); await flush();
  assert.equal(layer.classAt('k', 0, 5, 0), 2);
  layer.update({ entries: new Map([['k', entry('g2', [0, 0])]]), level: 50 });
  assert.equal(layer.classAt('k', 0, 5, 0), undefined, 'old revision gone before the replacement arrives');
  await flush(); pending.shift().resolve(); await flush();
  assert.equal(layer.classAt('k', 0, 5, 0), 3, 'new revision');
  layer.update({ entries: new Map([['k', entry('g2', [64, 0])]]), level: 50 });   // same pair, other origin: refused
  await flush();
  assert.equal(layer.classAt('k', 0, 5, 0), undefined);
  layer.update({ entries: new Map([['k', entry('g2', [0, 0])]]), level: null });
  assert.equal(layer.classAt('k', 0, 5, 0), undefined, 'cut off');
  layer.update({ entries: new Map([['k', entry('g2', [0, 0])]]), level: 50 });
  await flush(); pending.shift()?.resolve(); await flush();
  layer.dispose();
  assert.equal(layer.classAt('k', 0, 5, 0), undefined, 'disposed');
});

test('a tile whose attach was refused is never readable', async () => {
  const { layer } = harness({ tiles: { a: tileBuffer(0, 0, fluids) }, attachResult: () => false });
  layer.update({ entries: new Map([['a', entry('a', [0, 0])]]), level: 25 });   // level 25 is a cube cap: attach refused
  await flush();
  assert.equal(layer.snapshot().resident, 0);
  assert.equal(layer.classAt('a', 0, 5, 0), undefined);
  layer.dispose();
});

test('a refused attach leaks no counters; invalid levels are refused without changing state', async () => {
  let accept = false;
  const { layer, log } = harness({ tiles: { a: tileBuffer(0, 0) }, attachResult: () => accept });
  const entries = new Map([['a', entry('a', [0, 0])]]);
  layer.update({ entries, level: 50 });
  await flush();
  const snap = layer.snapshot();
  assert.deepEqual([snap.meshes, snap.meshBytes, snap.rejected, snap.resident, snap.failed], [0, 0, 0, 0, 1]);
  assert.deepEqual(log.drop, []);
  for (const level of [-1, 256, 1.5, '10', NaN, true]) {
    assert.throws(() => layer.update({ entries, level }), RangeError);
  }
  assert.throws(() => layer.update({ entries, level: 10, maskedChunks: ['0,0'] }), TypeError);
  assert.throws(() => layer.update({ entries, level: 10, maskedChunks: new Set(['0;0']) }), TypeError);
  assert.equal(layer.snapshot().level, 50, 'state unchanged after refused input');
  accept = true;
  layer.dispose();
});
