// Tests for web/hires-immutable.js (pure opt-in immutable hires client).  node --test test_hires_immutable.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { sha256Hex } from './web/sha256.js';
import { HiresImmutableError, MAX_INDEX_BYTES, MAX_INDEX_ENTRIES, MAX_POINTER_BYTES, immutableObjectUrl,
  loadImmutableHiresIndex, parseImmutableIndex, parseImmutablePointer, pinImmutableObject, readBounded,
} from './web/hires-immutable.js';

const enc = (text) => new TextEncoder().encode(text);
const nodeSha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const A = 'a'.repeat(64);
const T1 = '1'.repeat(12) + 'b'.repeat(52);
const T2 = 'c'.repeat(64);
// The publisher's canonical bytes (json.dumps sort_keys, no spaces).
const indexDoc = (tiles, extra = {}) => ({ atlasSha256: A, format: 'UIE_HIRES_IMMUTABLE_INDEX1',
  rendererIdentity: 'd'.repeat(64), tileSize: 64, tiles, ...extra });
const indexBytes = (doc) => enc(JSON.stringify(doc));
const pointerBytes = (sha) => enc(JSON.stringify({ format: 'UIE_HIRES_POINTER1', indexSha256: sha }));

function store(files) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push({ url, init });
    const file = files[url];
    if (file === undefined) return new Response('missing', { status: 404 });
    if (typeof file === 'number') return new Response('error', { status: file });
    return new Response(file, { status: 200 });
  };
  return { fetch, calls };
}

test('sha256.js agrees with node:crypto on the exact index bytes', () => {
  const bytes = indexBytes(indexDoc({ '1,-2': T1 }));
  assert.equal(sha256Hex(bytes), nodeSha(bytes));
});

test('pointer: exact UIE_HIRES_POINTER1 shape, bounded size', () => {
  assert.equal(parseImmutablePointer(pointerBytes(A)), A);
  for (const text of ['not json', '[]', 'null', JSON.stringify({ format: 'UIE_HIRES_POINTER2', indexSha256: A }),
    JSON.stringify({ format: 'UIE_HIRES_POINTER1', indexSha256: A.toUpperCase() }),
    JSON.stringify({ format: 'UIE_HIRES_POINTER1', indexSha256: A, extra: 1 }),
    JSON.stringify({ format: 'UIE_HIRES_POINTER1' })]) {
    assert.throws(() => parseImmutablePointer(enc(text)), HiresImmutableError, text);
  }
  assert.throws(() => parseImmutablePointer(new Uint8Array([0x7b, 0xff, 0x7d])), /UTF-8/);
  const big = new Uint8Array(MAX_POINTER_BYTES + 1).fill(0x20);
  big.set(pointerBytes(A));
  assert.throws(() => parseImmutablePointer(big), /příliš velký/);
});

test('index: adapted to the legacy shape (sha12 tiles) plus full objects, atlas by content address', () => {
  const bytes = indexBytes(indexDoc({ '1,-2': T1, '-468750,468750': T2 }));
  const index = parseImmutableIndex(bytes, nodeSha(bytes), sha256Hex);
  assert.equal(index.immutable, true);
  assert.equal(index.tileSize, 64);
  assert.equal(index.atlas, `atlases/${A}.png`);
  assert.equal(index.atlasSha256, A);
  assert.equal(index.indexSha256, nodeSha(bytes));
  assert.deepEqual([...index.tiles], [['1,-2', T1.slice(0, 12)], ['-468750,468750', T2.slice(0, 12)]]);
  assert.deepEqual([...index.objects], [['1,-2', T1], ['-468750,468750', T2]]);
});

test('index: SHA-256 of the exact bytes is checked before JSON parsing', () => {
  const garbage = enc('not json at all');
  assert.throws(() => parseImmutableIndex(garbage, A, sha256Hex), /SHA-256/, 'hash refusal, not a JSON error');
  assert.throws(() => parseImmutableIndex(garbage, nodeSha(garbage), sha256Hex), /JSON/);
  const bytes = indexBytes(indexDoc({ '0,0': T1 }));
  const tampered = bytes.slice(); tampered[tampered.length - 3] ^= 1;
  assert.throws(() => parseImmutableIndex(tampered, nodeSha(bytes), sha256Hex), /SHA-256/);
  assert.throws(() => parseImmutableIndex(bytes, 'A'.repeat(64), sha256Hex), /adresa/);
  let hashed = false;
  const oversized = new Uint8Array(MAX_INDEX_BYTES + 1);
  assert.throws(() => parseImmutableIndex(oversized, A, () => { hashed = true; return A; }), /příliš velký/);
  assert.equal(hashed, false, 'oversized input refused before hashing');
});

test('index: bundle shape and coordinates are strict', () => {
  const variants = [
    indexDoc({}, { format: 'UIE_HIRES_INDEX1' }), indexDoc({}, { tileSize: 32 }), indexDoc({}, { extra: 1 }),
    indexDoc({}, { atlasSha256: A.toUpperCase() }), indexDoc({}, { rendererIdentity: 'x' }), indexDoc([]),
    indexDoc({ '01,0': T1 }), indexDoc({ '-0,0': T1 }), indexDoc({ '468751,0': T1 }), indexDoc({ '0,0': T1.slice(0, 12) }),
    indexDoc({ '0,0,0': T1 }), [indexDoc({})],
  ];
  for (const doc of variants) {
    const bytes = indexBytes(doc);
    assert.throws(() => parseImmutableIndex(bytes, nodeSha(bytes), sha256Hex), HiresImmutableError, JSON.stringify(doc).slice(0, 80));
  }
  const tiles = {};
  for (let i = 0; i <= MAX_INDEX_ENTRIES; i += 1) tiles[`${i},0`] = T2;
  const many = indexBytes(indexDoc(tiles));
  assert.throws(() => parseImmutableIndex(many, nodeSha(many), sha256Hex), /příliš mnoho/);
});

