// Tests for web/hires.js (UIH1 tiles from world_hires.py). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_hires.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('./web/hires.js', import.meta.url), 'utf8');
const { HiresError, decodeHiresTile, decodeHiresTileCompact, parseHiresIndex, spriteIndex, surfaceHeights, tileChunks, tilesAround } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

// Uncompressed UIH1 bytes written by the Python encoder (world_hires.encode_tile) for one opaque quad at
// x -63.75..-62.75, y 70..71, z -1..0 and one translucent quad, tile origin (-64, -64).
const FIXTURE = '5549483101000000c0ffffffc0ffffff0100000001000000200100248020a00100248020a001722400212001802400219a193333cc4c3333cc4c9a199a199a19ff8040ff8040ff8040ff804000030020001c80030020001c80037220801c00038020801c9a193333cc4c3333cc4c9a199a199a19ff8040ff8040ff8040ff8040';
const buffer = () => { const b = Buffer.from(FIXTURE, 'hex'); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length); };
const near = (a, b, eps) => Math.abs(a - b) <= eps;

test('decodes the Python encoder output (cross-language contract)', () => {
  const tile = decodeHiresTile(buffer());
  assert.deepEqual(tile.origin, [-64, -64]);
  const [opaque, translucent] = tile.groups;
  assert.equal(opaque.quads, 1);
  assert.equal(translucent.quads, 1);
  const want = [-63.75, 70, -1, -62.75, 70, -1, -62.75, 70.8889, 0, -63.75, 71, 0];
  want.forEach((v, i) => assert.ok(near(opaque.positions[i], v, 0.5 / 128), `${i}: ${opaque.positions[i]} vs ${v}`));
  assert.ok(near(opaque.uvs[0], 0.1, 1e-4) && near(opaque.uvs[1], 0.2, 1e-4));
  assert.ok(near(opaque.colors[0], 1, 1e-6) && near(opaque.colors[1], 0.5, 1 / 255));
  assert.deepEqual([...opaque.indices], [0, 1, 2, 0, 2, 3]);
  assert.ok(near(translucent.positions[0], -60, 1e-6));
});

test('compact decode gives the same world positions through offset + scale', () => {
  const full = decodeHiresTile(buffer());
  const compact = decodeHiresTileCompact(buffer());
  assert.deepEqual(compact.offset, [-66, -2, -66]);
  compact.groups.forEach((g, gi) => {
    const f = full.groups[gi];
    assert.ok(g.positions instanceof Uint16Array && g.colors instanceof Uint8Array && g.indices instanceof Uint16Array);
    for (let i = 0; i < g.positions.length; i += 1) {
      const world = g.positions[i] * compact.scale + compact.offset[i % 3];
      assert.ok(near(world, f.positions[i], 1e-6), `${gi}/${i}`);
    }
    assert.ok(near(g.uvs[0] / 65535, f.uvs[0], 1e-6));   // full decode stores float32
  });
  assert.equal(compact.groups[0].colors[0], 255, 'white stays white in linear');
  assert.ok(compact.groups[0].colors[1] < 128, 'sRGB 0.5 is darker in linear');
});

test('surface heights take horizontal opaque faces per column', () => {
  // A synthetic compact tile: one top face at y 70 over column (1, 2) of a tile at origin (64, -64).
  const s = 1 / 128;
  const q = (v, o) => Math.round((v - o + 2) * 128);
  const verts = [[65, 70, -62], [66, 70, -62], [66, 70, -61], [65, 70, -61]];
  const positions = new Uint16Array(verts.flatMap(([x, y, z]) => [q(x, 64), q(y, 0), q(z, -64)]));
  const tile = { origin: [64, -64], offset: [62, -2, -66], scale: s,
    groups: [{ positions, quads: 1 }, { positions: new Uint16Array(0), quads: 0 }] };
  const h = surfaceHeights(tile, 64);
  assert.equal(h[2 * 64 + 1], 70);
  assert.equal(h[0], -1);
  // A vertical face is ignored.
  const wall = new Uint16Array([[65, 70, -62], [66, 70, -62], [66, 71, -62], [65, 71, -62]].flatMap(([x, y, z]) => [q(x, 64), q(y, 0), q(z, -64)]));
  assert.ok(surfaceHeights({ ...tile, groups: [{ positions: wall, quads: 1 }] }, 64).every((v) => v === -1));
});

test('sprite lookup names the texture under a UV point', () => {
  const at = spriteIndex({ registered: {
    stone: { iconName: 'stone', state: 'placed', uv: [0.0, 0.0, 0.0625, 0.0625] },
    'biomesoplenty:dirt_loamy': { iconName: 'biomesoplenty:dirt_loamy', state: 'placed', uv: [0.5, 0.25, 0.5625, 0.3125] },
    ghost: { iconName: 'ghost', state: 'missing', uv: [0.9, 0.9, 1, 1] } } });
  assert.equal(at(0.03, 0.03), 'stone');
  assert.equal(at(0.53, 0.28), 'biomesoplenty:dirt_loamy');
  assert.equal(at(0.95, 0.95), null, 'unplaced sprites are ignored');
  assert.equal(at(0.3, 0.7), null);
});

test('rejects foreign or truncated data', () => {
  assert.throws(() => decodeHiresTile(new ArrayBuffer(8)), HiresError);
  const b = new Uint8Array(buffer());
  b[0] = 0x58;
  assert.throws(() => decodeHiresTile(b.buffer), HiresError);
  assert.throws(() => decodeHiresTile(buffer().slice(0, 40)), HiresError);
});

test('index parsing, nearest tiles and covered chunks', () => {
  const index = parseHiresIndex({ format: 'UIE_HIRES_INDEX1', tileSize: 64,
    tiles: { '0,0': 'aaaaaaaaaaaa', '1,0': 'bbbbbbbbbbbb', '-1,-1': 'cccccccccccc', '9,9': 'dddddddddddd' } });
  assert.deepEqual(tilesAround(index, 10, 10, 100), ['0,0', '-1,-1', '1,0']);
  assert.deepEqual(tilesAround(index, 10, 10, 2000).length, 4);
  assert.equal(tileChunks('-1,0', 64).length, 16);
  assert.deepEqual(tileChunks('-1,0', 64).slice(0, 2), ['-4,0', '-3,0']);
  assert.throws(() => parseHiresIndex({ format: 'X' }), HiresError);
  assert.throws(() => parseHiresIndex({ format: 'UIE_HIRES_INDEX1', tileSize: 64, tiles: { 'a,b': 'x' } }), HiresError);
});
