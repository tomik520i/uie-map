import { createCaveCoverage } from './web/cave-coverage.js';
import { createWorldCaveOverlay } from './web/world-cave-overlay.js';
// Integration tests for the generation switch in web/app.js: prepareGeneration, commitGeneration and
// discardGeneration from the actual app source, with the real ChunkStream, generation-switch helpers and
// three.js scene objects. Only fetch, image decoding, the DOM and the WebGL renderer are stubbed.
// This is not a browser test. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_generation_app.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import * as Three from './web/vendor/three.module.js';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const moduleURL = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const threeURL = new URL('./web/vendor/three.module.js', import.meta.url).href;
const { OrbitControls } = await import(moduleURL(read('./web/vendor/OrbitControls.js')
  .replace("from 'three'", `from '${threeURL}'`)));
const { createKeyboardPan } = await import(moduleURL(read('./web/map-navigation.js')));
const blockModule = await import(moduleURL(read('./web/block-info.js')));
const caveCut = await import(moduleURL(read('./web/cave-cut.js')));
const overviewSelect = await import(moduleURL(read('./web/overview-select.js')));
const { ChunkStream, selectChunks } = await import(moduleURL(read('./web/chunk-stream.js')));
const generationSwitch = await import(moduleURL(read('./web/generation-switch.js')));
const { createRevisionWatcher, validateRevision } = await import(moduleURL(read('./web/revision-watch.js')));
// The actual app source without its imports and without the main() call; the imports are provided below.
let APP = read('./web/app.js').replace(/^import .*;\r?\n/gm, '');
APP = APP.slice(0, APP.lastIndexOf('\nmain().catch'));

const sha256Hex = (bytes) => createHash('sha256').update(bytes).digest('hex');
const encode = (value) => new TextEncoder().encode(JSON.stringify(value));
const A = 'a'.repeat(64);
const B = 'b'.repeat(64);
const base = (id) => `./data/generations/${id}/`;
const flush = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

class Element extends EventTarget {
  constructor() {
    super(); this.style = {}; this.dataset = {}; this.children = []; this.clientWidth = 800; this.clientHeight = 600;
    this.classList = { toggle() {} }; this.value = 'auto'; this.checked = false; this.hidden = false; this.textContent = '';
  }
  append(node) { this.children.push(node); }
  replaceChildren(...nodes) { this.children = nodes; }
  getRootNode() { return this.ownerDocument; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 600 }; }
  setAttribute() {}
  closest() { return null; }
}

function setup() {
  const elements = new Map();
  const doc = new EventTarget();
  doc.hidden = false;
  doc.createElement = () => new Element();
  doc.getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const win = new EventTarget();
  win.devicePixelRatio = 1;
  class Renderer {
    constructor() {
      this.domElement = new Element();
      this.domElement.ownerDocument = doc;
      this.capabilities = { maxTextureSize: 4096 };
    }
    setPixelRatio() {}
    setSize() {}
    render() {}
  }
  const h = { doc, server: new Map(), fetches: [], bitmaps: [] };
  // Default decoder: a closable stand-in for ImageBitmap; tests may replace it.
  h.decode = async (blob) => {
    const bitmap = { width: 16, height: 16, url: blob.url, closed: false, close() { this.closed = true; } };
    h.bitmaps.push(bitmap);
    return bitmap;
  };
  const context = vm.createContext({ createCaveCoverage, createWorldCaveOverlay, URLSearchParams,
    THREE: { ...Three, WebGLRenderer: Renderer }, OrbitControls, ...blockModule, ...caveCut, ...overviewSelect, ...generationSwitch,
    ChunkStream, selectChunks, createKeyboardPan, createRevisionWatcher, validateRevision, sha256Hex,
    window: win, document: doc, console, AbortController, DOMException, setTimeout, clearTimeout,
    // Response bytes must be the same Uint8Array the generation-switch module checks with instanceof.
    Uint8Array,
    ResizeObserver: class { observe() {} },
    requestAnimationFrame: () => {},
    createImageBitmap: (blob, options) => h.decode(blob, options),
    fetch: async (url, options) => {
      // Recorded synchronously when a load starts: URL, committed generation and atlas on the materials.
      h.fetches.push({ url, generation: h.api.state.generation?.id ?? null, atlas: h.api.state.materials?.opaque.map });
      if (options?.signal?.aborted) throw new DOMException('aborted', 'AbortError');
      const body = h.server.get(url);
      if (!(body instanceof Uint8Array)) return { ok: false, status: body ?? 404, statusText: '', body: null };
      return {
        ok: true, status: 200, statusText: 'OK', body: null,
        arrayBuffer: async () => body.slice().buffer,
        blob: async () => ({ url, size: body.length }),
        text: async () => new TextDecoder().decode(body),
      };
    },
  });
  h.api = vm.runInContext(`${APP}
({ initViewer, state, startChunkStream, updateChunkWindow, prepareGeneration, commitGeneration,
   discardGeneration, getCamera: () => camera, getControls: () => controls });`, context, { filename: 'app.js' });
  h.api.initViewer();
  doc.getElementById('view-distance').value = '1';  // window: target chunk +-1
  return h;
}

