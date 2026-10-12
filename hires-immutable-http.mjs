// Read-only endpoints of the immutable hires store (hires_bundle.py), wired into server.mjs.
//   /data/hires/current.json              no-store UIE_HIRES_POINTER1 (<= 4 KiB, canonical bytes), index must exist
//   /data/hires/indexes/<64hex>.json      immutable UIE_HIRES_IMMUTABLE_INDEX1: SHA-256 of the exact bytes first,
//                                         then the bounded shape and the publisher's canonical form
//   /data/hires/objects/<64hex>.uih       immutable gzip(UIH1): SHA-256 of the exact stored bytes; sent unchanged with
//                                         Content-Encoding: gzip like the legacy tiles, so the browser inflates once
//                                         and decodeHiresTile receives the raw UIH1 (never gunzip it again)
//   /data/hires/atlases/<64hex>.png       immutable PNG: SHA-256 of the exact bytes
// Every other path (legacy /data/hires/index.json, tiles/..., anything else) returns false for the caller.
// Pass the URL pathname (no query). Nothing is written, no directory is listed.
import path from 'node:path';
import { constants } from 'node:fs';
import { open, realpath, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { gzipSync } from 'node:zlib';
import { validHiresSource } from './web/hires-source.mjs';

// Same limits as hires_bundle.py.
export const MAX_OBJECT_BYTES = 64 * 1024 * 1024;
export const MAX_INDEX_BYTES = 16 * 1024 * 1024;
export const MAX_INDEX_ENTRIES = 100_000;
export const MAX_POINTER_BYTES = 4096;
export const OWNED_MARKER = '.uie-hires-bundle-owned';
const MAX_TILE_COORD = Math.floor(30_000_000 / 64);
const HEX64 = /^[0-9a-f]{64}$/;
const TILE_KEY = /^(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})$/;
const ROUTE = /^\/data\/hires\/(indexes|objects|atlases|layouts)\/(.*)$/;
const ADDRESS = { indexes: /^([0-9a-f]{64})\.json$/, objects: /^([0-9a-f]{64})\.uih$/, atlases: /^([0-9a-f]{64})\.png$/, layouts: /^([0-9a-f]{64})\.json$/ };
const IMMUTABLE = 'public, max-age=31536000, immutable';
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const JSON_TYPE = 'application/json; charset=utf-8';

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const sameKeys = (o, keys) => Object.keys(o).length === keys.length && keys.every((k) => Object.hasOwn(o, k));
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

// Kept here for Node18 releases without package.json type=module; browser equivalent
// lives in web/hires-context.js, checked against the same invalid-context fixtures.
function validHiresContext(value) {
  if(!isPlainObject(value)||!sameKeys(value,['world','dimension','renderer','caves','layoutSha256']))return false;
  for(const key of ['world','dimension','renderer']){
    const text=value[key];
    if(typeof text!=='string'||text.length<1||text.length>128||text.trim()!==text||/[^\x20-\x7e]/.test(text))return false;
  }
  return typeof value.caves==='boolean'&&typeof value.layoutSha256==='string'&&HEX64.test(value.layoutSha256);
}

