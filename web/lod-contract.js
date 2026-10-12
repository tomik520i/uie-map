// LOD publication contract for the main viewer: pure validation and byte accounting. No DOM, fetch or
// three.js. Hashes of response bytes are checked by the caller before these functions run.
//
// meshBytesFromCounts(parts)          -> reserved CPU+GPU bytes, same formula as app.js chunkBufferBytes
// validateLodManifest(manifest)       -> null when manifest.lod is absent, else Map 'x,z' -> frozen descriptor
// validateLodChunk(doc, descriptor)   -> doc (unchanged) when it is the coarse file the descriptor promises
//
// Frozen schema for this step (UIE_LOD1 / planar-unit-v1, colorSteps 8):
//   manifest.atlasSha256 = manifest.lod.atlasSha256
//   manifest.lod = { format, method, colorSteps, atlasSha256, chunks: [{ x, z, file: 'lod/<x>_<z>-<sha>.json',
//                    sourceSha256, coarse: { parts: [counts...] }, detail: { parts: [counts...] } }] }
//   counts = { vertices, indices }; coarse parts = [untextured, textured, ...transparentParts]
//   coarse file = { format: 'UIE_LOD_CHUNK1', x, z, sourceSha256, atlasSha256, colorSteps,
//                   untextured: part, textured: part, transparentParts: [part...] }

import { contentHash } from './generation-switch.js';

export const LOD_FORMAT = 'UIE_LOD1';
export const LOD_METHOD = 'planar-unit-v1';
export const LOD_CHUNK_FORMAT = 'UIE_LOD_CHUNK1';
export const COLOR_STEPS = 8;
const HEX64 = /^[0-9a-f]{64}$/;
const INT = '(0|-?[1-9][0-9]*)';  // canonical integers, as generation-switch.js
const LOD_FILE = new RegExp(`^lod/${INT}_${INT}-([0-9a-f]{64})\\.json$`);
const PART_KEYS = new Set(['positions', 'uvs', 'colors', 'indices', 'transparent']);

export class LodContractError extends Error {}

const fail = (text) => { throw new LodContractError(text); };
const plain = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const count = (v) => Number.isSafeInteger(v) && v >= 0;
const sameKeys = (obj, keys) => {
  const own = Object.keys(obj);
  return own.length === keys.length && keys.every((k) => Object.hasOwn(obj, k));
};

/** Validated, deep-frozen copy of [{ vertices, indices }] (exact keys). */
function countParts(parts, where, min = 0) {
  if (!Array.isArray(parts) || parts.length < min) fail(`${where} must be an array of at least ${min} parts`);
  return Object.freeze(parts.map((p, i) => {
    if (!plain(p) || !sameKeys(p, ['vertices', 'indices'])) fail(`${where}[${i}] must be exactly { vertices, indices }`);
    const { vertices, indices } = p;
    if (!count(vertices) || !count(indices) || indices % 3 !== 0 || (vertices === 0 && indices > 0)) {
      fail(`${where}[${i}]: counts must be safe integers >= 0, indices a multiple of 3, none without vertices`);
    }
    return Object.freeze({ vertices, indices });
  }));
}

/** Sum over parts with indices of 2 * (vertices * 44 + indices * (vertices > 65535 ? 4 : 2)). */
export function meshBytesFromCounts(parts) {
  let total = 0;
  for (const { vertices, indices } of countParts(parts, 'parts')) {
    if (indices === 0) continue;   // app.js builds no mesh for an empty part
    total += 2 * (vertices * 44 + indices * (vertices > 65535 ? 4 : 2));
    if (!Number.isSafeInteger(total)) fail('mesh bytes exceed the safe integer range');
  }
  return total;
}

