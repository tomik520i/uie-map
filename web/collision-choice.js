// Pure choice between exact loaded collision block data and the existing walk approximation. No fetch, no timers,
// no state: the caller passes the current source and the CURRENTLY visible geometry/atlas on every call.
//
//   chooseCollision({ source, strict, x, y, z, visibleGeometrySha, visibleAtlasSha }, blockShape)
//     -> { handled, shape, mode }
//       exact     handled true, shape = blockShape(name, meta) of an actual loaded block (air included)
//       waiting   handled true, shape undefined: strict mode (explicit ?collision pilot) without matching data;
//                 the walker treats undefined as unknown and waits, exactly as before
//       fallback  handled false: automatic mode without matching data; the caller uses its existing approximation
//     The shape callback receives (name, meta, readNeighbor). readNeighbor is for synchronous use in that callback;
//     it returns validated blocks from this SAME source, allowing shapes spanning neighboring cells. Undefined
//     shape results keep the same strict-wait/automatic-fallback behavior as missing own-cell data.
//
// source: null, a single-pair source (collision-source.js: .pair + .blockAt) or a validated world/follower
// (collision-world.js / collision-follow.js: .blockAt checks the queried tile against its live geometry callback).
// When source.pair is present it is checked FIRST (geometrySha256[:12] equals the visible tile hash and atlasSha256
// equals the visible atlas); on mismatch blockAt is never called. A missing, malformed (name not a non-empty string,
// meta not an integer 0..15) or throwing block lookup counts as unavailable: never invented air.
const WAITING = Object.freeze({ handled: true, shape: undefined, mode: 'waiting' });
const FALLBACK = Object.freeze({ handled: false, shape: undefined, mode: 'fallback' });

function pairMatches(pair, visibleGeometrySha, visibleAtlasSha) {
  return typeof pair?.geometrySha256 === 'string' && typeof visibleGeometrySha === 'string' && visibleGeometrySha.length > 0
    && pair.geometrySha256.slice(0, 12) === visibleGeometrySha
    && typeof pair.atlasSha256 === 'string' && pair.atlasSha256 === visibleAtlasSha;
}

// Raw lookup for neighbor-dependent shapes. Every neighbor passes the same revision/atlas validation.
export function collisionBlockAt({ source = null, x, y, z, visibleGeometrySha, visibleAtlasSha } = {}) {
  if (!source || typeof source.blockAt !== 'function') return undefined;
  if (source.pair !== undefined && source.pair !== null
      && !pairMatches(source.pair, visibleGeometrySha, visibleAtlasSha)) return undefined;
  let block;
  try {
    block = source.blockAt(x, y, z);
  } catch {
    return undefined;
  }
  if (!block || typeof block.name !== 'string' || !block.name || !Number.isInteger(block.meta)
      || block.meta < 0 || block.meta > 15) return undefined;
  return block;
}

export function chooseCollision(options = {}, blockShape) {
  if (typeof blockShape !== 'function') throw new TypeError('blockShape must be a function');
  const unavailable = options.strict === true ? WAITING : FALLBACK;
  const block = collisionBlockAt(options);
  if (!block) return unavailable;
  const readNeighbor = (x, y, z) => x===options.x && y===options.y && z===options.z
    ? block : collisionBlockAt({ ...options, x, y, z });
  let shape;
  try {
    shape = blockShape(block.name, block.meta, readNeighbor);
  } catch {
    return unavailable;
  }
  if (shape === undefined || shape === null) return unavailable;
  return { handled: true, shape, mode: 'exact' };
}
