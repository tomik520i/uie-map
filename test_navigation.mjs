import { createCaveCoverage } from './web/cave-coverage.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import vm from 'node:vm';
import * as Three from './web/vendor/three.module.js';

const moduleURL = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const threeURL = new URL('./web/vendor/three.module.js', import.meta.url).href;
const { OrbitControls } = await import(moduleURL(readFileSync(new URL('./web/vendor/OrbitControls.js', import.meta.url), 'utf8')
  .replace("from 'three'", `from '${threeURL}'`)));
const { createKeyboardPan } = await import(moduleURL(readFileSync(new URL('./web/map-navigation.js', import.meta.url), 'utf8')));
const blockModule = await import(moduleURL(readFileSync(new URL('./web/block-info.js', import.meta.url), 'utf8')));
const caveCut = await import(moduleURL(readFileSync(new URL('./web/cave-cut.js', import.meta.url), 'utf8')));
const overviewSelect = await import(moduleURL(readFileSync(new URL('./web/overview-select.js', import.meta.url), 'utf8')));
const { ChunkStream, selectChunks } = await import(moduleURL(readFileSync(new URL('./web/chunk-stream.js', import.meta.url), 'utf8')));
let app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8').replace(/^import .*;\r?\n/gm, '');
app = app.slice(0, app.lastIndexOf('\nmain().catch'));

class Element extends EventTarget {
  constructor() { super(); this.style = {}; this.dataset = {}; this.children = []; this.clientWidth = 800; this.clientHeight = 600;
    this.classList = { toggle() {} }; this.value = 'auto'; this.checked = false; this.hidden = false; }
  append(node) { this.children.push(node); }
  replaceChildren(...nodes) { this.children = nodes; }
  getRootNode() { return this.ownerDocument; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 600 }; }
  setAttribute() {}
  closest() { return null; }
}

function event(type, fields) {
  const e = new Event(type, { cancelable: true });
  Object.assign(e, fields);
  return e;
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
  const win = new EventTarget(); win.devicePixelRatio = 1;
  const frames = [], rendered = [];
  class Renderer {
    constructor() { this.domElement = new Element(); this.domElement.ownerDocument = doc; }
    setPixelRatio() {}
    setSize() {}
    render(scene, camera) { rendered.push(camera.position.clone()); }
  }
  const context = vm.createContext({ createCaveCoverage, URLSearchParams,
    THREE: { ...Three, WebGLRenderer: Renderer }, OrbitControls, ...blockModule, ...caveCut, ...overviewSelect,
    ChunkStream, selectChunks, createKeyboardPan, window: win, document: doc, console,
    AbortController, setTimeout, clearTimeout, ResizeObserver: class { observe() {} },
    requestAnimationFrame: (cb) => frames.push(cb),
  });
  const api = vm.runInContext(`${app}\n({initViewer, state, addChunk, removeChunk, chunkBufferBytes,
    startChunkStream, updateChunkWindow, getControls: () => controls, getRenderer: () => renderer,
    getScene: () => scene, setLoad: (fn) => { fetchJson = fn; }})`, context);
  api.initViewer();
  api.state.bounds = { min: [0, 0, 0], max: [128, 255, 128] };
  const controls = api.getControls();
  controls.target.set(0, 64, 0);
  controls.object.position.set(80, 140, 160);
  const frame = (time) => frames.shift()(time);
  frame(0); frame(16);
  return { api, controls, win, doc, frame, rendered, elements, canvas: api.getRenderer().domElement };
}

test('wheel renders on the next frame even when OrbitControls already consumed the update', () => {
  const h = setup();
  const before = h.rendered.at(-1).clone();
  const count = h.rendered.length;
  const distance = h.controls.getDistance();
  h.canvas.dispatchEvent(event('wheel', { clientX: 530, clientY: 320, deltaY: -100, deltaMode: 0, ctrlKey: false }));
  assert.ok(h.controls.getDistance() < distance, 'real wheel handler changes camera distance');
  h.frame(32);
  assert.equal(h.rendered.length, count + 1, 'changed camera must be presented without another input');
  assert.ok(h.rendered.at(-1).distanceTo(before) > 1);
  const zoomed = h.controls.getDistance();
  h.canvas.dispatchEvent(event('wheel', { clientX: 530, clientY: 320, deltaY: 100, deltaMode: 0, ctrlKey: false }));
  h.frame(48);
  assert.ok(h.controls.getDistance() > zoomed);
});

