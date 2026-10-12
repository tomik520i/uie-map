// Neighbor-dependent 1.7.10 collision for vanilla doors and chests, on top of the per-block blockShape().
// Pure, no DOM. Reference: javap of the original 1.7.10 client JAR (work/qa-20261008/vanilla-shapes/, provenance.json).
//
//   blockShapeAt(readBlock, x, y, z) -> blockShape-like result, or undefined when required data is unknown
//     readBlock(x, y, z) -> { name, meta } | undefined (unknown). Must be synchronous; a thenable result throws.
//     Every other block (vanilla or modded) is exactly blockShape(name, meta) and reads only its own cell.
//     Results depend on neighbors: never memoize them by name/meta of the cell alone.
//
// Doors (minecraft:wooden_door, minecraft:iron_door; BlockDoor akn):
//   collision func_149668_a calls func_149719_a -> func_150011_b(func_150012_g(world, x, y, z)), then Block's box.
//   func_150012_g reads metadata only: an upper half (meta & 8) takes direction/open from the cell below, a lower
//   half takes the hinge (meta & 1) from the cell above. func_150011_b: one 3/16 panel, 1 block high per half.
//   The partner cell must be known (else undefined). A partner that is not the same block (func_149695_a removes such
//   a half on the next neighbor update) is an invalid state: it blocks as a full cube instead of guessing a panel.
//   Two same-door halves with inconsistent half bits are kept as the game computes them (metadata only).
// Chests (minecraft:chest, minecraft:trapped_chest; BlockChest ajx, no collision override, so Block's box of
//   the bounds set by func_149719_a): 1/16 inset, 7/8 high; the side toward the FIRST identical block in the
//   order z-1, z+1, x-1, x+1 is flush. Chest and trapped chest are different blocks and never join. An unknown cell
//   met before a decision makes the shape unknown; cells after the first match are not read.
import { blockShape } from './walk-physics.js';

const T = 0.1875;
const solid = b => Object.freeze({ kind: 'solid', boxes: Object.freeze([Object.freeze(b)]) });
const WEST = solid([0, 0, 0, T, 1, 1]), EAST = solid([1 - T, 0, 0, 1, 1, 1]);
const NORTH = solid([0, 0, 0, 1, 1, T]), SOUTH = solid([0, 0, 1 - T, 1, 1, 1]);
const INVALID_DOOR = solid([0, 0, 0, 1, 1, 1]);
// func_150011_b: [direction] -> [closed, open hinge 0, open hinge 1]
const DOOR = [[WEST, NORTH, SOUTH], [NORTH, EAST, WEST], [EAST, SOUTH, NORTH], [SOUTH, WEST, EAST]];
const DOORS = new Set(['minecraft:wooden_door', 'minecraft:iron_door']);
const CHESTS = new Set(['minecraft:chest', 'minecraft:trapped_chest']);
const LO = 0.0625, HI = 0.9375, TOP = 0.875;
// func_149719_a order: neighbor offset and the bounds it selects.
const CHEST_JOINS = [
  [0, -1, solid([LO, 0, 0, HI, TOP, HI])],
  [0, 1, solid([LO, 0, LO, HI, TOP, 1])],
  [-1, 0, solid([0, 0, LO, HI, TOP, HI])],
  [1, 0, solid([LO, 0, LO, 1, TOP, HI])],
];
const CHEST_SINGLE = solid([LO, 0, LO, HI, TOP, HI]);

function read(readBlock, x, y, z) {
  const block = readBlock(x, y, z);
  if (block && typeof block.then === 'function') throw new TypeError('readBlock must be synchronous');
  return block && typeof block.name === 'string' ? block : undefined;
}
const validMeta = meta => Number.isInteger(meta) && meta >= 0 && meta <= 15;

export function blockShapeAt(readBlock, x, y, z) {
  const self = read(readBlock, x, y, z);
  if (!self) return undefined;
  if (DOORS.has(self.name)) return door(readBlock, self, x, y, z);
  if (CHESTS.has(self.name)) return chest(readBlock, self.name, x, y, z);
  return blockShape(self.name, self.meta);
}

function door(readBlock, self, x, y, z) {
  if (!validMeta(self.meta)) return undefined;
  const upper = (self.meta & 8) !== 0;
  const partner = read(readBlock, x, upper ? y - 1 : y + 1, z);
  if (!partner || !validMeta(partner.meta)) return undefined;
  if (partner.name !== self.name) return INVALID_DOOR;
  const lowerMeta = upper ? partner.meta : self.meta;
  const hinge = (upper ? self.meta : partner.meta) & 1;
  return DOOR[lowerMeta & 3][lowerMeta & 4 ? 1 + hinge : 0];
}

function chest(readBlock, name, x, y, z) {
  for (const [dx, dz, shape] of CHEST_JOINS) {
    const neighbor = read(readBlock, x + dx, y, z + dz);
    if (!neighbor) return undefined;
    if (neighbor.name === name) return shape;
  }
  return CHEST_SINGLE;
}
