// Tests for web/block-info.js (UIE_BLOCKS1 decoding, lookup, cell picking). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_block_info.mjs
// Expected values are written from the data contract, not derived from the implementation.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// web/ has no package.json, so load the real module source as ESM independent of Node's .js detection.
const source = readFileSync(new URL('./web/block-info.js', import.meta.url), 'utf8');
const { BlockDataError, LruCache, blockAt, chunkCoord, decodeBlockChunk, findBlocks, pickCell } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const REGISTRY = { 0: 'minecraft:air', 1: 'minecraft:stone', 7: 'minecraft:bedrock', 18: 'minecraft:leaves' };

// Cropped edge chunk: chunk (-1,-1) spans X -16..-1, Z -16..-1; the export covers X -3..-2, Z -16.
function cropped(overrides = {}) {
  return {
    format: 'UIE_BLOCKS1', x: -3, z: -16, width: 2, depth: 1, height: 256, registry: REGISTRY,
    // column x=-3: y0..63 stone, y64 leaves meta 5, y65..255 air; column x=-2: y0..254 air, y255 bedrock meta 15
    runs: [[64, 1, 0], [1, 18, 5], [191, 0, 0], [255, 0, 0], [1, 7, 15]],
    ...overrides,
  };
}

const rejects = (data, cx, cz, pattern, label) => assert.throws(() => decodeBlockChunk(data, cx, cz, 'blocks/t.json'),
  (err) => err instanceof BlockDataError && pattern.test(err.message) && /blocks\/t\.json/.test(err.message), label);

test('RLE decoding across columns, Y levels and negative coordinates', () => {
  const chunk = decodeBlockChunk(cropped(), -1, -1);
  assert.deepEqual(blockAt(chunk, -3, 0, -16), { id: 1, meta: 0, name: 'minecraft:stone' });
  assert.deepEqual(blockAt(chunk, -3, 63, -16), { id: 1, meta: 0, name: 'minecraft:stone' });
  assert.deepEqual(blockAt(chunk, -3, 64, -16), { id: 18, meta: 5, name: 'minecraft:leaves' });
  assert.deepEqual(blockAt(chunk, -3, 65, -16), { id: 0, meta: 0, name: 'minecraft:air' });
  assert.deepEqual(blockAt(chunk, -3, 255, -16), { id: 0, meta: 0, name: 'minecraft:air' });
  assert.deepEqual(blockAt(chunk, -2, 0, -16), { id: 0, meta: 0, name: 'minecraft:air' });
  assert.deepEqual(blockAt(chunk, -2, 254, -16), { id: 0, meta: 0, name: 'minecraft:air' });
  assert.deepEqual(blockAt(chunk, -2, 255, -16), { id: 7, meta: 15, name: 'minecraft:bedrock' });
});

test('lookups outside the cropped area or height return null', () => {
  const chunk = decodeBlockChunk(cropped(), -1, -1);
  for (const [x, y, z] of [[-4, 10, -16], [-1, 10, -16], [-3, 10, -15], [-3, 10, -17], [-3, -1, -16], [-3, 256, -16],
    [-3, 1.5, -16]]) {
    assert.equal(blockAt(chunk, x, y, z), null, `${x},${y},${z}`);
  }
});

test('voxel order is z-major, then x, then y', () => {
  // Two rows (z 48 and 49) of chunk (2,3); the only non-air voxel is x=33, z=49, y=7 at index (1*16+1)*256+7 = 4359.
  const data = { format: 'UIE_BLOCKS1', x: 32, z: 48, width: 16, depth: 2, height: 256, registry: { 0: 'minecraft:air', 5: 'mod:x' },
    runs: [[4359, 0, 0], [1, 5, 3], [16 * 2 * 256 - 4360, 0, 0]] };
  const chunk = decodeBlockChunk(data, 2, 3);
  assert.deepEqual(blockAt(chunk, 33, 7, 49), { id: 5, meta: 3, name: 'mod:x' });
  for (const [x, y, z] of [[33, 7, 48], [33, 8, 49], [32, 7, 49], [34, 7, 49], [33, 6, 49]]) {
    assert.equal(blockAt(chunk, x, y, z).id, 0, `${x},${y},${z}`);
  }
});

