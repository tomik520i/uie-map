// Tests for hires-immutable-http.mjs over a real local HTTP server (127.0.0.1, ephemeral port).
//   node --test test_hires_immutable_http.mjs      (from work/uie-map)
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { EventEmitter } from 'node:events';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, symlink, truncate } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { gzipSync, gunzipSync } from 'node:zlib';
import { createImmutableHiresHandler, canonicalJson, validHiresIndex, acceptsGzip,
  MAX_INDEX_BYTES, MAX_POINTER_BYTES, MAX_INDEX_ENTRIES } from './hires-immutable-http.mjs';

const sha = (b) => createHash('sha256').update(b).digest('hex');
const IMMUTABLE = 'public, max-age=31536000, immutable';
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(40, 7)]);

test('capacity stays reserved until the response drains; unknown routes do not consume slots', async () => {
  class Response extends EventEmitter {
    writeHead(status, headers) { this.status = status; this.headers = headers; }
    end(body) { this.body = body; }
  }
  const handler = createImmutableHiresHandler(path.resolve('nonexistent-private-test-store'), { maxConcurrent: 1 });
  const req = {method:'GET',headers:{}};
  const first = new Response();
  assert.equal(await handler(req, first, '/data/hires/current.json'), true);
  const refused = new Response();
  await handler(req, refused, '/data/hires/current.json');
  assert.equal(refused.status, 503); assert.equal(refused.headers['Retry-After'], '1');
  assert.equal(await handler(req, new Response(), '/data/hires/index.json'), false);
  first.emit('finish'); first.emit('close');
  const next = new Response();
  await handler(req, next, '/data/hires/current.json');
  assert.equal(next.headers['Retry-After'], undefined);
  next.emit('close');
  for (const maxConcurrent of [0,5,1.5]) assert.throws(()=>createImmutableHiresHandler(path.resolve('.'), {maxConcurrent}));
});

// A real gzip(UIH1) tile like world_hires.encode_tile: 24 byte header + one opaque quad (52 bytes).
function uih(tx, tz) {
  const raw = Buffer.alloc(24 + 52, 3);
  raw.write('UIH1', 0, 'latin1');
  raw.writeUInt16LE(1, 4); raw.writeUInt16LE(0, 6);
  raw.writeInt32LE(tx * 64, 8); raw.writeInt32LE(tz * 64, 12);
  raw.writeUInt32LE(1, 16); raw.writeUInt32LE(0, 20);
  return { raw, gz: gzipSync(raw, { level: 9 }) };
}
// Same canonical bytes as hires_bundle.publish_index (sort_keys, no spaces).
const indexBytes = (tiles, atlas, identity = 'a'.repeat(64)) => Buffer.from(canonicalJson(
  { format: 'UIE_HIRES_IMMUTABLE_INDEX1', tileSize: 64, rendererIdentity: identity, atlasSha256: atlas, tiles }));
const pointerBytes = (id) => Buffer.from(canonicalJson({ format: 'UIE_HIRES_POINTER1', indexSha256: id }));

async function withStore(fn) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'uie-hires-http-test-'));
  const server = http.createServer(async (req, res) => {
    const handled = await handler(req, res, req.url.split('?')[0]);   // raw path: traversal is not normalised away
    if (!handled) { res.writeHead(418); res.end('not mine'); }
  });
  let handler;
  try {
    for (const sub of ['objects', 'atlases', 'indexes']) await mkdir(path.join(dir, sub));
    await writeFile(path.join(dir, '.uie-hires-bundle-owned'), 'test');
    handler = createImmutableHiresHandler(dir);
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    // Raw http.request: no automatic decompression, so the exact transferred bytes are visible.
    const request = (url, { method = 'GET', headers = { 'accept-encoding': 'gzip, deflate, br' } } = {}) =>
      new Promise((resolve, reject) => {
        const req = http.request({ host: '127.0.0.1', port, path: url, method, headers }, (res) => {
          const chunks = [];
          res.on('data', (c) => chunks.push(c));
          res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
        });
        req.on('error', reject);
        req.end();
      });
    const put = (name, bytes) => writeFile(path.join(dir, ...name.split('/')), bytes);
    const atlas = sha(PNG);
    await put(`atlases/${atlas}.png`, PNG);
    const tile = uih(1, -2);
    const tileId = sha(tile.gz);
    await put(`objects/${tileId}.uih`, tile.gz);
    const index = indexBytes({ '1,-2': tileId }, atlas);
    const indexId = sha(index);
    await put(`indexes/${indexId}.json`, index);
    await fn({ dir, put, request, atlas, tile, tileId, index, indexId, setHandler: (h) => { handler = h; } });
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(dir, { recursive: true, force: true });   // exact mkdtemp-owned directory only
  }
}

