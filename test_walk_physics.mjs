// Tests for web/walk-physics.js (walk mode collisions). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_walk_physics.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('./web/walk-physics.js', import.meta.url), 'utf8');
const { BODY, WALK_SPEED, blockShape, createWalker } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

// Flat world: stone below y=64 inside |x|,|z| <= 20, air above; outside that the data is "not loaded".
function world(extra = {}) {
  const cells = new Map(Object.entries(extra));
  return (x, y, z) => {
    if (Math.abs(x) > 20 || Math.abs(z) > 20) return undefined;
    const named = cells.get(`${x},${y},${z}`);
    if (named) return blockShape(...named);
    return blockShape(y < 64 ? 'minecraft:stone' : 'minecraft:air', 0);
  };
}
const run = (walker, seconds, input = {}) => { for (let t = 0; t < seconds; t += 0.05) walker.step(0.05, input); };
const near = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;

test('block shapes: solid default, plants passable, slabs, stairs, fences, liquids and ladders', () => {
  assert.equal(blockShape('minecraft:grass').kind, 'solid', 'grass block is solid');
  assert.equal(blockShape('minecraft:stone').boxes.length, 1);
  for (const name of ['minecraft:air', 'minecraft:tallgrass', 'minecraft:torch', 'minecraft:wooden_door',
    'BiomesOPlenty:foliage', 'BiomesOPlenty:flowers', 'Natura:Cloud', 'magicalcrops:Crop', 'BiomesOPlenty:mushrooms']) {
    assert.equal(blockShape(name).kind, 'pass', name);
  }
  for (const name of ['minecraft:water', 'minecraft:flowing_lava', 'TConstruct:liquid.metal', 'BuildCraft|Energy:blockOil']) {
    assert.equal(blockShape(name).kind, 'liquid', name);
  }
  // Real LIVE-area names (2026-10-02).
  for (const name of ['BiomesOPlenty:coral2', 'BiomesOPlenty:lilyBop', 'harvestcraft:herbgarden', 'harvestcraft:pamPeach',
    'Natura:Bluebells', 'TConstruct:slime.grass.tall', 'Thaumcraft:blockAiry', 'mobdropcrops:Slime Pad',
    'mobdropcrops:Creeper Pod', 'tropicraft:tile.pineapple', 'InventoryPets:cloudblock']) {
    assert.equal(blockShape(name).kind, 'pass', name);
  }
  for (const name of ['OreSpawn:OreSpawn_CloudSharkSpawnBlock', 'OreSpawn:OreSpawn_OreRubyBlock', 'TConstruct:slime.grass',
    'BiomesOPlenty:newBopGrass', 'harvestcraft:beehive', 'IC2:blockRubLeaves']) {
    assert.equal(blockShape(name).kind, 'solid', name);
  }
  assert.equal(blockShape('MineFactoryReloaded:sewage.still').kind, 'liquid');
  assert.equal(blockShape('BiomesOPlenty:willow').kind, 'climb');
  assert.equal(blockShape('BuildCraft|Factory:tankBlock').kind, 'solid');
  assert.equal(blockShape('ThermalExpansion:FluidTank').kind, 'solid', 'tanks are not liquids');
  assert.equal(blockShape('BiomesOPlenty:mushroomBlock').kind, 'solid');
  assert.equal(blockShape('minecraft:ladder').kind, 'climb');
  assert.equal(blockShape('minecraft:vine').kind, 'climb');
  assert.deepEqual(blockShape('minecraft:stone_slab', 0).boxes, [[0, 0, 0, 1, 0.5, 1]]);
  assert.deepEqual(blockShape('minecraft:stone_slab', 8).boxes, [[0, 0.5, 0, 1, 1, 1]]);
  assert.deepEqual(blockShape('minecraft:double_stone_slab', 0).boxes, [[0, 0, 0, 1, 1, 1]]);
  assert.deepEqual(blockShape('minecraft:oak_stairs', 3).boxes, [[0, 0, 0, 1, 0.5, 1], [0, 0.5, 0, 1, 1, 0.5]]);
  assert.deepEqual(blockShape('minecraft:oak_stairs', 4).boxes, [[0, 0.5, 0, 1, 1, 1], [0.5, 0, 0, 1, 0.5, 1]]);
  assert.equal(blockShape('minecraft:fence').boxes[0][4], 1.5);
  assert.equal(blockShape('minecraft:fence_gate', 4).kind, 'pass', 'open gate');
  assert.equal(blockShape('minecraft:snow_layer', 0).kind, 'pass');
  assert.equal(blockShape('minecraft:snow_layer', 3).boxes[0][4], 0.375, '1.7.10: (meta & 7) / 8');
  assert.equal(blockShape('SomeMod:unknownMachine').kind, 'solid', 'unknown modded block is a full cube');
});

