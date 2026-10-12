import test from 'node:test';
import assert from 'node:assert/strict';
import { chooseCollision } from './web/collision-choice.js';
import { blockShape } from './web/walk-physics.js';

const GEO = 'a'.repeat(64), ATLAS = 'c'.repeat(64);
const visible = { visibleGeometrySha: GEO.slice(0, 12), visibleAtlasSha: ATLAS };
const pair = (changes = {}) => ({ x: 0, z: 0, geometrySha256: GEO, atlasSha256: ATLAS, ...changes });
function source(block, { withPair = true, pairValue = pair() } = {}) {
  const calls = [];
  const out = { calls, blockAt: (x, y, z) => { calls.push([x, y, z]); return typeof block === 'function' ? block(x, y, z) : block; } };
  if (withPair) out.pair = pairValue;
  return out;
}
const choose = (src, strict, extra = {}) => chooseCollision({ source: src, strict, x: 1, y: 64, z: 2, ...visible, ...extra }, blockShape);

test('exact loaded blocks, including real air and partial shapes, become blockShape', () => {
  for (const [name, meta] of [['minecraft:air', 0], ['minecraft:stone', 0], ['minecraft:stone_slab', 8],
    ['minecraft:oak_stairs', 2], ['minecraft:fence', 0], ['minecraft:water', 0]]) {
    for (const strict of [true, false]) {
      const src = source({ name, meta });
      const result = choose(src, strict);
      assert.deepEqual(result, { handled: true, shape: blockShape(name, meta), mode: 'exact' }, `${name} strict=${strict}`);
      assert.deepEqual(src.calls, [[1, 64, 2]]);
    }
  }
  assert.equal(choose(source({ name: 'minecraft:air', meta: 0 }), false).shape.kind, blockShape('minecraft:air').kind);
});

test('missing data waits in strict mode and falls back in automatic mode, never inventing air', () => {
  for (const missing of [undefined, null]) {
    assert.deepEqual(choose(source(missing), true), { handled: true, shape: undefined, mode: 'waiting' });
    assert.deepEqual(choose(source(missing), false), { handled: false, shape: undefined, mode: 'fallback' });
  }
  assert.deepEqual(chooseCollision({ source: null, strict: true }, blockShape).mode, 'waiting');
  assert.deepEqual(chooseCollision({ source: null, strict: false }, blockShape), { handled: false, shape: undefined, mode: 'fallback' });
  assert.equal(chooseCollision({ source: {}, strict: false }, blockShape).mode, 'fallback');
  assert.equal(chooseCollision({}, blockShape).mode, 'fallback', 'strict defaults to automatic');
});

test('a pair of another revision or atlas never reaches blockAt', () => {
  const cases = [pair({ geometrySha256: 'b'.repeat(64) }), pair({ atlasSha256: 'd'.repeat(64) }), pair({ geometrySha256: null }),
    pair({ geometrySha256: 7 }), pair({ atlasSha256: undefined })];
  for (const pairValue of cases) {
    for (const strict of [true, false]) {
      const src = source({ name: 'minecraft:stone', meta: 0 }, { pairValue });
      assert.equal(choose(src, strict).mode, strict ? 'waiting' : 'fallback');
      assert.deepEqual(src.calls, [], 'blockAt must not be called for a mismatching pair');
    }
  }
  for (const extra of [{ visibleGeometrySha: undefined }, { visibleGeometrySha: '' }, { visibleAtlasSha: null },
    { visibleGeometrySha: 'b'.repeat(12) }]) {
    const src = source({ name: 'minecraft:stone', meta: 0 });
    assert.equal(choose(src, false, extra).mode, 'fallback');
    assert.deepEqual(src.calls, []);
  }
});

test('a world/follower without pair relies on its own live check', () => {
  const src = source((x, y, z) => (x === 1 ? { name: 'minecraft:stone', meta: 0 } : undefined), { withPair: false });
  assert.equal(choose(src, false).mode, 'exact');
  assert.equal(chooseCollision({ source: src, strict: false, x: 99, y: 64, z: 2, ...visible }, blockShape).mode, 'fallback');
  assert.equal(chooseCollision({ source: src, strict: true, x: 99, y: 64, z: 2 }, blockShape).mode, 'waiting');
  assert.equal(src.calls.length, 3, 'without a pair the source decides on every call');
});

test('malformed or throwing block data counts as unavailable', () => {
  const malformed = [{ name: '', meta: 0 }, { name: 7, meta: 0 }, { meta: 0 }, { name: 'minecraft:stone', meta: 16 },
    { name: 'minecraft:stone', meta: -1 }, { name: 'minecraft:stone', meta: 1.5 }, { name: 'minecraft:stone', meta: '0' },
    { name: 'minecraft:stone' }, 'minecraft:stone', 0];
  for (const block of malformed) {
    assert.equal(choose(source(block), true).mode, 'waiting', JSON.stringify(block));
    assert.equal(choose(source(block), false).mode, 'fallback', JSON.stringify(block));
  }
  const throwing = source(() => { throw new Error('decode failed'); });
  assert.equal(choose(throwing, false).mode, 'fallback');
  const badShape = () => { throw new Error('unknown'); };
  assert.equal(chooseCollision({ source: source({ name: 'x:y', meta: 0 }), strict: false, ...visible }, badShape).mode, 'fallback');
  assert.equal(chooseCollision({ source: source({ name: 'x:y', meta: 0 }), strict: true, ...visible }, () => undefined).mode, 'waiting');
  assert.throws(() => chooseCollision({ source: null }, null), TypeError);
});

test('results are immutable and calls keep no state', () => {
  const waiting = choose(source(undefined), true);
  assert.throws(() => { 'use strict'; waiting.handled = false; }, TypeError);
  const src = source({ name: 'minecraft:stone', meta: 0 });
  const first = choose(src, false), second = choose(src, false);
  assert.deepEqual(first, second);
  assert.notEqual(first, second, 'exact results are fresh objects');
});

test('neighbor lookup revalidates the pair before returning data to a shape resolver', () => {
  const src=source({name:'minecraft:stone',meta:0});
  let reader;
  assert.equal(chooseCollision({source:src,...visible},(name,meta,read)=>{
    reader=read;return blockShape(name,meta);
  }).mode,'exact');
  assert.equal(reader(2,64,2).name,'minecraft:stone');
  const before=src.calls.length;
  src.pair=pair({atlasSha256:'f'.repeat(64)});
  assert.equal(reader(2,64,2),undefined);
  assert.equal(src.calls.length,before,'wrong revision never enters blockAt, also for neighbors');
});
