// HTTP tests for the optional immutable generation store in server.mjs. Node built-ins only.
// Starts the real server.mjs as a child process on 127.0.0.1 with a temporary store.
// Run from the work directory:  node --test uie-map/test_generation_http.mjs
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { gzipSync, gunzipSync } from 'node:zlib';

const SERVER = fileURLToPath(new URL('./server.mjs', import.meta.url));
const sha = (text) => createHash('sha256').update(text).digest('hex');
const PNG = Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex');

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
  });
}

async function startServer(env) {
  const port = await freePort();
  const child = spawn(process.execPath, [SERVER], {
    env: { ...process.env, PORT: String(port), HOST: '127.0.0.1', UIE_RENDER_STATUS_FILE: '', UIE_GENERATION_STORE: '', ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server did not start')), 10000);
    child.stdout.on('data', (chunk) => { if (String(chunk).includes('listening')) { clearTimeout(timer); resolve(); } });
    child.once('exit', (code) => { clearTimeout(timer); reject(new Error(`server exited ${code}`)); });
  });
  return { port, stop: () => new Promise((resolve) => { child.once('exit', resolve); child.kill(); }) };
}

/** Raw request: the path is sent exactly as given (no client-side URL normalisation). */
function request(port, rawPath, method = 'GET', headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: rawPath, method, headers }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    });
    req.on('error', reject);
    req.end();
  });
}

function makeGeneration(store, label, { complete = true } = {}) {
  const manifest = JSON.stringify({ format: 1, title: label, chunks: [{ x: 0, z: 0, file: 'chunks/0_0-a.json' }] });
  const id = sha(`generation ${label}`);
  const dir = path.join(store, 'generations', id);
  mkdirSync(path.join(dir, 'chunks'), { recursive: true });
  writeFileSync(path.join(dir, 'manifest.json'), manifest);
  writeFileSync(path.join(dir, 'atlas.png'), PNG);
  writeFileSync(path.join(dir, 'chunks', '0_0-a.json'), JSON.stringify({ label }));
  writeFileSync(path.join(dir, 'notes.txt'), 'not served');
  if (complete) writeFileSync(path.join(dir, 'COMPLETE.json'), JSON.stringify({ format: 'UIE_GENERATION1' }));
  return { id, manifestSha256: sha(manifest) };
}

function pointTo(store, generation, overrides = {}) {
  const pointer = { format: 'UIE_REVISION1', world: 'uie', dimension: 'overworld', pipelineKey: sha('p'),
    generation: generation.id, manifest: `generations/${generation.id}/manifest.json`,
    manifestSha256: generation.manifestSha256, ...overrides };
  const text = JSON.stringify(pointer);
  writeFileSync(path.join(store, 'current.json'), text);
  return text;
}

