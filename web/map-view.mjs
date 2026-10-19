// Explicit whole-world-only bootstrap. It is not a legacy detail manifest.
export function validateMapView(value) {
  const fail = message => { throw new Error(`map-view.json: ${message}`); };
  const keys = ['format', 'world', 'dimension', 'title', 'bounds', 'focus'];
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).length !== keys.length || !keys.every(k => Object.hasOwn(value, k))) fail('neplatná struktura');
  if (value.format !== 'UIE_MAP_VIEW1') fail('nepodporovaný formát');
  if (typeof value.world !== 'string' || !/^[\x21-\x7e]{1,128}$/.test(value.world)) fail('neplatný svět');
  const dimension = value.dimension;
  const numeric = typeof dimension === 'string' && /^(0|-?[1-9][0-9]*)$/.test(dimension)
    && Number.isSafeInteger(Number(dimension)) && Number(dimension) >= -2147483648 && Number(dimension) <= 2147483647;
  const custom = typeof dimension === 'string' && /^x:[A-Za-z0-9][A-Za-z0-9_.-]{0,125}$/.test(dimension);
  if (!numeric && !custom) fail('neplatná dimenze');
  if (typeof value.title !== 'string' || !value.title.trim() || value.title.length > 200
      || /[\x00-\x1f\x7f]/.test(value.title)) fail('neplatný název');
  const vector = v => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite)
    && Math.abs(v[0]) <= 30_000_000 && Math.abs(v[2]) <= 30_000_000 && v[1] >= 0 && v[1] <= 256;
  const b = value.bounds;
  if (!b || Object.keys(b).length !== 2 || !vector(b.min) || !vector(b.max)
      || b.min.some((n, i) => n >= b.max[i])) fail('neplatné hranice');
  if (!vector(value.focus) || value.focus.some((n, i) => n < b.min[i] || n > b.max[i])) fail('ohnisko mimo hranice');
  return structuredClone(value);
}

// Only an explicit 404 means legacy mode. A broken configured view must never
// quietly display a different world through a legacy fallback.
export async function loadMapView(fetcher, url, signal) {
  const response = await fetcher(url, { cache: 'no-store', signal });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`map-view.json: HTTP ${response.status}`);
  const text = await response.text();
  if (text.length > 4096) throw new Error('map-view.json: příliš velký dokument');
  return validateMapView(JSON.parse(text));
}
