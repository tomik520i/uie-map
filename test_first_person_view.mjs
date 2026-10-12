// Tests for web/first-person-view.js with real Three.js, real OrbitControls, the real fly motion module
// and a mocked DOM / pointer lock. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_first_person_view.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as Three from './web/vendor/three.module.js';

const moduleURL = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const threeURL = new URL('./web/vendor/three.module.js', import.meta.url).href;
const { OrbitControls } = await import(moduleURL(read('./web/vendor/OrbitControls.js').replace("from 'three'", `from '${threeURL}'`)));
const { createFlyMotion } = await import(moduleURL(read('./web/first-person-motion.js')));
const fpv = await import(moduleURL(read('./web/first-person-view.js')));
const walkPhysics = await import(moduleURL(read('./web/walk-physics.js')));

const flush = () => new Promise((resolve) => { setImmediate(resolve); });
const event = (type, fields = {}) => Object.assign(new Event(type, { cancelable: true }), fields);

function harness({ lock = 'promise', walker = null } = {}) {
  const doc = new EventTarget();
  Object.assign(doc, { hidden: false, pointerLockElement: null, activeElement: null, exitCalls: 0 });
  doc.exitPointerLock = () => {
    doc.exitCalls += 1;
    if (doc.pointerLockElement) {
      doc.pointerLockElement = null;
      doc.dispatchEvent(new Event('pointerlockchange'));
    }
  };
  const win = new EventTarget();
  const canvas = new EventTarget();
  Object.assign(canvas, { style: {}, ownerDocument: doc, clientWidth: 800, clientHeight: 600, requests: [] });
  canvas.getRootNode = () => doc;
  canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 600 });
  if (lock !== 'unsupported') {
    canvas.requestPointerLock = () => {
      let resolve, reject;
      const promise = new Promise((a, b) => { resolve = a; reject = b; });
      canvas.requests.push({ resolve, reject });
      return lock === 'legacy' ? undefined : promise;
    };
  }
  const grant = () => {
    doc.pointerLockElement = canvas;
    doc.dispatchEvent(new Event('pointerlockchange'));
    canvas.requests.at(-1)?.resolve();
  };
  const camera = new Three.PerspectiveCamera(55, 1.5, 0.5, 4000);
  camera.position.set(80, 140, 160);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.target.set(0, 64, 0);
  controls.update();
  const h = { doc, win, canvas, camera, controls, grant, changes: [], messages: [], modes: [] };
  h.motion = createFlyMotion({ speed: 10, boost: 4 });
  h.view = fpv.createFirstPersonView({
    camera, controls, canvas, window: win, document: doc, motion: h.motion, walker,
    onChange: (kind) => h.changes.push(kind), onModeChange: (mode) => h.modes.push(mode),
    onMessage: (text, tone) => h.messages.push([text, tone]),
  });
  h.key = (code, down = true, fields = {}) => win.dispatchEvent(event(down ? 'keydown' : 'keyup', { code, ...fields }));
  h.look = (movementX, movementY) => doc.dispatchEvent(event('mousemove', { movementX, movementY }));
  h.lastMessage = () => h.messages.at(-1)?.[0] ?? null;
  h.direction = () => camera.getWorldDirection(new Three.Vector3());
  return h;
}

test('entering Fly keeps the camera pose, disables OrbitControls and does not request the mouse', () => {
  const h = harness();
  const position = h.camera.position.clone();
  const direction = h.direction();
  h.view.enterFly();
  assert.equal(h.view.mode, 'fly');
  assert.ok(h.camera.position.distanceTo(position) < 1e-9, 'camera did not move');
  assert.ok(h.direction().dot(direction) > 1 - 1e-9, 'view direction kept');
  assert.equal(h.camera.rotation.order, 'YXZ');
  assert.equal(h.camera.rotation.z, 0, 'no roll');
  assert.equal(h.controls.enabled, false);
  assert.equal(h.camera.near, fpv.FLY_NEAR);
  assert.equal(h.canvas.requests.length, 0, 'pointer lock only on an explicit user gesture');
  assert.deepEqual(h.modes, ['fly']);
  h.view.enterFly();
  assert.deepEqual(h.modes, ['fly'], 'second enter is a no-op');
});

