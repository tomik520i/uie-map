// First-person WALK physics: pure, no Three.js or DOM. Minecraft-like body (0.6 x 1.8, eye 1.62), gravity,
// jump, 0.6 auto-step, swimming and ladders. Collision shapes come from block names (UIE_BLOCKS1 registry):
//
//   blockShape(name, meta) -> { kind: 'solid' | 'pass' | 'liquid' | 'climb', boxes: [[x0,y0,z0,x1,y1,z1], ...] }
//     boxes are block-local (0..1, fences 1.5 high). Unknown names are solid full cubes (safe default);
//     plants, torches, signs, rails, doors, clouds and similar are passable. An approximation of 1.7.10 collision
//     boxes: doors are always open; connected fences/walls and modded shapes remain approximate.
//     Vanilla trapdoors, fence gates, cactus and soul sand follow the 1.7.10 collision bytecode.
//
//   createWalker({ shapeAt }) where shapeAt(x, y, z) (integer cell) returns a blockShape result, or undefined
//   when the cell is not known (block data not loaded / outside the export). Unknown cells block like a wall,
//   and gravity waits while the cell under the feet is unknown, so nobody falls through unloaded ground.
//     .place({x, y, z})      feet position; moved up until the body does not overlap anything
//     .step(dt, input) -> bool   input { forward, right, jump, boost, yaw } (forward/right in -1..1); true if moved
//     .position (feet), .onGround, .inLiquid, .waiting
//
// Axis order Y, X, Z as in Minecraft; a blocked horizontal move on the ground retries 0.6 higher (stairs, slabs).

export const BODY = { half: 0.3, height: 1.8, eye: 1.62 };
export const WALK_SPEED = 4.3;
export const SPRINT_FACTOR = 1.3;
export const GRAVITY = 32;
export const JUMP_SPEED = 9;           // ~1.25 blocks
export const MAX_FALL = 60;
export const STEP_HEIGHT = 0.6;
export const MAX_SUBSTEP = 0.05;
export const MAX_DT = 0.1;
export const MAX_RECOVERY_RISE = 2;

const FULL = [0, 0, 0, 1, 1, 1];
const SOLID = Object.freeze({ kind: 'solid', boxes: [FULL] });
const PASS = Object.freeze({ kind: 'pass', boxes: [] });
const LIQUID = Object.freeze({ kind: 'liquid', boxes: [] });
const CLIMB = Object.freeze({ kind: 'climb', boxes: [] });

const VANILLA_PASS = new Set(['air', 'tallgrass', 'deadbush', 'yellow_flower', 'red_flower', 'double_plant', 'sapling',
  'torch', 'redstone_torch', 'unlit_redstone_torch', 'redstone_wire', 'rail', 'golden_rail', 'detector_rail',
  'activator_rail', 'reeds', 'web', 'wheat', 'carrots', 'potatoes', 'nether_wart', 'pumpkin_stem', 'melon_stem',
  'lever', 'stone_button', 'wooden_button', 'wall_sign', 'standing_sign', 'tripwire', 'tripwire_hook', 'fire',
  'portal', 'end_portal', 'brown_mushroom', 'red_mushroom', 'stone_pressure_plate', 'wooden_pressure_plate',
  'light_weighted_pressure_plate', 'heavy_weighted_pressure_plate', 'wooden_door', 'iron_door',
  'waterlily', 'cocoa', 'skull', 'flower_pot']);
