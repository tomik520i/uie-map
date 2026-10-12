// PRIVATE cave cap rendering controller: bounded UIC1 tile streaming (cave-cap-stream.js) plus cut-plane cap meshes
// (cut-cap.js) for the current cut level. No DOM, no three.js: the app supplies attach/drop for render resources.
//
//   const layer = createCaveCapLayer({ load, attach, drop, onError, budgetBytes, meshBudgetBytes, concurrency });
//   layer.update({ entries, level, maskedChunks })
//     entries       ordered Map (or iterable) key -> { pairSha256, expected } pinned to the visible geometry, nearest first
//     level         integer 0..255 (block level of the cut) or null (cut off: window emptied, every cap dropped)
//     maskedChunks  Set of GLOBAL chunk keys "cx,cz" already capped by resident detail geometry: those cells get no cap
//   layer.snapshot() -> { ...stream snapshot, level, meshes, meshBytes, meshBudgetBytes, budgetLimited, rejected }
//   layer.classAt(key, lx, y, lz) -> 0..3 | undefined   UIC1 class of an ALREADY RESIDENT tile of the current window
//     (integer local coordinates 0..63 / 0..255). undefined when disposed, cut off, the key is not in the window, the
//     tile is not resident, its origin differs from the entry, the chunk is absent or the coordinates are invalid.
//     Reads the stream cache only: no fetch, no allocation, no tile reference is exposed. Classes are cut classes
//     (0 air OR partial/other model, 1 cube OR diagnostic fallback, 2 water, 3 lava), never precise collision.
//   layer.dispose()
//
// load(entry) resolves { tile } with a decoded UIC1 tile (cave-cap-tile.js decodeCaveCapTile: x, z, bytes, slice(y)).
// attach(key, { positions, colors, indices }) replaces the previous cap mesh of `key` and returns false if it refuses;
// drop(key) disposes it. Cap arrays: capArrays(tile.x, tile.z, capSliceQuads(slice with masked chunks cleared),
// level + 0.998). A slice without quads needs no mesh.
//
// Memory: decoded tiles live only in the stream's bounded cache (budgetBytes, fixed 262168 B per tile); this module
// keeps no tile references of its own. Cap meshes are counted conservatively as 2 x (positions + colors + indices)
// bytes (CPU arrays plus GPU mirror) against meshBudgetBytes; nearest tiles win, caps that do not fit are omitted and
// counted in budgetLimited. While a rebuild replaces meshes, the app may briefly hold old and new GPU buffers.
// Level or mask changes rebuild resident caps without refetching; an unchanged update does no mesh work.
// Caps classify the cut plane only: they carry no block IDs and no precise collision.
import { createCaveCapStream } from './cave-cap-stream.js';
import { capArrays, capSliceQuads } from './cut-cap.js';

const TILE = 64;
const CHUNK_KEY = /^(-?[0-9]{1,7}),(-?[0-9]{1,7})$/;
const MAX_MASKED_CHUNKS = 100_000;