test('findBlocks lists cells of the named blocks with world coordinates, top first, bounded', () => {
  const chunk = decodeBlockChunk(cropped(), -1, -1);
  assert.deepEqual(findBlocks(chunk, new Set(['minecraft:leaves', 'minecraft:bedrock'])), [
    { x: -3, y: 64, z: -16, id: 18, name: 'minecraft:leaves', meta: 5 },
    { x: -2, y: 255, z: -16, id: 7, name: 'minecraft:bedrock', meta: 15 },
  ]);
  assert.equal(findBlocks(chunk, new Set(['minecraft:stone'])).length, 50, 'default limit');
  assert.equal(findBlocks(chunk, new Set(['minecraft:stone']), 100).length, 64);
  assert.deepEqual(findBlocks(chunk, new Set(['minecraft:stone']), 2).map((c) => c.y), [63, 62]);
  assert.deepEqual(findBlocks(chunk, new Set(['mod:absent'])), []);
  assert.deepEqual(findBlocks(chunk, new Set(['minecraft:stone']), 0), []);
});

test('full chunk with IDs above 4095 in positive coordinates', () => {
  const data = { format: 'UIE_BLOCKS1', x: 32, z: 48, width: 16, depth: 16, height: 256,
    registry: { 12965: 'hbm:tile.hadron_coil_gold' }, runs: [[65536, 12965, 9]] };
  const chunk = decodeBlockChunk(data, 2, 3);
  assert.deepEqual(blockAt(chunk, 47, 200, 63), { id: 12965, meta: 9, name: 'hbm:tile.hadron_coil_gold' });
  assert.equal(blockAt(chunk, 48, 200, 63), null);
});

test('dimensions and chunk alignment are validated', () => {
  const cases = {
    'width 0': [{ width: 0 }, /width\/depth/], 'width 17': [{ width: 17 }, /width\/depth/],
    'width fraction': [{ width: 1.5 }, /width\/depth/], 'depth 0': [{ depth: 0 }, /width\/depth/],
    'height 255': [{ height: 255 }, /height/], 'x string': [{ x: '-3' }, /x\/z/], 'z bool': [{ z: true }, /x\/z/],
    'crosses chunk X': [{ x: -1, width: 2 }, /neleží v chunku/], 'before chunk Z': [{ z: -17 }, /neleží v chunku/],
    format: [{ format: 'UIE_BLOCKS2' }, /format/],
  };
  for (const [label, [overrides, pattern]] of Object.entries(cases)) rejects(cropped(overrides), -1, -1, pattern, label);
  rejects(cropped(), 0, -1, /neleží v chunku 0,-1/, 'file of another chunk');
  for (const root of [null, [], 'x']) rejects(root, -1, -1, /kořen/, JSON.stringify(root));
});

test('registry is validated', () => {
  for (const registry of [null, [], 'x']) rejects(cropped({ registry }), -1, -1, /registry není objekt/, JSON.stringify(registry));
  for (const key of ['-1', '65536', '018', 'a', '1.5', '']) {
    rejects(cropped({ registry: { ...REGISTRY, [key]: 'mod:y' } }), -1, -1, /neplatné ID/, `key ${key}`);
  }
  for (const name of ['', 5, null]) rejects(cropped({ registry: { ...REGISTRY, 1: name } }), -1, -1, /nemá jméno/, `name ${name}`);
  const { 18: _leaves, ...withoutLeaves } = REGISTRY;
  rejects(cropped({ registry: withoutLeaves }), -1, -1, /id 18 chybí v registry/, 'used id missing');
});

test('runs are validated: count, id, meta, shape and total', () => {
  const tail = [[191, 0, 0], [255, 0, 0], [1, 7, 15]];
  const cases = {
    'count 0': [[[0, 1, 0], [64, 1, 0], [1, 18, 5], ...tail], /count/],
    'count negative': [[[-1, 1, 0], [65, 1, 0], [1, 18, 5], ...tail], /count/],
    'count fraction': [[[63.5, 1, 0], [0.5, 1, 0], [1, 18, 5], ...tail], /count/],
    'id 65536': [[[64, 65536, 0], [1, 18, 5], ...tail], /id mimo/],
    'id negative': [[[64, -1, 0], [1, 18, 5], ...tail], /id mimo/],
    'meta 16': [[[64, 1, 16], [1, 18, 5], ...tail], /meta/],
    'meta negative': [[[64, 1, -1], [1, 18, 5], ...tail], /meta/],
    'meta fraction': [[[64, 1, 0.5], [1, 18, 5], ...tail], /meta/],
    'short run': [[[64, 1], [1, 18, 5], ...tail], /\[count, id, meta\]/],
    'total short': [[[64, 1, 0], [1, 18, 5], [191, 0, 0], [255, 0, 0]], /neodpovídá 512/],
    'total long': [[[64, 1, 0], [1, 18, 5], ...tail, [1, 0, 0]], /přesahuje 512/],
    'huge count': [[[Number.MAX_SAFE_INTEGER, 0, 0]], /přesahuje/],
  };
  for (const [label, [runs, pattern]] of Object.entries(cases)) rejects(cropped({ runs }), -1, -1, pattern, label);
  rejects(cropped({ runs: {} }), -1, -1, /runs není pole/, 'runs object');
});