export function validateLodManifest(manifest) {
  if (!plain(manifest)) fail('manifest must be an object');
  if (manifest.lod === undefined) return null;
  const lod = manifest.lod;
  if (!plain(lod) || !sameKeys(lod, ['format', 'method', 'colorSteps', 'atlasSha256', 'chunks'])) {
    fail('lod must be exactly { format, method, colorSteps, atlasSha256, chunks }');
  }
  if (lod.format !== LOD_FORMAT || lod.method !== LOD_METHOD || lod.colorSteps !== COLOR_STEPS) {
    fail(`lod must be ${LOD_FORMAT} / ${LOD_METHOD} with colorSteps ${COLOR_STEPS}`);
  }
  if (typeof lod.atlasSha256 !== 'string' || !HEX64.test(lod.atlasSha256) || manifest.atlasSha256 !== lod.atlasSha256) {
    fail('lod.atlasSha256 must be 64 lowercase hex and equal manifest.atlasSha256');
  }
  if (!Array.isArray(manifest.chunks) || !Array.isArray(lod.chunks)) fail('chunks and lod.chunks must be arrays');

  const detail = new Map();
  manifest.chunks.forEach((c, i) => {
    if (!plain(c) || !Number.isSafeInteger(c.x) || !Number.isSafeInteger(c.z)) fail(`chunks[${i}] needs safe integer x/z`);
    const key = `${c.x},${c.z}`;
    if (detail.has(key)) fail(`chunks[${i}] repeats ${key}`);
    let sha;
    try {
      sha = contentHash(c.file, 'chunks', c.x, c.z);
    } catch (err) {
      fail(`chunks[${i}]: ${err.message}`);
    }
    detail.set(key, sha);
  });

  const result = new Map();
  lod.chunks.forEach((e, i) => {
    const where = `lod.chunks[${i}]`;
    if (!plain(e) || !sameKeys(e, ['x', 'z', 'file', 'sourceSha256', 'coarse', 'detail'])) {
      fail(`${where} must be exactly { x, z, file, sourceSha256, coarse, detail }`);
    }
    if (!Number.isSafeInteger(e.x) || !Number.isSafeInteger(e.z)) fail(`${where} needs safe integer x/z`);
    const key = `${e.x},${e.z}`;
    if (!detail.has(key)) fail(`${where}: ${key} is not a detail chunk`);
    if (result.has(key)) fail(`${where} repeats ${key}`);
    const match = typeof e.file === 'string' ? LOD_FILE.exec(e.file) : null;
    if (!match || Number(match[1]) !== e.x || Number(match[2]) !== e.z) {
      fail(`${where}.file must be lod/${e.x}_${e.z}-<sha256>.json`);
    }
    if (typeof e.sourceSha256 !== 'string' || !HEX64.test(e.sourceSha256) || e.sourceSha256 !== detail.get(key)) {
      fail(`${where}.sourceSha256 must equal the detail chunk content hash`);
    }
    for (const name of ['coarse', 'detail']) {
      if (!plain(e[name]) || !sameKeys(e[name], ['parts'])) fail(`${where}.${name} must be exactly { parts }`);
    }
    const coarseParts = countParts(e.coarse.parts, `${where}.coarse.parts`, 2);
    const detailParts = countParts(e.detail.parts, `${where}.detail.parts`, 1);
    result.set(key, Object.freeze({
      id: key, x: e.x, z: e.z, file: e.file, sourceSha256: e.sourceSha256, coarseSha256: match[3],
      atlasSha256: lod.atlasSha256, colorSteps: lod.colorSteps,
      coarseBytes: meshBytesFromCounts(coarseParts), detailBytes: meshBytesFromCounts(detailParts),
      coarseParts, detailParts,
    }));
  });
  if (result.size !== detail.size) {
    const missing = [...detail.keys()].filter((k) => !result.has(k));
    fail(`lod.chunks must cover every detail chunk; missing ${missing.slice(0, 5).join(' ')}`);
  }
  return result;
}

/** Counts of one mesh part after full array validation. role: 'untextured' | 'textured' | 'transparent'. */
function partCounts(part, where, role) {
  if (!plain(part) || !Object.keys(part).every((k) => PART_KEYS.has(k))) {
    fail(`${where} may only contain positions, uvs, colors, indices and transparent`);
  }
  const { positions, uvs, colors, indices } = part;
  if (![positions, uvs, colors, indices].every(Array.isArray)) fail(`${where} needs positions/uvs/colors/indices arrays`);
  if ('transparent' in part) {
    const ok = role === 'textured' ? typeof part.transparent === 'boolean'
      : role === 'untextured' ? part.transparent === false : part.transparent === true;
    if (!ok) fail(`${where}.transparent is not allowed here`);
  }
  if (positions.length % 3 !== 0) fail(`${where}.positions length is not a multiple of 3`);
  const vertices = positions.length / 3;
  if (uvs.length !== vertices * 2 || colors.length !== vertices * 3 || indices.length % 3 !== 0) {
    fail(`${where}: array lengths do not match ${vertices} vertices`);
  }
  for (const [name, values] of [['positions', positions], ['uvs', uvs], ['colors', colors]]) {
    if (!values.every((v) => typeof v === 'number' && Number.isFinite(v))) fail(`${where}.${name} must be finite numbers`);
  }
  if (!indices.every((i) => Number.isSafeInteger(i) && i >= 0 && i < vertices)) fail(`${where}: index outside 0..${vertices - 1}`);
  return { vertices, indices: indices.length };
}

export function validateLodChunk(doc, descriptor) {
  if (!plain(descriptor) || !Object.isFrozen(descriptor) || !Array.isArray(descriptor.coarseParts)) {
    fail('descriptor must come from validateLodManifest');
  }
  if (!plain(doc) || !sameKeys(doc, ['format', 'x', 'z', 'sourceSha256', 'atlasSha256', 'colorSteps', 'untextured',
    'textured', 'transparentParts'])) {
    fail(`${descriptor.file}: not a ${LOD_CHUNK_FORMAT} document with the expected fields`);
  }
  if (doc.format !== LOD_CHUNK_FORMAT) fail(`${descriptor.file}: format must be ${LOD_CHUNK_FORMAT}`);
  for (const key of ['x', 'z', 'sourceSha256', 'atlasSha256', 'colorSteps']) {
    if (doc[key] !== descriptor[key]) fail(`${descriptor.file}: ${key} does not match the manifest`);
  }
  if (!Array.isArray(doc.transparentParts)) fail(`${descriptor.file}: transparentParts must be an array`);
  const counts = [partCounts(doc.untextured, 'untextured', 'untextured'), partCounts(doc.textured, 'textured', 'textured'),
    ...doc.transparentParts.map((p, i) => partCounts(p, `transparentParts[${i}]`, 'transparent'))];
  const expected = descriptor.coarseParts;
  if (counts.length !== expected.length
    || counts.some((c, i) => c.vertices !== expected[i].vertices || c.indices !== expected[i].indices)) {
    fail(`${descriptor.file}: part counts do not match the manifest`);
  }
  if (meshBytesFromCounts(counts) !== descriptor.coarseBytes) fail(`${descriptor.file}: bytes do not match the manifest`);
  return doc;
}
