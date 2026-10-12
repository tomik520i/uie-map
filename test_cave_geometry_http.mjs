// Tests for cave-geometry-http.mjs over a real local HTTP server and a temporary owned immutable store.
//   node --test test_cave_geometry_http.mjs      (from work/uie-map)
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { gzipSync, gunzipSync } from 'node:zlib';
import { canonicalJson, OWNED_MARKER } from './hires-immutable-http.mjs';
import { createCaveGeometryHandler, mapCaveGeometryPath } from './cave-geometry-http.mjs';

const sha = (b) => createHash('sha256').update(b).digest('hex');
const IMMUTABLE = 'public, max-age=31536000, immutable';
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(40, 7)]);

function uih(tx, tz) {                       // a real gzip(UIH1) tile with one opaque quad
  const raw = Buffer.alloc(24 + 52, 3);
  raw.write('UIH1', 0, 'latin1');
  raw.writeUInt16LE(1, 4); raw.writeUInt16LE(0, 6);
  raw.writeInt32LE(tx * 64, 8); raw.writeInt32LE(tz * 64, 12);
  raw.writeUInt32LE(1, 16); raw.writeUInt32LE(0, 20);
  return { raw, gz: gzipSync(raw, { level: 9 }) };
}

// Temporary owned store with one published generation; `handlerFor(dir)` builds the handler under test.
async function withServer(fn, handlerFor = (dir) => createCaveGeometryHandler(dir)) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'uie-cave-geometry-test-'));
  let handler;
  const server = http.createServer(async (req, res) => {
    const handled = await handler(req, res, req.url.split('?')[0]);   // raw path: '..' is not normalised away
    if (!handled) { res.writeHead(418); res.end('not handled'); }
  });
  try {
    for (const sub of ['objects', 'atlases', 'indexes', 'layouts']) await mkdir(path.join(dir, sub));
    await writeFile(path.join(dir, OWNED_MARKER), 'test');
    const put = (name, bytes) => writeFile(path.join(dir, ...name.split('/')), bytes);
    const atlas = sha(PNG);
    await put(`atlases/${atlas}.png`, PNG);
    const tile = uih(2, -3);
    const tileId = sha(tile.gz);
    await put(`objects/${tileId}.uih`, tile.gz);
    const index = Buffer.from(canonicalJson({ format: 'UIE_HIRES_IMMUTABLE_INDEX1', tileSize: 64,
      rendererIdentity: 'a'.repeat(64), atlasSha256: atlas, tiles: { '2,-3': tileId } }));
    const indexId = sha(index);
    await put(`indexes/${indexId}.json`, index);
    const pointer = Buffer.from(canonicalJson({ format: 'UIE_HIRES_POINTER1', indexSha256: indexId }));
    await put('current.json', pointer);
    handler = handlerFor(dir);
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const request = (url, { method = 'GET', headers = { 'accept-encoding': 'gzip' } } = {}) =>
      new Promise((resolve, reject) => {
        const req = http.request({ host: '127.0.0.1', port, path: url, method, headers }, (res) => {
          const chunks = [];
          res.on('data', (c) => chunks.push(c));
          res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
        });
        req.on('error', reject);
        req.end();
      });
    await fn({ dir, put, request, atlas, tile, tileId, index, indexId, pointer });
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(dir, { recursive: true, force: true });   // exact mkdtemp-owned directory only
  }
}

test('path mapping: exact prefix and known folders only', () => {
  const id = 'a'.repeat(64);
  assert.equal(mapCaveGeometryPath('/data/cave-geometry/current.json'), '/data/hires/current.json');
  assert.equal(mapCaveGeometryPath(`/data/cave-geometry/objects/${id}.uih`), `/data/hires/objects/${id}.uih`);
  assert.equal(mapCaveGeometryPath(`/data/cave-geometry/layouts/${id}.json`), `/data/hires/layouts/${id}.json`);
  for (const p of ['/data/hires/current.json', `/data/hires/objects/${id}.uih`, '/data/cave-geometry',
    '/data/cave-geometry/', '/data/cave-geometryX/current.json', '/data/cave-geometry-x/current.json',
    '/data/cave-geometry/current.json/', '/data/cave-geometry/index.json', '/data/cave-geometry/status.json',
    '/data/cave-geometry/tiles/0_0.uih', '/data/cave-geometry/atlas.png', '/data/cave-geometry/../hires/current.json',
    `/data/cave-geometry/objects/sub/${id}.uih`, '/DATA/cave-geometry/current.json', null, 42]) {
    assert.equal(mapCaveGeometryPath(p), null, String(p));
  }
});

