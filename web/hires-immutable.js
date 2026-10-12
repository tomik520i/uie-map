// PRIVATE, opt-in (?immutableHires=1) client of the immutable hires store (hires_bundle.py, hires-immutable-http.mjs).
// Pure: no DOM, no three.js; fetch and sha256Hex are injected (sha256.js works on plain LAN HTTP).
//
// loadImmutableHiresIndex({ fetch, baseUrl, sha256Hex, current }) -> Promise<index>
//   GET <base>current.json (no-store, <= 4 KiB, UIE_HIRES_POINTER1); the same indexSha256 as `current` returns
//   `current` unchanged; otherwise GET <base>indexes/<sha>.json (<= 16 MiB), SHA-256 of the exact bytes BEFORE any
//   JSON parsing, then the bundle shape. Every failure throws HiresImmutableError (no legacy fallback here).
// The adapted index keeps the legacy shape for the rest of the app:
//   { tileSize: 64, atlas: 'atlases/<full>.png', atlasSha256, tiles: Map(key -> sha12), objects: Map(key -> full),
//     immutable: true, indexSha256, rendererIdentity }
// so collision/geometry contracts that compare sha12 stay unchanged; tile requests pin the full object hash.
// Tiles arrive with Content-Encoding: gzip and are inflated by the browser; the full hash addresses the COMPRESSED
// bytes and is verified by the server, not here (the client never sees those bytes).

import { validHiresContext, sameHiresWorld } from './hires-context.js';
import { validHiresSource } from './hires-source.mjs';
export const MAX_POINTER_BYTES = 4096;
export const MAX_INDEX_BYTES = 16 * 1024 * 1024;
export const MAX_INDEX_ENTRIES = 100_000;
const MAX_TILE_COORD = Math.floor(30_000_000 / 64);
const HEX64 = /^[0-9a-f]{64}$/;
const SHA12 = /^[0-9a-f]{12}$/;
const TILE_KEY = /^(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})$/;

export class HiresImmutableError extends Error {
  constructor(message, status = null) { super(message); this.name = 'HiresImmutableError'; this.status = status; }
}

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const sameKeys = (o, keys) => Object.keys(o).length === keys.length && keys.every((k) => Object.hasOwn(o, k));

function decodeJson(bytes, what) {
  let text;
  try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
  catch { throw new HiresImmutableError(`${what}: neplatné UTF-8`); }
  try { return JSON.parse(text); }
  catch { throw new HiresImmutableError(`${what}: odpověď není platný JSON`); }
}

/** Body of a fetch Response, at most `limit` bytes (refused as soon as it is exceeded; the caller's abort applies). */
export async function readBounded(response, limit, what) {
  const declared = Number(response.headers?.get?.('content-length'));
  if (Number.isFinite(declared) && declared > limit) {
    response.body?.cancel?.().catch?.(() => {});
    throw new HiresImmutableError(`${what}: odpověď je větší než ${limit} B`);
  }
  if (!response.body?.getReader) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > limit) throw new HiresImmutableError(`${what}: odpověď je větší než ${limit} B`);
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      reader.cancel().catch(() => {});
      throw new HiresImmutableError(`${what}: odpověď je větší než ${limit} B`);
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { out.set(chunk, offset); offset += chunk.byteLength; }
  return out;
}

export async function loadImmutableLayout({fetch,baseUrl,index,sha256Hex}) {
  if(!validHiresContext(index?.context))throw new HiresImmutableError('chybí ověřený kontext atlasu');
  const expected=index.context.layoutSha256;
  const response=await fetch(`${baseUrl}layouts/${expected}.json`);
  if(!response.ok){response.body?.cancel?.().catch?.(()=>{});throw new HiresImmutableError(`layout atlasu: HTTP ${response.status}`);}
  const bytes=await readBounded(response,MAX_INDEX_BYTES,'layout atlasu');
  if(sha256Hex(bytes)!==expected)throw new HiresImmutableError('layout atlasu neodpovídá SHA-256');
  const doc=decodeJson(bytes,'layout atlasu');
  if(!isPlainObject(doc)||doc.format!=='UIE_ATLAS_LAYOUT1'||!isPlainObject(doc.registered)) {
    throw new HiresImmutableError('neplatný layout atlasu');
  }
  return doc;
}

/** indexSha256 of a UIE_HIRES_POINTER1 document (exact keys). */
export function parseImmutablePointer(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.byteLength > MAX_POINTER_BYTES) {
    throw new HiresImmutableError('ukazatel dlaždic je příliš velký');
  }
  const doc = decodeJson(bytes, 'ukazatel dlaždic');
  if (!isPlainObject(doc) || !sameKeys(doc, ['format', 'indexSha256']) || doc.format !== 'UIE_HIRES_POINTER1'
    || typeof doc.indexSha256 !== 'string' || !HEX64.test(doc.indexSha256)) {
    throw new HiresImmutableError('neplatný ukazatel dlaždic');
  }
  return doc.indexSha256;
}

