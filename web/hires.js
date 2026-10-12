// Whole-world hires tiles (world_hires.py, UIH1, BlueMap-like). Pure: no DOM, no three.js.
//
// decodeHiresTile(arrayBuffer of the UNCOMPRESSED tile) -> { origin: [x, z], groups: [opaque, translucent] }
//   group = { positions: Float32Array, uvs: Float32Array, colors: Float32Array, indices: Uint32Array, quads }
// tilesAround(index, x, z, radius) -> ["tx,tz", ...] nearest first, only tiles the index lists.
// The server sends tiles gzip-encoded; the browser fetch decompresses (Content-Encoding) or DecompressionStream.

export const MAGIC = 'UIH1';
export const SCALE = 128;
export const BIAS = 2;
const HEADER_BYTES = 24;

export class HiresError extends Error {
  constructor(message) { super(message); this.name = 'HiresError'; }
}

export function decodeHiresTile(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < HEADER_BYTES) throw new HiresError('dlaždice je příliš krátká');
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
  if (magic !== MAGIC || view.getUint16(4, true) !== 1) throw new HiresError('neznámý formát dlaždice');
  const ox = view.getInt32(8, true);
  const oz = view.getInt32(12, true);
  const counts = [view.getUint32(16, true), view.getUint32(20, true)];
  let off = HEADER_BYTES;
  const groups = counts.map((quads) => {
    const verts = quads * 4;
    const need = verts * (6 + 4 + 3);
    if (off + need > buffer.byteLength) throw new HiresError('dlaždice je useknutá');
    const positions = new Float32Array(verts * 3);
    for (let i = 0; i < verts * 3; i += 1) {
      const q = view.getUint16(off + i * 2, true);
      const axis = i % 3;
      positions[i] = q / SCALE - BIAS + (axis === 0 ? ox : axis === 2 ? oz : 0);
    }
    off += verts * 6;
    const uvs = new Float32Array(verts * 2);
    for (let i = 0; i < verts * 2; i += 1) uvs[i] = view.getUint16(off + i * 2, true) / 65535;
    off += verts * 4;
    const colors = new Float32Array(verts * 3);
    for (let i = 0; i < verts * 3; i += 1) colors[i] = view.getUint8(off + i) / 255;
    off += verts * 3;
    const indices = new Uint32Array(quads * 6);
    for (let q = 0; q < quads; q += 1) {
      const b = q * 4;
      indices.set([b, b + 1, b + 2, b, b + 2, b + 3], q * 6);
    }
    return { positions, uvs, colors, indices, quads };
  });
  if (off !== buffer.byteLength) throw new HiresError('přebytečná data v dlaždici');
  return { origin: [ox, oz], groups };
}

