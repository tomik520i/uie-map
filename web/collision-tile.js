// UIB1 lossless block sidecars. Gzip is decoded by HTTP; this parser consumes uncompressed bytes.
// No Three.js dependency. Unknown chunks remain unknown (undefined), not empty air.
export const COLLISION_TILE_SIZE = 64;
export const COLLISION_TILE_BYTES = 64 * 64 * 256 * 3;
const HEADER = 20;
const COUNT = 64 * 64 * 256;
const MAX_REGISTRY = 1024 * 1024;

export function decodeCollisionTile(buffer, expectedX, expectedZ) {
  const fail = (s) => { throw new Error(`UIB1: ${s}`); };
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < HEADER) fail('truncated header');
  const view = new DataView(buffer);
  if (view.getUint32(0, true) !== 0x31424955) fail('wrong magic');
  const x = view.getInt32(4, true), z = view.getInt32(8, true);
  const present = view.getUint16(12, true);
  const length = view.getUint32(16, true);
  if (view.getUint16(14, true) || x % 64 || z % 64) fail('invalid header');
  if (x !== expectedX || z !== expectedZ) fail('wrong tile coordinates');
  if (!length || length > MAX_REGISTRY || buffer.byteLength !== HEADER + length + COLLISION_TILE_BYTES) fail('invalid length');
  const rawRegistry = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(buffer, HEADER, length)));
  if (!rawRegistry || typeof rawRegistry !== 'object' || Array.isArray(rawRegistry)) fail('invalid registry');
  const registry = new Map();
  for (const [id, name] of Object.entries(rawRegistry)) {
    if (!/^(0|[1-9][0-9]{0,4})$/.test(id) || Number(id) > 65535 || typeof name !== 'string' || !name) fail('invalid registry entry');
    registry.set(Number(id), name);
  }
  const offset = HEADER + length;
  // Registry byte length need not be aligned: DataView handles both alignment and endianness.
  const ids = new Uint16Array(COUNT);
  const metas = new Uint8Array(buffer, offset + COUNT * 2, COUNT);
  const seen = new Set();
  for (let i = 0; i < COUNT; i += 1) {
    ids[i] = view.getUint16(offset + i * 2, true);
    seen.add(ids[i]);
    if (metas[i] > 15) fail('metadata outside 0..15');
  }
  for (const id of seen) if (!registry.has(id)) fail(`unknown registry ID ${id}`);
  // Copy metadata to release the larger input buffer when callers discard the response.
  return { x, z, present, registry, ids, metas: metas.slice(), bytes: COLLISION_TILE_BYTES };
}

export function collisionBlockAt(tile, x, y, z) {
  if (![x,y,z].every(Number.isInteger)) return undefined;
  const dx = x - tile.x, dz = z - tile.z;
  if (dx < 0 || dx >= 64 || dz < 0 || dz >= 64 || y < 0 || y >= 256) return undefined;
  if (!(tile.present & (1 << (Math.floor(dz / 16) * 4 + Math.floor(dx / 16))))) return undefined;
  const i = (dz * 64 + dx) * 256 + y;
  const id = tile.ids[i];
  return { id, meta: tile.metas[i], name: tile.registry.get(id) };
}