test('G switches to walking: the body lands on the ground, the eye is 1.62 above it, Map ends walking', () => {
  const { blockShape, createWalker } = walkPhysics;
  const walker = createWalker({ shapeAt: (x, y) => blockShape(y < 64 ? 'minecraft:stone' : 'minecraft:air') });
  const h = harness({ walker });
  h.key('KeyG');
  assert.equal(h.view.walking, false, 'G does nothing outside Fly');
  h.view.enterFly();
  h.view.requestLock();
  h.grant();
  h.key('KeyG');
  assert.equal(h.view.walking, true);
  assert.ok(h.changes.includes('walk'));
  for (let i = 0; i < 80; i += 1) h.view.step(0.05);
  assert.ok(Math.abs(h.camera.position.y - (64 + 1.62)) < 1e-6, `eye height: ${h.camera.position.y}`);
  const z = h.camera.position.z;
  h.key('KeyW');
  h.view.step(0.1);
  h.key('KeyW', false);
  assert.ok(h.camera.position.z !== z && Math.abs(h.camera.position.y - 65.62) < 1e-6, 'walks along the ground');
  h.key('KeyG');
  assert.equal(h.view.walking, false, 'G again flies');
  h.key('KeyG');
  h.view.exitToMap();
  assert.equal(h.view.walking, false);
  const noWalker = harness();
  noWalker.view.enterFly();
  noWalker.view.setWalking(true);
  assert.equal(noWalker.view.walking, false, 'walking needs a walker');
});

test('unsupported, rejected or failed pointer lock shows a message and Map stays usable', async () => {
  const unsupported = harness({ lock: 'unsupported' });
  unsupported.view.enterFly();
  unsupported.view.requestLock();
  assert.match(unsupported.lastMessage(), /nepodporuje/);
  unsupported.view.exitToMap();
  assert.equal(unsupported.view.mode, 'orbit');
  assert.equal(unsupported.controls.enabled, true);

  const denied = harness();
  denied.view.enterFly();
  denied.view.requestLock();
  denied.canvas.requests[0].reject(new Error('NotAllowedError'));
  await flush();
  assert.match(denied.lastMessage(), /odmítl.*NotAllowedError/);
  assert.equal(denied.view.mode, 'fly');
  denied.doc.dispatchEvent(new Event('pointerlockerror'));
  assert.match(denied.lastMessage(), /odmítl/);
  denied.view.exitToMap();
  assert.equal(denied.view.mode, 'orbit');
  assert.equal(denied.camera.near, 0.5);
});

test('returning to Map restores the saved orbit pose with damping flushed (no snap)', () => {
  const h = harness();
  h.controls.rotateLeft(0.5);  // leaves residual damping that later updates would still apply
  const position = h.camera.position.clone();
  const target = h.controls.target.clone();
  const quaternion = h.camera.quaternion.clone();
  h.view.enterFly();
  h.view.requestLock();
  h.grant();
  h.key('KeyW');
  for (let i = 0; i < 5; i += 1) h.view.step(0.1);
  h.look(300, -120);
  assert.ok(h.camera.position.distanceTo(position) > 1, 'flying moved the camera');
  h.view.exitToMap();
  assert.equal(h.doc.pointerLockElement, null, 'mouse released');
  assert.ok(h.camera.position.distanceTo(position) < 1e-6, 'orbit position restored');
  assert.ok(h.controls.target.distanceTo(target) < 1e-9, 'orbit target restored');
  assert.ok(Math.abs(Math.abs(h.camera.quaternion.dot(quaternion)) - 1) < 1e-9, 'orientation restored');
  assert.equal(h.camera.near, 0.5);
  assert.equal(h.controls.enabled, true);
  for (let i = 0; i < 20; i += 1) h.controls.update();
  assert.ok(h.camera.position.distanceTo(position) < 1e-6, 'no residual damping motion afterwards');
  assert.deepEqual(h.modes, ['fly', 'orbit']);
});