const VANILLA_LIQUID = new Set(['water', 'flowing_water', 'lava', 'flowing_lava']);
// Checked against every registry name of the LIVE area (273 names, 2026-10-02).
const MOD_PASS = /(flower|foliage|plant|sapling|tallgrass|crop|torch|sign|sprout|bush|reed|cloud|door|grass_?tall|tall_?grass|[._]tall$|coral|lily|garden|bluebell|^pam|pineapple|airy|pod$|pad$|^fire|^air$)/i;
const MOD_NOT_PASS = /(spawn|ore|block_|_block)/i;   // e.g. OreSpawn_CloudSharkSpawnBlock is an ore, not a cloud
const MOD_LIQUID = /(fluid|liquid|molten)|(water|lava|oil)$|\.(still|flowing)$/i;
const MOD_NOT_LIQUID = /(tank|pipe|duct|conduit|machine|cell|pump|valve|drain|transposer)/i;
const MOD_CLIMB = /(ladder|vine|rope|^willow$|^moss$)/i;

const memo = new Map();

export function blockShape(name, meta = 0) {
  const key = `${name}\u0000${meta}`;
  let shape = memo.get(key);
  if (!shape) {
    shape = classify(String(name ?? ''), meta | 0);
    memo.set(key, shape);
  }
  return shape;
}

function classify(name, meta) {
  const colon = name.indexOf(':');
  const domain = colon >= 0 ? name.slice(0, colon) : 'minecraft';
  const path = colon >= 0 ? name.slice(colon + 1) : name;
  if (!path) return PASS;
  if (domain === 'minecraft') {
    if (VANILLA_PASS.has(path)) return PASS;
    if (VANILLA_LIQUID.has(path)) return LIQUID;
    if (path === 'ladder' || path === 'vine') return CLIMB;
    if (path === 'snow_layer') {
      const h = (meta & 7) / 8;
      return h <= 0 ? PASS : { kind: 'solid', boxes: [[0, 0, 0, 1, h, 1]] };
    }
    if (path === 'carpet') return { kind: 'solid', boxes: [[0, 0, 0, 1, 1 / 16, 1]] };
    if (path === 'bed') return { kind: 'solid', boxes: [[0, 0, 0, 1, 0.5625, 1]] };
    if (path === 'trapdoor') return trapdoor(meta);
    if (path === 'fence_gate') return meta & 4 ? PASS : { kind: 'solid', boxes: [
      meta === 0 || meta === 2 ? [0, 0, 0.375, 1, 1.5, 0.625] : [0.375, 0, 0, 0.625, 1.5, 1],
    ] };
    if (path === 'cactus') return { kind: 'solid', boxes: [[0.0625, 0, 0.0625, 0.9375, 0.9375, 0.9375]] };
    if (path === 'soul_sand') return { kind: 'solid', boxes: [[0, 0, 0, 1, 0.875, 1]] };
    if (path === 'fence' || path === 'nether_brick_fence' || path === 'cobblestone_wall') {
      return { kind: 'solid', boxes: [[0, 0, 0, 1, 1.5, 1]] };
    }
  } else {
    if (MOD_CLIMB.test(path)) return CLIMB;
    if (MOD_LIQUID.test(path) && !MOD_NOT_LIQUID.test(path)) return LIQUID;
    if (/mushroom/i.test(path) && !/(huge|block|cap|stem)/i.test(path)) return PASS;
    if (MOD_PASS.test(path) && !MOD_NOT_PASS.test(path)) return PASS;
    if (/fence_?gate/i.test(path)) return meta & 4 ? PASS : { kind: 'solid', boxes: [[0, 0, 0, 1, 1.5, 1]] };
    if (/(fence|wall$)/i.test(path)) return { kind: 'solid', boxes: [[0, 0, 0, 1, 1.5, 1]] };
  }
  if (/stairs/i.test(path)) return stairs(meta);
  if (/slab/i.test(path) && !/double/i.test(path)) {
    return { kind: 'solid', boxes: [meta & 8 ? [0, 0.5, 0, 1, 1, 1] : [0, 0, 0, 1, 0.5, 1]] };
  }
  return SOLID;
}

