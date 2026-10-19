// Static preview only. No access to the Minecraft process or live world.
import http from 'node:http';
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { stat, realpath, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { pipeline } from 'node:stream/promises';
import { createGunzip, gzipSync } from 'node:zlib';
import { createCollisionHandler } from './collision-http.mjs';
import { createImmutableHiresHandler } from './hires-immutable-http.mjs';
import { createCavePairHandler } from './cave-pair-http.mjs';
import { createCaveGeometryHandler } from './cave-geometry-http.mjs';
import { createCaveStatusHandler } from './cave-status-http.mjs';
import { createModelStatusHandler } from './model-status-http.mjs';
import { createCollisionStatusHandler } from './collision-status-http.mjs';
import { createMapViewHandler } from './map-view-http.mjs';


export async function createMapHandler(env = {}) {
// Explicit independent surface viewer; never borrow the old spawn generation.
const mapViewFile = env.UIE_MAP_VIEW_FILE || null;
if (mapViewFile && (env.UIE_GENERATION_STORE || !env.UIE_HIRES_STORE)) {
  throw new Error('UIE_MAP_VIEW_FILE requires UIE_HIRES_STORE and no UIE_GENERATION_STORE');
}
const serveMapView = createMapViewHandler(mapViewFile);

const root = await realpath(fileURLToPath(new URL('./web/', import.meta.url)));
// A worker-owned atomic document lives outside immutable web releases.
const statusFile = env.UIE_RENDER_STATUS_FILE ? path.resolve(env.UIE_RENDER_STATUS_FILE) : null;
// Optional player positions (players_export.py, UIE_PLAYERS1): UIE_PLAYERS_FILE, or players.json next to the
// generation store (written by the players timer). A missing file answers 503 and the web hides the layer.
const playersFile = env.UIE_PLAYERS_FILE ? path.resolve(env.UIE_PLAYERS_FILE)
  : env.UIE_GENERATION_STORE
    ? path.join(path.dirname(path.resolve(env.UIE_GENERATION_STORE)), 'players.json') : null;
// Optional immutable generation store written by publish_generation.py. Unset: behaviour is unchanged.
// /data/revision.json -> <store>/current.json (validated, no-store);
// /data/generations/<64 hex>/<path>.json|.png -> <store>/generations/<id>/<path> for complete generations only.
const generationStore = env.UIE_GENERATION_STORE ? path.resolve(env.UIE_GENERATION_STORE) : null;
// Optional exported collision store beside the generation store. Missing data stays unavailable;
// HTTP requests never start rendering or read the Minecraft world.
const collisionStore = env.UIE_COLLISION_STORE ? path.resolve(env.UIE_COLLISION_STORE)
  : generationStore ? path.join(path.dirname(generationStore), 'collision-store') : null;
const serveCollision = createCollisionHandler(collisionStore);
// Private opt-in paired cave data; no automatic fallback to the surface store.
const serveCavePairs = createCavePairHandler(env.UIE_CAVE_PAIR_STORE || null);
const serveCaveGeometry = createCaveGeometryHandler(env.UIE_CAVE_GEOMETRY_STORE || null);
const serveCaveStatus = createCaveStatusHandler(env.UIE_CAVE_GEOMETRY_STORE || null);
const serveModelStatus = createModelStatusHandler(env.UIE_MODEL_CANDIDATE_STORE || null);
const serveCollisionStatus = createCollisionStatusHandler(env.UIE_COLLISION_PREPARATION_OUT || null);
// Whole-world overview store (overview_tiles.py + publish_overview.py): UIE_OVERVIEW_STORE, or by default
// overview-store next to the generation store. /data/overview.json -> <store>/current.json (validated, no-store);
// /data/overviews/<64 hex>/index.json|L<k>/<x>_<z>[.h].png -> <store>/builds/<id>/... (immutable).
const overviewStore = env.UIE_OVERVIEW_STORE ? path.resolve(env.UIE_OVERVIEW_STORE)
  : generationStore ? path.join(path.dirname(generationStore), 'overview-store') : null;
// Whole-world hires tiles (world_hires.py, BlueMap-like): UIE_HIRES_STORE, or hires-store next to the generation
// store. /data/hires/index.json|status.json (no-store), /data/hires/atlas.png, /data/hires/tiles/<x>_<z>.uih
// (gzip bytes sent with Content-Encoding: gzip; immutable when requested with ?v=<sha12>).
const hiresStore = env.UIE_HIRES_STORE ? path.resolve(env.UIE_HIRES_STORE)
  : generationStore ? path.join(path.dirname(generationStore), 'hires-store') : null;
// Immutable publication shares the producer-owned store unless explicitly overridden.
const serveImmutableHires = createImmutableHiresHandler(env.UIE_HIRES_IMMUTABLE_STORE || hiresStore);
const HIRES_URL = /^\/data\/hires\/(index\.json|status\.json|publisher-status\.json|diagnostics\.json|atlas\.png|atlas-layout\.json|tiles\/-?[0-9]{1,6}_-?[0-9]{1,6}\.uih)$/;
const OVERVIEW_URL =/^\/data\/overviews\/([0-9a-f]{64})\/(index\.json|L(?:[0-9]|1[0-9]|2[0-3])\/-?[0-9]{1,6}_-?[0-9]{1,6}(?:\.h)?\.png)$/;
const HEX64 = /^[0-9a-f]{64}$/;
const GENERATION_URL = /^\/data\/generations\/([0-9a-f]{64})\/(.+)$/;
const SAFE_SEGMENT = /^[A-Za-z0-9_.-]+$/;
const IMMUTABLE = 'public, max-age=31536000, immutable';
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.png': 'image/png', '.svg': 'image/svg+xml', '.txt': 'text/plain; charset=utf-8' };
function acceptsGzip(header) {
  const encodings = new Map();
  for (const item of String(header ?? '').split(',')) {
    const [rawName, ...params] = item.trim().toLowerCase().split(';');
    const name = rawName.trim();
    let quality = 1;
    for (const param of params) if (param.trim().startsWith('q=')) {
      const raw = param.trim().slice(2);
      quality = /^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$/.test(raw) ? Number(raw) : 0;
    }
    encodings.set(name, quality);
  }
  return (encodings.get('gzip') ?? encodings.get('*') ?? 0) > 0;
}
const within = (base, p) => p.startsWith(base + path.sep);

// Streams one already contained regular file, with optional smaller .gz sidecar inside the same base.
async function serveFile(req, res, file, base, cacheControl, fail) {
  let info = await stat(file);
  if (!info.isFile()) return fail(404, 'Not found');
  let servedFile = file;
  const compressible = ['.html', '.js', '.css', '.json', '.svg', '.txt'].includes(path.extname(file));
  let compressed = false;
  if (compressible && acceptsGzip(req.headers['accept-encoding'])) {
    try {
      const gzipFile = await realpath(file + '.gz');
      if (!within(base, gzipFile)) return fail(403, 'Forbidden');
      const gzipInfo = await stat(gzipFile);
      if (gzipInfo.isFile() && gzipInfo.size < info.size) {
        servedFile = gzipFile; info = gzipInfo; compressed = true;
      }
    } catch (err) { if (err.code !== 'ENOENT') throw err; }
  }
  res.writeHead(200, {
    'Content-Type': types[path.extname(file)] ?? 'application/octet-stream',
    'Content-Length': info.size, 'Cache-Control': cacheControl,
    'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
    ...(compressible ? { Vary: 'Accept-Encoding' } : {}),
    ...(compressed ? { 'Content-Encoding': 'gzip' } : {}),
  });
  if (req.method === 'HEAD') return res.end();
  await pipeline(createReadStream(servedFile), res);
}

// Real path of a complete generation directory, or null (unknown, incomplete, or escaping the store).
async function completeGeneration(id) {
  const store = await realpath(generationStore);
  const generations = path.join(store, 'generations');
  let dir;
  try {
    dir = await realpath(path.join(generations, id));
    if (!within(generations, dir) || path.dirname(dir) !== generations) return null;
    const complete = await realpath(path.join(dir, 'COMPLETE.json'));
    if (!within(dir, complete) || !(await stat(complete)).isFile()) return null;
  } catch (err) {
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return null;
    throw err;
  }
  return dir;
}

async function serveRevision(req, res, fail) {
  let body;
  try {
    const store = await realpath(generationStore);
    const pointer = await realpath(path.join(store, 'current.json'));
    if (!within(store, pointer)) return fail(503, 'Revision unavailable');
    const info = await stat(pointer);
    if (!info.isFile() || info.size > 16384) return fail(503, 'Revision unavailable');
    body = await readFile(pointer);
  } catch (err) {
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return fail(404, 'No revision published');
    throw err;
  }
  let doc;
  try { doc = JSON.parse(body.toString('utf8')); } catch { return fail(503, 'Revision unavailable'); }
  const valid = body.length <= 16384 && doc && typeof doc === 'object' && !Array.isArray(doc)
    && doc.format === 'UIE_REVISION1' && typeof doc.generation === 'string' && HEX64.test(doc.generation)
    && doc.manifest === `generations/${doc.generation}/manifest.json`
    && typeof doc.manifestSha256 === 'string' && HEX64.test(doc.manifestSha256);
  // A pointer to a generation that is not complete is never offered.
  if (!valid || !(await completeGeneration(doc.generation))) return fail(503, 'Revision unavailable');
  res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length,
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
  res.end(req.method === 'HEAD' ? undefined : body);
}

async function serveGeneration(req, res, id, rest, fail) {
  const segments = rest.split('/');
  if (segments.some((s) => !SAFE_SEGMENT.test(s) || s === '.' || s === '..')
    || !['.json', '.png'].includes(path.extname(rest)) || path.basename(rest) === 'COMPLETE.json') {
    return fail(404, 'Not found');
  }
  const dir = await completeGeneration(id);
  if (!dir) return fail(404, 'Not found');
  const logical = path.join(dir, ...segments);
  let file;
  try { file = await realpath(logical); }
  catch (err) {
    // Chunk JSON may be stored only compressed; the manifest URL stays ordinary .json.
    // Keep the manifest itself uncompressed for pointer hashing and publisher recovery.
    if (err.code !== 'ENOENT' || !rest.endsWith('.json') || rest === 'manifest.json') throw err;
    const gzipFile = await realpath(logical + '.gz');
    if (!within(dir, gzipFile)) return fail(404, 'Not found');
    const info = await stat(gzipFile);
    if (!info.isFile()) return fail(404, 'Not found');
    const encoded = acceptsGzip(req.headers['accept-encoding']);
    res.writeHead(200, { 'Content-Type': types['.json'], 'Cache-Control': IMMUTABLE,
      'X-Content-Type-Options': 'nosniff', Vary: 'Accept-Encoding',
      ...(encoded ? { 'Content-Encoding': 'gzip', 'Content-Length': info.size } : {}),
    });
    if (req.method === 'HEAD') return res.end();
    if (encoded) await pipeline(createReadStream(gzipFile), res);
    else await pipeline(createReadStream(gzipFile), createGunzip(), res);
    return;
  }
  if (!within(dir, file)) return fail(404, 'Not found');
  return serveFile(req, res, file, dir, IMMUTABLE, fail);
}

async function overviewBuild(id) {
  const store = await realpath(overviewStore);
  const builds = path.join(store, 'builds');
  try {
    const dir = await realpath(path.join(builds, id));
    if (!within(builds, dir) || path.dirname(dir) !== builds) return null;
    if (!(await stat(path.join(dir, 'index.json'))).isFile()) return null;
    return dir;
  } catch (err) {
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return null;
    throw err;
  }
}

async function serveHires(req, res, rest, versioned, fail) {
  let store, file, info;
  try {
    store = await realpath(hiresStore);
    file = await realpath(path.join(store, ...rest.split('/')));
    if (!within(store, file)) return fail(404, 'Not found');
    info = await stat(file);
  } catch (err) {
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return fail(404, 'Not found');
    throw err;
  }
  if (!info.isFile() || info.size > (rest === 'publisher-status.json' ? 4096 : 64 * 1024 * 1024)) return fail(404, 'Not found');
  if (rest === 'atlas.png') return serveFile(req, res, file, store, 'no-cache', fail);
  let body = await readFile(file);
  const tile = rest.startsWith('tiles/');
  // The path is mutable: never cache replacement bytes under a previous revision's URL.
  // Hash the same buffer that is sent, so atomic writer replacement during this read cannot mix versions.
  if (tile && versioned && createHash('sha256').update(body).digest('hex').slice(0,12) !== versioned) {
    res.setHeader('Cache-Control', 'no-store');
    return fail(409, 'Tile revision changed; refresh the index');
  }
  const zipJson = !tile && body.length > 4096 && acceptsGzip(req.headers['accept-encoding']);
  if (zipJson) body = gzipSync(body, { level: 6 });
  const headers = { 'Content-Length': body.length, 'X-Content-Type-Options': 'nosniff',
    'Cache-Control': tile ? (versioned ? IMMUTABLE : 'no-cache') : 'no-store',
    'Content-Type': tile ? 'application/octet-stream' : 'application/json; charset=utf-8' };
  if (tile || zipJson) headers['Content-Encoding'] = 'gzip';
  if (!tile) headers.Vary = 'Accept-Encoding';
  res.writeHead(200, headers);
  res.end(req.method === 'HEAD' ? undefined : body);
}

async function serveOverviewPointer(req, res, fail) {
  let body;
  try {
    const store = await realpath(overviewStore);
    const pointer = await realpath(path.join(store, 'current.json'));
    if (!within(store, pointer)) return fail(503, 'Overview unavailable');
    const info = await stat(pointer);
    if (!info.isFile() || info.size > 4096) return fail(503, 'Overview unavailable');
    body = await readFile(pointer);
  } catch (err) {
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return fail(404, 'No overview published');
    throw err;
  }
  let doc;
  try { doc = JSON.parse(body.toString('utf8')); } catch { return fail(503, 'Overview unavailable'); }
  const valid = doc && typeof doc === 'object' && !Array.isArray(doc) && doc.format === 'UIE_OVERVIEW_POINTER1'
    && typeof doc.build === 'string' && HEX64.test(doc.build) && doc.indexSha256 === doc.build;
  if (!valid || !(await overviewBuild(doc.build))) return fail(503, 'Overview unavailable');
  res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length,
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
  res.end(req.method === 'HEAD' ? undefined : body);
}

async function serveOverviewStatus(req, res, fail) {
  try {
    const store = await realpath(overviewStore);
    const file = await realpath(path.join(store, 'status.json'));
    if (!within(store, file)) return fail(503, 'Overview status unavailable');
    const info = await stat(file);
    if (!info.isFile() || info.size > 16384) return fail(503, 'Overview status unavailable');
    const body = await readFile(file);
    if (body.length > 16384) return fail(503, 'Overview status unavailable');
    let doc; try { doc = JSON.parse(body.toString('utf8')); } catch { return fail(503, 'Overview status unavailable'); }
    if (doc?.format !== 'UIE_OVERVIEW_UPDATE1') return fail(503, 'Overview status unavailable');
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length,
      'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
    res.end(req.method === 'HEAD' ? undefined : body);
  } catch (err) {
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return fail(404, 'No overview status');
    throw err;
  }
}

async function serveOverview(req, res, id, rest, fail) {
  const dir = await overviewBuild(id);
  if (!dir) return fail(404, 'Not found');
  const file = await realpath(path.join(dir, ...rest.split('/')));
  if (!within(dir, file)) return fail(404, 'Not found');
  return serveFile(req, res, file, dir, IMMUTABLE, fail);
}

return async (req, res) => {
  const fail = (code, message) => { res.writeHead(code, { 'Content-Type': 'text/plain; charset=utf-8' }); res.end(message); };
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.setHeader('Allow', 'GET, HEAD'); return fail(405, 'Method not allowed');
  }
  try {
    let urlPath;
    try { urlPath = decodeURIComponent(new URL(req.url, 'http://localhost').pathname); }
    catch { return fail(400, 'Invalid URL'); }
    if (urlPath.includes('\0') || urlPath.includes('\\')) return fail(400, 'Invalid path');
    if (await serveMapView(req, res, urlPath)) return;
    if (await serveCollision(req, res, urlPath)) return;
    if (await serveCaveStatus(req,res,urlPath)) return;
    if (await serveModelStatus(req,res,urlPath)) return;
    if (await serveCollisionStatus(req,res,urlPath)) return;
    if (await serveCavePairs(req, res, urlPath)) return;
    if (await serveImmutableHires(req, res, urlPath)) return;
    if(urlPath.startsWith('/data/cave-geometry/')){
      if(await serveCaveGeometry(req,res,urlPath))return;
      res.writeHead(404,{'Cache-Control':'no-store'});res.end('Cave geometry unavailable');return;
    }
    if (urlPath === '/data/render-status.json' && statusFile) {
      const info = await stat(statusFile);
      if (!info.isFile() || info.size > 16384) return fail(503, 'Render status unavailable');
      const body = await readFile(statusFile);
      if (body.length > 16384) return fail(503, 'Render status unavailable');
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': body.length, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
      res.end(req.method === 'HEAD' ? undefined : body); return;
    }
    if (urlPath === '/data/players.json' && playersFile) {
      let body;
      try {
        const info = await stat(playersFile);
        if (!info.isFile() || info.size > 262144) return fail(503, 'Players unavailable');
        body = await readFile(playersFile);
      } catch {
        return fail(503, 'Players unavailable');
      }
      if (body.length > 262144) return fail(503, 'Players unavailable');
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': body.length, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
      res.end(req.method === 'HEAD' ? undefined : body); return;
    }
    if (generationStore) {
      if (urlPath === '/data/revision.json') return await serveRevision(req, res, fail);
      if (urlPath.startsWith('/data/generations/')) {
        const match = GENERATION_URL.exec(urlPath);
        return match ? await serveGeneration(req, res, match[1], match[2], fail) : fail(404, 'Not found');
      }
    }
    if (hiresStore && urlPath.startsWith('/data/hires/')) {
      const match = HIRES_URL.exec(urlPath);
      const versioned = /^v=([0-9a-f]{12})$/.exec(new URL(req.url, 'http://localhost').search.slice(1))?.[1] ?? null;
      return match ? await serveHires(req, res, match[1], versioned, fail) : fail(404, 'Not found');
    }
    if (overviewStore) {
      if (urlPath === '/data/overview-status.json') return await serveOverviewStatus(req, res, fail);
      if (urlPath === '/data/overview.json')return await serveOverviewPointer(req, res, fail);
      if (urlPath.startsWith('/data/overviews/')) {
        const match = OVERVIEW_URL.exec(urlPath);
        return match ? await serveOverview(req, res, match[1], match[2], fail) : fail(404, 'Not found');
      }
    }
    if (urlPath.endsWith('/')) urlPath += 'index.html';
    const candidate = path.resolve(root, '.' + urlPath);
    if (!within(root, candidate)) return fail(403, 'Forbidden');
    const file = await realpath(candidate);
    if (!within(root, file)) return fail(403, 'Forbidden');
    await serveFile(req, res, file, root, 'no-cache', fail);
  } catch (err) {
    if (res.headersSent) { res.destroy(); return; }
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return fail(404, 'Not found');
    console.error(err.message); fail(500, 'Server error');
  }
};
}