test('movement follows the view only while locked and reports changes', () => {
  const h = harness();
  h.view.enterFly();
  h.key('KeyW');
  assert.equal(h.view.step(0.1), false, 'no movement before the mouse is locked');
  h.view.requestLock();
  h.grant();
  h.key('KeyW');
  const start = h.camera.position.clone();
  const forward = h.direction();
  assert.equal(h.view.step(0.1), true);
  const moved = h.camera.position.clone().sub(start);
  assert.ok(moved.distanceTo(forward.multiplyScalar(1)) < 1e-9, '10 blocks/s * 0.1 s along the view direction');
  assert.ok(h.changes.includes('move'));
  h.key('Space');
  h.key('KeyW', false);
  const before = h.camera.position.y;
  h.view.step(0.05);
  assert.ok(Math.abs(h.camera.position.y - before - 0.5) < 1e-9, 'Space rises along world Y');
});

test('blur clears keys and releases the mouse; nothing moves or looks afterwards', () => {
  const h = harness();
  h.view.enterFly();
  h.view.requestLock();
  h.grant();
  h.key('KeyW');
  const position = h.camera.position.clone();
  const yaw = h.view.yaw;
  h.win.dispatchEvent(new Event('blur'));
  assert.equal(h.doc.pointerLockElement, null, 'own pointer lock released on blur');
  assert.match(h.lastMessage(), /pozastavené/);
  h.key('KeyW');
  h.look(200, 0);
  assert.equal(h.view.step(0.1), false);
  assert.ok(h.camera.position.distanceTo(position) < 1e-12);
  assert.equal(h.view.yaw, yaw);
  assert.equal(h.view.mode, 'fly', 'Fly stays selected; resume needs a new click');
  h.view.requestLock();
  h.grant();
  h.key('KeyW');
  assert.equal(h.view.step(0.1), true, 'resumes after an explicit new lock');
});

test('an unfocused document or a focused form field blocks keys, movement and look', () => {
  const h = harness();
  let hasFocus = true;
  h.doc.hasFocus = () => hasFocus;
  h.view.enterFly();
  h.view.requestLock();
  h.grant();
  h.key('KeyW');
  hasFocus = false;  // focus left without a blur event reaching us
  const position = h.camera.position.clone();
  const yaw = h.view.yaw;
  assert.equal(h.view.step(0.1), false, 'held key cleared, no movement');
  h.key('KeyD');
  h.look(150, 40);
  assert.equal(h.view.step(0.1), false);
  assert.equal(h.view.yaw, yaw, 'no mouse look while unfocused');
  hasFocus = true;
  assert.equal(h.view.step(0.1), false, 'keys pressed while unfocused were not kept');
  assert.ok(h.camera.position.distanceTo(position) < 1e-12);
  h.doc.activeElement = { closest: () => ({ tagName: 'SELECT' }) };
  h.key('KeyW');
  h.look(150, 40);
  assert.equal(h.view.step(0.1), false, 'form field focused');
  assert.equal(h.view.yaw, yaw);
  h.doc.activeElement = null;
  h.key('KeyW');
  assert.equal(h.view.step(0.1), true);
});

test('a lock granted after blur, a hidden tab or dispose is released', async () => {
  const blurred = harness();
  blurred.view.enterFly();
  blurred.view.requestLock();
  blurred.win.dispatchEvent(new Event('blur'));
  blurred.grant();
  await flush();
  assert.equal(blurred.doc.pointerLockElement, null, 'late grant after blur');
  blurred.key('KeyW');
  assert.equal(blurred.view.step(0.1), false);

  const hidden = harness();
  hidden.view.enterFly();
  hidden.view.requestLock();
  hidden.grant();
  hidden.doc.hidden = true;
  hidden.doc.dispatchEvent(new Event('visibilitychange'));
  assert.equal(hidden.doc.pointerLockElement, null, 'hidden tab releases the lock');
  hidden.view.requestLock();
  hidden.doc.dispatchEvent(new Event('visibilitychange'));
  hidden.grant();
  await flush();
  assert.equal(hidden.doc.pointerLockElement, null, 'late grant while hidden');

  const disposed = harness({ lock: 'legacy' });
  disposed.view.enterFly();
  disposed.view.requestLock();
  disposed.view.dispose();
  disposed.grant();
  assert.equal(disposed.doc.pointerLockElement, null, 'late legacy grant after dispose');
});