// BlockTrapDoor.b(int), vanilla 1.7.10: 3/16 panel, bit 4 opens, bit 8 puts a closed panel at the top.
// Open panels keep a vertical collision box; they are not air. No neighbor metadata is required.
function trapdoor(meta) {
  if (!(meta & 4)) return { kind: 'solid', boxes: [meta & 8
    ? [0, 0.8125, 0, 1, 1, 1] : [0, 0, 0, 1, 0.1875, 1]] };
  const panel = [[0, 0, 0.8125, 1, 1, 1], [0, 0, 0, 1, 1, 0.1875],
    [0.8125, 0, 0, 1, 1, 1], [0, 0, 0, 0.1875, 1, 1]][meta & 3];
  return { kind: 'solid', boxes: [panel] };
}

// 1.7.10 BlockStairs: low bits = ascending direction (0 east, 1 west, 2 south, 3 north), bit 4 = upside down.
function stairs(meta) {
  const top = (meta & 4) !== 0;
  const half = top ? [0, 0.5, 0, 1, 1, 1] : [0, 0, 0, 1, 0.5, 1];
  const [y0, y1] = top ? [0, 0.5] : [0.5, 1];
  const quarter = [[0.5, y0, 0, 1, y1, 1], [0, y0, 0, 0.5, y1, 1], [0, y0, 0.5, 1, y1, 1], [0, y0, 0, 1, y1, 0.5]][meta & 3];
  return { kind: 'solid', boxes: [half, quarter] };
}

const EPS = 1e-7;