export function createCaveCapLayer({ load, attach, drop, onError = () => {}, budgetBytes = 262168 * 16,
  meshBudgetBytes = 8 * 1024 * 1024, concurrency = 2, timeoutMs, now } = {}) {
  for (const [name, fn] of Object.entries({ load, attach, drop, onError })) {
    if (typeof fn !== 'function') throw new TypeError(`${name} must be a function`);
  }
  if (!Number.isInteger(meshBudgetBytes) || meshBudgetBytes < 1 || meshBudgetBytes > 256 * 1024 * 1024) {
    throw new RangeError('meshBudgetBytes must be an integer in 1..256 MiB');
  }
  let level = null;
  let mask = new Set();
  let order = new Map();          // key -> priority index of the current window (0 = nearest)
  let current = new Map();        // key -> entry of the current window (for the origin check of classAt)
  let signature = '';             // window identity, for the unchanged-update fast path
  let disposed = false;
  const meshes = new Map();       // key -> mesh bytes (accounted); arrays themselves belong to the app's mesh
  let meshBytes = 0;
  const limited = new Set();      // resident tiles without a cap because of meshBudgetBytes
  const rejected = new Set();     // resident tiles whose cap attach() refused

  const report = (key, error) => { try { onError(key, error); } catch { /* UI errors never break the layer */ } };

  function removeMesh(key) {
    limited.delete(key);
    rejected.delete(key);
    if (!meshes.has(key)) return;
    meshBytes -= meshes.get(key);
    meshes.delete(key);
    try { drop(key); } catch (error) { report(key, error); }
  }

  // Cap arrays of one decoded tile at the current level with the detail-covered chunks cleared; null when empty.
  function build(tile) {
    if (!Number.isInteger(tile?.x) || !Number.isInteger(tile?.z) || typeof tile.slice !== 'function') {
      throw new Error('Invalid decoded cave cap tile');
    }
    const classes = tile.slice(level);                  // fresh 4096-byte copy, safe to modify
    if (!(classes instanceof Uint8Array) || classes.length !== TILE * TILE) throw new Error('Invalid cave cap slice');
    if (mask.size) {
      for (let cz = 0; cz < 4; cz += 1) for (let cx = 0; cx < 4; cx += 1) {
        const gx = Math.floor((tile.x + cx * 16) / 16) + 0;
        const gz = Math.floor((tile.z + cz * 16) / 16) + 0;
        if (!mask.has(`${gx},${gz}`)) continue;
        for (let z = cz * 16; z < cz * 16 + 16; z += 1) classes.fill(0, z * TILE + cx * 16, z * TILE + cx * 16 + 16);
      }
    }
    const quads = capSliceQuads(classes, TILE, TILE);
    if (!quads.length) return null;
    const arrays = capArrays(tile.x, tile.z, quads, level + 0.998);
    return { arrays, bytes: 2 * (arrays.positions.byteLength + arrays.colors.byteLength + arrays.indices.byteLength) };
  }

  // Place one cap given the bytes other caps use; returns 'mesh' | 'empty' | 'limited' | 'rejected'.
  function place(key, tile, used, built = build(tile)) {
    if (!built) { removeMesh(key); return 'empty'; }
    if (used + built.bytes > meshBudgetBytes) { removeMesh(key); limited.add(key); return 'limited'; }
    let ok;
    try { ok = attach(key, built.arrays); } catch (error) { report(key, error); ok = false; }
    if (ok === false) { removeMesh(key); rejected.add(key); return 'rejected'; }
    if (meshes.has(key)) meshBytes -= meshes.get(key);   // attach replaced the previous mesh of this key
    meshes.set(key, built.bytes);
    meshBytes += built.bytes;
    limited.delete(key);
    rejected.delete(key);
    return 'mesh';
  }

  // Rebuild every resident cap in priority order (nearest first). `extra` supplies a tile that the stream is about
  // to insert (its onAttach runs before the stream stores the result). Returns the outcome for extra's key.
  function rebuild(extra = null) {
    let used = 0;
    let outcome = null;
    for (const key of order.keys()) {
      const tile = extra?.key === key ? extra.tile : stream.get(key)?.tile;
      if (!tile) continue;
      const result = place(key, tile, used);
      if (result === 'mesh') used += meshes.get(key);
      if (extra?.key === key) outcome = result;
    }
    return outcome;
  }

  function arrive(key, result) {
    if (disposed || level === null || !order.has(key)) return false;
    const tile = result?.tile;
    const built = build(tile);                  // throws on a malformed tile: the stream records a failure
    if (!built) { removeMesh(key); return true; }
    let outcome;
    if (meshBytes + built.bytes <= meshBudgetBytes) outcome = place(key, tile, meshBytes, built);
    else {
      // Over budget: a nearer arrival may displace farther caps; otherwise it is simply omitted.
      const rank = order.get(key);
      const farther = [...meshes.keys()].some((other) => (order.get(other) ?? Infinity) > rank);
      if (!farther) { removeMesh(key); limited.add(key); return true; }
      outcome = rebuild({ key, tile });
    }
    // A refused arrival is not stored by the stream (it records a failure with backoff): keep no state for it.
    if (outcome === 'rejected') { rejected.delete(key); return false; }
    return true;
  }

  const stream = createCaveCapStream({
    load,
    onAttach: (key, result) => arrive(key, result),
    onDrop: (key) => removeMesh(key),
    onError: report,
    budgetBytes, concurrency,
    ...(timeoutMs === undefined ? {} : { timeoutMs }),
    ...(now === undefined ? {} : { now }),
  });

  function validLevel(value) {
    if (value === null || value === undefined) return null;
    if (!Number.isInteger(value) || value < 0 || value > 255) throw new RangeError('Cut level must be an integer 0..255 or null');
    return value;
  }

  function validMask(value) {
    if (value === null || value === undefined) return new Set();
    if (!(value instanceof Set) || value.size > MAX_MASKED_CHUNKS) throw new TypeError('maskedChunks must be a bounded Set');
    const out = new Set();
    for (const key of value) {
      if (typeof key !== 'string' || !CHUNK_KEY.test(key)) throw new TypeError('maskedChunks keys must be "cx,cz"');
      out.add(key);
    }
    return out;
  }

  const sameSet = (a, b) => a.size === b.size && [...a].every((v) => b.has(v));
  const entryId = (e) => [e?.pairSha256, e?.expected?.geometrySha256, e?.expected?.atlasSha256, e?.expected?.rendererIdentity,
    e?.expected?.capIdentity, Array.isArray(e?.expected?.origin) ? e.expected.origin.join(',') : ''].join('/');

  return {
    update({ entries, level: nextLevel, maskedChunks } = {}) {
      if (disposed) return;
      const lvl = validLevel(nextLevel);
      const nextMask = validMask(maskedChunks);
      const list = lvl === null ? [] : [...(entries ?? [])];
      const nextSignature = list.map(([key, entry]) => `${key}\u0000${entryId(entry)}`).join('\u0001');
      const levelChanged = lvl !== level;
      const maskChanged = !sameSet(nextMask, mask);
      const windowChanged = nextSignature !== signature;
      // The stream validates the entries; if it throws, the layer state stays as it was. Drops of left or
      // replaced tiles (and their caps) happen synchronously here, before any replacement can load.
      stream.update(list);
      level = lvl;
      mask = nextMask;
      signature = nextSignature;
      order = new Map(list.map(([key], index) => [key, index]));
      current = new Map(list);
      if (level === null) {
        for (const key of [...meshes.keys()]) removeMesh(key);
        limited.clear(); rejected.clear();
        return;
      }
      if (levelChanged || maskChanged || (windowChanged && (limited.size || rejected.size))) rebuild();
    },
    classAt(key, lx, y, lz) {
      if (disposed || level === null || !order.has(key)) return undefined;
      if (!Number.isInteger(lx) || !Number.isInteger(y) || !Number.isInteger(lz)
          || lx < 0 || lx >= TILE || lz < 0 || lz >= TILE || y < 0 || y > 255) return undefined;
      const tile = stream.get(key)?.tile;       // resident only for the window's current identity
      if (!tile || typeof tile.at !== 'function') return undefined;
      const origin = current.get(key)?.expected?.origin;
      if (!Array.isArray(origin) || tile.x !== origin[0] || tile.z !== origin[1]) return undefined;
      try {
        const value = tile.at(lx, y, lz);
        return Number.isInteger(value) && value >= 0 && value <= 3 ? value : undefined;   // absent chunk: undefined
      } catch {
        return undefined;
      }
    },
    snapshot() {
      return { ...stream.snapshot(), level, meshes: meshes.size, meshBytes, meshBudgetBytes,
        budgetLimited: limited.size, rejected: rejected.size };
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      stream.dispose();                          // drops resident tiles -> removeMesh
      for (const key of [...meshes.keys()]) removeMesh(key);
      limited.clear(); rejected.clear(); order = new Map(); current = new Map(); level = null; mask = new Set();
    },
  };
}