test('hidden tab, unlock, form focus and modifiers stop movement', () => {
  const h = harness();
  h.view.enterFly();
  h.view.requestLock();
  h.grant();
  h.key('KeyW');
  h.doc.hidden = true;
  assert.equal(h.view.step(0.1), false, 'hidden tab');
  h.doc.hidden = false;
  assert.equal(h.view.step(0.1), false, 'keys were cleared while hidden');
  h.key('KeyW');
  assert.equal(h.view.step(0.1), true);
  const position = h.camera.position.clone();
  h.doc.exitPointerLock();  // Escape: the browser ends pointer lock
  assert.match(h.lastMessage(), /pozastavené/);
  assert.equal(h.view.step(0.1), false);
  assert.ok(h.camera.position.distanceTo(position) < 1e-12, 'unlock leaves the camera stationary');
  h.key('KeyW');
  assert.equal(h.view.step(0.1), false, 'keys are ignored while unlocked');
  h.view.requestLock();
  h.grant();
  h.doc.activeElement = { closest: () => ({ tagName: 'INPUT' }) };
  h.key('KeyW');
  assert.equal(h.view.step(0.1), false, 'focus in a form field');
  h.doc.activeElement = null;
  h.key('KeyW', true, { ctrlKey: true });
  assert.equal(h.view.step(0.1), false, 'Ctrl shortcuts are not movement');
  h.key('KeyW');
  h.doc.dispatchEvent(new Event('focusin'));
  assert.equal(h.view.step(0.1), false, 'focus moving into the page clears keys');
});

test('mouse look turns, clamps pitch and never rolls', () => {
  const h = harness();
  h.view.enterFly();
  const yaw = h.view.yaw;
  h.look(100, 0);
  assert.equal(h.view.yaw, yaw, 'mouse ignored while unlocked');
  h.view.requestLock();
  h.grant();
  h.look(100, 0);
  assert.ok(Math.abs(h.view.yaw - (yaw - 100 * fpv.LOOK_RADIANS_PER_PIXEL)) < 1e-12);
  h.look(0, -1e6);
  assert.equal(h.view.pitch, fpv.PITCH_LIMIT);
  assert.ok(Math.abs(h.direction().y - Math.sin(fpv.PITCH_LIMIT)) < 1e-9);
  h.look(0, 1e6);
  assert.equal(h.view.pitch, -fpv.PITCH_LIMIT);
  for (let i = 0; i < 50; i += 1) h.look(977, 13);
  assert.equal(h.camera.rotation.z, 0, 'no roll after many turns');
  assert.ok(Math.abs(h.view.yaw) <= Math.PI, 'yaw stays wrapped');
  const before = h.view.yaw;
  h.look(NaN, 5);
  assert.equal(h.view.yaw, before, 'non-finite movement ignored');
  assert.ok(h.changes.includes('look'));
});

test('OrbitControls.update is never called while flying', () => {
  const h = harness();
  let updates = 0;
  const original = h.controls.update.bind(h.controls);
  h.controls.update = (...args) => { updates += 1; return original(...args); };
  h.view.enterFly();
  h.view.requestLock();
  h.grant();
  h.key('KeyD');
  h.look(40, 20);
  for (let i = 0; i < 10; i += 1) h.view.step(0.05);
  h.doc.exitPointerLock();
  h.view.requestLock();
  h.grant();
  assert.equal(updates, 0);
  h.view.exitToMap();
  assert.equal(updates, 1, 'one update when OrbitControls takes over again');
});

test('a pointer lock that arrives after leaving Fly is released immediately', async () => {
  const h = harness();
  h.view.enterFly();
  h.view.requestLock();
  h.view.exitToMap();
  h.grant();  // late success of the earlier request
  await flush();
  assert.equal(h.doc.pointerLockElement, null);
  assert.ok(h.doc.exitCalls >= 1);
  assert.equal(h.view.mode, 'orbit');
  const position = h.camera.position.clone();
  h.key('KeyW');
  assert.equal(h.view.step(0.1), false);
  assert.ok(h.camera.position.distanceTo(position) < 1e-12);

  const legacy = harness({ lock: 'legacy' });  // requestPointerLock without a promise
  legacy.view.enterFly();
  legacy.view.requestLock();
  legacy.view.exitToMap();
  legacy.grant();
  assert.equal(legacy.doc.pointerLockElement, null, 'released through pointerlockchange as well');
});