test('pointer, index, object and atlas are served through the cave namespace with inherited semantics', async () => {
  await withServer(async ({ request, pointer, index, indexId, tile, tileId, atlas }) => {
    const current = await request('/data/cave-geometry/current.json');
    assert.equal(current.status, 200);
    assert.deepEqual(current.body, pointer);
    assert.equal(current.headers['cache-control'], 'no-store');
    const idx = await request(`/data/cave-geometry/indexes/${indexId}.json`, { headers: {} });
    assert.equal(idx.status, 200);
    assert.deepEqual(idx.body, index);
    assert.equal(idx.headers['cache-control'], IMMUTABLE);
    const object = await request(`/data/cave-geometry/objects/${tileId}.uih`);
    assert.equal(object.status, 200);
    assert.equal(object.headers['content-encoding'], 'gzip');
    assert.deepEqual(gunzipSync(object.body), tile.raw);
    const png = await request(`/data/cave-geometry/atlases/${atlas}.png`);
    assert.equal(png.status, 200);
    assert.equal(png.headers['content-type'], 'image/png');
    const head = await request('/data/cave-geometry/current.json', { method: 'HEAD' });
    assert.equal(head.status, 200);
    assert.equal(head.body.length, 0);
    const post = await request('/data/cave-geometry/current.json', { method: 'POST' });
    assert.equal(post.status, 405);
    assert.equal(post.headers.allow, 'GET, HEAD');
  });
});

test('surface and look-alike paths are never handled', async () => {
  await withServer(async ({ request, tileId, indexId }) => {
    for (const p of ['/data/hires/current.json', `/data/hires/objects/${tileId}.uih`, `/data/hires/indexes/${indexId}.json`,
      '/data/cave-geometryX/current.json', '/data/cave-geometry', '/data/cave-geometry/', '/data/cave-geometry/index.json',
      '/data/cave-geometry/tiles/2_-3.uih', '/data/cave-geometry/../hires/current.json',
      `/data/cave-geometry/objects/sub/${tileId}.uih`]) {
      assert.equal((await request(p)).status, 418, p);
    }
  });
});

test('malformed addresses and content mismatch are refused, never cached', async () => {
  await withServer(async ({ request, put, tileId }) => {
    for (const p of [`/data/cave-geometry/objects/${tileId.toUpperCase()}.uih`, `/data/cave-geometry/objects/${tileId}.png`,
      '/data/cave-geometry/objects/%2e%2e', '/data/cave-geometry/indexes/']) {
      const r = await request(p);
      assert.equal(r.status, 400, p);
      assert.equal(r.headers['cache-control'], 'no-store');
    }
    await put(`objects/${tileId}.uih`, uih(5, 5).gz);
    const mismatch = await request(`/data/cave-geometry/objects/${tileId}.uih`);
    assert.equal(mismatch.status, 409);
    assert.equal(mismatch.headers['cache-control'], 'no-store');
    const missing = await request(`/data/cave-geometry/objects/${'e'.repeat(64)}.uih`);
    assert.equal(missing.status, 404);
  });
});

test('a missing store serves nothing; a foreign or absent directory is unavailable', async () => {
  for (const unset of [null, undefined]) {
    const handler = createCaveGeometryHandler(unset);
    assert.equal(await handler({ method: 'GET', headers: {} }, {}, '/data/cave-geometry/current.json'), false);
  }
  assert.throws(() => createCaveGeometryHandler('relative/store'), TypeError);
  assert.throws(() => createCaveGeometryHandler(path.resolve('x'), { maxConcurrent: 0 }), TypeError);
  const absent = path.join(os.tmpdir(), `uie-cave-geometry-absent-${process.pid}-${Date.now()}`);
  await withServer(async ({ request }) => {
    const r = await request('/data/cave-geometry/current.json');
    assert.equal(r.status, 503, 'no store, no data');
  }, () => createCaveGeometryHandler(absent));
  await withServer(async ({ dir, request }) => {
    await rm(path.join(dir, OWNED_MARKER));
    assert.equal((await request('/data/cave-geometry/current.json')).status, 503, 'unowned store is unavailable');
  });
});