test('close orbit zoom does not fog out terrain below a high look-at target', () => {
  const h = setup();
  h.controls.target.set(192, 128, 192);
  h.controls.object.position.set(192, 132, 192);
  h.api.state.dirty = true;
  h.frame(32);
  const fog = h.api.getScene().fog;
  // The real generation can start with targetY128 while terrain is around Y76.
  // This visible terrain must remain before the fade even at minimum dolly distance.
  assert.ok(fog.near > 132 - 76);
  assert.ok(fog.far >= 256 && fog.far > fog.near);
  h.controls.object.position.set(192, 328, 192);
  h.api.state.dirty = true;
  h.frame(48);
  assert.ok(Math.abs(fog.near - 180) < 1e-5);
  assert.ok(Math.abs(fog.far - 584) < 1e-5);
  clearTimeout(h.api.state.streamTimer);
});

test('held WASD and arrows pan actual camera; release decays damping; focus in form stops movement', () => {
  const h = setup();
  const start = h.controls.target.clone();
  h.win.dispatchEvent(event('keydown', { code: 'KeyW' }));
  for (let t = 32; t <= 512; t += 16) h.frame(t);
  assert.ok(h.controls.target.distanceTo(start) > 10);
  assert.ok(Math.abs(h.controls.target.y - start.y) < 1e-9, 'ground-plane panning');
  h.win.dispatchEvent(event('keyup', { code: 'KeyW' }));
  for (let t = 528; t <= 2512; t += 16) h.frame(t);
  const stopped = h.controls.target.clone();
  h.frame(2528);
  assert.ok(h.controls.target.distanceTo(stopped) < 0.0001);
  h.doc.activeElement = { closest: () => ({ tagName: 'INPUT' }) };
  h.win.dispatchEvent(event('keydown', { code: 'ArrowRight' }));
  h.frame(2544);
  assert.ok(h.controls.target.distanceTo(stopped) < 0.0001);
  h.doc.activeElement = null;
  h.win.dispatchEvent(event('keyup', { code: 'ArrowRight' }));
  h.win.dispatchEvent(event('keydown', { code: 'ArrowLeft' }));
  for (let t = 2560; t <= 3040; t += 16) h.frame(t);
  assert.ok(h.controls.target.distanceTo(stopped) > 10);
});

test('keyboard diagonal is normalized, duplicate aliases do not accelerate, blur clears keys', () => {
  const win = new EventTarget(), doc = new EventTarget();
  const calls = [];
  const nav = createKeyboardPan({ pan: (...args) => calls.push(args) }, win, doc);
  win.dispatchEvent(event('keydown', { code: 'KeyW' }));
  win.dispatchEvent(event('keydown', { code: 'ArrowUp' }));
  nav.update(1 / 60);
  assert.deepEqual(calls.at(-1), [0, 4]);
  win.dispatchEvent(event('keydown', { code: 'KeyD' }));
  nav.update(1 / 60);
  assert.ok(Math.abs(Math.hypot(...calls.at(-1)) - 4) < 1e-10);
  const count = calls.length;
  win.dispatchEvent(new Event('blur'));
  nav.update(1 / 60);
  assert.equal(calls.length, count);
  nav.dispose();
});

test('a short keyboard tap between frames still pans, while repeat events do not multiply speed', () => {
  const h = setup();
  const start = h.controls.target.clone();
  h.win.dispatchEvent(event('keydown', { code: 'KeyD' }));
  h.win.dispatchEvent(event('keyup', { code: 'KeyD' }));
  h.frame(32);
  assert.ok(h.controls.target.distanceTo(start) > 0.01);
  const win = new EventTarget(), doc = new EventTarget(), calls = [];
  const nav = createKeyboardPan({ pan: (...args) => calls.push(args) }, win, doc);
  win.dispatchEvent(event('keydown', { code: 'KeyW' }));
  win.dispatchEvent(event('keydown', { code: 'KeyW', repeat: true }));
  win.dispatchEvent(event('keydown', { code: 'ArrowUp' }));
  assert.equal(calls.length, 1);
  nav.dispose();
});

