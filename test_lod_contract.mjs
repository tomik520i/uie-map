// Tests for web/lod-contract.js. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_lod_contract.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const dataUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const switchUrl = dataUrl(readFileSync(new URL('./web/generation-switch.js', import.meta.url), 'utf8'));
const contract = await import(dataUrl(readFileSync(new URL('./web/lod-contract.js', import.meta.url), 'utf8')
  .replace("from './generation-switch.js'", `from '${switchUrl}'`)));
const { LodContractError, meshBytesFromCounts, validateLodManifest, validateLodChunk } = contract;

const H = (c) => c.repeat(64);
const ATLAS = H('f');

// Realistic small fixture: two detail chunks with content-addressed names and their coarse files.
function fixture() {
  return {
    format: 1, atlasSha256: ATLAS,
    chunks: [{ x: 0, z: -1, file: `chunks/0_-1-${H('1')}.json` }, { x: -3, z: 2, file: `chunks/-3_2-${H('2')}.json` }],
    lod: {
      format: 'UIE_LOD1', method: 'planar-unit-v1', colorSteps: 8, atlasSha256: ATLAS,
      chunks: [
        { x: -3, z: 2, file: `lod/-3_2-${H('4')}.json`, sourceSha256: H('2'),
          coarse: { parts: [{ vertices: 0, indices: 0 }, { vertices: 3, indices: 3 }] },
          detail: { parts: [{ vertices: 8, indices: 12 }] } },
        { x: 0, z: -1, file: `lod/0_-1-${H('3')}.json`, sourceSha256: H('1'),
          coarse: { parts: [{ vertices: 4, indices: 6 }, { vertices: 3, indices: 3 }, { vertices: 3, indices: 3 }] },
          detail: { parts: [{ vertices: 12, indices: 18 }, { vertices: 3, indices: 3 }] } },
      ],
    },
  };
}

const quad = () => ({ positions: [0, 64, -16, 16, 64, -16, 16, 64, 0, 0, 64, 0], uvs: Array(8).fill(0),
  colors: [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5], indices: [0, 1, 2, 0, 2, 3] });
const tri = (extra = {}) => ({ positions: [0, 1, 0, 1, 1, 0, 0, 1, 1], uvs: [0.1, 0.1, 0.2, 0.1, 0.1, 0.2],
  colors: Array(9).fill(1), indices: [0, 1, 2], ...extra });

function coarseDoc() {
  return { format: 'UIE_LOD_CHUNK1', x: 0, z: -1, sourceSha256: H('1'), atlasSha256: ATLAS, colorSteps: 8,
    untextured: quad(), textured: tri(), transparentParts: [tri()] };
}

const rejects = (fn, label) => assert.throws(fn, LodContractError, label);

test('byte accounting matches chunkBufferBytes, including the index width boundary', () => {
  assert.equal(meshBytesFromCounts([{ vertices: 4, indices: 6 }]), 2 * (4 * 44 + 6 * 2));
  assert.equal(meshBytesFromCounts([]), 0);
  assert.equal(meshBytesFromCounts([{ vertices: 10, indices: 0 }, { vertices: 0, indices: 0 }]), 0, 'empty parts build no mesh');
  assert.equal(meshBytesFromCounts([{ vertices: 65535, indices: 3 }]), 2 * (65535 * 44 + 3 * 2), 'Uint16 up to 65535');
  assert.equal(meshBytesFromCounts([{ vertices: 65536, indices: 3 }]), 2 * (65536 * 44 + 3 * 4), 'Uint32 from 65536');
  assert.equal(meshBytesFromCounts([{ vertices: 3, indices: 3 }, { vertices: 4, indices: 6 }]),
    2 * (3 * 44 + 6) + 2 * (4 * 44 + 12));
  const bad = [[{ vertices: -1, indices: 0 }], [{ vertices: 1.5, indices: 0 }], [{ vertices: 4, indices: 4 }],
    [{ vertices: 0, indices: 3 }], [{ vertices: 3, indices: 3, extra: 1 }], [{ vertices: '3', indices: 3 }],
    [{ vertices: 2 ** 53, indices: 0 }], [null], 'parts',
    [{ vertices: 2 ** 47, indices: 3 }, { vertices: 2 ** 47, indices: 3 }]];   // last: total beyond safe range
  for (const parts of bad) rejects(() => meshBytesFromCounts(parts), JSON.stringify(parts));
});