async function tree(dir) {
  const out = {};
  for (const sub of ['', 'objects', 'atlases', 'indexes']) {
    for (const name of await readdir(path.join(dir, sub))) {
      const p = path.join(dir, sub, name);
      try { out[p] = sha(await readFile(p)); } catch { out[p] = 'dir'; }
    }
  }
  return out;
}

test('V2 index and layout use pinned hashes; changed layout refused',async()=>{
 await withStore(async({dir,put,request,atlas,tileId})=>{
  await mkdir(path.join(dir,'layouts'));
  const layout=Buffer.from(JSON.stringify({format:'UIE_ATLAS_LAYOUT1',registered:{stone:{uv:[0,0,.1,.1]}}}));
  const layoutSha=sha(layout);await put(`layouts/${layoutSha}.json`,layout);
  const index=Buffer.from(canonicalJson({format:'UIE_HIRES_IMMUTABLE_INDEX2',tileSize:64,rendererIdentity:'a'.repeat(64),atlasSha256:atlas,
   tiles:{'1,-2':tileId},context:{world:'uie',dimension:'overworld',renderer:'uie-offline-mesher/1+hires',caves:false,layoutSha256:layoutSha}}));
  const indexSha=sha(index);await put(`indexes/${indexSha}.json`,index);
  assert.equal((await request(`/data/hires/indexes/${indexSha}.json`)).status,200);
  const response=await request(`/data/hires/layouts/${layoutSha}.json`);
  assert.equal(response.status,200);assert.deepEqual(response.body,layout);assert.match(response.headers['content-type'],/application\/json/);
  await put(`layouts/${layoutSha}.json`,Buffer.from('{}'));
  assert.equal((await request(`/data/hires/layouts/${layoutSha}.json`)).status,409);
 });
});

test('objects: exact gzip bytes, decoded once into UIH1, immutable; HEAD; identity and encoding refusals', async () => {
  await withStore(async ({ dir, put, request, tile, tileId }) => {
    const before = await tree(dir);
    const url = `/data/hires/objects/${tileId}.uih`;
    const ok = await request(url);
    assert.equal(ok.status, 200);
    assert.deepEqual(ok.body, tile.gz, 'exact stored bytes on the wire');
    assert.equal(ok.headers['content-encoding'], 'gzip');
    assert.equal(ok.headers['content-type'], 'application/octet-stream');
    assert.equal(ok.headers['cache-control'], IMMUTABLE);
    assert.equal(ok.headers.etag, `"${tileId}"`);
    assert.equal(ok.headers.vary, 'Accept-Encoding');
    assert.equal(Number(ok.headers['content-length']), tile.gz.length);
    const decoded = gunzipSync(ok.body);   // what the browser hands to decodeHiresTile after Content-Encoding
    assert.deepEqual(decoded, tile.raw);
    assert.equal(decoded.subarray(0, 4).toString('latin1'), 'UIH1', 'one decode yields the raw tile, no second gzip layer');
    const head = await request(url, { method: 'HEAD' });
    assert.equal(head.status, 200); assert.equal(head.body.length, 0);
    assert.equal(Number(head.headers['content-length']), tile.gz.length);
    const noGzip = await request(url, { headers: { 'accept-encoding': 'identity, gzip;q=0' } });
    assert.equal(noGzip.status, 406); assert.equal(noGzip.headers['cache-control'], 'no-store');
    assert.equal((await request(url, { headers: {} })).status, 200, 'absent Accept-Encoding accepts any coding');
    assert.deepEqual(await tree(dir), before, 'read-only: nothing created, changed or removed');

    // Truncated, replaced and invalid content under a content address: never a cacheable 200.
    await truncate(path.join(dir, 'objects', `${tileId}.uih`), tile.gz.length - 4);
    const truncated = await request(url);
    assert.equal(truncated.status, 409); assert.equal(truncated.headers['cache-control'], 'no-store');
    await put(`objects/${tileId}.uih`, uih(2, 2).gz);
    assert.equal((await request(url)).status, 409);
    const notGzip = Buffer.from('UIH1 but not gzip at all......'); const ng = sha(notGzip);
    await put(`objects/${ng}.uih`, notGzip);
    assert.equal((await request(`/data/hires/objects/${ng}.uih`)).status, 409);
    const missing = await request(`/data/hires/objects/${'e'.repeat(64)}.uih`);
    assert.equal(missing.status, 404); assert.equal(missing.headers['cache-control'], 'no-store');
    assert.ok(!missing.body.toString().includes(dir), 'no filesystem path leaks');
  });
});

