// Dedicated atlas + materials of the whole-world (hires) tiles, bound to the atlasSha256 of the hires index.
// No DOM and no three.js here: app.js injects loading, material creation and disposal, so this is testable in Node.
//
// createHiresAtlas({ loadAtlas, makeMaterials, releaseAtlas, disposeMaterials })
//   loadAtlas(url, expectedSha256) -> Promise<texture>  (must verify the bytes against expectedSha256 or throw)
//   makeMaterials(texture) -> { opaque, transparent }
//   releaseAtlas(texture), disposeMaterials(materials)  only ever called for resources created here
// API
//   .prepare(index, baseUrl) -> Promise<prepared>  loads + verifies the index's atlas; the active set is untouched
//                                                  (same sha and url: no load, prepared.switched === false)
//   .commit(prepared, detach) -> active | null     activates the prepared set; detach(previous | null) runs right
//                                                  after activation (not for a reused set) and must remove every
//                                                  mesh of the previous set before its materials and texture are
//                                                  released. A prepared set that is no longer based on the active
//                                                  one is discarded (null).
//   .discard(prepared)                             releases an uncommitted prepared set
//   .active -> { sha, url, texture, materials, generation } | null;  .generation -> number (0 = none)
//   .isCurrent(generation) -> boolean              for tiles fetched against an earlier set
// Rules: an index without a valid atlasSha256 or with an unsafe atlas path is refused before any request; a
// failed load or hash mismatch rejects and keeps the previous set (the caller keeps the previous index too).

const SHA256 = /^[0-9a-f]{64}$/;
const SAFE_PATH = /^[\w.\-/]+$/;

export class HiresAtlasError extends Error {}

export function createHiresAtlas({ loadAtlas, makeMaterials, releaseAtlas, disposeMaterials } = {}) {
  for (const [name, fn] of Object.entries({ loadAtlas, makeMaterials, releaseAtlas, disposeMaterials })) {
    if (typeof fn !== 'function') throw new TypeError(`${name} must be a function`);
  }
  let active = null;
  let generation = 0;

  function release(set) {
    try { disposeMaterials(set.materials); } finally { releaseAtlas(set.texture); }
  }

  return {
    get active() { return active; },
    get generation() { return active ? active.generation : 0; },
    isCurrent(value) { return active !== null && value === active.generation; },

    async prepare(index, baseUrl) {
      const sha = index?.atlasSha256;
      if (typeof sha !== 'string' || !SHA256.test(sha)) throw new HiresAtlasError('index dlaždic nemá platný atlasSha256');
      const atlas = index.atlas ?? 'atlas.png';
      if (typeof atlas !== 'string' || !SAFE_PATH.test(atlas) || atlas.startsWith('/') || atlas.split('/').includes('..')) {
        throw new HiresAtlasError(`neplatná cesta atlasu dlaždic ${JSON.stringify(atlas)}`);
      }
      const url = `${baseUrl}${atlas}`;
      const from = active;
      if (active && active.sha === sha && active.url === url) return { set: active, from, switched: false, committed: false };
      const texture = await loadAtlas(url, sha);
      let materials;
      try {
        materials = makeMaterials(texture);
      } catch (error) {
        releaseAtlas(texture);
        throw error;
      }
      return { set: { sha, url, texture, materials, generation: 0 }, from, switched: true, committed: false };
    },

    commit(prepared, detach) {
      if (!prepared.switched) return active === prepared.set ? active : null;
      if (prepared.committed) return active;
      if (active !== prepared.from) {      // another set was activated meanwhile: never activate an older plan
        this.discard(prepared);
        return null;
      }
      const previous = active;
      generation += 1;
      prepared.set.generation = generation;
      prepared.committed = true;
      active = prepared.set;
      // Release the previous texture/materials only after every mesh using them is gone. If detach throws, they
      // are kept (a leak is safer than drawing with a closed bitmap).
      detach(previous);                     // also for the first set (previous null): the caller switches its index
      if (previous) release(previous);
      return active;
    },

    discard(prepared) {
      if (prepared?.switched && !prepared.committed) {
        prepared.committed = true;          // released once
        release(prepared.set);
      }
    },
  };
}
