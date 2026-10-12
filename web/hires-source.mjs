// Shared V3 immutable hires source validator for browser and Node 18.
// `source` describes the producer run bound into one immutable index, never a
// mutable status file or the time of a later publisher polling tick.
const SOURCE_KEYS = ['legacyIndexSha256', 'producerFinishedAt', 'planComplete',
  'plannedJobs', 'notScheduled', 'failedJobs', 'failedTiles', 'staleTiles'];
const HEX64 = /^[0-9a-f]{64}$/;
const TILE_KEY = /^(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})$/;
const MAX_TILE_COORD = Math.floor(30_000_000 / 64);
const MAX_STALE_TILES = 100_000;
const plainObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
const count = value => Number.isSafeInteger(value) && value >= 0;

export function validHiresSource(source, tilesObject) {
  if (!plainObject(source) || !plainObject(tilesObject)
      || Object.keys(source).length !== SOURCE_KEYS.length
      || !SOURCE_KEYS.every(key => Object.hasOwn(source, key))) return false;
  if (typeof source.legacyIndexSha256 !== 'string' || !HEX64.test(source.legacyIndexSha256)
      || !Number.isSafeInteger(source.producerFinishedAt) || source.producerFinishedAt <= 0
      || typeof source.planComplete !== 'boolean') return false;
  for (const key of ['plannedJobs', 'notScheduled', 'failedJobs', 'failedTiles']) {
    if (!count(source[key])) return false;
  }
  if (source.notScheduled > source.plannedJobs || source.failedJobs > source.plannedJobs
      || !Array.isArray(source.staleTiles) || source.staleTiles.length > MAX_STALE_TILES) return false;
  let previous = null;
  for (const key of source.staleTiles) {
    if (typeof key !== 'string' || (previous !== null && previous >= key)
        || !Object.hasOwn(tilesObject, key)) return false;
    const match = TILE_KEY.exec(key);
    if (!match || Math.abs(Number(match[1])) > MAX_TILE_COORD
        || Math.abs(Number(match[2])) > MAX_TILE_COORD) return false;
    previous = key;
  }
  if (source.planComplete && (source.notScheduled !== 0 || source.failedJobs !== 0
      || source.failedTiles !== 0 || source.staleTiles.length !== 0)) return false;
  return true;
}