test('falls onto the ground and stands still', () => {
  const w = createWalker({ shapeAt: world() });
  w.place({ x: 0.5, y: 70, z: 0.5 });
  run(w, 2);
  assert.ok(near(w.position.y, 64), String(w.position.y));
  assert.ok(w.onGround);
  run(w, 1);
  assert.ok(near(w.position.y, 64));
});

test('walks forward along -Z at walking speed (yaw 0) and strafes along +X', () => {
  const w = createWalker({ shapeAt: world() });
  w.place({ x: 0.5, y: 64, z: 0.5 });
  run(w, 0.2);
  const start = w.position;
  run(w, 1, { forward: 1, yaw: 0 });
  const p = w.position;
  assert.ok(near(p.z - start.z, -WALK_SPEED, 0.25), String(p.z - start.z));
  assert.ok(near(p.x, start.x) && near(p.y, 64));
  run(w, 0.5, { right: 1, yaw: 0 });
  assert.ok(w.position.x > p.x + 1.5);
});

test('a one-block wall stops walking but can be jumped; a two-block wall cannot', () => {
  const extra = {};
  for (let x = -20; x <= 20; x += 1) extra[`${x},64,-3`] = ['minecraft:stone', 0];
  const w = createWalker({ shapeAt: world(extra) });
  w.place({ x: 0.5, y: 64, z: 0.5 });
  run(w, 2, { forward: 1, yaw: 0 });
  assert.ok(near(w.position.z, -2 + BODY.half, 1e-4), `stopped at the wall: ${w.position.z}`);
  assert.ok(near(w.position.y, 64));
  run(w, 1, { forward: 1, yaw: 0, jump: true });
  assert.ok(w.position.z < -4 && w.position.y >= 64, `jumped over the wall: ${JSON.stringify(w.position)}`);

  for (let x = -20; x <= 20; x += 1) { extra[`${x},64,-8`] = ['minecraft:stone', 0]; extra[`${x},65,-8`] = ['minecraft:stone', 0]; extra[`${x},66,-8`] = ['minecraft:stone', 0]; }
  const v = createWalker({ shapeAt: world(extra) });
  v.place({ x: 0.5, y: 65, z: -4.5 });
  run(v, 2, { forward: 1, yaw: 0, jump: true });
  assert.ok(v.position.z > -7.4, `blocked by the 2-high wall: ${v.position.z}`);
});

test('slabs and a staircase are climbed without jumping', () => {
  const extra = { '0,64,-2': ['minecraft:stone_slab', 0] };
  const w = createWalker({ shapeAt: world(extra) });
  w.place({ x: 0.5, y: 64, z: 0.5 });
  run(w, 0.25);
  run(w, 0.4, { forward: 1, yaw: 0 });
  assert.ok(w.position.z < -1 && near(w.position.y, 64.5), `on the slab: ${JSON.stringify(w.position)}`);

  const stairs = {};
  for (let i = 0; i < 4; i += 1) {
    for (let k = 0; k < i; k += 1) stairs[`0,${64 + k},${-2 - i}`] = ['minecraft:stone', 0];
    stairs[`0,${64 + i},${-2 - i}`] = ['minecraft:oak_stairs', 3];
  }
  const s = createWalker({ shapeAt: world(stairs) });
  s.place({ x: 0.5, y: 64, z: 0.5 });
  run(s, 1.2, { forward: 1, yaw: 0 });   // the top stair is 4 blocks up at z -5; beyond it the ground drops again
  assert.ok(s.position.y >= 67.9 && s.position.z < -4, `climbed the stairs: ${JSON.stringify(s.position)}`);
});

test('unloaded ground holds the body; unloaded cells block like walls', () => {
  const w = createWalker({ shapeAt: () => undefined });
  w.place({ x: 0.5, y: 80, z: 0.5 });
  run(w, 1);
  assert.equal(w.position.y, 80);
  assert.ok(w.waiting);

  const edge = createWalker({ shapeAt: world() });
  edge.place({ x: 19.5, y: 64, z: 0.5 });
  run(edge, 2, { right: 1, yaw: 0 });
  assert.ok(near(edge.position.x, 21 - BODY.half, 1e-4), `stopped at the unloaded edge: ${edge.position.x}`);
});

