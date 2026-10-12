import { createCaveCoverage } from './web/cave-coverage.js';
import { createWorldCaveOverlay } from './web/world-cave-overlay.js';
// Opt-in LOD integration in web/app.js, with the actual app source in a vm (imports stripped and provided),
// real three.js, OrbitControls, ChunkStream, LodSwap, lod-distance and lod-contract. Only fetch, the DOM and
// the WebGL renderer are stubbed. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_lod_app.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import * as Three from './web/vendor/three.module.js';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const moduleURL = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const threeURL = new URL('./web/vendor/three.module.js', import.meta.url).href;
const { OrbitControls } = await import(moduleURL(read('./web/vendor/OrbitControls.js').replace("from 'three'", `from '${threeURL}'`)));
const { createKeyboardPan } = await import(moduleURL(read('./web/map-navigation.js')));
const blockModule = await import(moduleURL(read('./web/block-info.js')));
const caveCut = await import(moduleURL(read('./web/cave-cut.js')));
const overviewSelect = await import(moduleURL(read('./web/overview-select.js')));
const { ChunkStream, selectChunks } = await import(moduleURL(read('./web/chunk-stream.js')));
const switchURL = moduleURL(read('./web/generation-switch.js'));
const generationSwitch = await import(switchURL);
const { createRevisionWatcher, validateRevision } = await import(moduleURL(read('./web/revision-watch.js')));
const { LodSwap } = await import(moduleURL(read('./web/lod-swap.js')));
const { selectDistanceLod } = await import(moduleURL(read('./web/lod-distance.js')));
const lodContract = await import(moduleURL(read('./web/lod-contract.js').replace("from './generation-switch.js'", `from '${switchURL}'`)));
let APP = read('./web/app.js').replace(/^import .*;\r?\n/gm, '');
APP = APP.slice(0, APP.lastIndexOf('\nmain().catch'));