export function createWalker({ shapeAt }) {
  const pos = { x: 0, y: 0, z: 0 };
  let vy = 0;
  let onGround = false;
  let inLiquid = false;
  let waiting = false;
  let waitingReason = null;
  let lastSafe = null;

  const box = (x, y, z) => [x - BODY.half, y, z - BODY.half, x + BODY.half, y + BODY.height, z + BODY.half];
  const shape = (x, y, z) => {
    if (y < 0) return SOLID;
    if (y > 255) return PASS;
    return shapeAt(x, y, z);
  };

  // World boxes of every cell touching [a, b] (inclusive bounds), unknown cells as full cubes.
  function collect(a) {
    const out = [];
    for (let cx = Math.floor(a[0]); cx <= Math.floor(a[3] - EPS); cx += 1) {
      for (let cz = Math.floor(a[2]); cz <= Math.floor(a[5] - EPS); cz += 1) {
        for (let cy = Math.floor(a[1]) - 1; cy <= Math.floor(a[4] - EPS); cy += 1) {
          const s = shape(cx, cy, cz) ?? SOLID;
          for (const b of s.boxes) out.push([cx + b[0], cy + b[1], cz + b[2], cx + b[3], cy + b[4], cz + b[5]]);
        }
      }
    }
    return out;
  }

  const overlap = (a, b, i) => a[i] < b[i + 3] - EPS && a[i + 3] > b[i] + EPS;

  // Clip a movement along axis (0 x, 1 y, 2 z) against boxes; returns the allowed distance.
  function clip(boxes, a, axis, d) {
    if (d === 0) return 0;
    const o1 = axis === 0 ? 1 : 0;
    const o2 = axis === 2 ? 1 : 2;
    for (const b of boxes) {
      if (!overlap(a, b, o1) || !overlap(a, b, o2)) continue;
      if (d > 0 && b[axis] >= a[axis + 3] - EPS) d = Math.min(d, b[axis] - a[axis + 3]);
      else if (d < 0 && b[axis + 3] <= a[axis] + EPS) d = Math.max(d, b[axis + 3] - a[axis]);
    }
    return Math.abs(d) < EPS ? 0 : d;
  }

  function shift(a, axis, d) { a[axis] += d; a[axis + 3] += d; }

  function sweep(boxes, start, dx, dy, dz) {
    const a = start.slice();
    const my = clip(boxes, a, 1, dy); shift(a, 1, my);
    const mx = clip(boxes, a, 0, dx); shift(a, 0, mx);
    const mz = clip(boxes, a, 2, dz); shift(a, 2, mz);
    return { a, mx, my, mz };
  }

  function move(dx, dy, dz, grounded) {
    const start = box(pos.x, pos.y, pos.z);
    const reach = [Math.min(start[0], start[0] + dx), Math.min(start[1], start[1] + dy), Math.min(start[2], start[2] + dz),
      Math.max(start[3], start[3] + dx), Math.max(start[4], start[4] + dy) + STEP_HEIGHT, Math.max(start[5], start[5] + dz)];
    const boxes = collect(reach);
    let r = sweep(boxes, start, dx, dy, dz);
    const blocked = (r.mx !== dx || r.mz !== dz);
    if (blocked && (grounded || (dy < 0 && r.my !== dy))) {
      // Step up: rise, move horizontally, settle back down; keep it only if it went further.
      const up = start.slice();
      const rise = clip(boxes, up, 1, STEP_HEIGHT); shift(up, 1, rise);
      const sx = clip(boxes, up, 0, dx); shift(up, 0, sx);
      const sz = clip(boxes, up, 2, dz); shift(up, 2, sz);
      const fall = clip(boxes, up, 1, -rise + Math.min(dy, 0)); shift(up, 1, fall);
      if (sx * sx + sz * sz > r.mx * r.mx + r.mz * r.mz + EPS) r = { a: up, mx: sx, my: rise + fall, mz: sz, stepped: true };
    }
    pos.x = r.a[0] + BODY.half;
    pos.y = r.a[1];
    pos.z = r.a[2] + BODY.half;
    return { hitBelow: dy < 0 && r.my > dy + EPS && !r.stepped, hitAbove: dy > 0 && r.my < dy - EPS,
      stepped: Boolean(r.stepped), moved: r.mx !== 0 || r.my !== 0 || r.mz !== 0 };
  }

  const kindAt = (x, y, z) => shape(Math.floor(x), Math.floor(y), Math.floor(z))?.kind;
  const touches = (kind) => {
    const a = box(pos.x, pos.y, pos.z);
    for (let cx = Math.floor(a[0]); cx <= Math.floor(a[3] - EPS); cx += 1) {
      for (let cz = Math.floor(a[2]); cz <= Math.floor(a[5] - EPS); cz += 1) {
        for (let cy = Math.floor(a[1]); cy <= Math.floor(a[4] - EPS); cy += 1) {
          if (shape(cx, cy, cz)?.kind === kind) return true;
        }
      }
    }
    return false;
  };
  // Include the cell below for fences that extend above their owning cell. Unknown body cells are
  // never treated as a safe destination, even when the ground has already arrived.
  const occupancy = () => {
    const a = box(pos.x, pos.y, pos.z);
    let unknown = false;
    let blocked = false;
    for (let cx = Math.floor(a[0]); cx <= Math.floor(a[3] - EPS); cx += 1) {
      for (let cz = Math.floor(a[2]); cz <= Math.floor(a[5] - EPS); cz += 1) {
        for (let cy = Math.floor(a[1]) - 1; cy <= Math.floor(a[4] - EPS); cy += 1) {
          const s = shape(cx, cy, cz);
          if (s === undefined) unknown = true;
          for (const b of s?.boxes ?? []) {
            const w = [cx + b[0], cy + b[1], cz + b[2], cx + b[3], cy + b[4], cz + b[5]];
            if (overlap(a, w, 0) && overlap(a, w, 1) && overlap(a, w, 2)) blocked = true;
          }
        }
      }
    }
    return { unknown, blocked };
  };

  function reconcile() {
    waitingReason = null;
    const status = occupancy();
    if (status.unknown) { waitingReason = 'unloaded'; return false; }
    if (!status.blocked) { lastSafe = { ...pos }; return true; }
    const original = { ...pos };
    // Prefer a previously clear position, but revalidate it against the current data first.
    if (lastSafe) {
      Object.assign(pos, lastSafe);
      const old = occupancy();
      if (!old.unknown && !old.blocked) { vy = 0; onGround = false; return true; }
      Object.assign(pos, original);
    }
    // Test actual box tops, preserving slabs/fences. Bound recovery so malformed collision data
    // cannot send a body hundreds of blocks upwards. Otherwise stay put until data or mode changes.
    const reach = box(pos.x, pos.y, pos.z);
    reach[4] += MAX_RECOVERY_RISE;
    const tops = [...new Set(collect(reach).map(b => b[4]))]
      .filter(y => y > original.y + EPS && y <= original.y + MAX_RECOVERY_RISE)
      .sort((a, b) => a - b);
    for (const y of tops) {
      pos.y = y;
      const candidate = occupancy();
      if (!candidate.unknown && !candidate.blocked) {
        lastSafe = { ...pos }; vy = 0; onGround = false;
        return true;
      }
    }
    Object.assign(pos, original);
    waitingReason = 'embedded';
    return false;
  }

  function substep(dt, input) {
    const before = { ...pos };
    if (!reconcile()) {
      waiting = true; vy = 0; onGround = false; inLiquid = false;
      return false;
    }
    const recovered = pos.x !== before.x || pos.y !== before.y || pos.z !== before.z;
    const below = shape(Math.floor(pos.x), Math.floor(pos.y - 0.01), Math.floor(pos.z));
    waiting = below === undefined;
    waitingReason = waiting ? 'unloaded' : null;
    inLiquid = kindAt(pos.x, pos.y + 0.4, pos.z) === 'liquid';
    const climbing = touches('climb');
    const f = clampUnit(input.forward);
    const r = clampUnit(input.right);
    const len = Math.hypot(f, r);
    let speed = WALK_SPEED * (input.boost ? SPRINT_FACTOR : 1) * (inLiquid ? 0.5 : 1);
    if (len > 1) speed /= len;
    const yaw = Number.isFinite(input.yaw) ? input.yaw : 0;
    const dx = (f * -Math.sin(yaw) + r * Math.cos(yaw)) * speed * dt;
    const dz = (f * -Math.cos(yaw) - r * Math.sin(yaw)) * speed * dt;

    if (waiting) {
      vy = 0;
    } else if (inLiquid) {
      vy = input.jump ? 3 : Math.max(vy - 10 * dt, -3);
    } else if (climbing) {
      vy = input.jump || f > 0 ? 2.4 : Math.max(vy - GRAVITY * dt, -2.4);
    } else {
      if (onGround && input.jump) vy = JUMP_SPEED;
      vy = Math.max(vy - GRAVITY * dt, -MAX_FALL);
    }
    const res = move(dx, vy * dt, dz, onGround);
    onGround = waiting || res.hitBelow || res.stepped;
    if ((res.hitBelow || res.stepped) && vy < 0) vy = 0;
    if (res.hitAbove && vy > 0) vy = 0;
    return recovered || res.moved;
  }

  return {
    get position() { return { ...pos }; },
    get onGround() { return onGround; },
    get inLiquid() { return inLiquid; },
    get waiting() { return waiting; },
    get waitingReason() { return waitingReason; },
    place({ x, y, z }) {
      pos.x = x; pos.y = y; pos.z = z;
      vy = 0;
      onGround = false;
      inLiquid = false;
      lastSafe = null;
      waiting = !reconcile();
    },
    step(dt, input = {}) {
      let left = Math.min(Math.max(Number.isFinite(dt) ? dt : 0, 0), MAX_DT);
      let moved = false;
      while (left > 1e-6) {
        const h = Math.min(left, MAX_SUBSTEP);
        if (substep(h, input)) moved = true;
        left -= h;
      }
      return moved;
    },
  };
}

const clampUnit = (v) => (Number.isFinite(v) ? Math.max(-1, Math.min(1, v)) : 0);