/** Adapted index from the exact bytes of indexes/<expectedSha>.json; the hash is checked before parsing. */
export function parseImmutableIndex(bytes, expectedSha, sha256Hex) {
  if (typeof expectedSha !== 'string' || !HEX64.test(expectedSha)) throw new HiresImmutableError('neplatná adresa indexu');
  if (!(bytes instanceof Uint8Array) || bytes.byteLength > MAX_INDEX_BYTES) {
    throw new HiresImmutableError('index dlaždic je příliš velký');
  }
  if (sha256Hex(bytes) !== expectedSha) throw new HiresImmutableError('index dlaždic neodpovídá své adrese SHA-256');
  const doc = decodeJson(bytes, 'index dlaždic');
  const v3=doc?.format==='UIE_HIRES_IMMUTABLE_INDEX3';
  const v2=v3||doc?.format==='UIE_HIRES_IMMUTABLE_INDEX2';
  if (!isPlainObject(doc) || !sameKeys(doc, ['atlasSha256', 'format', 'rendererIdentity', 'tileSize', 'tiles', ...(v2?['context']:[]),...(v3?['source']:[])])
    || (v2 ? !validHiresContext(doc.context) : doc.format !== 'UIE_HIRES_IMMUTABLE_INDEX1') || doc.tileSize !== 64 || !isPlainObject(doc.tiles)
    || typeof doc.atlasSha256 !== 'string' || !HEX64.test(doc.atlasSha256)
    || typeof doc.rendererIdentity !== 'string' || !HEX64.test(doc.rendererIdentity)) {
    throw new HiresImmutableError('neznámý formát neměnného indexu dlaždic');
  }
  const entries = Object.entries(doc.tiles);
  if(v3&&!validHiresSource(doc.source,doc.tiles))throw new HiresImmutableError('neplatná metadata zobrazené mapy');
  if (entries.length > MAX_INDEX_ENTRIES) throw new HiresImmutableError('index má příliš mnoho dlaždic');
  const tiles = new Map();
  const objects = new Map();
  for (const [key, sha] of entries) {
    const m = TILE_KEY.exec(key);
    if (!m || Math.abs(Number(m[1])) > MAX_TILE_COORD || Math.abs(Number(m[2])) > MAX_TILE_COORD
      || typeof sha !== 'string' || !HEX64.test(sha)) {
      throw new HiresImmutableError(`neplatná dlaždice ${JSON.stringify(key).slice(0, 40)}`);
    }
    tiles.set(key, sha.slice(0, 12));
    objects.set(key, sha);
  }
  return { tileSize: 64, atlas: `atlases/${doc.atlasSha256}.png`, atlasSha256: doc.atlasSha256, tiles, objects,
    immutable: true, indexSha256: expectedSha, rendererIdentity: doc.rendererIdentity,
    ...(v2 ? {context:doc.context,layout:`layouts/${doc.context.layoutSha256}.json`} : {}),
    ...(v3 ? {source:doc.source,stale:new Set(doc.source.staleTiles)} : {}) };
}

/** Full object hash of `key` in `index`, only while it is the revision `sha12` was requested for. */
export function pinImmutableObject(index, key, sha12) {
  const full = index?.immutable ? index.objects?.get(key) : undefined;
  if (typeof sha12 !== 'string' || !SHA12.test(sha12) || typeof full !== 'string' || !HEX64.test(full)
    || !full.startsWith(sha12)) {
    throw new HiresImmutableError(`dlaždice ${key} už v aktuálním indexu nemá revizi ${sha12}`);
  }
  return full;
}

export function immutableObjectUrl(baseUrl, full) {
  if (typeof full !== 'string' || !HEX64.test(full)) throw new HiresImmutableError('neplatná adresa dlaždice');
  return `${baseUrl}objects/${full}.uih`;
}

export async function loadImmutableHiresIndex({ fetch, baseUrl, sha256Hex, current = null, expectedWorld = null }) {
  const checkWorld=index=>{
    if(expectedWorld && index.context && (index.context.world!==expectedWorld.world || index.context.dimension!==expectedWorld.dimension)) {
      throw new HiresImmutableError('dlaždice nepatří světu nebo dimenzi otevřené mapy');
    }
    return index;
  };
  const pointerRes = await fetch(`${baseUrl}current.json`, { cache: 'no-store' });
  if (!pointerRes.ok) {
    pointerRes.body?.cancel?.().catch?.(() => {});
    throw new HiresImmutableError(pointerRes.status === 404 ? 'neměnné dlaždice zatím nejsou publikované'
      : `ukazatel dlaždic: HTTP ${pointerRes.status}`, pointerRes.status);
  }
  const indexSha = parseImmutablePointer(await readBounded(pointerRes, MAX_POINTER_BYTES, 'ukazatel dlaždic'));
  if (current?.immutable && current.indexSha256 === indexSha) return checkWorld(current);
  const indexRes = await fetch(`${baseUrl}indexes/${indexSha}.json`);
  if (!indexRes.ok) {
    indexRes.body?.cancel?.().catch?.(() => {});
    throw new HiresImmutableError(`index dlaždic: HTTP ${indexRes.status}`, indexRes.status);
  }
  const next=parseImmutableIndex(await readBounded(indexRes, MAX_INDEX_BYTES, 'index dlaždic'), indexSha, sha256Hex);
  if(current?.source && !next.source)throw new HiresImmutableError('aktualizace ztratila metadata zobrazené mapy');
  if(current?.context && (!next.context || !sameHiresWorld(current.context,next.context))) {
    throw new HiresImmutableError('aktualizace dlaždic patří jinému světu nebo dimenzi');
  }
  return checkWorld(next);
}