test('real exported meshes respect byte accounting and release scene/picking resources on eviction',
  { skip: !existsSync(new URL('./web/data/manifest.json', import.meta.url)) }, async () => {
  const h = setup();
  const manifest = JSON.parse(readFileSync(new URL('./web/data/manifest.json', import.meta.url), 'utf8'));
  const entry = manifest.chunks[0];
  const chunk = JSON.parse(readFileSync(new URL(`./web/data/${entry.file}`, import.meta.url), 'utf8'));
  h.api.state.materials = { opaque: new Three.MeshBasicMaterial(), transparent: new Three.MeshBasicMaterial() };
  const baseline = h.api.getScene().children.length;
  const meshes = h.api.addChunk(chunk);
  const allocated = meshes.reduce((sum, mesh) => sum + mesh.geometry.index.array.byteLength
    + Object.values(mesh.geometry.attributes).reduce((n, a) => n + a.array.byteLength, 0), 0);
  assert.equal(h.api.chunkBufferBytes(chunk), allocated * 2);
  assert.ok(h.api.state.triangles > 0);
  let disposed = 0;
  meshes.forEach((m) => m.geometry.addEventListener('dispose', () => disposed++));
  h.api.removeChunk(meshes);
  assert.equal(disposed, meshes.length);
  assert.equal(h.api.state.triangles, 0);
  assert.equal(h.api.state.pickTargets.length, 0);
  assert.equal(h.api.state.raycastTargets.length, 0);
  assert.equal(h.api.getScene().children.length, baseline);
  // Exercise actual app streaming callbacks and fetch wrapper boundary over repeat visits.
  h.api.setLoad(async () => chunk);
  h.controls.target.set(entry.x * 16 + 8, 64, entry.z * 16 + 8);
  h.api.startChunkStream([entry]);
  for (let i = 0; i < 10; i++) await Promise.resolve();
  assert.equal(h.api.state.stream.getSnapshot().resident.length, 1);
  for (let pass = 0; pass < 3; pass++) {
    h.controls.target.set(-1000, 64, -1000);
    h.api.updateChunkWindow();
    assert.equal(h.api.state.triangles, 0);
    assert.equal(h.api.state.pickTargets.length, 0);
    h.controls.target.set(entry.x * 16 + 8, 64, entry.z * 16 + 8);
    h.api.updateChunkWindow();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    assert.equal(h.api.state.stream.getSnapshot().resident.length, 1);
    assert.equal(h.api.state.pickTargets.length, meshes.length);
  }
  h.api.state.stream.dispose();
  assert.equal(h.api.getScene().children.length, baseline);
  clearTimeout(h.api.state.streamTimer);
});

test('app Fly frames skip orbit updates and streaming follows camera instead of frozen target', () => {
  const h = setup();
  let updates = 0, steps = 0, wanted;
  h.controls.update = () => { updates++; return false; };
  h.api.state.fly = { mode: 'fly', step: () => { steps++; return true; } };
  h.controls.target.set(-1000, 64, -1000);
  h.controls.object.position.set(168, 30, 168);
  h.api.state.stream = { setDesired: (entries) => { wanted = entries; } };
  h.api.state.chunkEntries = [{ x: 10, z: 10, file: 'near.json' }, { x: -63, z: -63, file: 'orbit.json' }];
  h.doc.getElementById('view-distance').value = 'auto';
  h.api.updateChunkWindow();
  assert.deepEqual(Array.from(wanted, (e) => e.file), ['near.json']);
  h.frame(32);
  assert.equal(updates, 0);
  assert.equal(steps, 1);
  assert.match(h.doc.getElementById('fly-position').textContent, /Y 30.0/);
  h.controls.object.position.set(-1000, 30, -1000);
  h.api.updateChunkWindow();
  assert.deepEqual(Array.from(wanted, (e) => e.file), ['orbit.json']);
  h.api.state.fly.mode = 'orbit';
  h.frame(48);
  assert.equal(updates, 1);
  clearTimeout(h.api.state.streamTimer);
});