function fixture() {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'uie-store-'));
  const store = path.join(dir, 'store');
  mkdirSync(store);
  return { dir, store, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test('revision pointer: exact bytes, no-store, HEAD, pointer switch keeps old generation readable', { timeout: 30000 }, async () => {
  const f = fixture();
  const g1 = makeGeneration(f.store, 'one');
  const g2 = makeGeneration(f.store, 'two');
  const first = pointTo(f.store, g1);
  const server = await startServer({ UIE_GENERATION_STORE: f.store });
  try {
    const revision = await request(server.port, '/data/revision.json');
    assert.equal(revision.status, 200);
    assert.equal(revision.body.toString(), first);
    assert.equal(revision.headers['cache-control'], 'no-store');
    assert.match(revision.headers['content-type'], /application\/json/);
    const head = await request(server.port, '/data/revision.json', 'HEAD');
    assert.equal(head.status, 200);
    assert.equal(head.body.length, 0);
    assert.equal(Number(head.headers['content-length']), Buffer.byteLength(first));

    const manifest = await request(server.port, `/data/generations/${g1.id}/manifest.json`);
    assert.equal(manifest.status, 200);
    assert.equal(sha(manifest.body), g1.manifestSha256);
    assert.equal(manifest.headers['cache-control'], 'public, max-age=31536000, immutable');
    const atlas = await request(server.port, `/data/generations/${g1.id}/atlas.png`);
    assert.equal(atlas.status, 200);
    assert.equal(atlas.headers['content-type'], 'image/png');
    assert.deepEqual(atlas.body, PNG);
    const chunk = await request(server.port, `/data/generations/${g1.id}/chunks/0_0-a.json`, 'HEAD');
    assert.equal(chunk.status, 200);
    assert.equal(chunk.body.length, 0);

    const second = pointTo(f.store, g2);
    assert.equal((await request(server.port, '/data/revision.json')).body.toString(), second);
    const old = await request(server.port, `/data/generations/${g1.id}/chunks/0_0-a.json`);
    assert.equal(old.status, 200, 'old generation stays fetchable after the pointer moved');
    assert.deepEqual(JSON.parse(old.body), { label: 'one' });
    assert.equal((await request(server.port, `/data/generations/${g2.id}/manifest.json`)).status, 200);
  } finally {
    await server.stop();
    f.cleanup();
  }
});

test('generation paths: traversal, encodings, symlinks, file types and incomplete generations are refused', { timeout: 30000 }, async () => {
  const f = fixture();
  const g1 = makeGeneration(f.store, 'one');
  const partial = makeGeneration(f.store, 'partial', { complete: false });
  pointTo(f.store, g1);
  writeFileSync(path.join(f.dir, 'secret.json'), '{"secret":true}');
  let symlinked = true;
  try {
    symlinkSync(path.join(f.dir, 'secret.json'), path.join(f.store, 'generations', g1.id, 'link.json'));
  } catch { symlinked = false; }
  const server = await startServer({ UIE_GENERATION_STORE: f.store });
  try {
    const base = `/data/generations/${g1.id}`;
    const refused = [
      `${base}/COMPLETE.json`, `${base}/notes.txt`, `${base}/../../current.json`, `${base}/%2e%2e/%2e%2e/current.json`,
      `${base}/..%2f..%2fcurrent.json`, `${base}/chunks/..%2f..%2f..%2fsecret.json`, `${base}/chunks%2f..%2f..%2fx.json`,
      `${base}/`, `/data/generations/${g1.id.toUpperCase()}/manifest.json`, `/data/generations/abc/manifest.json`,
      `/data/generations/${partial.id}/manifest.json`, `${base}/missing.json`, `/data/generations/`,
    ];
    for (const raw of refused) {
      const res = await request(server.port, raw);
      assert.ok([400, 403, 404].includes(res.status), `${raw} -> ${res.status}`);
      assert.doesNotMatch(res.body.toString(), /secret|UIE_REVISION1|UIE_GENERATION1/, raw);
    }
    assert.equal((await request(server.port, `${base}/%5cmanifest.json`)).status, 400, 'encoded backslash');
    if (symlinked) {
      const res = await request(server.port, `${base}/link.json`);
      assert.equal(res.status, 404, 'symlink out of the generation');
      assert.doesNotMatch(res.body.toString(), /secret/);
    }
    assert.equal((await request(server.port, '/index.html')).status, 200, 'static site still served');
  } finally {
    await server.stop();
    f.cleanup();
  }
});

test('missing or bad revision pointer fails closed without breaking the static map', { timeout: 30000 }, async () => {
  const f = fixture();
  const g1 = makeGeneration(f.store, 'one');
  const partial = makeGeneration(f.store, 'partial', { complete: false });
  const server = await startServer({ UIE_GENERATION_STORE: f.store });
  try {
    assert.equal((await request(server.port, '/data/revision.json')).status, 404, 'nothing published yet');
    const bad = {
      'not json': () => writeFileSync(path.join(f.store, 'current.json'), '{nope'),
      'array': () => writeFileSync(path.join(f.store, 'current.json'), '[]'),
      'format': () => pointTo(f.store, g1, { format: 'UIE_REVISION2' }),
      'generation uppercase': () => pointTo(f.store, g1, { generation: g1.id.toUpperCase(),
        manifest: `generations/${g1.id.toUpperCase()}/manifest.json` }),
      'manifest path': () => pointTo(f.store, g1, { manifest: `generations/${g1.id}/../manifest.json` }),
      'manifest sha': () => pointTo(f.store, g1, { manifestSha256: 'abc' }),
      'incomplete generation': () => pointTo(f.store, partial),
      'unknown generation': () => pointTo(f.store, { id: sha('none'), manifestSha256: sha('x') }),
      oversized: () => pointTo(f.store, g1, { padding: 'x'.repeat(17000) }),
    };
    for (const [label, write] of Object.entries(bad)) {
      write();
      const res = await request(server.port, '/data/revision.json');
      assert.equal(res.status, 503, label);
      assert.equal((await request(server.port, '/index.html')).status, 200, `${label}: static map still served`);
    }
    pointTo(f.store, g1);
    assert.equal((await request(server.port, '/data/revision.json')).status, 200, 'recovers once the pointer is valid');
    assert.equal((await request(server.port, '/data/manifest.json')).headers['cache-control'] === 'public, max-age=31536000, immutable', false,
      'the static manifest is never redirected to a generation');
  } finally {
    await server.stop();
    f.cleanup();
  }
});

test('without UIE_GENERATION_STORE the old static behaviour is unchanged', { timeout: 30000 }, async () => {
  const server = await startServer({});
  try {
    const index = await request(server.port, '/index.html');
    assert.equal(index.status, 200);
    assert.equal(index.headers['cache-control'], 'no-cache');
    assert.equal((await request(server.port, '/data/revision.json')).status, 404);
    assert.equal((await request(server.port, `/data/generations/${sha('x')}/manifest.json`)).status, 404);
    assert.equal((await request(server.port, '/%2e%2e/server.mjs')).status === 200, false, 'no escape from web/');
    assert.equal((await request(server.port, '/index.html', 'POST')).status, 405);
  } finally {
    await server.stop();
  }
});

test('gzip-only generation chunks support gzip, identity, HEAD and contained paths', { timeout: 30000 }, async () => {
  const f = fixture();
  const g = makeGeneration(f.store, 'compressed');
  const dir = path.join(f.store, 'generations', g.id);
  const body = Buffer.from(JSON.stringify({ positions: Array(100).fill(0), label: 'lossless' }));
  const zipped = gzipSync(body);
  writeFileSync(path.join(dir, 'chunks', 'compressed.json.gz'), zipped);
  writeFileSync(path.join(f.dir, 'secret.json.gz'), gzipSync('secret'));
  let linked = true;
  try { symlinkSync(path.join(f.dir, 'secret.json.gz'), path.join(dir, 'escape.json.gz')); }
  catch { linked = false; }
  const server = await startServer({ UIE_GENERATION_STORE: f.store });
  try {
    const url = `/data/generations/${g.id}/chunks/compressed.json`;
    const compressed = await request(server.port, url, 'GET', { 'Accept-Encoding': 'gzip' });
    assert.equal(compressed.status, 200);
    assert.equal(compressed.headers['content-encoding'], 'gzip');
    assert.equal(Number(compressed.headers['content-length']), zipped.length);
    assert.deepEqual(gunzipSync(compressed.body), body);
    const identity = await request(server.port, url, 'GET', { 'Accept-Encoding': 'gzip;q=0' });
    assert.equal(identity.status, 200);
    assert.equal(identity.headers['content-encoding'], undefined);
    assert.deepEqual(identity.body, body);
    const spaced = await request(server.port, url, 'GET', { 'Accept-Encoding': 'gzip ;q=1' });
    assert.equal(spaced.headers['content-encoding'], 'gzip', 'whitespace before a parameter keeps the coding name');
    assert.deepEqual(gunzipSync(spaced.body), body);
    const spacedOff = await request(server.port, url, 'GET', { 'Accept-Encoding': 'gzip ; q=0' });
    assert.equal(spacedOff.headers['content-encoding'], undefined);
    assert.deepEqual(spacedOff.body, body);
    for (const encoding of ['gzip', 'identity']) {
      const head = await request(server.port, url, 'HEAD', { 'Accept-Encoding': encoding });
      assert.equal(head.status, 200); assert.equal(head.body.length, 0);
      assert.equal(head.headers.vary, 'Accept-Encoding');
    }
    assert.equal((await request(server.port, url + '.gz')).status, 404);
    if (linked) assert.equal((await request(server.port, `/data/generations/${g.id}/escape.json`)).status, 404);
  } finally { await server.stop(); f.cleanup(); }
});