test('valid manifest gives frozen, deep-copied descriptors for every detail chunk', () => {
  const manifest = fixture();
  const result = validateLodManifest(manifest);
  assert.deepEqual([...result.keys()], ['-3,2', '0,-1']);
  const d = result.get('0,-1');
  assert.deepEqual({ ...d, coarseParts: [...d.coarseParts], detailParts: [...d.detailParts] }, {
    id: '0,-1', x: 0, z: -1, file: `lod/0_-1-${H('3')}.json`, sourceSha256: H('1'), coarseSha256: H('3'),
    atlasSha256: ATLAS, colorSteps: 8,
    coarseBytes: 2 * (4 * 44 + 12) + 2 * (3 * 44 + 6) * 2, detailBytes: 2 * (12 * 44 + 36) + 2 * (3 * 44 + 6),
    coarseParts: [{ vertices: 4, indices: 6 }, { vertices: 3, indices: 3 }, { vertices: 3, indices: 3 }],
    detailParts: [{ vertices: 12, indices: 18 }, { vertices: 3, indices: 3 }],
  });
  assert.equal(result.get('-3,2').coarseBytes, 2 * (3 * 44 + 6), 'an empty untextured part costs nothing');
  assert.ok(Object.isFrozen(d) && Object.isFrozen(d.coarseParts) && Object.isFrozen(d.coarseParts[0]));
  const before = JSON.stringify(manifest);
  manifest.lod.chunks[1].coarse.parts[0].vertices = 999;     // caller mutation after validation
  assert.equal(d.coarseParts[0].vertices, 4, 'validated counts are copies');
  manifest.lod.chunks[1].coarse.parts[0].vertices = 4;
  assert.equal(JSON.stringify(manifest), before, 'input not mutated by validation');
});

test('absent lod is null; present but malformed lod throws', () => {
  const { lod, ...plain } = fixture();
  assert.equal(validateLodManifest(plain), null);
  assert.equal(validateLodManifest({ ...plain, lod: undefined }), null);
  for (const value of [null, false, [], 'UIE_LOD1', {}]) {
    rejects(() => validateLodManifest({ ...plain, lod: value }), String(value));
  }
  rejects(() => validateLodManifest(null));
  rejects(() => validateLodManifest([]));
});

test('malformed lod blocks, paths, bindings and coverage are rejected', () => {
  const cases = {
    format: (m) => { m.lod.format = 'UIE_LOD2'; },
    method: (m) => { m.lod.method = 'heightfield'; },
    colorSteps: (m) => { m.lod.colorSteps = 16; },
    'extra lod key': (m) => { m.lod.extra = 1; },
    'atlas mismatch': (m) => { m.atlasSha256 = H('e'); },
    'atlas missing on manifest': (m) => { delete m.atlasSha256; },
    'atlas uppercase': (m) => { m.lod.atlasSha256 = ATLAS.toUpperCase(); m.atlasSha256 = m.lod.atlasSha256; },
    'missing coverage': (m) => { m.lod.chunks.pop(); },
    duplicate: (m) => { m.lod.chunks.push({ ...m.lod.chunks[0] }); },
    'unknown chunk': (m) => { m.lod.chunks[0].x = 9; m.lod.chunks[0].file = `lod/9_2-${H('4')}.json`; },
    'file coords': (m) => { m.lod.chunks[0].file = `lod/-3_3-${H('4')}.json`; },
    'file -0': (m) => { m.lod.chunks[1].file = `lod/-0_-1-${H('3')}.json`; },
    'file leading zero': (m) => { m.lod.chunks[1].file = `lod/00_-1-${H('3')}.json`; },
    'file kind': (m) => { m.lod.chunks[1].file = `chunks/0_-1-${H('3')}.json`; },
    'file gz': (m) => { m.lod.chunks[1].file = `lod/0_-1-${H('3')}.json.gz`; },
    'file hash case': (m) => { m.lod.chunks[1].file = `lod/0_-1-${H('A')}.json`; },
    'source mismatch': (m) => { m.lod.chunks[1].sourceSha256 = H('2'); },
    'source case': (m) => { m.chunks[0].file = `chunks/0_-1-${H('a')}.json`; m.lod.chunks[1].sourceSha256 = H('A'); },
    'one coarse part': (m) => { m.lod.chunks[1].coarse.parts = [{ vertices: 4, indices: 6 }]; },
    'no detail parts': (m) => { m.lod.chunks[1].detail.parts = []; },
    'bad count': (m) => { m.lod.chunks[1].detail.parts[0].indices = 17; },
    'extra entry key': (m) => { m.lod.chunks[1].bytes = 1; },
    'extra counts key': (m) => { m.lod.chunks[1].coarse.bytes = 1; },
    'unsafe x': (m) => { m.lod.chunks[1].x = 2 ** 53; },
    'detail not content-addressed': (m) => { m.chunks[0].file = 'chunk_0_-1.json'; },
    'detail duplicate': (m) => { m.chunks.push({ ...m.chunks[0] }); },
    'detail coords string': (m) => { m.chunks[0].x = '0'; },
    'chunks not array': (m) => { m.lod.chunks = {}; },
  };
  for (const [label, change] of Object.entries(cases)) {
    const m = fixture();
    change(m);
    rejects(() => validateLodManifest(m), label);
  }
});