// json.dumps(doc, sort_keys=True, separators=(',', ':')) of the publisher, for the ASCII documents it writes.
export function canonicalJson(value) {
  if (isPlainObject(value)) {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function validHiresIndex(doc) {
  if (!isPlainObject(doc)) return false;
  const v3=doc.format==='UIE_HIRES_IMMUTABLE_INDEX3';
  const v2=v3||doc.format==='UIE_HIRES_IMMUTABLE_INDEX2';
  if (!sameKeys(doc, ['atlasSha256', 'format', 'rendererIdentity', 'tileSize', 'tiles', ...(v2?['context']:[]),...(v3?['source']:[])])) return false;
  if ((v2 ? !validHiresContext(doc.context) : doc.format !== 'UIE_HIRES_IMMUTABLE_INDEX1') || doc.tileSize !== 64 || !isPlainObject(doc.tiles)) return false;
  if (typeof doc.atlasSha256 !== 'string' || !HEX64.test(doc.atlasSha256)) return false;
  if (typeof doc.rendererIdentity !== 'string' || !HEX64.test(doc.rendererIdentity)) return false;
  const entries = Object.entries(doc.tiles);
  if(v3&&!validHiresSource(doc.source,doc.tiles))return false;
  if (entries.length > MAX_INDEX_ENTRIES) return false;
  return entries.every(([key, sha]) => {
    const m = TILE_KEY.exec(key);
    return m && Math.abs(Number(m[1])) <= MAX_TILE_COORD && Math.abs(Number(m[2])) <= MAX_TILE_COORD
      && typeof sha === 'string' && HEX64.test(sha);
  });
}

export function validHiresPointer(doc) {
  return isPlainObject(doc) && sameKeys(doc, ['format', 'indexSha256'])
    && doc.format === 'UIE_HIRES_POINTER1' && typeof doc.indexSha256 === 'string' && HEX64.test(doc.indexSha256);
}

// Absent header: any coding is acceptable (RFC 9110); otherwise gzip (or *) must have q > 0.
export function acceptsGzip(header) {
  if (header === undefined) return true;
  const codings = new Map();
  for (const item of String(header).split(',')) {
    const [rawName, ...params] = item.trim().toLowerCase().split(';');
    let quality = 1;
    for (const param of params) if (param.trim().startsWith('q=')) {
      const raw = param.trim().slice(2);
      quality = /^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$/.test(raw) ? Number(raw) : 0;
    }
    if (rawName.trim()) codings.set(rawName.trim(), quality);
  }
  return (codings.get('gzip') ?? codings.get('*') ?? 0) > 0;
}

const missing = (error) => ['ENOENT', 'ENOTDIR', 'ELOOP'].includes(error?.code);

export function createImmutableHiresHandler(storePath, { maxConcurrent = 4 } = {}) {
  if (!Number.isInteger(maxConcurrent) || maxConcurrent < 1 || maxConcurrent > 4) {
    throw new TypeError('maxConcurrent must be an integer in 1..4');
  }
  if (storePath === null || storePath === undefined) return async () => false;
  if (typeof storePath !== 'string' || !path.isAbsolute(storePath) || path.resolve(storePath) !== storePath) {
    throw new TypeError('hires store must be an explicitly configured absolute normalized path');
  }

  // Real path of `<store>/<folder>/<name>` when it is exactly that entry (no symlinked folder or file, even one
  // pointing inside the store) and the store carries the owner marker; null for a foreign or misconfigured store.
  async function entry(folder, name) {
    let base;
    try {
      base = await realpath(storePath);
      const marker = path.join(base, OWNED_MARKER);
      if (await realpath(marker) !== marker || !(await stat(marker)).isFile()) return null;
    } catch (error) {
      if (missing(error)) return null;
      throw error;
    }
    const expected = folder ? path.join(base, folder, name) : path.join(base, name);
    return await realpath(expected) === expected ? expected : undefined;
  }

  // Exact bytes of a contained regular file of 1..limit bytes; undefined when not acceptable (refused unread when
  // oversized), null for a foreign store. The size is checked on the open handle and the read must match it.
  async function readEntry(folder, name, limit) {
    const file = await entry(folder, name);
    if (!file) return file;
    const handle = await open(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    try {
      const info = await handle.stat();
      if (!info.isFile() || info.size < 1 || info.size > limit) return undefined;
      const buffer = Buffer.allocUnsafe(info.size + 1);
      let total = 0;
      while (total < buffer.length) {
        const { bytesRead } = await handle.read(buffer, total, buffer.length - total, total);
        if (!bytesRead) break;
        total += bytesRead;
      }
      return total === info.size ? buffer.subarray(0, total) : undefined;   // changed while reading
    } finally {
      await handle.close();
    }
  }

  const handle = async (req, res, urlPath) => {
    if (typeof urlPath !== 'string') return false;
    const route = ROUTE.exec(urlPath);
    if (urlPath !== '/data/hires/current.json' && !route) return false;
    const head = req.method === 'HEAD';
    const fail = (status, message, extra = {}) => {
      res.writeHead(status, { 'Cache-Control': 'no-store', 'Content-Type': 'text/plain; charset=utf-8',
        'X-Content-Type-Options': 'nosniff', ...extra });
      res.end(head ? undefined : message);
      return true;
    };
    const send = (body, cache, type, extra = {}) => {
      res.writeHead(200, { 'Content-Length': body.length, 'Cache-Control': cache, 'Content-Type': type,
        'X-Content-Type-Options': 'nosniff', ...extra });
      res.end(head ? undefined : body);
      return true;
    };
    if (req.method !== 'GET' && !head) return fail(405, 'Method not allowed', { Allow: 'GET, HEAD' });

    if (!route) {                                   // /data/hires/current.json
      let body, pointer;
      try {
        body = await readEntry(null, 'current.json', MAX_POINTER_BYTES);
        if (body === null) return fail(503, 'Hires store unavailable');
        if (body === undefined) return fail(503, 'Hires pointer invalid');
        try { pointer = JSON.parse(body.toString('utf8')); } catch { pointer = null; }
        if (!validHiresPointer(pointer) || canonicalJson(pointer) !== body.toString('utf8')) {
          return fail(503, 'Hires pointer invalid');
        }
        // Never hand out a dangling pointer: the index must be a contained regular file within the limit
        // (its bytes are verified on its own GET, not on every pointer poll).
        const index = await entry('indexes', `${pointer.indexSha256}.json`);
        const info = index ? await stat(index) : null;
        if (!info || !info.isFile() || info.size < 1 || info.size > MAX_INDEX_BYTES) return fail(503, 'Hires index unavailable');
      } catch (error) {
        if (missing(error)) return fail(pointer ? 503 : 404, pointer ? 'Hires index unavailable' : 'No hires pointer published');
        return fail(503, 'Hires data unavailable');
      }
      return send(body, 'no-store', JSON_TYPE);
    }

    const [, folder, name] = route;
    const address = ADDRESS[folder].exec(name);
    if (!address) return fail(400, 'Bad hires address');
    const id = address[1];
    const gzipObject = folder === 'objects';
    if (gzipObject && !acceptsGzip(req.headers?.['accept-encoding'])) {
      return fail(406, 'Hires tiles are only available gzip-encoded', { Vary: 'Accept-Encoding' });
    }
    let body;
    try {
      body = await readEntry(folder, name, ['indexes','layouts'].includes(folder) ? MAX_INDEX_BYTES : MAX_OBJECT_BYTES);
    } catch (error) {
      return missing(error) ? fail(404, 'Not found') : fail(503, 'Hires data unavailable');
    }
    if (body === null) return fail(503, 'Hires store unavailable');
    if (body === undefined) return fail(404, 'Not found');
    // The address is the SHA-256 of exactly these bytes: checked before anything is parsed or cached.
    if (sha256(body) !== id) return fail(409, 'Content identity mismatch');
    const etag = `"${id}"`;
    if(folder==='layouts') {
      let doc;
      try { doc=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(body)); } catch { doc=null; }
      if(!isPlainObject(doc) || doc.format!=='UIE_ATLAS_LAYOUT1' || !isPlainObject(doc.registered))return fail(409,'Invalid hires layout');
      return send(body,IMMUTABLE,JSON_TYPE,{ETag:etag});
    }
    if (folder === 'indexes') {
      let doc;
      try { doc = JSON.parse(body.toString('utf8')); } catch { doc = null; }
      if (!validHiresIndex(doc) || canonicalJson(doc) !== body.toString('utf8')) return fail(409, 'Invalid hires index');
      // Large indexes travel compressed when the client accepts it; the decoded entity is the exact hashed bytes.
      if (body.length > 4096 && acceptsGzip(req.headers?.['accept-encoding'])) {
        return send(gzipSync(body, { level: 6 }), IMMUTABLE, JSON_TYPE,
          { 'Content-Encoding': 'gzip', Vary: 'Accept-Encoding', ETag: `"${id}.gz"` });
      }
      return send(body, IMMUTABLE, JSON_TYPE, { Vary: 'Accept-Encoding', ETag: etag });
    }
    if (folder === 'atlases') {
      if (body.length < 24 || !body.subarray(0, 8).equals(PNG_SIGNATURE)) return fail(409, 'Invalid hires atlas');
      return send(body, IMMUTABLE, 'image/png', { ETag: etag });
    }
    if (body.length < 18 || body[0] !== 0x1f || body[1] !== 0x8b || body[2] !== 0x08) {
      return fail(409, 'Invalid hires tile');
    }
    return send(body, IMMUTABLE, 'application/octet-stream',
      { 'Content-Encoding': 'gzip', Vary: 'Accept-Encoding', ETag: etag });
  };
  // Hold the slot until the response has drained or closed, not merely until disk reading has finished.
  // This bounds the large verified buffers retained by slow clients as well as concurrent reads.
  let active = 0;
  return async (req, res, urlPath) => {
    if (typeof urlPath !== 'string' || (urlPath !== '/data/hires/current.json' && !ROUTE.test(urlPath))) return false;
    if (active >= maxConcurrent) {
      res.writeHead(503, { 'Cache-Control': 'no-store', 'Retry-After': '1', 'Content-Type': 'text/plain; charset=utf-8', 'X-Content-Type-Options': 'nosniff' });
      res.end(req.method === 'HEAD' ? undefined : 'Hires request capacity reached');
      return true;
    }
    active += 1;
    let released = false;
    let handled = false, drained = false;
    const release = () => {
      if (released || !handled || !drained) return;
      released = true; active -= 1;
      res.removeListener('finish', terminal); res.removeListener('close', terminal);
    };
    const terminal = () => { drained = true; release(); };
    res.once('finish', terminal); res.once('close', terminal);
    try { return await handle(req, res, urlPath); }
    catch (error) { drained = true; throw error; }
    finally { handled = true; release(); }
  };
}
