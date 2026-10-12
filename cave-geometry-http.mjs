// Read-only HTTP namespace for a SEPARATE immutable cave geometry store. Wraps createImmutableHiresHandler unchanged.
//   /data/cave-geometry/current.json                          -> the wrapped handler's /data/hires/current.json
//   /data/cave-geometry/{indexes|objects|atlases|layouts}/... -> /data/hires/{same}/...
// Everything inherited: GET/HEAD only (405 + Allow), bounded concurrency (503 Retry-After), SHA-256 of the exact
// bytes, canonical pointer/index validation, owned-store marker, no symlinks, no-store pointer, immutable objects.
// No filesystem writes.
//
// The handler returns false for every other path (also /data/hires/* and look-alikes such as
// /data/cave-geometryX/...), so the server decides; it should answer an explicit 404 for the rest of the namespace.
// A null/undefined store serves nothing (always false): the surface store is never used as a fallback.
//
// Context responsibility: this wrapper does not read or rewrite the index context. The client loading this
// namespace MUST reject an index whose context.caves is not true (a surface generation published into this store
// would otherwise be served as-is, with valid hashes).
import { createImmutableHiresHandler } from './hires-immutable-http.mjs';

export const CAVE_GEOMETRY_PREFIX = '/data/cave-geometry/';
const INTERNAL_PREFIX = '/data/hires/';
const FOLDER = /^(indexes|objects|atlases|layouts)\/[^/]*$/;

/** Internal /data/hires/ path for an external cave path, or null when the path is not ours. */
export function mapCaveGeometryPath(urlPath) {
  if (typeof urlPath !== 'string' || !urlPath.startsWith(CAVE_GEOMETRY_PREFIX)) return null;
  const rest = urlPath.slice(CAVE_GEOMETRY_PREFIX.length);
  if (rest === 'current.json' || FOLDER.test(rest)) return INTERNAL_PREFIX + rest;
  return null;
}

export function createCaveGeometryHandler(storePath, options = {}) {
  if (storePath === null || storePath === undefined) return async () => false;
  // Absolute normalized path and option limits are validated (TypeError) by the wrapped factory.
  const inner = createImmutableHiresHandler(storePath, options);
  return async (req, res, urlPath) => {
    const internal = mapCaveGeometryPath(urlPath);
    if (internal === null) return false;
    // The name segment is validated by the wrapped handler (64-hex address and suffix, else 400).
    return inner(req, res, internal);
  };
}
