// Camera-driven coarse/detail selection with hysteresis for the bounded LOD experiment.
// Pure and stateless: no DOM, no three.js, no geometry. The caller keeps the previous selection.
//
// selectDistanceLod({ tiles, focus, enterRadius, exitRadius, previousDetailIds = [] })
//   tiles:  [{ id, minX, maxX, minZ, maxZ }]   non-empty unique ids, finite bounds with positive area
//   focus:  { x, z }                            finite
//   radii:  finite, 0 <= enterRadius < exitRadius
//   previousDetailIds: unique strings, the previous DESIRED detail ids (not what actually loaded)
// -> [{ id, level: 'detail' | 'coarse', distance }] for every tile, nearest first, ties in input order.
//
// distance = horizontal Euclidean distance from the focus to the tile's x/z rectangle (0 inside or on an
// edge). A tile that was desired as detail stays detail while distance <= exitRadius; any other tile becomes
// detail when distance <= enterRadius. Ids in previousDetailIds that are not in tiles are ignored.
// This is only a desired level: LodSwap owns memory budget, coverage and failures.

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

export function selectDistanceLod({ tiles, focus, enterRadius, exitRadius, previousDetailIds = [] } = {}) {
  if (!Array.isArray(tiles)) throw new TypeError('tiles must be an array');
  if (!focus || typeof focus !== 'object' || !finite(focus.x) || !finite(focus.z)) {
    throw new TypeError('focus needs finite x and z');
  }
  if (!finite(enterRadius) || !finite(exitRadius) || enterRadius < 0 || !(enterRadius < exitRadius)) {
    throw new RangeError('radii must be finite with 0 <= enterRadius < exitRadius');
  }
  if (!Array.isArray(previousDetailIds)) throw new TypeError('previousDetailIds must be an array');
  const previous = new Set();
  for (const id of previousDetailIds) {
    if (typeof id !== 'string' || id === '' || previous.has(id)) {
      throw new TypeError('previousDetailIds must be unique non-empty strings');
    }
    previous.add(id);
  }
  const ids = new Set();
  const rows = tiles.map((t, index) => {
    if (!t || typeof t !== 'object') throw new TypeError(`tiles[${index}] must be an object`);
    if (typeof t.id !== 'string' || t.id === '' || ids.has(t.id)) {
      throw new TypeError(`tiles[${index}] needs a unique non-empty string id`);
    }
    const { minX, maxX, minZ, maxZ } = t;
    if (![minX, maxX, minZ, maxZ].every(finite) || !(minX < maxX) || !(minZ < maxZ)) {
      throw new RangeError(`tiles[${index}] needs finite bounds with minX < maxX and minZ < maxZ`);
    }
    ids.add(t.id);
    const dx = Math.max(minX - focus.x, 0, focus.x - maxX);
    const dz = Math.max(minZ - focus.z, 0, focus.z - maxZ);
    const distance = Math.hypot(dx, dz);
    const detail = previous.has(t.id) ? distance <= exitRadius : distance <= enterRadius;
    return { id: t.id, level: detail ? 'detail' : 'coarse', distance, index };
  });
  rows.sort((a, b) => a.distance - b.distance || a.index - b.index);
  return rows.map(({ id, level, distance }) => ({ id, level, distance }));
}