test('readBounded: streamed limit, declared length, exact concatenation', async () => {
  const chunks = [enc('abc'), enc('def'), enc('gh')];
  const stream = () => new ReadableStream({ start(c) { for (const x of chunks) c.enqueue(x); c.close(); } });
  assert.deepEqual(await readBounded(new Response(stream()), 8, 'x'), enc('abcdefgh'));
  await assert.rejects(readBounded(new Response(stream()), 7, 'x'), /větší než 7/);
  let pulled = false;
  const lazy = new ReadableStream({ pull() { pulled = true; } }, { highWaterMark: 0 });   // pulls only when read
  await assert.rejects(readBounded(new Response(lazy, { headers: { 'content-length': '100' } }), 10, 'x'), /větší/);
  assert.equal(pulled, false, 'a declared oversize is refused before reading the body');
});

test('loader: pointer no-store, content-addressed index, unchanged pointer keeps the current index', async () => {
  const bytes = indexBytes(indexDoc({ '1,-2': T1 }));
  const sha = nodeSha(bytes);
  const base = '/data/hires/';
  const { fetch, calls } = store({ [`${base}current.json`]: pointerBytes(sha), [`${base}indexes/${sha}.json`]: bytes });
  const index = await loadImmutableHiresIndex({ fetch, baseUrl: base, sha256Hex });
  assert.equal(index.indexSha256, sha);
  assert.deepEqual(calls.map((c) => c.url), [`${base}current.json`, `${base}indexes/${sha}.json`]);
  assert.equal(calls[0].init.cache, 'no-store');
  const again = await loadImmutableHiresIndex({ fetch, baseUrl: base, sha256Hex, current: index });
  assert.equal(again, index, 'same pointer: same object, no index download');
  assert.equal(calls.length, 3);
  const legacyLike = { tiles: new Map(), indexSha256: sha };
  assert.notEqual(await loadImmutableHiresIndex({ fetch, baseUrl: base, sha256Hex, current: legacyLike }), legacyLike);
});

test('loader: every failure throws and leaves the current index untouched (no legacy fallback)', async () => {
  const base = '/data/hires/';
  const good = indexBytes(indexDoc({ '1,-2': T1 }));
  const current = parseImmutableIndex(good, nodeSha(good), sha256Hex);
  const snapshot = JSON.stringify({ ...current, tiles: [...current.tiles], objects: [...current.objects] });
  const next = indexBytes(indexDoc({ '0,0': T2 }));
  const nextSha = nodeSha(next);
  const cases = {
    'no pointer (404)': {},
    'pointer 503': { [`${base}current.json`]: 503 },
    'invalid pointer': { [`${base}current.json`]: enc('{"format":"x"}') },
    'missing index': { [`${base}current.json`]: pointerBytes(nextSha) },
    'index bytes changed': { [`${base}current.json`]: pointerBytes(nextSha), [`${base}indexes/${nextSha}.json`]: good },
    'oversized pointer': { [`${base}current.json`]: new Uint8Array(MAX_POINTER_BYTES + 1).fill(0x20) },
  };
  for (const [label, files] of Object.entries(cases)) {
    const { fetch, calls } = store(files);
    await assert.rejects(loadImmutableHiresIndex({ fetch, baseUrl: base, sha256Hex, current }), HiresImmutableError, label);
    assert.ok(calls.every((c) => !c.url.endsWith('index.json') && !c.url.includes('/tiles/')), `${label}: no legacy request`);
  }
  const { fetch } = store({});
  const error = await loadImmutableHiresIndex({ fetch, baseUrl: base, sha256Hex }).catch((e) => e);
  assert.equal(error.status, 404);
  assert.equal(JSON.stringify({ ...current, tiles: [...current.tiles], objects: [...current.objects] }), snapshot);
});

test('pin: a request uses the full object of the current index only for its sha12 revision', () => {
  const bytes = indexBytes(indexDoc({ '1,-2': T1 }));
  const index = parseImmutableIndex(bytes, nodeSha(bytes), sha256Hex);
  assert.equal(pinImmutableObject(index, '1,-2', T1.slice(0, 12)), T1);
  assert.throws(() => pinImmutableObject(index, '1,-2', 'f'.repeat(12)), HiresImmutableError, 'newer revision in the index');
  assert.throws(() => pinImmutableObject(index, '0,0', T1.slice(0, 12)), HiresImmutableError, 'tile gone');
  assert.throws(() => pinImmutableObject(index, '1,-2', T1.slice(0, 11)), HiresImmutableError, 'malformed sha12');
  assert.throws(() => pinImmutableObject({ tiles: index.tiles, objects: index.objects }, '1,-2', T1.slice(0, 12)),
    HiresImmutableError, 'a legacy index never yields an object');
  assert.throws(() => pinImmutableObject(null, '1,-2', T1.slice(0, 12)), HiresImmutableError);
  assert.equal(immutableObjectUrl('/data/hires/', T1), `/data/hires/objects/${T1}.uih`);
  assert.throws(() => immutableObjectUrl('/data/hires/', '../x'), HiresImmutableError);
});