test('atlases: exact PNG bytes, image/png, no content coding; non-PNG with a correct hash is refused', async () => {
  await withStore(async ({ put, request, atlas }) => {
    const ok = await request(`/data/hires/atlases/${atlas}.png`);
    assert.equal(ok.status, 200); assert.deepEqual(ok.body, PNG);
    assert.equal(ok.headers['content-type'], 'image/png');
    assert.equal(ok.headers['content-encoding'], undefined);
    assert.equal(ok.headers['cache-control'], IMMUTABLE);
    const fake = Buffer.from('not a png, just bytes of sufficient length'); const id = sha(fake);
    await put(`atlases/${id}.png`, fake);
    assert.equal((await request(`/data/hires/atlases/${id}.png`)).status, 409);
  });
});

test('indexes: hash before parse, bounded canonical shape, gzip transfer of large indexes decodes to exact bytes', async () => {
  await withStore(async ({ put, request, atlas, tileId, index, indexId }) => {
    const url = `/data/hires/indexes/${indexId}.json`;
    const ok = await request(url);
    assert.equal(ok.status, 200); assert.deepEqual(ok.body, index);
    assert.equal(ok.headers['content-type'], 'application/json; charset=utf-8');
    assert.equal(ok.headers['content-encoding'], undefined, 'small index sent as is');
    assert.equal(ok.headers['cache-control'], IMMUTABLE);
    const head = await request(url, { method: 'HEAD' });
    assert.equal(head.status, 200); assert.equal(head.body.length, 0);

    const tiles = {};
    for (let i = 0; i < 200; i += 1) tiles[`${i},${-i}`] = tileId;
    const big = indexBytes(tiles, atlas); const bigId = sha(big);
    await put(`indexes/${bigId}.json`, big);
    const zipped = await request(`/data/hires/indexes/${bigId}.json`);
    assert.equal(zipped.status, 200); assert.equal(zipped.headers['content-encoding'], 'gzip');
    assert.deepEqual(gunzipSync(zipped.body), big, 'the decoded entity is exactly the hashed bytes');
    const plain = await request(`/data/hires/indexes/${bigId}.json`, { headers: { 'accept-encoding': 'identity' } });
    assert.equal(plain.headers['content-encoding'], undefined); assert.deepEqual(plain.body, big);

    await put(`indexes/${indexId}.json`, indexBytes({ '0,0': tileId }, atlas));
    const mismatch = await request(url);
    assert.equal(mismatch.status, 409); assert.equal(mismatch.headers['cache-control'], 'no-store');
    const base = { format: 'UIE_HIRES_IMMUTABLE_INDEX1', tileSize: 64, rendererIdentity: 'a'.repeat(64), atlasSha256: atlas, tiles: {} };
    const invalid = [
      canonicalJson({ ...base, format: 'UIE_HIRES_IMMUTABLE_INDEX2' }), canonicalJson({ ...base, tileSize: 32 }),
      canonicalJson({ ...base, extra: 1 }), canonicalJson({ ...base, atlasSha256: atlas.toUpperCase() }),
      canonicalJson({ ...base, tiles: { '01,0': tileId } }), canonicalJson({ ...base, tiles: { '-0,0': tileId } }),
      canonicalJson({ ...base, tiles: { '468751,0': tileId } }), canonicalJson({ ...base, tiles: [] }),
      JSON.stringify(base),                                              // valid shape, not the canonical bytes
      canonicalJson(base).replace('{"atlasSha256"', '{"tiles":{},"atlasSha256"'),   // duplicate key
      'not json',
    ];
    for (const text of invalid) {
      const bytes = Buffer.from(text); const id = sha(bytes);
      await put(`indexes/${id}.json`, bytes);
      const r = await request(`/data/hires/indexes/${id}.json`);
      assert.equal(r.status, 409, text); assert.equal(r.headers['cache-control'], 'no-store');
    }
    const oversized = Buffer.alloc(MAX_INDEX_BYTES + 1, 0x20); const oid = sha(oversized);
    await put(`indexes/${oid}.json`, oversized);
    assert.equal((await request(`/data/hires/indexes/${oid}.json`)).status, 404, 'oversized file refused unread');
  });
});

