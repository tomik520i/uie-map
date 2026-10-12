// Tests for web/first-person-motion.js (fly movement). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_first_person_motion.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// web/ has no package.json, so load the real module source as ESM independent of Node's .js detection.
const source = readFileSync(new URL('./web/first-person-motion.js', import.meta.url), 'utf8');
const { createFlyMotion, MAX_STEP_SECONDS } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const DEG = Math.PI / 180;
const close = (actual, expected, label) => {
  for (const axis of ['x', 'y', 'z']) {
    assert.ok(Math.abs(actual[axis] - expected[axis]) < 1e-9, `${label}: ${axis} ${actual[axis]} != ${expected[axis]}`);
  }
};
const length = (v) => Math.hypot(v.x, v.y, v.z);
const hold = (fly, ...codes) => codes.forEach((code) => fly.handleKey(code, true));

test('forward follows yaw and pitch at known angles', () => {
  const fly = createFlyMotion({ speed: 10 });
  hold(fly, 'KeyW');
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: -1 }, 'yaw 0 looks along -Z');
  close(fly.step(0.1, 90 * DEG, 0), { x: -1, y: 0, z: 0 }, 'yaw +90 turns left, towards -X');
  close(fly.step(0.1, -90 * DEG, 0), { x: 1, y: 0, z: 0 }, 'yaw -90 towards +X');
  close(fly.step(0.1, 180 * DEG, 0), { x: 0, y: 0, z: 1 }, 'yaw 180 towards +Z');
  const s = Math.SQRT1_2;
  close(fly.step(0.1, 0, 45 * DEG), { x: 0, y: s, z: -s }, 'pitch up 45 climbs');
  close(fly.step(0.1, 0, -90 * DEG), { x: 0, y: -1, z: 0 }, 'looking straight down dives');
});

test('strafe, backwards and vertical keys', () => {
  const fly = createFlyMotion({ speed: 10 });
  const cases = [
    [['KeyD'], { x: 1, y: 0, z: 0 }], [['KeyA'], { x: -1, y: 0, z: 0 }], [['KeyS'], { x: 0, y: 0, z: 1 }],
    [['Space'], { x: 0, y: 1, z: 0 }], [['KeyE'], { x: 0, y: 1, z: 0 }], [['KeyQ'], { x: 0, y: -1, z: 0 }],
  ];
  for (const [codes, expected] of cases) {
    fly.clear();
    hold(fly, ...codes);
    close(fly.step(0.1, 0, 0), expected, codes.join('+'));
  }
  fly.clear();
  hold(fly, 'KeyD');
  close(fly.step(0.1, 0, 60 * DEG), { x: 1, y: 0, z: 0 }, 'strafe stays horizontal when pitched');
  fly.clear();
  hold(fly, 'Space');
  close(fly.step(0.1, 1.2, -70 * DEG), { x: 0, y: 1, z: 0 }, 'up is world Y regardless of view');
});

test('arrow aliases match WASD and do not add up', () => {
  const fly = createFlyMotion({ speed: 10 });
  const pairs = [['KeyW', 'ArrowUp'], ['KeyS', 'ArrowDown'], ['KeyA', 'ArrowLeft'], ['KeyD', 'ArrowRight'], ['Space', 'KeyE']];
  for (const [a, b] of pairs) {
    fly.clear();
    hold(fly, a);
    const one = fly.step(0.1, 0.3, 0.2);
    fly.clear();
    hold(fly, b);
    close(fly.step(0.1, 0.3, 0.2), one, `${b} equals ${a}`);
    hold(fly, a);
    close(fly.step(0.1, 0.3, 0.2), one, `${a}+${b} is not faster`);
  }
});

