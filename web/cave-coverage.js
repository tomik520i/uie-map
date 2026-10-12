// Where the height cut ("řez podzemím") can show the underground: chunks of the detail generation that have both
// geometry (manifest.chunks) and block data (manifest.blockInfo.chunks), i.e. where cut caps can be filled.
// Pure: no DOM, no three.js, no network. Chunk-level membership only (no per-block storage).
//
//   const coverage = createCaveCoverage(manifest);
//   coverage.valid            false when the manifest inventory is missing or over MAX_COVERAGE_CHUNKS
//   coverage.count            chunks with geometry AND block data (the "inside" area)
//   coverage.geometryCount    valid geometry chunks
//   coverage.has(cx, cz)      chunk coordinates inside the area
//   coverage.describe({ x, z, cut })
//     -> { visible, state, chunk, text }
//        visible false (state 'off', text null) when the cut is off (cut null/undefined/not a finite number);
//        state 'inside'        detail geometry and block data at the view target's chunk
//              'geometry-only' detail geometry without block data (the cut is not capped there)
//              'outside'       no detail data there (also for an invalid or empty manifest)
//              'unknown'       the view target is not a finite in-world position
//        chunk  [cx, cz] = [floor(x / 16), floor(z / 16)] or null
//
// x and z are WORLD BLOCK coordinates of the camera / view target; manifest entries are CHUNK coordinates.
// "inside" only says that detail data exists there. It is not a proof of caves: the manifest has no reliable flag
// for the renderer's cave mode, and enclosed caves can still be absent from a surface-mode export.

export const MAX_COVERAGE_CHUNKS = 9216;          // 256 detail tiles x 36 chunks (detail_update MAX_RECT_TILES)
export const WORLD_BORDER = 30_000_000;
const MAX_CHUNK = WORLD_BORDER / 16;              // 1 875 000

export const CAVE_COVERAGE_TEXT = Object.freeze({
  inside: 'Řez podzemím: oblast s detailními daty.',
  outside: 'Mimo detailní oblast: zde je dostupný pouze povrch, podzemí není vykreslené.',
  geometryOnly: 'Řez podzemím: detailní geometrie bez blokových dat, řez zde není vyplněný.',
  unknown: 'Polohu pohledu nelze určit, dostupnost podzemí není známa.',
});

const chunkCoord = (v) => Number.isSafeInteger(v) && Math.abs(v) <= MAX_CHUNK;

// Keys of valid {x, z} chunk entries, or null when the list is missing or too long. Malformed entries are skipped:
// they never create coverage.
function chunkKeys(list) {
  if (!Array.isArray(list) || list.length > MAX_COVERAGE_CHUNKS) return null;
  const keys = new Set();
  for (const entry of list) {
    if (entry && typeof entry === 'object' && chunkCoord(entry.x) && chunkCoord(entry.z)) keys.add(`${entry.x},${entry.z}`);
  }
  return keys;
}

export function createCaveCoverage(manifest) {
  const geometry = chunkKeys(manifest?.chunks);
  const info = manifest?.blockInfo;
  const blocks = info && typeof info === 'object' && info.format === 'UIE_BLOCKS1' ? chunkKeys(info.chunks) : null;
  const valid = geometry !== null;
  const inside = new Set();
  if (geometry && blocks) for (const key of geometry) if (blocks.has(key)) inside.add(key);
  const geometryCount = geometry ? geometry.size : 0;

  const has = (cx, cz) => chunkCoord(cx) && chunkCoord(cz) && inside.has(`${cx},${cz}`);

  function describe({ x, z, cut } = {}) {
    if (typeof cut !== 'number' || !Number.isFinite(cut)) return { visible: false, state: 'off', chunk: null, text: null };
    if (typeof x !== 'number' || typeof z !== 'number' || !Number.isFinite(x) || !Number.isFinite(z)
        || Math.abs(x) > WORLD_BORDER || Math.abs(z) > WORLD_BORDER) {
      return { visible: true, state: 'unknown', chunk: null, text: CAVE_COVERAGE_TEXT.unknown };
    }
    const cx = Math.floor(x / 16) + 0;              // + 0 turns -0 into 0
    const cz = Math.floor(z / 16) + 0;
    const chunk = [cx, cz];
    if (has(cx, cz)) return { visible: true, state: 'inside', chunk, text: CAVE_COVERAGE_TEXT.inside };
    if (geometry?.has(`${cx},${cz}`)) {
      return { visible: true, state: 'geometry-only', chunk, text: CAVE_COVERAGE_TEXT.geometryOnly };
    }
    return { visible: true, state: 'outside', chunk, text: CAVE_COVERAGE_TEXT.outside };
  }

  return Object.freeze({ valid, count: inside.size, geometryCount, has, describe });
}
