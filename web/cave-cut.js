// Height cut ("řez výšky") for looking into caves: everything above block level Y is clipped away.
// No DOM or three.js, so it can be tested directly in Node. app.js turns the level into a clipping plane
// (normal (0,-1,0), constant = clipTop(level)) and filters ray hits with firstVisibleHit.
//
// A level Y keeps every block whose Y coordinate is <= Y, i.e. geometry up to the top face at Y + 1.
// The cut is not capped: solid rock under the cut is open (only exposed faces exist in the meshes), so cave
// floors, walls and ores facing a cavity become visible from above.

const EPS = 1e-3;

/** Integer block levels the slider offers for the manifest bounds ({min:[x,y,z], max:[x,y,z]}). */
export function cutRange(bounds) {
  const lo = Math.floor(bounds.min[1]);
  const hi = Math.max(lo, Math.ceil(bounds.max[1]) - 1);
  return { min: lo, max: hi };
}

/** The world Y of the clipping plane for a block level (top face of that level). */
export function clipTop(level) {
  return level + 1;
}

/** ?cut=<int> → a level inside the range, or null when absent/invalid. Out-of-range values are clamped. */
export function parseCutParam(value, range) {
  if (value === null || value === undefined || !/^-?[0-9]{1,4}$/.test(String(value).trim())) return null;
  const level = Number.parseInt(String(value).trim(), 10);
  return Math.min(range.max, Math.max(range.min, level));
}

/** First intersection not clipped away (hits sorted by distance, as three.js returns them). */
export function firstVisibleHit(hits, level) {
  if (level === null || level === undefined) return hits[0];
  const top = clipTop(level) + EPS;
  return hits.find((hit) => hit.point.y <= top);
}

/** Short Czech label for the panel. */
export function describeCut(level) {
  return level === null || level === undefined ? 'vypnuto' : `bloky do Y ${level}`;
}
