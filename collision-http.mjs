// Optional immutable collision candidate endpoints. No world reads or render-on-request.
//   /data/collision/pairs/<sha>.json, /data/collision/objects/<sha>.uib|uih   immutable, content identity checked
//   /data/collision/indexes/<sha>.json   immutable UIE_COLLISION_INDEX1, hash of the exact bytes checked + shape
//   /data/collision/current.json         no-store UIE_COLLISION_POINTER1, strictly validated, index must exist
import path from 'node:path';
import { realpath, stat, readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';

// Same limits as collision_bundle.py publish_index.
export const MAX_INDEX_BYTES = 8 * 1024 * 1024;
export const MAX_INDEX_ENTRIES = 90_000;
const MAX_TILE_COORD = Math.floor(30_000_000 / 64);
const MAX_POINTER_BYTES = 4096;
const HEX64 = /^[a-f0-9]{64}$/;
const TILE_KEY = /^(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})$/;
const IMMUTABLE = 'public, max-age=31536000, immutable';

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const sameKeys = (o, keys) => Object.keys(o).length === keys.length && keys.every((k) => Object.hasOwn(o, k));

export function validCollisionIndex(doc) {
  if (!isPlainObject(doc) || !sameKeys(doc, ['format', 'tileSize', 'pairs'])) return false;
  if (doc.format !== 'UIE_COLLISION_INDEX1' || doc.tileSize !== 64 || !isPlainObject(doc.pairs)) return false;
  const entries = Object.entries(doc.pairs);
  if (entries.length > MAX_INDEX_ENTRIES) return false;
  return entries.every(([key, sha]) => {
    const m = TILE_KEY.exec(key);
    return m && Math.abs(Number(m[1])) <= MAX_TILE_COORD && Math.abs(Number(m[2])) <= MAX_TILE_COORD
      && typeof sha === 'string' && HEX64.test(sha);
  });
}

export function validCollisionPointer(doc) {
  return isPlainObject(doc) && sameKeys(doc, ['format', 'indexSha256'])
    && doc.format === 'UIE_COLLISION_POINTER1' && typeof doc.indexSha256 === 'string' && HEX64.test(doc.indexSha256);
}

export function createCollisionHandler(storePath) {
  return async (req, res, urlPath) => {
    if (!storePath || !urlPath.startsWith('/data/collision/')) return false;
    const fail = (status, message) => {
      res.writeHead(status, {'Cache-Control':'no-store','Content-Type':'text/plain; charset=utf-8'});
      res.end(req.method === 'HEAD' ? undefined : message);
      return true;
    };
    const send = (body, cache, type, gzip = false) => {
      const headers = {'Content-Length':body.length,'Cache-Control':cache,'Content-Type':type,'X-Content-Type-Options':'nosniff'};
      if (gzip) headers['Content-Encoding']='gzip';
      res.writeHead(200,headers);
      res.end(req.method === 'HEAD' ? undefined : body);
      return true;
    };
    if (!['GET','HEAD'].includes(req.method)) return fail(405,'Method not allowed');
    // Bytes of a regular file inside the store (symlinks resolved and contained), at most `limit` bytes.
    const readContained = async (relative, limit) => {
      const base = await realpath(storePath);
      const file = await realpath(path.join(base,...relative));
      if (!file.startsWith(base + path.sep)) return null;
      const info = await stat(file);
      if (!info.isFile() || info.size > limit) return null;
      const body = await readFile(file);
      return body.length > limit ? null : body;
    };
    const unavailable = (error) => fail(['ENOENT','ENOTDIR'].includes(error.code) ? 404 : 503,'Collision data unavailable');

    if (urlPath === '/data/collision/current.json') {
      let pointer;
      try {
        const body = await readContained(['current.json'],MAX_POINTER_BYTES);
        if (!body) return fail(503,'Collision pointer invalid');   // present but not a small regular file in the store
        try { pointer = JSON.parse(body.toString('utf8')); } catch { pointer = null; }
        if (!validCollisionPointer(pointer)) return fail(503,'Collision pointer invalid');
        // Never hand out a dangling pointer: the referenced index must be present (its content is checked on GET).
        if (!await readContained(['indexes',`${pointer.indexSha256}.json`],MAX_INDEX_BYTES)) return fail(503,'Collision index unavailable');
      } catch (error) {
        if (['ENOENT','ENOTDIR'].includes(error.code) && pointer) return fail(503,'Collision index unavailable');
        return unavailable(error);
      }
      const canonical = Buffer.from(JSON.stringify({format:pointer.format,indexSha256:pointer.indexSha256}));
      return send(canonical,'no-store','application/json; charset=utf-8');
    }

    const index = /^\/data\/collision\/indexes\/([a-f0-9]{64})\.json$/.exec(urlPath);
    if (index) {
      let body;
      try {
        body = await readContained(['indexes',`${index[1]}.json`],MAX_INDEX_BYTES);
        if (!body) return fail(404,'Not found');
      } catch (error) {
        return unavailable(error);
      }
      if (createHash('sha256').update(body).digest('hex') !== index[1]) return fail(409,'Content identity mismatch');
      let doc;
      try { doc = JSON.parse(body.toString('utf8')); } catch { doc = null; }
      if (!validCollisionIndex(doc)) return fail(409,'Invalid collision index');
      return send(body,IMMUTABLE,'application/json; charset=utf-8');
    }

    const match = /^\/data\/collision\/(pairs|objects)\/([a-f0-9]{64})\.(json|uib|uih)$/.exec(urlPath);
    if (!match || (match[1] === 'pairs') !== (match[3] === 'json')) return fail(404,'Not found');
    let body;
    try {
      body = await readContained([match[1],`${match[2]}.${match[3]}`],match[3] === 'json' ? 16384 : 64*1024*1024);
      if (!body) return fail(404,'Not found');
      // UIB identity deliberately hashes decompressed content, unlike existing UIH compressed identity.
      const canonical = match[3] === 'uib' ? gunzipSync(body,{maxOutputLength:5*1024*1024}) : body;
      if (createHash('sha256').update(canonical).digest('hex') !== match[2]) return fail(409,'Content identity mismatch');
    } catch (error) {
      return unavailable(error);
    }
    return send(body,IMMUTABLE,match[3] === 'json' ? 'application/json; charset=utf-8' : 'application/octet-stream',
      match[3] !== 'json');
  };
}