test('coarse chunk validation binds the descriptor and the exact counts without mutating', () => {
  const descriptor = validateLodManifest(fixture()).get('0,-1');
  const doc = coarseDoc();
  const before = JSON.stringify(doc);
  assert.equal(validateLodChunk(doc, descriptor), doc);
  assert.equal(JSON.stringify(doc), before);
  const optional = coarseDoc();
  optional.textured.transparent = true;
  optional.untextured.transparent = false;
  optional.transparentParts[0].transparent = true;
  assert.equal(validateLodChunk(optional, descriptor), optional);
  // Valid empty untextured part (no recognized squares).
  const empty = validateLodManifest(fixture()).get('-3,2');
  const emptyDoc = { ...coarseDoc(), x: -3, z: 2, sourceSha256: H('2'),
    untextured: { positions: [], uvs: [], colors: [], indices: [] }, transparentParts: [] };
  assert.equal(validateLodChunk(emptyDoc, empty), emptyDoc);
});

test('malformed coarse chunks are rejected', () => {
  const descriptor = validateLodManifest(fixture()).get('0,-1');
  const cases = {
    format: (d) => { d.format = 'UIE_LOD_CHUNK2'; },
    'x binding': (d) => { d.x = 1; },
    'source binding': (d) => { d.sourceSha256 = H('2'); },
    'atlas binding': (d) => { d.atlasSha256 = H('e'); },
    colorSteps: (d) => { d.colorSteps = 16; },
    'extra key': (d) => { d.extra = 1; },
    'missing transparentParts': (d) => { delete d.transparentParts; },
    'untextured transparent': (d) => { d.untextured.transparent = true; },
    'textured transparent string': (d) => { d.textured.transparent = 'yes'; },
    'transparent part false': (d) => { d.transparentParts[0].transparent = false; },
    'nested parts': (d) => { d.textured.transparentParts = []; },
    'non-finite position': (d) => { d.untextured.positions[0] = Infinity; },
    'NaN colour': (d) => { d.textured.colors[0] = NaN; },
    'string uv': (d) => { d.textured.uvs[0] = '0'; },
    'index out of range': (d) => { d.untextured.indices[5] = 4; },
    'non-integer index': (d) => { d.untextured.indices[0] = 0.5; },
    'uv length': (d) => { d.textured.uvs.pop(); },
    'positions length': (d) => { d.textured.positions.push(0); },
    'partial triangle': (d) => { d.textured.indices.push(0); },
    'counts: extra triangle': (d) => { d.untextured.indices.push(0, 1, 2); },
    'counts: extra part': (d) => { d.transparentParts.push(tri()); },
    'counts: missing part': (d) => { d.transparentParts = []; },
    'part not object': (d) => { d.textured = null; },
  };
  for (const [label, change] of Object.entries(cases)) {
    const doc = coarseDoc();
    change(doc);
    rejects(() => validateLodChunk(doc, descriptor), label);
  }
  rejects(() => validateLodChunk(coarseDoc(), { ...descriptor }), 'unfrozen descriptor');
  rejects(() => validateLodChunk(coarseDoc(), null));
});