test('dispose releases the mode and listeners', () => {
  const h = harness();
  h.view.enterFly();
  h.view.requestLock();
  h.grant();
  h.view.dispose();
  h.view.dispose();
  assert.equal(h.view.mode, 'orbit');
  assert.equal(h.doc.pointerLockElement, null);
  assert.equal(h.controls.enabled, true);
  const changes = h.changes.length;
  h.look(10, 10);
  h.key('KeyW');
  h.view.enterFly();
  assert.equal(h.view.mode, 'orbit', 'no re-entry after dispose');
  assert.equal(h.changes.length, changes);
});

test('drag fallback flies without pointer lock, looks only during drag, Escape pauses', () => {
  const h = harness({ lock: 'unsupported' });
  h.canvas.focus = () => { h.doc.activeElement = h.canvas; };
  h.view.enterFly();
  h.view.enableDragLook();
  assert.equal(h.view.dragMode, true);
  assert.equal(h.doc.pointerLockElement, null);
  h.key('KeyW');
  assert.equal(h.view.step(0.1), true);
  h.key('KeyW', false);
  const yaw = h.view.yaw;
  h.canvas.dispatchEvent(event('pointermove', { pointerId: 1, clientX: 300, clientY: 200 }));
  assert.equal(h.view.yaw, yaw);
  h.canvas.dispatchEvent(event('pointerdown', { pointerId: 1, button: 0, clientX: 300, clientY: 200 }));
  h.canvas.dispatchEvent(event('pointermove', { pointerId: 1, clientX: 340, clientY: 200 }));
  assert.ok(Math.abs(h.view.yaw - yaw + 40 * fpv.LOOK_RADIANS_PER_PIXEL) < 1e-9);
  h.canvas.dispatchEvent(event('pointerup', { pointerId: 1 }));
  const stopped = h.view.yaw;
  h.canvas.dispatchEvent(event('pointermove', { pointerId: 1, clientX: 500, clientY: 200 }));
  assert.equal(h.view.yaw, stopped);
  h.key('Escape');
  assert.equal(h.view.dragMode, false);
  h.key('KeyW');
  assert.equal(h.view.step(0.1), false);
  h.view.enableDragLook();
  h.win.dispatchEvent(new Event('blur'));
  assert.equal(h.view.dragMode, false);
  assert.equal(h.view.step(0.1), false);
});

test('switching drag to pointer lock then browser Escape cannot leave drag movement active', () => {
  const h = harness();
  h.canvas.focus = () => { h.doc.activeElement = h.canvas; };
  h.view.enterFly(); h.view.enableDragLook();
  h.view.requestLock(); h.grant();
  assert.equal(h.view.dragMode, false);
  h.doc.exitPointerLock();
  h.key('KeyW');
  assert.equal(h.view.step(0.1), false);
  h.view.enableDragLook();
  h.doc.activeElement = {};
  h.doc.dispatchEvent(new Event('focusin'));
  assert.equal(h.view.dragMode, false);
  assert.match(h.lastMessage(), /pozastavené/);
});

test('walking messages describe walking during drag and after pointer lock is released', () => {
  const walker = walkPhysics.createWalker({ shapeAt: () => walkPhysics.blockShape('minecraft:air') });
  const h = harness({ walker });
  h.canvas.focus = () => { h.doc.activeElement = h.canvas; };
  h.view.enterFly();
  h.view.enableDragLook();
  assert.match(h.lastMessage(), /WASD létá/);
  h.view.setWalking(true);
  assert.match(h.lastMessage(), /WASD chodí, mezerník skáče/);
  h.view.setWalking(false);
  assert.match(h.lastMessage(), /WASD létá/);
  h.view.setWalking(true);
  h.win.dispatchEvent(new Event('blur'));
  assert.match(h.lastMessage(), /^Chůze je pozastavená/);
  h.view.requestLock();
  h.grant();
  h.doc.exitPointerLock();
  assert.match(h.lastMessage(), /^Chůze je pozastavená/);
  h.view.setWalking(false);
  assert.match(h.lastMessage(), /^Létání je pozastavené/);
  h.view.setWalking(true);
  assert.match(h.lastMessage(), /^Chůze je pozastavená/);
});
