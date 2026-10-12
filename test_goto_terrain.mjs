import assert from 'node:assert/strict';
import test from 'node:test';
import { createGotoTerrain } from './web/goto-terrain.js';

test('goto before index arrives can settle after caller matches the target tile', () => {
  const goto = createGotoTerrain();
  goto.start({ key: null, x: 80, z: -90, targetY: 128 });
  assert.equal(goto.resolve({ key: '1,-2', height: 81, targetX: 80, targetZ: -90, targetY: 128 }), -47);
  assert.equal(goto.pending, false);
});

test('one matching tile corrects Y once without changing orbit offset', () => {
  let time = 1000;
  const goto = createGotoTerrain({ now: () => time });
  goto.start({ key: '1,-2', x: 80, z: -90, targetY: 70 });
  const target = { x: 80, y: 70, z: -90 };
  const camera = { x: 140, y: 160, z: -30 };
  assert.equal(goto.resolve({ key: '2,-2', height: 110, targetX: 80, targetZ: -90, targetY: 70 }), null);
  assert.equal(goto.pending, true);
  const delta = goto.resolve({ key: '1,-2', height: 110, targetX: target.x, targetZ: target.z,
    targetY: target.y });
  assert.equal(delta, 40);
  target.y += delta; camera.y += delta;
  assert.deepEqual(target, { x: 80, y: 110, z: -90 });
  assert.deepEqual(camera, { x: 140, y: 200, z: -30 });
  assert.equal(goto.resolve({ key: '1,-2', height: 120, targetX: 80, targetZ: -90, targetY: 110 }), null);
  time += 1000;
});

test('manual cancellation and a newer goto prevent a late correction', () => {
  const goto = createGotoTerrain();
  goto.start({ key: '1,-2', x: 80, z: -90, targetY: 70 });
  goto.cancel();
  assert.equal(goto.resolve({ key: '1,-2', height: 110, targetX: 80, targetZ: -90, targetY: 70 }), null);
  goto.start({ key: '1,-2', x: 80, z: -90, targetY: 70 });
  goto.start({ key: '17,-120', x: 1102, z: -7656, targetY: 70 });
  assert.equal(goto.resolve({ key: '1,-2', height: 110, targetX: 1102, targetZ: -7656, targetY: 70 }), null);
  assert.equal(goto.pending, true);
});

test('changed target, fly mode, expired intent and unknown height cannot retarget', () => {
  let time = 0;
  const goto = createGotoTerrain({ now: () => time, maxWaitMs: 100 });
  const start = () => goto.start({ key: '1,-2', x: 80, z: -90, targetY: 70 });
  const resolve = (extra = {}) => goto.resolve({ key: '1,-2', height: 100, targetX: 80,
    targetZ: -90, targetY: 70, ...extra });
  start();
  assert.equal(resolve({ height: null }), null);
  assert.equal(goto.pending, true);
  assert.equal(resolve({ targetX: 81 }), null);
  start();
  assert.equal(resolve({ orbit: false }), null);
  start();
  time = 101;
  assert.equal(resolve(), null);
});