const sha256Hex = (bytes) => createHash('sha256').update(bytes).digest('hex');
const encode = (value) => new TextEncoder().encode(JSON.stringify(value));
const flush = async () => { for (let i = 0; i < 60; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const CHUNKS = [[0, 0], [1, 0], [5, 0]];          // focus at chunk 0,0: 0,0 and 1,0 detail, 5,0 coarse
const ATLAS_A = 'a'.repeat(64);
const ATLAS_B = 'b'.repeat(64);

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

function detailMesh(x, z, variant = 0) {
  const y = 64 + variant;
  return { positions: [x * 16, y, z * 16, x * 16 + 1, y, z * 16, x * 16, y, z * 16 + 1], uvs: [0, 0, 1, 0, 0, 1],
    colors: Array(9).fill(1), indices: [0, 1, 2] };
}

function coarseDoc(x, z, source, atlasSha) {
  const y = 63;
  return { format: 'UIE_LOD_CHUNK1', x, z, sourceSha256: source, atlasSha256: atlasSha, colorSteps: 8,
    untextured: { positions: [x * 16, y, z * 16, x * 16 + 16, y, z * 16, x * 16 + 16, y, z * 16 + 16, x * 16, y, z * 16 + 16],
      uvs: Array(8).fill(0), colors: Array(12).fill(0.5), indices: [0, 1, 2, 0, 2, 3] },
    textured: detailMesh(x, z, 2), transparentParts: [] };
}

function setup() {
  const elements = new Map();
  const doc = new EventTarget();
  doc.hidden = false;
  doc.createElement = () => new Element();
  doc.getElementById = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const win = new EventTarget();
  win.devicePixelRatio = 1;
  class Renderer {
    constructor() { this.domElement = new Element(); this.domElement.ownerDocument = doc; this.capabilities = { maxTextureSize: 4096 }; }
    setPixelRatio() {} setSize() {} render() {}
  }
  const h = { doc, server: new Map(), fetches: [] };
  const context = vm.createContext({ createCaveCoverage, createWorldCaveOverlay, URLSearchParams,
    THREE: { ...Three, WebGLRenderer: Renderer }, OrbitControls, ...blockModule, ...caveCut, ...overviewSelect, ...generationSwitch, ...lodContract,
    ChunkStream, selectChunks, createKeyboardPan, createRevisionWatcher, validateRevision, LodSwap, selectDistanceLod,
    sha256Hex, window: win, document: doc, console, AbortController, DOMException, setTimeout, clearTimeout, Uint8Array,
    ResizeObserver: class { observe() {} }, requestAnimationFrame: () => {},
    fetch: async (url) => {
      h.fetches.push(url);
      const body = h.server.get(url);
      if (!(body instanceof Uint8Array)) return { ok: false, status: 404, statusText: '', body: null };
      return { ok: true, status: 200, statusText: 'OK', body: null, arrayBuffer: async () => body.slice().buffer,
        text: async () => new TextDecoder().decode(body) };
    },
  });
  h.api = vm.runInContext(`${APP}
({ initViewer, state, startStream, commitGeneration, attachLod, setLodVisible, detachLod, updateChunkWindow,
   getCamera: () => camera, getControls: () => controls, getScene: () => scene });`, context, { filename: 'app.js' });
  h.api.initViewer();
  doc.getElementById('view-distance').value = '6';
  const { state } = h.api;
  state.bakedLighting = true;
  h.atlasA = new Three.Texture({ width: 1, height: 1, close() { this.closed = true; } });
  state.materials = { opaque: new Three.MeshBasicMaterial({ map: h.atlasA, vertexColors: true }),
    transparent: new Three.MeshBasicMaterial({ map: h.atlasA, vertexColors: true, transparent: true }) };
  h.api.getControls().target.set(8, 64, 8);
  h.api.getCamera().position.set(8, 100, 40);
  h.meshes = () => h.api.getScene().children.filter((o) => o.isMesh);
  h.status = () => doc.getElementById('status-detail').children.map((p) => p.textContent).join('\n');
  return h;
}

function publish(h, id, { lod = true, atlasSha = ATLAS_A, variant = 0, lodChange } = {}) {
  const base = `./data/generations/${id}/`;
  const chunks = [];
  const lodChunks = [];
  for (const [x, z] of CHUNKS) {
    const raw = encode(detailMesh(x, z, variant));
    const source = sha256Hex(raw);
    const file = `chunks/${x}_${z}-${source}.json`;
    h.server.set(base + file, raw);
    chunks.push({ x, z, file });
    const coarseRaw = encode(coarseDoc(x, z, source, atlasSha));
    const coarseFile = `lod/${x}_${z}-${sha256Hex(coarseRaw)}.json`;
    h.server.set(base + coarseFile, coarseRaw);
    lodChunks.push({ x, z, file: coarseFile, sourceSha256: source,
      coarse: { parts: [{ vertices: 4, indices: 6 }, { vertices: 3, indices: 3 }] },
      detail: { parts: [{ vertices: 3, indices: 3 }] } });
  }
  const manifest = { format: 1, title: id.slice(0, 1), generatedAt: '2026-10-01T00:00:00Z', sample: true, atlas: 'atlas.png',
    bakedLighting: true, bounds: { min: [0, 0, 0], max: [96, 256, 16] },
    coverage: { renderedBlocks: 1, unsupportedBlocks: 0, unsupportedNames: [] }, chunks };
  if (lod) {
    manifest.atlasSha256 = atlasSha;
    manifest.lod = { format: 'UIE_LOD1', method: 'planar-unit-v1', colorSteps: 8, atlasSha256: atlasSha, chunks: lodChunks };
    lodChange?.(manifest.lod);
  }
  return { id, base, manifest };
}

function boot(h, generation, { optIn = true } = {}) {
  h.api.state.lodOptIn = optIn;
  h.api.state.generation = { id: generation.id, base: generation.base };
  h.api.startStream(generation.manifest);
}

function prepared(h, generation, atlas) {
  return { id: generation.id, base: generation.base, manifest: generation.manifest, blockIndex: null, atlas, committed: false };
}

function cleanup(h) {
  h.api.state.stream?.dispose();
  clearTimeout(h.api.state.streamTimer);
}

function assertPickingMatchesVisibility(h) {
  const { state } = h.api;
  for (const mesh of h.meshes()) {
    assert.equal(state.pickTargets.includes(mesh), mesh.visible, 'only visible meshes are pickable');
  }
  assert.ok(state.pickTargets.every((mesh) => mesh.visible));
  assert.ok(state.raycastTargets.every((mesh) => mesh.visible && state.pickTargets.includes(mesh)));
  const triangles = h.meshes().filter((m) => m.visible).reduce((sum, m) => sum + m.geometry.index.count / 3, 0);
  assert.equal(state.triangles, triangles, 'triangles count exactly the visible meshes');
}

test('without ?lod=1 a generation with a valid lod block uses the unchanged ChunkStream', async () => {
  const h = setup();
  try {
    boot(h, publish(h, 'g'.repeat(64)), { optIn: false });
    await flush();
    assert.ok(h.api.state.stream instanceof ChunkStream);
    assert.equal(h.api.state.untexturedMaterial, null);
    assert.equal(h.api.state.lodNote, null);
    assert.equal(h.fetches.filter((url) => url.includes('/lod/')).length, 0);
    assert.equal(h.api.state.stream.getSnapshot().resident.length, 3);
    assert.equal(h.meshes().length, 3);
    assertPickingMatchesVisibility(h);
  } finally { cleanup(h); }
});

test('opt-in LOD: coarse coverage first, detail near the focus, untextured material without atlas', async () => {
  const h = setup();
  try {
    boot(h, publish(h, 'g'.repeat(64)));
    assert.equal(h.api.state.stream.kind, 'lod');
    await flush();
    const coarseFetches = h.fetches.filter((url) => url.includes('/lod/'));
    assert.equal(coarseFetches.length, 3, 'every tile gets coarse coverage');
    assert.ok(h.fetches.indexOf(coarseFetches[2]) < h.fetches.findIndex((url) => url.includes('/chunks/')),
      'all coarse before the first detail');
    const snapshot = h.api.state.stream.getSnapshot();
    assert.deepEqual([snapshot.resident.length, snapshot.desired.length, snapshot.pending.length, snapshot.failed.length],
      [3, 3, 0, 0]);
    assert.equal(snapshot.resident[0].file, h.api.state.chunkEntries[0].file, 'residents keep the detail file key');
    assert.match(snapshot.lodNote, /2 detailních a 1 hrubých/);
    assert.match(h.status(), /Experimentální LOD/);
    const untextured = h.api.state.untexturedMaterial;
    assert.equal(untextured.map, null);
    assert.equal(untextured.alphaTest, 0);
    assert.ok(h.meshes().some((m) => m.material === untextured && m.visible), 'the far tile shows coarse');
    assert.equal(h.meshes().length, 2 + 2, 'two detail meshes + coarse untextured and textured parts; replaced coarse gone');
    assertPickingMatchesVisibility(h);
  } finally { cleanup(h); }
});

test('LOD resources are hidden and unpickable until shown; toggles are idempotent; dispose once', () => {
  const h = setup();
  const { state } = h.api;
  state.untexturedMaterial = new Three.MeshBasicMaterial({ vertexColors: true });
  const doc = coarseDoc(0, 0, 'c'.repeat(64), ATLAS_A);
  const before = { pick: state.pickTargets.length, ray: state.raycastTargets.length, tri: state.triangles };
  const resource = h.api.attachLod({ kind: 'coarse', doc });
  assert.equal(resource.meshes.length, 2);
  assert.ok(resource.meshes.every((m) => !m.visible && h.meshes().includes(m)), 'attached hidden');
  assert.deepEqual([state.pickTargets.length, state.raycastTargets.length, state.triangles], [before.pick, before.ray, before.tri]);
  h.api.setLodVisible(resource, true);
  h.api.setLodVisible(resource, true);
  assert.equal(state.pickTargets.length, before.pick + 2);
  assert.equal(state.raycastTargets.length, before.ray + 2);
  assert.equal(state.triangles, before.tri + 3);
  h.api.setLodVisible(resource, false);
  h.api.setLodVisible(resource, false);
  assert.deepEqual([state.pickTargets.length, state.triangles], [before.pick, before.tri]);
  h.api.setLodVisible(resource, true);
  let disposed = 0;
  resource.meshes.forEach((m) => m.geometry.addEventListener('dispose', () => { disposed += 1; }));
  h.api.detachLod(resource);
  h.api.detachLod(resource);
  assert.equal(disposed, 2, 'each geometry disposed exactly once');
  assert.deepEqual([state.pickTargets.length, state.raycastTargets.length, state.triangles], [before.pick, before.ray, before.tri],
    'detaching a visible resource unregisters it once (no double triangle decrement)');
  assert.ok(resource.meshes.every((m) => !h.meshes().includes(m)));
  h.api.setLodVisible(resource, true);
  assert.equal(state.pickTargets.length, before.pick, 'a detached resource can never become pickable');
});

test('invalid or oversized LOD falls back to full detail with a warning, never partial LOD', async () => {
  const cases = {
    'missing coverage': (lod) => lod.chunks.pop(),
    'reserve over budget': (lod) => lod.chunks[2].coarse.parts[0].vertices = 9_000_000,   // > 500 MB budget
  };
  for (const [label, lodChange] of Object.entries(cases)) {
    const h = setup();
    try {
      boot(h, publish(h, 'g'.repeat(64), { lodChange }));
      await flush();
      assert.ok(h.api.state.stream instanceof ChunkStream, label);
      assert.equal(h.api.state.lodNote.warn, true);
      assert.match(h.api.state.lodNote.text, /Experimentální LOD vypnuto/);
      assert.equal(h.fetches.filter((url) => url.includes('/lod/')).length, 0, `${label}: no LOD request`);
      assert.equal(h.api.state.stream.getSnapshot().resident.length, 3);
      assert.match(h.status(), /plné detaily/);
    } finally { cleanup(h); }
  }
});

test('count mismatches are rejected: detail stays coarse, coarse failure leaves only that tile uncovered', async () => {
  const h = setup();
  try {
    boot(h, publish(h, 'g'.repeat(64), { lodChange: (lod) => {
      lod.chunks[0].detail.parts[0].vertices = 4;          // 0,0: published detail has 3 vertices
      lod.chunks[2].coarse.parts[0].indices = 3;           // 5,0: coarse document has 6 indices
    } }));
    await flush();
    const snapshot = h.api.state.stream.getSnapshot();
    const files = (list) => list.map((e) => `${e.x},${e.z}`).sort();
    assert.deepEqual(files(snapshot.resident), ['0,0', '1,0']);
    assert.deepEqual(files(snapshot.failed), ['5,0'], 'the uncovered tile is the only failed chunk');
    assert.ok(snapshot.errors.some((e) => /počty geometrie neodpovídají/.test(e.message)));
    assert.ok(snapshot.errors.some((e) => /hrubá verze/.test(e.message)));
    assert.equal(snapshot.retryable, 2);
    assert.match(snapshot.lodNote, /1 detailních a 1 hrubých/, '0,0 keeps its coarse coverage');
    assertPickingMatchesVisibility(h);
  } finally { cleanup(h); }
});

test('generation commit in LOD mode removes every old mesh before the atlas swap; camera unchanged', async () => {
  const h = setup();
  try {
    boot(h, publish(h, 'g'.repeat(64)));
    await flush();
    assert.ok(h.meshes().length > 0);
    const camera = h.api.getCamera();
    const position = camera.position.clone();
    const target = h.api.getControls().target.clone();
    let oldAtlasDisposed = false;
    h.atlasA.addEventListener('dispose', () => { oldAtlasDisposed = true; });
    const atlasB = new Three.Texture({ width: 1, height: 1 });
    const next = publish(h, 'n'.repeat(64), { atlasSha: ATLAS_B, variant: 5 });
    const fetchesBefore = h.fetches.length;
    h.api.commitGeneration(prepared(h, next, atlasB));
    // Synchronously after the commit: no old geometry anywhere, new atlas on textured materials only.
    assert.equal(h.meshes().filter((m) => m.visible).length, 0, 'no old mesh is drawn with the new atlas');
    assert.equal(h.meshes().length, 0);
    assert.deepEqual([h.api.state.pickTargets.length, h.api.state.raycastTargets.length, h.api.state.triangles], [0, 0, 0]);
    assert.equal(h.api.state.materials.opaque.map, atlasB);
    assert.equal(h.api.state.materials.transparent.map, atlasB);
    assert.equal(h.api.state.untexturedMaterial.map, null);
    assert.ok(oldAtlasDisposed);
    assert.equal(h.api.state.stream.kind, 'lod');
    assert.deepEqual(h.api.state.lastSwitch, { retained: 0, removed: 0, added: 3 }, 'no claimed reuse');
    assert.equal(camera.position.distanceTo(position), 0);
    assert.equal(h.api.getControls().target.distanceTo(target), 0);
    await flush();
    assert.ok(h.fetches.slice(fetchesBefore).every((url) => url.startsWith(next.base)), 'loads use the new generation');
    assert.equal(h.api.state.stream.getSnapshot().resident.length, 3);
    assertPickingMatchesVisibility(h);
  } finally { cleanup(h); }
});

test('transitions LOD -> generation without lod -> LOD keep nothing old and leak nothing', async () => {
  const h = setup();
  try {
    boot(h, publish(h, 'g'.repeat(64)));
    await flush();
    const plain = publish(h, 'p'.repeat(64), { lod: false, variant: 1 });
    h.api.commitGeneration(prepared(h, plain, h.atlasA));
    assert.ok(h.api.state.stream instanceof ChunkStream, 'back to the detailed stream');
    assert.equal(h.meshes().length, 0, 'all LOD meshes removed synchronously');
    assert.match(h.api.state.lodNote.text, /data LOD neobsahuje/);
    await flush();
    assert.equal(h.api.state.stream.getSnapshot().resident.length, 3);
    assert.equal(h.meshes().length, 3);
    assertPickingMatchesVisibility(h);
    const again = publish(h, 'q'.repeat(64), { variant: 2 });
    h.api.commitGeneration(prepared(h, again, h.atlasA));
    assert.equal(h.api.state.stream.kind, 'lod', 'detailed stream -> LOD');
    assert.equal(h.meshes().length, 0, 'the detailed meshes were disposed before the switch');
    assert.equal(h.api.state.lodNote, null);
    await flush();
    assert.equal(h.api.state.stream.getSnapshot().resident.length, 3);
    assertPickingMatchesVisibility(h);
  } finally { cleanup(h); }
});