test('diagonals are normalised in 3D and opposing keys cancel', () => {
  const fly = createFlyMotion({ speed: 10 });
  hold(fly, 'KeyW', 'KeyD');
  const diagonal = fly.step(0.1, 0, 0);
  assert.ok(Math.abs(length(diagonal) - 1) < 1e-9);
  close(diagonal, { x: Math.SQRT1_2, y: 0, z: -Math.SQRT1_2 }, 'forward-right');
  hold(fly, 'Space');
  assert.ok(Math.abs(length(fly.step(0.1, 0, 0)) - 1) < 1e-9, 'forward+right+up still speed');
  for (const [yaw, pitch] of [[0.7, 0.4], [-2, -1.1], [3, 1.5]]) {
    assert.ok(Math.abs(length(fly.step(0.1, yaw, pitch)) - 1) < 1e-9, `${yaw},${pitch}`);
  }
  fly.clear();
  hold(fly, 'KeyW', 'KeyS', 'KeyA', 'ArrowRight', 'Space', 'KeyQ');
  close(fly.step(0.1, 0.5, 0.5), { x: 0, y: 0, z: 0 }, 'all opposing inputs cancel');
  fly.clear();
  hold(fly, 'KeyW', 'KeyQ');
  close(fly.step(0.1, 0, 90 * DEG), { x: 0, y: 0, z: 0 }, 'forward up while diving cancels to zero, not NaN');
});

test('boost multiplies speed; Shift alone does not move', () => {
  const fly = createFlyMotion({ speed: 10, boost: 4 });
  hold(fly, 'ShiftLeft');
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: 0 }, 'shift alone');
  assert.equal(fly.isActive(), false);
  hold(fly, 'KeyW');
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: -4 }, 'boosted');
  fly.handleKey('ShiftLeft', false);
  hold(fly, 'ShiftRight');
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: -4 }, 'right shift too');
  fly.handleKey('ShiftRight', false);
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: -1 }, 'released');
});

test('release, clear and taps', () => {
  const fly = createFlyMotion({ speed: 10 });
  hold(fly, 'KeyW');
  assert.ok(fly.isActive());
  fly.clear();
  assert.equal(fly.isActive(), false);
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: 0 }, 'nothing held after clear');
  hold(fly, 'KeyW');
  fly.handleKey('KeyW', false);
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: -1 }, 'a tap between frames moves once');
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: 0 }, 'and only once');
  hold(fly, 'KeyD');
  fly.handleKey('KeyD', false);
  close(fly.step(0, 0, 0), { x: 0, y: 0, z: 0 }, 'zero dt');
  close(fly.step(0.1, 0, 0), { x: 1, y: 0, z: 0 }, 'tap survives a zero-dt frame');
  hold(fly, 'KeyW');
  hold(fly, 'KeyW');  // auto-repeat
  fly.step(0.1, 0, 0);
  fly.handleKey('KeyW', false);
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: 0 }, 'repeat does not leave a stale tap');
  assert.equal(fly.handleKey('KeyZ', true), false);
  assert.equal(fly.handleKey('ControlLeft', true), false);
  assert.equal(fly.handleKey('KeyW', true), true);
});

test('long frames are capped and negative dt gives no movement', () => {
  const fly = createFlyMotion({ speed: 10 });
  hold(fly, 'KeyW');
  close(fly.step(5, 0, 0), { x: 0, y: 0, z: -10 * MAX_STEP_SECONDS }, 'tab resume');
  close(fly.step(0.05, 0, 0), { x: 0, y: 0, z: -0.5 }, 'ordinary frame');
  close(fly.step(-1, 0, 0), { x: 0, y: 0, z: 0 }, 'negative dt');
});

test('speed changes and validation', () => {
  const fly = createFlyMotion();
  assert.equal(fly.speed, 10);
  assert.equal(fly.boost, 4);
  fly.setSpeed(2.5);
  fly.setBoost(2);
  hold(fly, 'KeyW', 'ShiftLeft');
  close(fly.step(0.1, 0, 0), { x: 0, y: 0, z: -0.5 }, '2.5 * 2 * 0.1');
  for (const bad of [0, -1, NaN, Infinity, '5', null]) {
    assert.throws(() => fly.setSpeed(bad), RangeError, `speed ${bad}`);
    assert.throws(() => fly.setBoost(bad), RangeError, `boost ${bad}`);
    assert.throws(() => createFlyMotion({ speed: bad }), RangeError);
  }
  assert.equal(fly.speed, 2.5, 'invalid values leave the previous speed');
  for (const args of [[NaN, 0, 0], [Infinity, 0, 0], [0.1, NaN, 0], [0.1, 0, -Infinity], ['0.1', 0, 0], [0.1, undefined, 0]]) {
    assert.throws(() => fly.step(...args), RangeError, JSON.stringify(args));
  }
});