test('swimming up in water and climbing a ladder', () => {
  const water = {};
  for (let y = 60; y < 64; y += 1) water[`0,${y},0`] = ['minecraft:water', 0];
  const w = createWalker({ shapeAt: world(water) });
  w.place({ x: 0.5, y: 60, z: 0.5 });
  run(w, 0.3);
  assert.ok(w.inLiquid);
  const sunk = w.position.y;
  run(w, 1, { jump: true });
  assert.ok(w.position.y > sunk + 2, `swam up: ${w.position.y}`);

  const ladder = {};
  for (let y = 64; y < 70; y += 1) { ladder[`0,${y},0`] = ['minecraft:ladder', 2]; ladder[`0,${y},-1`] = ['minecraft:stone', 0]; }
  const l = createWalker({ shapeAt: world(ladder) });
  l.place({ x: 0.5, y: 64, z: 0.5 });
  run(l, 1, { forward: 1, yaw: 0 });
  assert.ok(l.position.y > 66, `climbed: ${l.position.y}`);
});

test('placing inside terrain moves the body up to free space', () => {
  const w = createWalker({ shapeAt: world() });
  w.place({ x: 0.5, y: 62.2, z: 0.5 });
  assert.equal(w.position.y, 64);
});

test('late terrain arrival recovers the body instead of keeping it buried', () => {
  let ready = false;
  const w = createWalker({ shapeAt: (x, y, z) => ready ? blockShape(y < 70 ? 'stone' : 'air') : undefined });
  w.place({ x: 0.5, y: 69, z: 0.5 });
  run(w, 0.2, { right: 1 });
  assert.deepEqual(w.position, { x: 0.5, y: 69, z: 0.5 });
  assert.ok(w.waiting);
  ready = true;
  assert.equal(w.step(0.05), true, 'camera must be notified about recovery');
  assert.equal(w.position.y, 70);
  assert.equal(w.waiting, false);
  run(w, 0.2, { right: 1 });
  assert.ok(w.position.x > 1);
});

test('known floor is insufficient while body cells are still unknown', () => {
  let ready = false;
  const w = createWalker({ shapeAt: (x, y) => y < 64 ? blockShape('stone') : ready ? blockShape('air') : undefined });
  w.place({ x: 0.5, y: 64, z: 0.5 });
  run(w, 0.2, { jump: true, right: 1 });
  assert.deepEqual(w.position, { x: 0.5, y: 64, z: 0.5 });
  assert.ok(w.waiting);
  ready = true;
  run(w, 0.2, { right: 1 });
  assert.ok(w.position.x > 1);
});

test('late slab/fence overlap recovers to the precise top, including boxes from the cell below', () => {
  for (const [name, start, expected] of [['stone_slab', 64.2, 64.5], ['fence', 65.1, 65.5]]) {
    let ready = false;
    const w = createWalker({ shapeAt: (x, y) => !ready ? undefined : y === 64 ? blockShape(name) : blockShape(y < 64 ? 'stone' : 'air') });
    w.place({ x: 0.5, y: start, z: 0.5 });
    ready = true;
    run(w, 0.1);
    assert.equal(w.position.y, expected, name);
  }
});

test('recovery is bounded and does not repeatedly climb towards a remote canopy', () => {
  let ready = false;
  const w = createWalker({ shapeAt: (x, y) => ready ? blockShape(y < 110 ? 'stone' : 'air') : undefined });
  w.place({ x: 0.5, y: 64, z: 0.5 });
  assert.equal(w.waitingReason, 'unloaded');
  ready = true;
  run(w, 2, { jump: true, right: 1 });
  assert.deepEqual(w.position, { x: 0.5, y: 64, z: 0.5 });
  assert.ok(w.waiting);
  assert.equal(w.waitingReason, 'embedded', 'loaded solid terrain is not pending data');
  w.place({ x: 0.5, y: 110, z: 0.5 });
  assert.equal(w.waiting, false);
  assert.equal(w.waitingReason, null);
});

test('a world edit rolls back to a still-clear position instead of climbing a tall new wall', () => {
  let wall = false;
  const w = createWalker({ shapeAt: (x, y) => blockShape(y < 64 || (wall && x >= 1 && y < 100) ? 'stone' : 'air') });
  w.place({ x: 0.5, y: 64, z: 0.5 });
  w.step(0.05, { right: 1 });
  assert.ok(w.position.x > 0.7);
  wall = true;
  w.step(0.05);
  assert.equal(w.position.x, 0.5);
  assert.equal(w.position.y, 64);
  assert.equal(w.waiting, false);
});

test('late solid column cannot teleport the body onto a distant roof', () => {
  let ready = false;
  const w = createWalker({ shapeAt: (x, y, z) => !ready ? undefined : blockShape(
    y < 64 || (x === 0 && z === 0 && y >= 65 && y <= 75) ? 'stone' : 'air') });
  w.place({ x: 0.5, y: 65.2, z: 0.5 });
  ready = true;
  run(w, 2);
  assert.deepEqual(w.position, { x: 0.5, y: 65.2, z: 0.5 });
  assert.ok(w.waiting);
});
