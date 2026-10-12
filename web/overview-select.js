// Whole-world overview (UIE_OVERVIEW1 from overview_tiles.py): index validation and tile selection.
// No DOM or three.js, so it can be tested directly in Node. app.js turns selected tiles into heightfield meshes.
//
// Level k tile (tx, tz) is TILE x TILE pixels covering 2^k x 2^k regions: block x = tx * TILE * 2^k + px * 2^k.

export const OVERVIEW_FORMAT = 'UIE_OVERVIEW1';
export const MAX_TILES = 40;
const isInt = Number.isSafeInteger;

export class OverviewError extends Error {}

/** Validate index.json; returns { tileSize, levels, tiles: Map(level -> Set("tx,tz")), blockBounds }. */
export function validateOverviewIndex(doc) {
  const fail = (msg) => { throw new OverviewError(`overview index: ${msg}`); };
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) fail('root is not an object');
  if (doc.format !== OVERVIEW_FORMAT) fail(`unsupported format ${JSON.stringify(doc.format)}`);
  if (doc.tileSize !== 512) fail('tileSize must be 512');
  if (!isInt(doc.levels) || doc.levels < 1 || doc.levels > 24) fail('levels must be 1..24');
  const tiles = new Map();
  for (let level = 0; level < doc.levels; level += 1) {
    const list = doc.tiles?.[String(level)];
    if (!Array.isArray(list)) fail(`tiles of level ${level} missing`);
    const set = new Set();
    for (const t of list) {
      if (!Array.isArray(t) || t.length !== 2 || !t.every(isInt)) fail(`invalid tile in level ${level}`);
      set.add(`${t[0]},${t[1]}`);
    }
    tiles.set(level, set);
  }
  const b = doc.blockBounds;
  if (!b || ![b.min, b.max].every((v) => Array.isArray(v) && v.length === 2 && v.every(isInt))
    || b.min[0] >= b.max[0] || b.min[1] >= b.max[1]) fail('invalid blockBounds');
  return { tileSize: doc.tileSize, levels: doc.levels, tiles, blockBounds: b };
}

/** Coarser level for a farther camera: about 1 texel per screen pixel at a 55 degree field of view. */
export function levelForDistance(distance, levels) {
  const blocksPerTexel = Math.max(1, distance / 600);
  return Math.max(0, Math.min(levels - 1, Math.floor(Math.log2(blocksPerTexel))));
}

/** World rectangle of a tile: { x0, z0, size } in blocks. */
export function tileRect(level, tx, tz, tileSize = 512) {
  const size = tileSize * 2 ** level;
  return { x0: tx * size, z0: tz * size, size };
}

/**
 * Tiles to show around (x, z) for a camera at `distance`: the chosen level within a radius of
 * 2.5 x distance (at least one tile), nearest first, at most maxTiles. Only tiles that exist are returned.
 */
export function selectOverviewTiles(index, x, z, distance, maxTiles = MAX_TILES) {
  let level = levelForDistance(distance, index.levels);
  for (;;) {
    const size = index.tileSize * 2 ** level;
    const radius = Math.max(distance * 2.5, size);
    const [ax, bx] = [Math.floor((x - radius) / size), Math.floor((x + radius) / size)];
    const [az, bz] = [Math.floor((z - radius) / size), Math.floor((z + radius) / size)];
    const span = (bx - ax + 1) * (bz - az + 1);
    if (span > maxTiles * 4 && level < index.levels - 1) { level += 1; continue; }
    const present = index.tiles.get(level);
    const picked = [];
    for (let tz = az; tz <= bz; tz += 1) {
      for (let tx = ax; tx <= bx; tx += 1) {
        if (!present.has(`${tx},${tz}`)) continue;
        const cx = (tx + 0.5) * size - x;
        const cz = (tz + 0.5) * size - z;
        picked.push({ level, tx, tz, key: `${level}/${tx}_${tz}`, d2: cx * cx + cz * cz });
      }
    }
    picked.sort((a, b) => a.d2 - b.d2);
    return picked.slice(0, maxTiles).map(({ d2, ...tile }) => tile);
  }
}

/** Distance at which the whole overview fits the view (for zooming out to the full world). */
export function wholeWorldDistance(index) {
  const { min, max } = index.blockBounds;
  return Math.max(max[0] - min[0], max[1] - min[1]) * 0.95;
}
