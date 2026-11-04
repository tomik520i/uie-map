// A newer, identity-checked world publication covering the detail may retire it.
// Call after the immutable index AND its atlas have been validated and committed.
export function canRetireDetail(index, identity, generatedAt, chunks) {
  if (!index?.immutable || !identity || !index.context || !index.source
      || index.context.world !== identity.world || index.context.dimension !== identity.dimension
      || typeof index.context.caves !== 'boolean' || index.tileSize !== 64 || !index.tiles?.size) return false;
  const created = Date.parse(generatedAt);
  if (!Number.isFinite(created) || !(index.source.producerFinishedAt * 1000 > created)) return false;
  // Do not remove a detail extending outside a partial published world footprint.
  return chunks?.length > 0 && chunks.every(({x,z}) => index.tiles.has(`${Math.floor(x/4)},${Math.floor(z/4)}`));
}

export function publishedBounds(index) {
  const keys=[...index.tiles.keys()].map(key=>key.split(',').map(Number));
  if (!keys.length) return null;
  let minX=Infinity,minZ=Infinity,maxX=-Infinity,maxZ=-Infinity;
  for (const [x,z] of keys) { minX=Math.min(minX,x);minZ=Math.min(minZ,z);maxX=Math.max(maxX,x);maxZ=Math.max(maxZ,z); }
  return {min:[minX*64,0,minZ*64],max:[(maxX+1)*64,256,(maxZ+1)*64]};
}
