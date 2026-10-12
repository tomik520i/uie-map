// First-person FLY movement (not walk): pure state machine, no Three.js, DOM, listeners or pointer lock.
// There is no gravity and no collision; the camera passes through terrain.
//
// Key mapping (KeyboardEvent.code):
//   forward  KeyW, ArrowUp          back   KeyS, ArrowDown
//   left     KeyA, ArrowLeft        right  KeyD, ArrowRight
//   up       Space, KeyE            down   KeyQ
//   boost    ShiftLeft, ShiftRight  (Ctrl is avoided: it collides with browser shortcuts)
//
// API
//   const fly = createFlyMotion({ speed = 10, boost = 4 });   // blocks per second, boost multiplier
//   fly.handleKey(code, down) -> true if the code belongs to fly movement (caller may preventDefault)
//   fly.clear()                   release everything (call on blur, pointer-lock exit, focus in forms, hidden tab)
//   fly.step(dt, yaw, pitch) -> { x, y, z }   world displacement for this frame
//   fly.setSpeed(speed), fly.setBoost(multiplier), fly.speed, fly.boost, fly.isActive()
//   fly.input() -> { forward, right, jump, down, boost }   raw input for walk mode (walk-physics.js)
//
// Orientation (Three.js camera convention, Euler order 'YXZ'): yaw rotates about world +Y, pitch looks up
// when positive. yaw = pitch = 0 looks along -Z; forward = (-sin yaw * cos pitch, sin pitch, -cos yaw * cos pitch),
// right = (cos yaw, 0, -sin yaw). Forward follows yaw AND pitch (flight); up/down always use world Y.
// The combined input vector is normalised, so diagonals and forward+up never exceed speed; opposing inputs
// cancel, and aliases (W + ArrowUp) do not add up. dt is clamped to 0..0.1 s so a resumed tab cannot
// teleport; dt <= 0 gives no movement. A key pressed and released between two steps still moves for one step.
// The caller filters events (form focus, Ctrl/Meta/Alt shortcuts) before calling handleKey.

const ACTIONS = new Map([
  ['KeyW', 'forward'], ['ArrowUp', 'forward'], ['KeyS', 'back'], ['ArrowDown', 'back'],
  ['KeyA', 'left'], ['ArrowLeft', 'left'], ['KeyD', 'right'], ['ArrowRight', 'right'],
  ['Space', 'up'], ['KeyE', 'up'], ['KeyQ', 'down'], ['ShiftLeft', 'boost'], ['ShiftRight', 'boost'],
]);
export const MAX_STEP_SECONDS = 0.1;

const positive = (value, label) => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    throw new RangeError(`${label} must be a positive finite number`);
  }
  return value;
};
const finite = (value, label) => {
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new RangeError(`${label} must be a finite number`);
  return value;
};

export function createFlyMotion({ speed = 10, boost = 4 } = {}) {
  let baseSpeed = positive(speed, 'speed');
  let boostFactor = positive(boost, 'boost');
  const held = new Set();     // key codes currently down
  const tapped = new Set();   // codes pressed since the last step (a tap still moves once)

  const actions = () => new Set([...held, ...tapped].map((code) => ACTIONS.get(code)));

  return {
    handleKey(code, down) {
      if (!ACTIONS.has(code)) return false;
      if (down) {
        if (!held.has(code)) tapped.add(code);
        held.add(code);
      } else {
        held.delete(code);
      }
      return true;
    },
    clear() {
      held.clear();
      tapped.clear();
    },
    isActive() {
      const a = actions();
      return ['forward', 'back', 'left', 'right', 'up', 'down'].some((name) => a.has(name));
    },
    step(dt, yaw, pitch) {
      finite(dt, 'dt');
      finite(yaw, 'yaw');
      finite(pitch, 'pitch');
      const seconds = Math.min(Math.max(dt, 0), MAX_STEP_SECONDS);
      if (seconds === 0) return { x: 0, y: 0, z: 0 };  // keeps a pending tap for the next real step
      const a = actions();
      tapped.clear();
      const f = (a.has('forward') ? 1 : 0) - (a.has('back') ? 1 : 0);
      const r = (a.has('right') ? 1 : 0) - (a.has('left') ? 1 : 0);
      const u = (a.has('up') ? 1 : 0) - (a.has('down') ? 1 : 0);
      const cp = Math.cos(pitch);
      const vx = f * -Math.sin(yaw) * cp + r * Math.cos(yaw);
      const vy = f * Math.sin(pitch) + u;
      const vz = f * -Math.cos(yaw) * cp - r * Math.sin(yaw);
      const length = Math.hypot(vx, vy, vz);
      if (length < 1e-9) return { x: 0, y: 0, z: 0 };
      const distance = baseSpeed * (a.has('boost') ? boostFactor : 1) * seconds / length;
      return { x: vx * distance, y: vy * distance, z: vz * distance };
    },
    /** Walk-mode input snapshot { forward, right, jump, down, boost }; consumes taps like step(). */
    input() {
      const a = actions();
      tapped.clear();
      return {
        forward: (a.has('forward') ? 1 : 0) - (a.has('back') ? 1 : 0),
        right: (a.has('right') ? 1 : 0) - (a.has('left') ? 1 : 0),
        jump: a.has('up'),
        down: a.has('down'),
        boost: a.has('boost'),
      };
    },
    setSpeed(value) { baseSpeed = positive(value, 'speed'); },
    setBoost(value) { boostFactor = positive(value, 'boost'); },
    get speed() { return baseSpeed; },
    get boost() { return boostFactor; },
  };
}