// sRGB byte -> linear byte (vertex colours are authored in sRGB; three.js works in linear space).
const LINEAR = new Uint8Array(256).map((_, i) => {
  const c = i / 255;
  return Math.round(255 * (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
});

/**
 * Compact decode for the GPU (about half the memory of decodeHiresTile): per group the quantised arrays as stored
 * (positions Uint16 = (v - origin + BIAS) * SCALE, uvs Uint16 normalised, colours Uint8 linear normalised) plus
 * indices (Uint16 when possible). World position = quantised / SCALE + (originX - BIAS, -BIAS, originZ - BIAS).
 */
export function decodeHiresTileCompact(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < HEADER_BYTES) throw new HiresError('dlaždice je příliš krátká');
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
  if (magic !== MAGIC || view.getUint16(4, true) !== 1) throw new HiresError('neznámý formát dlaždice');
  const ox = view.getInt32(8, true);
  const oz = view.getInt32(12, true);
  const counts = [view.getUint32(16, true), view.getUint32(20, true)];
  let off = HEADER_BYTES;
  const u16 = (bytes) => { const out = new Uint16Array(bytes / 2); for (let i = 0; i < out.length; i += 1) out[i] = view.getUint16(off + i * 2, true); return out; };
  const groups = counts.map((quads) => {
    const verts = quads * 4;
    if (off + verts * 13 > buffer.byteLength) throw new HiresError('dlaždice je useknutá');
    const positions = u16(verts * 6); off += verts * 6;
    const uvs = u16(verts * 4); off += verts * 4;
    const colors = new Uint8Array(verts * 3);
    for (let i = 0; i < colors.length; i += 1) colors[i] = LINEAR[view.getUint8(off + i)];
    off += verts * 3;
    const indices = verts <= 65536 ? new Uint16Array(quads * 6) : new Uint32Array(quads * 6);
    for (let q = 0; q < quads; q += 1) {
      const b = q * 4;
      indices.set([b, b + 1, b + 2, b, b + 2, b + 3], q * 6);
    }
    return { positions, uvs, colors, indices, quads };
  });
  if (off !== buffer.byteLength) throw new HiresError('přebytečná data v dlaždici');
  return { origin: [ox, oz], offset: [ox - BIAS, -BIAS, oz - BIAS], scale: 1 / SCALE, groups };
}

/**
 * Walkable surface of a compact-decoded tile: per column (z * size + x) the highest horizontal opaque face,
 * rounded to the block grid (= y of the block top), or -1 without one. Used for walking outside the detail
 * export (which has real block data). Computed before the CPU arrays are released.
 */
export function surfaceHeights(tile, size = 64) {
  const heights = new Int16Array(size * size).fill(-1);
  const group = tile.groups[0];
  const p = group.positions;
  const [offX, offY, offZ] = tile.offset;
  const s = tile.scale;
  const [ox, oz] = tile.origin;
  for (let q = 0; q < group.quads; q += 1) {
    const v = q * 12;
    const y = p[v + 1];
    if (p[v + 4] !== y || p[v + 7] !== y || p[v + 10] !== y) continue;   // not horizontal
    const cx = Math.floor((p[v] + p[v + 6]) * s / 2 + offX - ox);
    const cz = Math.floor((p[v + 2] + p[v + 8]) * s / 2 + offZ - oz);
    if (cx < 0 || cz < 0 || cx >= size || cz >= size) continue;
    const top = Math.round(y * s + offY);
    const i = cz * size + cx;
    if (top > heights[i]) heights[i] = top;
  }
  return heights;
}

/**
 * Atlas sprite lookup (atlas-layout.json, UIE_ATLAS_LAYOUT1): spriteAt(u, v) -> icon name or null. Used to name
 * the texture of a picked whole-world face (the tiles carry no block ids). Rects are bucketed on a 64 x 64 grid.
 */
export function spriteIndex(layout) {
  const grid = new Map();
  for (const [name, sprite] of Object.entries(layout?.registered ?? {})) {
    if (sprite?.state && sprite.state !== 'placed') continue;
    const [a, b, c, d] = sprite?.uv ?? [];
    if (![a, b, c, d].every(Number.isFinite)) continue;
    const r = [Math.min(a, c), Math.min(b, d), Math.max(a, c), Math.max(b, d), sprite.iconName ?? name];
    for (let gu = Math.floor(r[0] * 64); gu <= Math.floor(r[2] * 64); gu += 1) {
      for (let gv = Math.floor(r[1] * 64); gv <= Math.floor(r[3] * 64); gv += 1) {
        const key = gu * 65 + gv;
        if (!grid.has(key)) grid.set(key, []);
        grid.get(key).push(r);
      }
    }
  }
  return (u, v) => {
    for (const r of grid.get(Math.floor(u * 64) * 65 + Math.floor(v * 64)) ?? []) {
      if (u >= r[0] && u <= r[2] && v >= r[1] && v <= r[3]) return r[4];
    }
    return null;
  };
}

/** Validated index (UIE_HIRES_INDEX1): { tileSize, atlas, tiles: Map("tx,tz" -> sha12) }. */
export function parseHiresIndex(doc) {
  if (!doc || doc.format !== 'UIE_HIRES_INDEX1') throw new HiresError('neznámý index dlaždic');
  if (!Number.isInteger(doc.tileSize) || doc.tileSize <= 0) throw new HiresError('neplatná velikost dlaždice');
  if (!doc.tiles || typeof doc.tiles !== 'object') throw new HiresError('index nemá dlaždice');
  const tiles = new Map();
  for (const [key, sha] of Object.entries(doc.tiles)) {
    if (!/^-?\d+,-?\d+$/.test(key) || !/^[0-9a-f]{12}$/.test(sha)) throw new HiresError(`neplatná dlaždice ${key}`);
    tiles.set(key, sha);
  }
  return { tileSize: doc.tileSize, atlas: doc.atlas ?? 'atlas.png', atlasSha256: doc.atlasSha256 ?? null, tiles };
}

/** Listed tiles within radius (blocks) of (x, z), nearest first. */
export function tilesAround(index, x, z, radius) {
  const size = index.tileSize;
  const r = Math.ceil(radius / size);
  const cx = Math.floor(x / size);
  const cz = Math.floor(z / size);
  const out = [];
  for (let tz = cz - r; tz <= cz + r; tz += 1) {
    for (let tx = cx - r; tx <= cx + r; tx += 1) {
      const key = `${tx},${tz}`;
      if (!index.tiles.has(key)) continue;
      const d = Math.hypot((tx + 0.5) * size - x, (tz + 0.5) * size - z);
      if (d <= radius + size) out.push([d, key]);
    }
  }
  return out.sort((a, b) => a[0] - b[0]).map(([, key]) => key);
}

/** Chunk keys "cx,cz" covered by a tile (to hide the overview under it). */
export function tileChunks(key, tileSize) {
  const [tx, tz] = key.split(',').map(Number);
  const n = tileSize / 16;
  const out = [];
  for (let dz = 0; dz < n; dz += 1) for (let dx = 0; dx < n; dx += 1) out.push(`${tx * n + dx},${tz * n + dz}`);
  return out;
}