test('current.json: canonical no-store pointer, never dangling, strict shape and size', async () => {
  await withStore(async ({ put, request, indexId, index }) => {
    const url = '/data/hires/current.json';
    const absent = await request(url);
    assert.equal(absent.status, 404); assert.equal(absent.headers['cache-control'], 'no-store');
    await put('current.json', pointerBytes(indexId));
    const ok = await request(url);
    assert.equal(ok.status, 200); assert.equal(ok.headers['cache-control'], 'no-store');
    assert.deepEqual(ok.body, pointerBytes(indexId));
    assert.equal(ok.headers['content-type'], 'application/json; charset=utf-8');
    assert.equal((await request(url, { method: 'HEAD' })).body.length, 0);
    await put('current.json', pointerBytes('f'.repeat(64)));
    assert.equal((await request(url)).status, 503, 'dangling pointer');
    for (const text of ['not json', '[]', 'null', JSON.stringify({ format: 'UIE_HIRES_POINTER2', indexSha256: indexId }),
      canonicalJson({ format: 'UIE_HIRES_POINTER1', indexSha256: indexId.toUpperCase() }),
      canonicalJson({ format: 'UIE_HIRES_POINTER1', indexSha256: indexId, extra: true }),
      JSON.stringify({ indexSha256: indexId, format: 'UIE_HIRES_POINTER1' }),       // not canonical order
      `${pointerBytes(indexId)} `]) {
      await put('current.json', Buffer.from(text));
      const r = await request(url);
      assert.equal(r.status, 503, text); assert.equal(r.headers['cache-control'], 'no-store');
    }
    await put('current.json', Buffer.concat([pointerBytes(indexId), Buffer.alloc(MAX_POINTER_BYTES, 32)]));
    assert.equal((await request(url)).status, 503, 'oversized pointer refused unread');
    assert.ok(index.length > 0);
  });
});

