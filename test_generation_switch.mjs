// Tests for web/generation-switch.js. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_generation_switch.mjs
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('./web/generation-switch.js', import.meta.url), 'utf8');
const gs = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const ID = 'c'.repeat(64);
const H = 'a'.repeat(64);
const bytes = (text) => new TextEncoder().encode(text);

test('generation base URL is exact and only for 64 lowercase hex ids', () => {
  assert.equal(gs.generationBase(ID), `./data/generations/${ID}/`);
  assert.equal(gs.GENERATIONS_URL, './data/generations/');
  for (const bad of [ID.toUpperCase(), 'c'.repeat(63), `${'c'.repeat(63)}g`, '', null, 42, `../${ID}`]) {
    assert.throws(() => gs.generationBase(bad), gs.GenerationError, String(bad));
  }
});

test('content hash is read only from exact publisher names that agree with kind and coordinates', () => {
  assert.equal(gs.contentHash(`chunks/0_0-${H}.json`), H);
  assert.equal(gs.contentHash(`chunks/-3_12-${H}.json`, 'chunks', -3, 12), H);
  assert.equal(gs.contentHash(`blocks/7_-1-${H}.json`, 'blocks', 7, -1), H);
  const refused = [
    `chunks/1_0-${H}.json`,               // wrong x
    `chunks/0_1-${H}.json`,               // wrong z
    `blocks/0_0-${H}.json`,               // wrong kind
    `chunks/00_0-${H}.json`, `chunks/-0_0-${H}.json`, `chunks/+1_0-${H}.json`,
    `chunks/0_0-${H.toUpperCase()}.json`, `chunks/0_0-${H.slice(1)}.json`,
    `chunks/0_0-${H}.json.gz`, `chunks/0_0-${H}.JSON`, `./chunks/0_0-${H}.json`,
    `chunks/../chunks/0_0-${H}.json`, `x/chunks/0_0-${H}.json`, 'chunk_0_0.json',
    `blocks/${H}/chunk_0_0.json`, null, 7,
  ];
  for (const file of refused) {
    assert.throws(() => gs.contentHash(file, 'chunks', 0, 0), gs.GenerationError, String(file));
  }
  assert.throws(() => gs.contentHash(`chunks/9007199254740993_0-${H}.json`), gs.GenerationError, 'unsafe integer');
});

test('generation manifest files: all chunk and block names must be content-addressed', () => {
  const manifest = {
    chunks: [{ x: 0, z: 0, file: `chunks/0_0-${H}.json` }, { x: -1, z: 2, file: `chunks/-1_2-${'b'.repeat(64)}.json` }],
    blockInfo: { format: 'UIE_BLOCKS1', chunks: [{ x: 0, z: 0, file: `blocks/0_0-${'d'.repeat(64)}.json` }] },
  };
  const { blockIndex } = gs.validateGenerationFiles(manifest);
  assert.deepEqual([...blockIndex], [['0,0', `blocks/0_0-${'d'.repeat(64)}.json`]]);
  assert.equal(gs.validateGenerationFiles({ chunks: manifest.chunks }).blockIndex, null);
  assert.equal(gs.validateGenerationFiles({ chunks: manifest.chunks, blockInfo: { chunks: [] } }).blockIndex, null);
  assert.throws(() => gs.validateGenerationFiles({ chunks: [{ x: 0, z: 0, file: 'chunk_0_0.json' }] }), gs.GenerationError,
    'legacy names cannot be reused by key');
  assert.throws(() => gs.validateGenerationFiles({ chunks: manifest.chunks,
    blockInfo: { chunks: [{ x: 1, z: 0, file: `blocks/0_0-${H}.json` }] } }), gs.GenerationError);
});

test('verify-before-decode: bytes must match the hash before UTF-8 and JSON decoding', () => {
  const good = bytes('{"format":1,"name":"žluť"}');
  assert.deepEqual(gs.decodeVerifiedJson(good, sha(good), sha, 'm.json'), { format: 1, name: 'žluť' });
  let hashed = 0;
  const counting = (b) => { hashed += 1; return sha(b); };
  // A mismatching hash is refused even when the bytes are valid JSON; nothing is parsed.
  assert.throws(() => gs.decodeVerifiedJson(good, H, counting, 'm.json'), /SHA-256 odpovědi neodpovídá/);
  assert.equal(hashed, 1);
  const tampered = bytes('{"format":2,"name":"žluť"}');
  assert.throws(() => gs.decodeVerifiedJson(tampered, sha(good), sha, 'm.json'), /SHA-256/);
  // Hash matches but the content is not decodable: still refused.
  const latin = new Uint8Array([0x7b, 0xff, 0x7d]);
  assert.throws(() => gs.decodeVerifiedJson(latin, sha(latin), sha, 'm.json'), /UTF-8/);
  const notJson = bytes('{nope');
  assert.throws(() => gs.decodeVerifiedJson(notJson, sha(notJson), sha, 'm.json'), /JSON/);
  // Expected hash and input shape are strict.
  assert.throws(() => gs.decodeVerifiedJson(good, sha(good).toUpperCase(), sha, 'm.json'), /64 malých hex/);
  assert.throws(() => gs.decodeVerifiedJson('{"format":1}', sha(good), sha, 'm.json'), /bajty/);
  assert.throws(() => gs.decodeVerifiedJson(good.buffer, sha(good), sha, 'm.json'), /bajty/);
  // A hash function returning another case is a mismatch, never normalised.
  assert.throws(() => gs.decodeVerifiedJson(good, sha(good), (b) => sha(b).toUpperCase(), 'm.json'), /SHA-256/);
});

test('switch counts: retained by identical file key, removed and added', () => {
  const next = [{ file: 'a' }, { file: 'b' }, { file: 'c' }];
  assert.deepEqual(gs.diffEntries(['a', 'x', 'y'], next), { retained: 1, removed: 2, added: 2 });
  assert.deepEqual(gs.diffEntries([], next), { retained: 0, removed: 0, added: 3 });
  assert.deepEqual(gs.diffEntries(['a', 'b', 'c'], next), { retained: 3, removed: 0, added: 0 });
  assert.deepEqual(gs.diffEntries(['a', 'a'], []), { retained: 0, removed: 1, added: 0 });
});