test('pickCell selects the block that emitted the face, from either side of it', () => {
  const up = { x: 0, y: 1, z: 0 };
  const down = { x: 0, y: -1, z: 0 };
  const east = { x: 1, y: 0, z: 0 };
  const west = { x: -1, y: 0, z: 0 };
  // Top face of block Y63 lies on the plane Y64 with normal +Y: seen from above or from below, it is Y63.
  for (const direction of [down, up]) {
    assert.deepEqual(pickCell({ x: 0.5, y: 64, z: 0.5 }, up, direction), { x: 0, y: 63, z: 0 });
    assert.deepEqual(pickCell({ x: -2.5, y: 65, z: -7.2 }, up, direction), { x: -3, y: 64, z: -8 });
  }
  // Bottom face of block Y63 lies on the plane Y63 with normal -Y.
  for (const direction of [up, down]) {
    assert.deepEqual(pickCell({ x: 0.5, y: 63, z: 0.5 }, down, direction), { x: 0, y: 63, z: 0 });
  }
  // East face of block X9 lies on X10 with normal +X; west face of block X-3 lies on X-3 with normal -X.
  for (const direction of [west, east]) {
    assert.deepEqual(pickCell({ x: 10, y: 3.2, z: 4.9 }, east, direction), { x: 9, y: 3, z: 4 });
    assert.deepEqual(pickCell({ x: -3, y: 10.5, z: -0.5 }, west, direction), { x: -3, y: 10, z: -1 });
  }
  // Without a direction argument the result is the same.
  assert.deepEqual(pickCell({ x: 0.5, y: 64, z: 0.5 }, up), { x: 0, y: 63, z: 0 });
});

test('decodeBlockChunk rejects non-integer or unsafe chunk coordinates', () => {
  const edge = 2 ** 49;  // edge * 16 === 2 ** 53, beyond Number.MAX_SAFE_INTEGER
  const bad = [[NaN, -1], [-1, NaN], [0.5, -1], [-1, 1.5], ['-1', -1], [-1, null], [undefined, -1], [true, -1],
    [Infinity, -1], [-1, -Infinity], [Number.MAX_SAFE_INTEGER, -1], [edge, -1], [edge - 1, -1], [-edge, -1], [-1, -edge]];
  for (const [cx, cz] of bad) rejects(cropped(), cx, cz, /nejsou bezpečná celá čísla/, `${String(cx)},${String(cz)}`);
  // The largest chunk whose min and max block coordinates are safe integers is accepted as a coordinate.
  const top = edge - 2;
  const data = { format: 'UIE_BLOCKS1', x: top * 16, z: 0, width: 1, depth: 1, height: 256, registry: { 0: 'minecraft:air' },
    runs: [[256, 0, 0]] };
  assert.equal(blockAt(decodeBlockChunk(data, top, 0), top * 16, 0, 0).id, 0);
});

test('chunkCoord floors negative coordinates', () => {
  assert.deepEqual([-17, -16, -1, 0, 15, 16].map(chunkCoord), [-2, -1, -1, 0, 0, 1]);
});

test('LruCache evicts the least recently used entry', () => {
  const cache = new LruCache(2);
  cache.set('a', 1);
  cache.set('b', 2);
  assert.equal(cache.get('a'), 1);
  cache.set('c', 3);
  assert.equal(cache.get('b'), undefined);
  assert.equal(cache.get('a'), 1);
  assert.equal(cache.get('c'), 3);
  assert.equal(cache.size, 2);
});