test('routing: methods, malformed addresses, traversal, unknown and legacy paths left to the caller', async () => {
  await withStore(async ({ request, tileId, indexId }) => {
    for (const method of ['POST', 'PUT', 'DELETE']) {
      const r = await request(`/data/hires/objects/${tileId}.uih`, { method });
      assert.equal(r.status, 405); assert.equal(r.headers.allow, 'GET, HEAD');
    }
    assert.equal((await request('/data/hires/current.json', { method: 'POST' })).status, 405);
    for (const bad of [`/data/hires/objects/${tileId.toUpperCase()}.uih`, `/data/hires/objects/${tileId}.png`,
      `/data/hires/indexes/${indexId}.uih`, '/data/hires/objects/../current.json', '/data/hires/objects/%2e%2e/x',
      `/data/hires/objects/${tileId}.uih/`, `/data/hires/objects/sub/${tileId}.uih`, '/data/hires/atlases/']) {
      const r = await request(bad);
      assert.equal(r.status, 400, bad); assert.equal(r.headers['cache-control'], 'no-store');
    }
    for (const other of ['/data/hires/index.json', '/data/hires/tiles/1_-2.uih', '/data/hires/atlas.png',
      '/data/hires/current.json/', '/data/collision/current.json', '/']) {
      assert.equal((await request(other)).status, 418, `${other} is not handled`);
    }
  });
});

test('symlinked entries and folders are refused, also when they point at valid content', async (t) => {
  await withStore(async ({ dir, put, request, tile, tileId, setHandler }) => {
    const outside = await mkdtemp(path.join(os.tmpdir(), 'uie-hires-http-outside-'));
    try {
      await writeFile(path.join(outside, `${tileId}.uih`), tile.gz);
      const other = uih(3, 3); const otherId = sha(other.gz);
      await writeFile(path.join(outside, `${otherId}.uih`), other.gz);
      try {
        await symlink(path.join(outside, `${otherId}.uih`), path.join(dir, 'objects', `${otherId}.uih`));
      } catch (error) {
        t.skip(`symlinks unavailable: ${error.code}`);
        return;
      }
      assert.equal((await request(`/data/hires/objects/${otherId}.uih`)).status, 404, 'symlinked file escaping the store');
      const inner = uih(4, 4); const innerId = sha(inner.gz);
      await put(`objects/inner.bin`, inner.gz);
      await symlink(path.join(dir, 'objects', 'inner.bin'), path.join(dir, 'objects', `${innerId}.uih`));
      assert.equal((await request(`/data/hires/objects/${innerId}.uih`)).status, 404, 'symlink inside the store');
      const linked = path.join(dir, 'linked');
      await symlink(outside, linked, 'dir');
      setHandler(createImmutableHiresHandler(linked));
      assert.equal((await request(`/data/hires/objects/${tileId}.uih`)).status, 503, 'store without marker');
    } finally {
      await rm(outside, { recursive: true, force: true });   // exact mkdtemp-owned directory only
    }
  });
});

test('store configuration: absent store is not handled, relative path refused, missing marker unavailable', async () => {
  const none = createImmutableHiresHandler(null);
  assert.equal(await none({ method: 'GET' }, {}, '/data/hires/current.json'), false);
  for (const bad of ['relative/store', '', 42]) assert.throws(() => createImmutableHiresHandler(bad), TypeError);
  await withStore(async ({ dir, request, tileId }) => {
    await rm(path.join(dir, '.uie-hires-bundle-owned'));
    assert.equal((await request(`/data/hires/objects/${tileId}.uih`)).status, 503);
    assert.equal((await request('/data/hires/current.json')).status, 503);
  });
});

test('validators share the publisher limits', () => {
  const tiles = {};
  for (let i = 0; i <= MAX_INDEX_ENTRIES; i += 1) tiles[`${i},0`] = 'a'.repeat(64);
  const doc = { format: 'UIE_HIRES_IMMUTABLE_INDEX1', tileSize: 64, rendererIdentity: 'b'.repeat(64), atlasSha256: 'c'.repeat(64), tiles };
  assert.equal(validHiresIndex(doc), false, 'entry limit');
  delete tiles[`${MAX_INDEX_ENTRIES},0`];
  assert.equal(validHiresIndex(doc), true);
  assert.equal(acceptsGzip(undefined), true);
  assert.equal(acceptsGzip('gzip;q=0'), false);
  assert.equal(acceptsGzip('br, *;q=0.5'), true);
  assert.equal(acceptsGzip(''), false);
});
