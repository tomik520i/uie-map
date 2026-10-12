// UIE_BLOCKS1 block data for click selection: validation, RLE decoding, lookup and cell picking.
// No DOM or three.js, so it can be tested directly in Node.

export const BLOCK_FORMAT = 'UIE_BLOCKS1';
const HEIGHT = 256;
const CHUNK = 16;
const isInt = Number.isSafeInteger;

export class BlockDataError extends Error {}

/**
 * Validate one block file for chunk (chunkX, chunkZ) and expand its runs.
 * Voxel order: ((worldZ - z) * width + (worldX - x)) * 256 + y; the RLE runs over the whole array.
 */
export function decodeBlockChunk(data, chunkX, chunkZ, where = 'bloková data') {
  const fail = (msg) => { throw new BlockDataError(`${where}: ${msg}`); };
  if (!isInt(chunkX) || !isInt(chunkZ) || ![chunkX, chunkZ].every((c) => isInt(c * CHUNK) && isInt(c * CHUNK + CHUNK))) {
    fail(`souřadnice chunku ${String(chunkX)},${String(chunkZ)} nejsou bezpečná celá čísla`);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) fail('kořen není objekt');
  if (data.format !== BLOCK_FORMAT) fail(`nepodporovaný format ${JSON.stringify(data.format)}`);
  if (data.height !== HEIGHT) fail(`height musí být ${HEIGHT}`);
  const { x, z, width, depth } = data;
  if (!isInt(x) || !isInt(z)) fail('x/z nejsou celá čísla');
  if (!isInt(width) || width < 1 || width > CHUNK || !isInt(depth) || depth < 1 || depth > CHUNK) {
    fail('width/depth musí být celá čísla 1..16');
  }
  const minX = chunkX * CHUNK;
  const minZ = chunkZ * CHUNK;
  if (x < minX || x + width > minX + CHUNK || z < minZ || z + depth > minZ + CHUNK) {
    fail(`oblast X ${x}+${width}, Z ${z}+${depth} neleží v chunku ${chunkX},${chunkZ}`);
  }
  const rawRegistry = data.registry;
  if (!rawRegistry || typeof rawRegistry !== 'object' || Array.isArray(rawRegistry)) fail('registry není objekt');
  const registry = new Map();
  for (const [key, name] of Object.entries(rawRegistry)) {
    if (!/^(0|[1-9]\d{0,4})$/.test(key) || Number(key) > 65535) fail(`neplatné ID v registry: ${JSON.stringify(key)}`);
    if (typeof name !== 'string' || name === '') fail(`registry ${key} nemá jméno`);
    registry.set(Number(key), name);
  }
  if (!Array.isArray(data.runs)) fail('runs není pole');
  const total = width * depth * HEIGHT;
  const ids = new Uint16Array(total);
  const metas = new Uint8Array(total);
  let pos = 0;
  for (let i = 0; i < data.runs.length; i += 1) {
    const run = data.runs[i];
    if (!Array.isArray(run) || run.length !== 3) fail(`runs[${i}] není [count, id, meta]`);
    const [count, id, meta] = run;
    if (!isInt(count) || count < 1) fail(`runs[${i}]: count musí být kladné celé číslo`);
    if (!isInt(id) || id < 0 || id > 65535) fail(`runs[${i}]: id mimo 0..65535`);
    if (!isInt(meta) || meta < 0 || meta > 15) fail(`runs[${i}]: meta mimo 0..15`);
    if (!registry.has(id)) fail(`runs[${i}]: id ${id} chybí v registry`);
    if (count > total - pos) fail(`součet count přesahuje ${total}`);
    ids.fill(id, pos, pos + count);
    metas.fill(meta, pos, pos + count);
    pos += count;
  }
  if (pos !== total) fail(`součet count ${pos} neodpovídá ${total}`);
  return { x, z, width, depth, registry, ids, metas };
}

/** Block at world coordinates, or null outside the decoded (possibly cropped) chunk area. */
export function blockAt(chunk, x, y, z) {
  if (!Number.isInteger(y) || y < 0 || y >= HEIGHT) return null;
  const dx = x - chunk.x;
  const dz = z - chunk.z;
  if (!Number.isInteger(dx) || !Number.isInteger(dz) || dx < 0 || dx >= chunk.width || dz < 0 || dz >= chunk.depth) {
    return null;
  }
  const i = (dz * chunk.width + dx) * HEIGHT + y;
  const id = chunk.ids[i];
  return { id, meta: chunk.metas[i], name: chunk.registry.get(id) };
}

/**
 * Cells of a decoded chunk whose registry name is in `names` (a Set), highest Y first per column, at most `limit`.
 * Used by the coverage panel to locate unsupported blocks; ids are resolved once per chunk, not per cell.
 */
export function findBlocks(chunk, names, limit = 50) {
  const wanted = new Set();
  for (const [id, name] of chunk.registry) if (names.has(name)) wanted.add(id);
  const found = [];
  if (wanted.size === 0 || limit <= 0) return found;
  for (let dz = 0; dz < chunk.depth; dz += 1) {
    for (let dx = 0; dx < chunk.width; dx += 1) {
      const base = (dz * chunk.width + dx) * HEIGHT;
      for (let y = HEIGHT - 1; y >= 0; y -= 1) {
        const id = chunk.ids[base + y];
        if (!wanted.has(id)) continue;
        found.push({ x: chunk.x + dx, y, z: chunk.z + dz, id, name: chunk.registry.get(id), meta: chunk.metas[base + y] });
        if (found.length >= limit) return found;
      }
    }
  }
  return found;
}

export const chunkCoord = (v) => Math.floor(v / CHUNK);

/**
 * Cell of the block that emitted a hit face: floor(point - normal * 0.001). The world-space normal is
 * the geometric face normal from the renderer's winding; it points out of the emitting block, so the
 * result is the same from either side of the face. The ray direction is intentionally ignored.
 */
export function pickCell(point, normal, _direction) {
  return {
    x: Math.floor(point.x - normal.x * 0.001),
    y: Math.floor(point.y - normal.y * 0.001),
    z: Math.floor(point.z - normal.z * 0.001),
  };
}

/** Small least-recently-used cache. */
export class LruCache {
  constructor(limit) {
    this.limit = limit;
    this.map = new Map();
  }

  get(key) {
    if (!this.map.has(key)) return undefined;
    const value = this.map.get(key);
    this.map.delete(key);
    this.map.set(key, value);
    return value;
  }

  set(key, value) {
    this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.limit) this.map.delete(this.map.keys().next().value);
  }

  get size() {
    return this.map.size;
  }
}
