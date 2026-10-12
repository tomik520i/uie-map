// Cap for the height cut ("uzavřený řez"): flat quads on the cut plane over every cut-through block, so solid rock
// reads as a cross-section and caves show as openings. No DOM or three.js; app.js turns the arrays into meshes.
//
//   capClasses(chunk, shapeOf) -> Uint8Array   per voxel (same z-major, x, y order as decoded block data):
//       CAP_NONE (air, plants, ladders), CAP_SOLID, CAP_WATER, CAP_LAVA; shapeOf(name, meta) is walk-physics blockShape.
//   capQuads(classes, width, depth, level) -> [{ x0, z0, x1, z1, code }]   cells of one level merged into rectangles
//       (row runs, then identical runs on following rows).
//   capArrays(originX, originZ, quads, y) -> { positions, colors, indices }   upward quads at height y.
//
// app.js places the cap just below the clipping plane: exposed top faces of the cut level keep their textures and
// only hidden (cut) tops show the cap colour.

const HEIGHT = 256;
export const CAP_NONE = 0;
export const CAP_SOLID = 1;
export const CAP_WATER = 2;
export const CAP_LAVA = 3;
export const CAP_COLORS = {
  [CAP_SOLID]: [0.33, 0.32, 0.34],    // unlit (MeshBasicMaterial): a little darker than the stone texture
  [CAP_WATER]: [0.15, 0.27, 0.62],
  [CAP_LAVA]: [0.92, 0.42, 0.08],
};

export function capClasses(chunk, shapeOf) {
  const byId = new Map();
  for (const [id, name] of chunk.registry) byId.set(id, name);
  const memo = new Map();
  const classOf = (id, meta) => {
    const key = id * 16 + meta;
    let code = memo.get(key);
    if (code === undefined) {
      const name = byId.get(id) ?? '';
      const kind = shapeOf(name, meta).kind;
      code = kind === 'solid' ? CAP_SOLID
        : kind === 'liquid' ? (/lava/i.test(name) ? CAP_LAVA : CAP_WATER) : CAP_NONE;
      memo.set(key, code);
    }
    return code;
  };
  const out = new Uint8Array(chunk.ids.length);
  for (let i = 0; i < out.length; i += 1) {
    const id = chunk.ids[i];
    if (id !== 0) out[i] = classOf(id, chunk.metas[i] & 15);
  }
  return out;
}

export function capQuads(classes, width, depth, level) {
  if (!Number.isInteger(level) || level < 0 || level >= HEIGHT) return [];
  const at = (x, z) => classes[(z * width + x) * HEIGHT + level];
  return mergeRuns(width, depth, at);
}

// Expand only the selected level. Unknown cells (255) leave no cap; coverage UI
// must identify missing data separately rather than claiming these cells are air.
export function capSliceQuads(classes, width, depth) {
  if (!Number.isInteger(width) || !Number.isInteger(depth) || width<1 || depth<1 ||
      width>64 || depth>64 || classes.length!==width*depth) throw new RangeError('Invalid cap slice');
  for (const code of classes) if (![0,1,2,3,255].includes(code)) throw new Error('Invalid cap class');
  return mergeRuns(width, depth, (x,z)=>classes[z*width+x]);
}

function mergeRuns(width, depth, at) {
  const runs = [];
  for (let z = 0; z < depth; z += 1) {
    let x = 0;
    while (x < width) {
      const code = at(x, z);
      if (code === CAP_NONE || code === 255) { x += 1; continue; }
      let end = x + 1;
      while (end < width && at(end, z) === code) end += 1;
      runs.push({ x0: x, x1: end, z, code });
      x = end;
    }
  }
  // Merge a run with the same span and code on the next row into one rectangle.
  const open = new Map();
  const quads = [];
  for (const r of runs) {
    const key = `${r.x0},${r.x1},${r.code}`;
    const q = open.get(key);
    if (q && q.z1 === r.z) {
      q.z1 = r.z + 1;
    } else {
      const fresh = { x0: r.x0, z0: r.z, x1: r.x1, z1: r.z + 1, code: r.code };
      open.set(key, fresh);
      quads.push(fresh);
    }
  }
  return quads;
}

export function capArrays(originX, originZ, quads, y) {
  const positions = new Float32Array(quads.length * 12);
  const colors = new Float32Array(quads.length * 12);
  const indices = new Uint32Array(quads.length * 6);
  quads.forEach((q, n) => {
    const corners = [[q.x0, q.z0], [q.x0, q.z1], [q.x1, q.z1], [q.x1, q.z0]];
    const color = CAP_COLORS[q.code];
    corners.forEach(([x, z], k) => {
      positions.set([originX + x, y, originZ + z], n * 12 + k * 3);
      colors.set(color, n * 12 + k * 3);
    });
    // Counter-clockwise seen from above (+Y normal).
    indices.set([n * 4, n * 4 + 1, n * 4 + 2, n * 4, n * 4 + 2, n * 4 + 3], n * 6);
  });
  return { positions, colors, indices };
}