function cleanup(h) {
  h.api.state.stream?.dispose();
  clearTimeout(h.api.state.streamTimer);
}

// One triangle per chunk; variant shifts its height so a reloaded chunk is distinguishable.
function chunkFile(x, z, variant = 0) {
  const y = 64 + variant;
  const bytes = encode({ positions: [x * 16, y, z * 16, x * 16 + 1, y, z * 16, x * 16, y, z * 16 + 1],
    uvs: [0, 0, 1, 0, 0, 1], colors: Array(9).fill(1), indices: [0, 1, 2] });
  return { x, z, file: `chunks/${x}_${z}-${sha256Hex(bytes)}.json`, bytes };
}

function publish(h, id, chunks, { bakedLighting = true, max = [160, 256, 16], title = id.slice(0, 1) } = {}) {
  const manifest = {
    format: 1, title, generatedAt: '2026-09-30T12:00:00Z', sample: true, atlas: 'atlas.png', bakedLighting,
    bounds: { min: [0, 0, 0], max }, chunks: chunks.map(({ x, z, file }) => ({ x, z, file })),
    coverage: { renderedBlocks: chunks.length, unsupportedBlocks: 0, emptyBlocks: 0, unsupportedNames: [] },
  };
  const bytes = encode(manifest);
  h.server.set(`${base(id)}manifest.json`, bytes);
  h.server.set(`${base(id)}atlas.png`, new TextEncoder().encode(`png ${id}`));
  for (const chunk of chunks) h.server.set(base(id) + chunk.file, chunk.bytes);
  const revision = { format: 'UIE_REVISION1', world: 'uie', dimension: '0', pipelineKey: 'e'.repeat(64),
    generation: id, manifest: `generations/${id}/manifest.json`, manifestSha256: sha256Hex(bytes) };
  return { manifest, revision };
}

// Live state as after a generation boot of A (main() is not run).
function liveA(h, chunks, { bakedLighting = true } = {}) {
  const { manifest } = publish(h, A, chunks, { bakedLighting });
  const bitmap = { width: 16, height: 16, closed: false, close() { this.closed = true; } };
  const atlas = new Three.Texture(bitmap);
  const { state } = h.api;
  state.generation = { id: A, base: base(A) };
  state.bakedLighting = bakedLighting;
  state.bounds = manifest.bounds;
  state.materials = { opaque: new Three.MeshBasicMaterial({ map: atlas }),
    transparent: new Three.MeshBasicMaterial({ map: atlas, transparent: true }) };
  state.fly = { mode: 'orbit' };
  h.api.getControls().maxDistance = 100;
  h.api.getCamera().far = 500;
  return { manifest, atlas, bitmap };
}

function aim(h, cx, cz) {
  const target = h.api.getControls().target;
  target.set(cx * 16 + 8, 64, cz * 16 + 8);
  h.api.getCamera().position.set(target.x, target.y + 40, target.z + 30);
}

const resident = (h) => h.api.state.stream.getSnapshot().resident.map((entry) => entry.file).sort();
const chunkOf = (mesh) => {
  const p = mesh.geometry.attributes.position;
  return `${Math.floor(p.getX(0) / 16)},${Math.floor(p.getZ(0) / 16)}`;
};

// Snapshot of everything prepare must not change and a failed commit preflight must not change.
function liveSnapshot(h) {
  const { state } = h.api;
  return { generation: state.generation, map: state.materials.opaque.map, transparentMap: state.materials.transparent.map,
    chunkEntries: state.chunkEntries, pickTargets: [...state.pickTargets], pickSeq: state.pickSeq,
    bounds: state.bounds, fly: state.fly, camera: h.api.getCamera(), controls: h.api.getControls(),
    position: h.api.getCamera().position.clone(), target: h.api.getControls().target.clone() };
}

function assertLiveUnchanged(h, before, label) {
  const now = liveSnapshot(h);
  for (const key of ['generation', 'map', 'transparentMap', 'chunkEntries', 'pickSeq', 'bounds', 'fly', 'camera', 'controls']) {
    assert.equal(now[key], before[key], `${label}: ${key}`);
  }
  assert.deepEqual(now.pickTargets, before.pickTargets, `${label}: scene meshes`);
  assert.equal(now.position.distanceTo(before.position), 0, `${label}: camera position`);
  assert.equal(now.target.distanceTo(before.target), 0, `${label}: orbit target`);
}

