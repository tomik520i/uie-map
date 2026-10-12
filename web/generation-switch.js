// Pure helpers for switching the browser between immutable published generations
// (publish_generation.py, UIE_REVISION1). No DOM, three.js or fetch: app.js owns loading and the scene;
// this module owns generation URLs, content-addressed names and verify-before-decode of response bytes.
//
// Chunk and block files of a generation are named by the SHA-256 of their bytes:
//   chunks/<x>_<z>-<sha256>.json   blocks/<x>_<z>-<sha256>.json
// A file name therefore identifies identical bytes in every generation. That is what lets the viewer keep
// an unchanged resident mesh across a switch (the chunk stream is keyed by file) and cache block data by
// file, provided every response is checked against the hash in its name before it is decoded.

export const GENERATIONS_URL = './data/generations/';
const HEX64 = /^[0-9a-f]{64}$/;
const INT = '(0|-?[1-9][0-9]*)';  // Python str(int): no leading zeros, no "-0"
const CONTENT_FILE = new RegExp(`^(chunks|blocks)/${INT}_${INT}-([0-9a-f]{64})\\.json$`);

export class GenerationError extends Error {}

/** Immutable base URL of one generation, e.g. ./data/generations/<64 hex>/ */
export function generationBase(id) {
  if (typeof id !== 'string' || !HEX64.test(id)) throw new GenerationError('neplatné ID generace');
  return `${GENERATIONS_URL}${id}/`;
}

/**
 * SHA-256 named by a content-addressed generation file. When kind ('chunks' or 'blocks') and chunk
 * coordinates are given, the name must agree with them.
 */
export function contentHash(file, kind = null, x = null, z = null) {
  const match = typeof file === 'string' ? CONTENT_FILE.exec(file) : null;
  if (!match) throw new GenerationError(`${String(file)}: není obsahově adresovaný soubor generace`);
  const [, dir, rawX, rawZ, sha] = match;
  const fileX = Number(rawX);
  const fileZ = Number(rawZ);
  if (!Number.isSafeInteger(fileX) || !Number.isSafeInteger(fileZ)) {
    throw new GenerationError(`${file}: souřadnice nejsou bezpečná celá čísla`);
  }
  if ((kind !== null && dir !== kind) || (x !== null && fileX !== x) || (z !== null && fileZ !== z)) {
    throw new GenerationError(`${file}: neodpovídá ${kind ?? dir} ${x ?? fileX},${z ?? fileZ}`);
  }
  return sha;
}

/**
 * Requires content-addressed names for every chunk and block file of an already validated generation
 * manifest. Returns the block lookup index ("x,z" -> file), or null when the generation has no block data.
 */
export function validateGenerationFiles(manifest) {
  for (const chunk of manifest.chunks) contentHash(chunk.file, 'chunks', chunk.x, chunk.z);
  const info = manifest.blockInfo;
  if (!info || !info.chunks.length) return { blockIndex: null };
  const blockIndex = new Map();
  for (const chunk of info.chunks) {
    contentHash(chunk.file, 'blocks', chunk.x, chunk.z);
    blockIndex.set(`${chunk.x},${chunk.z}`, chunk.file);
  }
  return { blockIndex };
}

/**
 * Verify-before-decode: the exact response bytes must hash to expectedSha (lowercase hex) before they are
 * decoded as UTF-8 and parsed. hash(Uint8Array) -> lowercase hex is supplied by the caller.
 */
export function decodeVerifiedJson(bytes, expectedSha, hash, where) {
  if (!(bytes instanceof Uint8Array)) throw new GenerationError(`${where}: odpověď nejsou bajty`);
  if (typeof expectedSha !== 'string' || !HEX64.test(expectedSha)) {
    throw new GenerationError(`${where}: očekávaný SHA-256 není 64 malých hex znaků`);
  }
  const actual = hash(bytes);
  if (actual !== expectedSha) {
    throw new GenerationError(`${where}: SHA-256 odpovědi neodpovídá (${String(actual).slice(0, 12)}… ≠ ${expectedSha.slice(0, 12)}…)`);
  }
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    throw new GenerationError(`${where}: odpověď není platné UTF-8`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new GenerationError(`${where}: odpověď není platný JSON`);
  }
}

/**
 * Counts for a switch of the desired chunk window: resident files kept (same content), resident files
 * removed (changed or out of the new window), and new files still to load.
 */
export function diffEntries(residentFiles, nextEntries) {
  const resident = new Set(residentFiles);
  const next = new Set(nextEntries.map((entry) => entry.file));
  let retained = 0;
  for (const file of resident) if (next.has(file)) retained += 1;
  return { retained, removed: resident.size - retained, added: next.size - retained };
}
