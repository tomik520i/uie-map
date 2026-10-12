// Approximate block collision from oriented opaque horizontal faces. Unlike a highest-point
// heightfield this preserves air between the ground and a canopy/overhang. This is NOT
// registry collision data: partial models are conservatively expanded to occupied cells.
export function buildHiresWalk(tile, size = 64) {
  if (!Number.isInteger(size) || size < 1 || size > 64) throw new Error('Invalid walk tile size');
  const columns = size * size;
  const events = new Int32Array(columns * 257);
  const seen = new Uint8Array(columns);
  const p = tile.groups[0].positions, s = tile.scale;
  const [ox, oz] = tile.origin, [dx, dy, dz] = tile.offset;
  for (let v = 0; v < p.length; v += 12) {
    const y = p[v + 1];
    if (p[v + 4] !== y || p[v + 7] !== y || p[v + 10] !== y) continue;
    const ny = (p[v + 2] - p[v + 8]) * (p[v + 3] - p[v])
      - (p[v] - p[v + 6]) * (p[v + 5] - p[v + 2]);
    if (!ny) continue;
    const up = ny > 0;
    const h = up ? Math.ceil(y * s + dy) : Math.floor(y * s + dy);
    if (h < 0 || h > 256) continue;
    const xs = [p[v], p[v+3], p[v+6], p[v+9]];
    const zs = [p[v+2], p[v+5], p[v+8], p[v+11]];
    const x0 = Math.max(0, Math.floor(Math.min(...xs)*s+dx-ox));
    const x1 = Math.min(size, Math.ceil(Math.max(...xs)*s+dx-ox));
    const z0 = Math.max(0, Math.floor(Math.min(...zs)*s+dz-oz));
    const z1 = Math.min(size, Math.ceil(Math.max(...zs)*s+dz-oz));
    for (let z=z0; z<z1; z++) for (let x=x0; x<x1; x++) {
      const c=z*size+x;
      events[c*257+h] += up ? -1 : 1;
      seen[c]=1;
    }
  }
  const solid = new Uint32Array(columns * 8);
  const minimum = new Int16Array(columns).fill(257);
  for (let c=0; c<columns; c++) {
    if (!seen[c]) continue;
    const base=c*257;
    let count=0;
    for (let y=0; y<=256; y++) count-=events[base+y];
    if (count < 0) continue; // incomplete/inconsistent shell: unknown, never silently air
    minimum[c] = count > 0 ? 0 : 257;
    let valid=true;
    for (let y=0; y<=256; y++) {
      count+=events[base+y];
      if (events[base+y] && minimum[c]===257) minimum[c]=y;
      if (count<0) { valid=false; break; }
      if (count>0 && y<256) solid[c*8+(y>>>5)] |= 1<<(y&31);
    }
    if (!valid) minimum[c]=257;
  }
  return { size, solid, minimum, byteLength: solid.byteLength+minimum.byteLength };
}

// undefined = no evidence / unloaded; true = solid cell; false = air cell.
export function hiresWalkCell(walk, x, y, z) {
  if (!walk || x<0 || z<0 || x>=walk.size || z>=walk.size) return undefined;
  const c=Math.floor(z)*walk.size+Math.floor(x), bottom=walk.minimum[c];
  if (bottom===257 || y<bottom) return undefined;
  if (y>=256) return false;
  return Boolean(walk.solid[c*8+(Math.floor(y)>>>5)] & (1<<(Math.floor(y)&31)));
}