test('commit reuses unchanged meshes, swaps the atlas before loads, releases the old atlas after detach '
  + 'and clears old-generation failures', async () => {
  const h = setup();
  const c00 = chunkFile(0, 0);
  const c10 = chunkFile(1, 0);
  const c90 = chunkFile(9, 0);
  const live = liveA(h, [c00, c10, c90]);
  try {
    // c90 fails at generation A, then leaves the window: its failure is kept but hidden by getSnapshot.
    h.server.set(base(A) + c90.file, 500);
    aim(h, 9, 0);
    h.api.startChunkStream(live.manifest.chunks);
    await flush();
    assert.equal(h.api.state.stream.getSnapshot().failed.length, 1);
    aim(h, 0, 0);
    h.api.updateChunkWindow();
    await flush();
    assert.deepEqual(resident(h), [c00.file, c10.file].sort());

    // B: c00 and c90 unchanged (same content key), c10 changed, c01 new, larger bounds.
    const c10b = chunkFile(1, 0, 1);
    const c01 = chunkFile(0, 1);
    const next = publish(h, B, [c00, c10b, c01, c90], { max: [320, 256, 16] });
    const kept = h.api.state.pickTargets.find((mesh) => chunkOf(mesh) === '0,0');
    const changed = h.api.state.pickTargets.find((mesh) => chunkOf(mesh) === '1,0');

    const beforePrepare = liveSnapshot(h);
    const prepared = await h.api.prepareGeneration(next.revision, new AbortController().signal);
    assertLiveUnchanged(h, beforePrepare, 'successful prepare');
    assert.deepEqual(resident(h), [c00.file, c10.file].sort(), 'prepare loads no geometry');
    assert.equal(prepared.committed, false);
    assert.ok(prepared.atlas instanceof Three.Texture);
    assert.notEqual(prepared.atlas, live.atlas);

    const order = [];
    changed.geometry.addEventListener('dispose', () => order.push('changed mesh detached'));
    live.atlas.addEventListener('dispose', () => order.push('old atlas released'));
    const fly = h.api.state.fly;
    const fetchesBefore = h.fetches.length;
    const before = liveSnapshot(h);
    h.api.commitGeneration(prepared);

    // Synchronous effects of the commit, before any frame or pending promise.
    assert.equal(h.api.state.generation.id, B);
    assert.equal(h.api.state.generation.base, base(B));
    assert.equal(h.api.state.materials.opaque.map, prepared.atlas);
    assert.equal(h.api.state.materials.transparent.map, prepared.atlas);
    assert.deepEqual(order, ['changed mesh detached', 'old atlas released']);
    assert.ok(live.bitmap.closed, 'old bitmap closed');
    assert.ok(h.api.state.pickTargets.includes(kept), 'unchanged mesh object kept');
    assert.ok(!h.api.state.pickTargets.includes(changed), 'changed mesh removed');
    const started = h.fetches.slice(fetchesBefore);
    assert.deepEqual(started.map((f) => f.url).sort(), [base(B) + c10b.file, base(B) + c01.file].sort());
    for (const f of started) {
      assert.equal(f.generation, B, 'new base active before setDesired starts loads');
      assert.equal(f.atlas, prepared.atlas, 'materials carry the new atlas before loads start');
    }
    assert.equal(h.api.state.pickSeq, before.pickSeq + 1, 'pending lookups invalidated');
    assert.equal(h.doc.getElementById('pick').hidden, true);
    assert.equal(h.api.getCamera(), before.camera);
    assert.equal(h.api.getControls(), before.controls);
    assert.equal(h.api.state.fly, fly);
    assert.equal(fly.mode, 'orbit');
    assert.equal(h.api.getCamera().position.distanceTo(before.position), 0, 'camera position preserved');
    assert.equal(h.api.getControls().target.distanceTo(before.target), 0, 'orbit target preserved');
    assert.ok(h.api.getControls().maxDistance >= 320 * 3, 'orbit limit follows the new bounds');
    assert.ok(h.api.getCamera().far > 500, 'far plane follows the new bounds');
    assert.deepEqual(h.api.state.lastSwitch, { retained: 1, removed: 1, added: 2 });
    h.api.discardGeneration(prepared);
    assert.equal(prepared.atlas.image.closed, false, 'discard after commit must not release the atlas in use');

    await flush();
    assert.deepEqual(resident(h), [c00.file, c01.file, c10b.file].sort());
    for (const mesh of h.api.state.pickTargets) assert.equal(mesh.material.map, prepared.atlas);
    const reloaded = h.api.state.pickTargets.find((mesh) => chunkOf(mesh) === '1,0');
    assert.equal(reloaded.geometry.attributes.position.getY(0), 65, 'changed chunk loaded with new content');

    // The old-generation failure of the unchanged key c90 was cleared by the commit (retry after setDesired).
    aim(h, 9, 0);
    h.api.updateChunkWindow();
    await flush();
    assert.deepEqual(resident(h), [c90.file]);
    assert.equal(h.fetches.at(-1).url, base(B) + c90.file);
  } finally {
    cleanup(h);
  }
});

test('a preflight failure in commit changes nothing; discard then releases the prepared atlas', async () => {
  const h = setup();
  const c00 = chunkFile(0, 0);
  const live = liveA(h, [c00]);
  try {
    aim(h, 0, 0);
    h.api.startChunkStream(live.manifest.chunks);
    await flush();
    const prepared = await h.api.prepareGeneration(publish(h, B, [chunkFile(0, 0, 1)]).revision);
    // desiredEntries (selectChunks) refuses this before the first mutation.
    prepared.manifest.chunks = [{ x: 0.5, z: 0, file: `chunks/0_0-${'f'.repeat(64)}.json` }];
    let oldAtlasReleased = false;
    live.atlas.addEventListener('dispose', () => { oldAtlasReleased = true; });
    const before = liveSnapshot(h);
    const fetchesBefore = h.fetches.length;
    assert.throws(() => h.api.commitGeneration(prepared), TypeError);
    assertLiveUnchanged(h, before, 'failed preflight');
    assert.equal(prepared.committed, false);
    assert.equal(oldAtlasReleased, false);
    assert.equal(live.bitmap.closed, false);
    assert.equal(h.fetches.length, fetchesBefore, 'no load started');
    assert.deepEqual(resident(h), [c00.file]);
    let preparedReleased = false;
    prepared.atlas.addEventListener('dispose', () => { preparedReleased = true; });
    h.api.discardGeneration(prepared);
    assert.ok(preparedReleased);
    assert.ok(prepared.atlas.image.closed);
  } finally {
    cleanup(h);
  }
});

test('prepare refuses a lighting change before any atlas request and leaves the live state', async () => {
  const h = setup();
  liveA(h, [chunkFile(0, 0)], { bakedLighting: false });
  const { revision } = publish(h, B, [chunkFile(0, 0, 1)], { bakedLighting: true });
  const before = liveSnapshot(h);
  await assert.rejects(h.api.prepareGeneration(revision, new AbortController().signal), /osvětlení/);
  assert.equal(h.fetches.some((f) => f.url.endsWith('atlas.png')), false);
  assert.equal(h.bitmaps.length, 0);
  assertLiveUnchanged(h, before, 'lighting mismatch');
});

test('prepare with a missing atlas rejects and leaves the live state', async () => {
  const h = setup();
  liveA(h, [chunkFile(0, 0)]);
  const { revision } = publish(h, B, [chunkFile(0, 0, 1)]);
  h.server.delete(`${base(B)}atlas.png`);
  const before = liveSnapshot(h);
  await assert.rejects(h.api.prepareGeneration(revision, new AbortController().signal), /HTTP 404/);
  assert.equal(h.bitmaps.length, 0);
  assertLiveUnchanged(h, before, 'missing atlas');
});

test('prepare with a manifest hash mismatch rejects before the atlas is requested', async () => {
  const h = setup();
  liveA(h, [chunkFile(0, 0)]);
  const { revision } = publish(h, B, [chunkFile(0, 0, 1)]);
  const before = liveSnapshot(h);
  await assert.rejects(h.api.prepareGeneration({ ...revision, manifestSha256: 'd'.repeat(64) }), /SHA-256/);
  assert.equal(h.fetches.some((f) => f.url.endsWith('atlas.png')), false);
  assertLiveUnchanged(h, before, 'manifest hash mismatch');
});

test('an abort during atlas decoding closes the late bitmap and leaves the live state', async () => {
  const h = setup();
  liveA(h, [chunkFile(0, 0)]);
  const { revision } = publish(h, B, [chunkFile(0, 0, 1)]);
  const controller = new AbortController();
  const late = { width: 16, height: 16, closed: false, close() { this.closed = true; } };
  h.decode = async () => { controller.abort(); return late; };
  const before = liveSnapshot(h);
  await assert.rejects(h.api.prepareGeneration(revision, controller.signal));
  assert.ok(late.closed, 'bitmap decoded after the abort is closed');
  assertLiveUnchanged(h, before, 'aborted prepare');
});
