import { buildHiresWalk, hiresWalkCell } from './hires-walk.js';
// UIE mapa – static 3D viewer for exported terrain chunks (data contract format 1).
import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { useCentroidAtlasSampling } from './atlas-sampling.js';
import { LruCache, blockAt, chunkCoord, decodeBlockChunk, findBlocks, pickCell } from './block-info.js';
import { clipTop, cutRange, describeCut, firstVisibleHit, parseCutParam } from './cave-cut.js';
import { createCaveCoverage } from './cave-coverage.js';
import { createWorldCaveOverlay } from './world-cave-overlay.js';
import { selectOverviewTiles, tileRect, validateOverviewIndex, wholeWorldDistance } from './overview-select.js';
import { ChunkStream, selectChunks } from './chunk-stream.js';
import { createKeyboardPan } from './map-navigation.js';
import { createGotoTerrain } from './goto-terrain.js';
import { createRenderStatusPoller, describeRenderStatus, fetchRenderStatus, formatDuration } from './render-status.js';
import { createHiresStatusPoller, describeHiresStatus } from './hires-status.js';
import { describeHiresPublication } from './hires-publication.js';
import { describePublisherStatus } from './hires-publisher-status.js';
import { describeCaveStatus } from './cave-status.js';
import { modelStatusPanel } from './model-status.js';
import { collisionStatusPanel } from './collision-status.js';
import { validateOverviewStatus, describeOverviewStatus } from './overview-status.js';
import { createFlyMotion } from './first-person-motion.js';
import { BODY, SPRINT_FACTOR, WALK_SPEED, blockShape, createWalker } from './walk-physics.js';
import { blockShapeAt } from './walk-neighbor-shapes.js';
import { capArrays, capClasses, capQuads } from './cut-cap.js';
import { markerLabel, parsePlayers, playersInDimension, staleness } from './players.js';
import { decodeHiresTileCompact, parseHiresIndex, spriteIndex, surfaceHeights, tileChunks, tilesAround } from './hires.js';
import { createHiresRequests } from './hires-requests.js';
import { createHiresAtlas } from './hires-atlas.js';
import { immutableObjectUrl, loadImmutableHiresIndex, loadImmutableLayout, pinImmutableObject } from './hires-immutable.js';
import { createCollisionSource } from './collision-source.js';
import { createCollisionWorld } from './collision-world.js';
import { createCollisionFollower } from './collision-follow.js';
import { chooseCollision } from './collision-choice.js';
import { renderWorldDiagnostics } from './world-diagnostics.js';
import { createFirstPersonView } from './first-person-view.js';
import { createRevisionWatcher, validateRevision } from './revision-watch.js';
import { contentHash, decodeVerifiedJson, diffEntries, generationBase, validateGenerationFiles } from './generation-switch.js';
import { sha256Hex } from './sha256.js';
import { LodSwap } from './lod-swap.js';
import { selectDistanceLod } from './lod-distance.js';
import { loadMapView } from './map-view.mjs';
import { mountDimensionMenu } from './dimension-menu.mjs';
import { meshBytesFromCounts, validateLodChunk, validateLodManifest } from './lod-contract.js';

const DATA_URL = './data/';
// Optional immutable generation store (server UIE_GENERATION_STORE). Without a valid revision the legacy
// ./data/manifest.json export is shown and nothing is refreshed.
const REVISION_URL = `${DATA_URL}revision.json`;
const RENDER_STATUS_URL = `${DATA_URL}render-status.json`;
const RENDER_STATUS_TIMEOUT_MS = 10000;
// Whole-world surface build (world_hires.py), independent of the spawn detail render status above.
const HIRES_STATUS_URL = `${DATA_URL}hires/status.json`;
const MAX_PARALLEL = 4;
const MAX_RESIDENT_CHUNKS = 128;
// Attribute + index buffers only, including their CPU and GPU copies. The atlas,
// browser overhead and up to four transient decoded JSON responses are separate.
const GEOMETRY_BUDGET = 500 * 1024 * 1024;
const MAX_LISTED_ERRORS = 8;
const MAX_LISTED_NAMES = 80;
const TIMEOUT_MS = { manifest: 15000, atlas: 60000, chunk: 30000, blocks: 15000, revision: 10000 };
const BLOCK_CACHE_CHUNKS = 24;   // ~190 KB per decoded chunk; walking keeps the chunks around the player here
// A pointer gesture counts as a click only if it stays short and nearly still.
const PICK_SLOP_PX = 5;
const PICK_MAX_MS = 500;
const BACKGROUND = 0x0f151c;
// Fly mode loads around the camera itself; "auto" means this radius (9 x 9 chunks), not the orbit zoom rule.
const FLY_AUTO_RADIUS = 4;
const ORBIT_ONLY_CONTROLS = ['goto-x', 'goto-z', 'goto-submit', 'reset', 'zoom-in', 'zoom-out'];
// three.js physical light units (r155+); starting values, tune against the real atlas.
const LIGHT = { hemisphere: 1.7, ambient: 0.6, sun: 1.0 };
// Experimental opt-in LOD (?lod=1, generation mode with a valid manifest.lod): detail within these horizontal
// block distances of the focus (hysteresis), coarse elsewhere in the same loaded window.
const LOD_ENTER_RADIUS = 32;
const LOD_EXIT_RADIUS = 48;

const $ = (id) => document.getElementById(id);
const fmtNumber = new Intl.NumberFormat('cs-CZ');
const fmtDate = new Intl.DateTimeFormat('cs-CZ', { dateStyle: 'long', timeStyle: 'short' });
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const safePath = (s) => typeof s === 'string' && /^[\w.\-/]+$/.test(s) && !s.startsWith('/')
  && !s.split('/').includes('..');

class DataError extends Error {
  constructor(lines) {
    const list = [].concat(lines);
    super(list.join(' '));
    this.lines = list;
  }
}

const state = {
  bounds: null, materials: null, raycastTargets: [], pickTargets: [], triangles: 0, dirty: true, userMoved: false,
  blockIndex: null, pickSeq: 0, findSeq: 0, unsupportedNames: null, walker: null, flyMotion: null, stream: null, chunkEntries: [], streamTimer: null, fly: null,
  // Generation mode only: { id, base } of the committed generation; null for the legacy export.
  generation: null, generationBoot: false, bakedLighting: false, pickingWired: false, refreshNote: null,
  watcher: null, lastSwitch: null,
  // Opt-in LOD: requested by ?lod=1; lodNote is a persistent fallback warning; the untextured material is
  // separate from state.materials so a generation atlas swap never touches it.
  lodOptIn: false, lodNote: null, untexturedMaterial: null,
  // Height cut (cave view): block level or null; cutRequest is ?cut= until the first bounds are known.
  cut: null, cutRequest: null, cutWired: false,
};
// Keeps y <= constant: normal (0,-1,0). Shared by every material while the cut is on.
const cutPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0);
// Keyed by block file: generation block files are content-addressed, so an entry never goes stale.
const blockCache = new LruCache(BLOCK_CACHE_CHUNKS);
const blockLoads = new Map();
let renderer, scene, camera, controls, fog;

// ---------------------------------------------------------------- status UI

function setStatus(kind, text, detail = null) {
  $('status').dataset.state = kind;
  $('status-text').textContent = text;
  const box = $('status-detail');
  box.replaceChildren(...[].concat(detail ?? []).map((line) => {
    const p = document.createElement('p');
    p.textContent = line;
    return p;
  }));
  box.hidden = !detail;
}

function setProgress(done, total) {
  const active = total > 0 && done < total;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  $('progress').hidden = !active;
  $('progress').setAttribute('aria-valuenow', String(pct));
  $('progress-bar').style.width = `${pct}%`;
}

function setChunkCounts(loaded, failed, total) {
  $('chunks').textContent = `${fmtNumber.format(loaded)} / ${fmtNumber.format(total)}`
    + (failed ? ` · ${fmtNumber.format(failed)} chyb` : '');
  $('chunks').classList.toggle('warn', failed > 0);
  $('triangles').textContent = fmtNumber.format(state.triangles);
}

// ---------------------------------------------------------------- data loading and validation

// The timer covers the headers and the whole response body; there are no retries.
async function fetchWithTimeout(url, timeoutMs, readBody, signal) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) controller.abort();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: 'no-cache', signal: controller.signal });
    if (!response.ok) {
      response.body?.cancel().catch(() => {});
      const error = new DataError(`${url}: HTTP ${response.status} ${response.statusText}`.trim());
      error.status = response.status;
      throw error;
    }
    return await readBody(response);
  } catch (err) {
    if (signal?.aborted) throw signal.reason ?? err;
    if (err instanceof DataError) throw err;
    if (controller.signal.aborted) {
      throw new DataError(`${url}: vypršel časový limit ${timeoutMs / 1000} s, server neodpověděl včas`);
    }
    throw new DataError(`${url}: síťová chyba (${err.message})`);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

function fetchJson(url, timeoutMs, signal) {
  return fetchWithTimeout(url, timeoutMs, async (response) => {
    const text = await response.text();
    try {
      return JSON.parse(text);
    } catch {
      throw new DataError(`${url}: odpověď není platný JSON`);
    }
  }, signal);
}

function fetchBytes(url, timeoutMs, signal) {
  return fetchWithTimeout(url, timeoutMs, async (response) => new Uint8Array(await response.arrayBuffer()), signal);
}

// Generation mode: exact response bytes are hashed before any decoding (sha256.js works without crypto.subtle).
async function fetchVerifiedJson(url, expectedSha, timeoutMs, signal) {
  const bytes = await fetchBytes(url, timeoutMs, signal);
  try {
    return decodeVerifiedJson(bytes, expectedSha, sha256Hex, url);
  } catch (err) {
    throw new DataError(err.message);
  }
}

function validateManifest(m) {
  if (!m || typeof m !== 'object' || Array.isArray(m)) throw new DataError('manifest.json: kořen není objekt');
  const errors = [];
  const need = (ok, msg) => { if (!ok) errors.push(msg); };
  const vec3 = (v) => Array.isArray(v) && v.length === 3 && v.every(isNum);
  need(m.format === 1, `nepodporovaný format ${JSON.stringify(m.format)} (očekáván 1)`);
  need(typeof m.title === 'string', 'title není text');
  need(typeof m.generatedAt === 'string' && !Number.isNaN(Date.parse(m.generatedAt)), 'generatedAt není datum ISO 8601');
  need(typeof m.sample === 'boolean', 'sample není boolean');
  need(m.bounds && vec3(m.bounds.min) && vec3(m.bounds.max)
    && m.bounds.min.every((v, i) => v <= m.bounds.max[i]), 'bounds.min/max nejsou platné [x, y, z]');
  need(m.focus === undefined || vec3(m.focus), 'focus není platné [x, y, z]');
  need(safePath(m.atlas), 'atlas není bezpečná relativní cesta');
  need(m.atlasSha256 === undefined || typeof m.atlasSha256 === 'string' && /^[0-9a-f]{64}$/.test(m.atlasSha256),
    'atlasSha256 není platný SHA-256');
  need(m.bakedLighting === undefined || typeof m.bakedLighting === 'boolean', 'bakedLighting není boolean');
  if (!Array.isArray(m.chunks)) {
    errors.push('chunks není pole');
  } else {
    const files = new Set();
    const coords = new Set();
    const bad = m.chunks.findIndex((c) => {
      if (!c || !safePath(c.file) || !Number.isSafeInteger(c.x) || !Number.isSafeInteger(c.z)) return true;
      const key = `${c.x},${c.z}`;
      if (files.has(c.file) || coords.has(key)) return true;
      files.add(c.file);
      coords.add(key);
      return false;
    });
    need(bad < 0, `chunks[${bad}] nemá platné file/x/z`);
    need(m.chunks.length > 0, 'manifest neobsahuje žádné chunky');
  }
  const cov = m.coverage;
  const count = (v) => Number.isSafeInteger(v) && v >= 0;
  need(cov && count(cov.renderedBlocks) && count(cov.unsupportedBlocks) && Array.isArray(cov.unsupportedNames)
    && cov.unsupportedNames.every((n) => typeof n === 'string'), 'coverage nemá očekávaný tvar');
  need(!cov || cov.emptyBlocks === undefined || count(cov.emptyBlocks), 'coverage.emptyBlocks není nezáporné celé číslo');
  const info = m.blockInfo;
  if (info !== undefined) {
    const shaped = Boolean(info) && typeof info === 'object' && !Array.isArray(info) && info.format === 'UIE_BLOCKS1'
      && Array.isArray(info.chunks);
    need(shaped, 'blockInfo nemá format UIE_BLOCKS1 se seznamem chunks');
    if (shaped) {
      const seen = new Set();
      const bad = info.chunks.findIndex((c) => {
        if (!c || !safePath(c.file) || !Number.isInteger(c.x) || !Number.isInteger(c.z)) return true;
        const key = `${c.x},${c.z}`;
        if (seen.has(key)) return true;
        seen.add(key);
        return false;
      });
      need(bad < 0, `blockInfo.chunks[${bad}] nemá platné file/x/z nebo je duplicitní`);
    }
  }
  if (errors.length) throw new DataError(['manifest.json neodpovídá datovému kontraktu:', ...errors]);
  return m;
}

function validatePart(p, where) {
  if (!p || typeof p !== 'object' || Array.isArray(p)) throw new DataError(`${where}: není objekt`);
  const { positions, uvs, colors, indices } = p;
  if (![positions, uvs, colors, indices].every(Array.isArray)) {
    throw new DataError(`${where}: chybí pole positions/uvs/colors/indices`);
  }
  const vertices = positions.length / 3;
  if (!Number.isInteger(vertices) || uvs.length !== vertices * 2 || colors.length !== vertices * 3
    || indices.length % 3 !== 0) {
    throw new DataError(`${where}: délky polí si neodpovídají (${vertices} vrcholů)`);
  }
  if (p.transparent !== undefined && typeof p.transparent !== 'boolean') {
    throw new DataError(`${where}: transparent není boolean`);
  }
  for (const [name, arr] of [['positions', positions], ['uvs', uvs], ['colors', colors]]) {
    if (!arr.every(isNum)) throw new DataError(`${where}: ${name} obsahuje nečíselnou hodnotu`);
  }
  if (!indices.every((i) => Number.isInteger(i) && i >= 0 && i < vertices)) {
    throw new DataError(`${where}: index mimo rozsah vrcholů`);
  }
}

function validateChunk(chunk, file) {
  validatePart(chunk, file);
  const parts = chunk.transparentParts;
  if (parts === undefined) return chunk;
  if (!Array.isArray(parts)) throw new DataError(`${file}: transparentParts není pole`);
  parts.forEach((part, i) => {
    validatePart(part, `${file} transparentParts[${i}]`);
    if (part.transparentParts !== undefined) throw new DataError(`${file}: vnořené transparentParts nejsou povoleny`);
  });
  return chunk;
}

async function loadAtlas(url, signal, expectedSha = undefined, hashUnverified = false) {
  const blob = await fetchWithTimeout(url, TIMEOUT_MS.atlas, (response) => response.blob(), signal);
  let actualSha = expectedSha;
  if (expectedSha !== undefined || hashUnverified) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    actualSha = sha256Hex(bytes);
    if (expectedSha !== undefined && (!/^[0-9a-f]{64}$/.test(expectedSha) || actualSha !== expectedSha)) {
      throw new DataError(`${url}: SHA-256 atlasu neodpovídá publikované verzi mapy`);
    }
  }
  let bitmap;
  try {
    // No premultiplication, so cut-out edges keep their colour; PNG rows stay in GL order.
    bitmap = await createImageBitmap(blob, { premultiplyAlpha: 'none', colorSpaceConversion: 'none' });
  } catch (err) {
    throw new DataError(`${url}: obrázek atlasu nelze dekódovat (${err.message})`);
  }
  // Decoding cannot be cancelled; a bitmap that arrives after an abort is released here.
  if (signal?.aborted) {
    bitmap.close();
    throw signal.reason ?? new DOMException('Aborted', 'AbortError');
  }
  const maxSize = renderer.capabilities.maxTextureSize;
  if (bitmap.width > maxSize || bitmap.height > maxSize) {
    bitmap.close();
    throw new DataError(`Atlas ${bitmap.width}×${bitmap.height} px přesahuje limit GPU ${maxSize} px.`);
  }
  const texture = new THREE.Texture(bitmap);
  texture.userData = {...texture.userData, sha256:actualSha};
  texture.flipY = false;
  texture.colorSpace = THREE.SRGBColorSpace;
  // Mod sprites include sub-16px and unaligned rectangles. Downsampling the entire
  // atlas mixes unrelated sprites; use exact texels until padded per-sprite mips exist.
  texture.magFilter = THREE.NearestFilter;
  texture.minFilter = THREE.NearestFilter;
  texture.generateMipmaps = false;
  texture.needsUpdate = true;
  return texture;
}

// GPU texture and decoded bitmap; three.js dispose() does not close an ImageBitmap.
function releaseAtlas(texture) {
  texture.dispose();
  texture.image?.close?.();
}

// ---------------------------------------------------------------- scene

const srgbToLinear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);

function buildMesh(part, material) {
  if (part.indices.length === 0) return null;
  const geometry = new THREE.BufferGeometry();
  try {
    const vertices = part.positions.length / 3;
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(part.positions, 3));
    geometry.setAttribute('uv', new THREE.Float32BufferAttribute(part.uvs, 2));
    // Tints are authored like Minecraft colours (sRGB); the renderer works in linear space.
    geometry.setAttribute('color', new THREE.Float32BufferAttribute(part.colors.map(srgbToLinear), 3));
    geometry.setIndex(vertices > 65535 ? new THREE.Uint32BufferAttribute(part.indices, 1)
      : new THREE.Uint16BufferAttribute(part.indices, 1));
    geometry.computeVertexNormals();
    geometry.computeBoundingSphere();
    const mesh = new THREE.Mesh(geometry, material);
    mesh.matrixAutoUpdate = false;
    return mesh;
  } catch (err) {
    geometry.dispose();  // never leak a half-built geometry
    throw err;
  }
}

// position3, uv2, color3, normal3 float32 attributes, and 16/32-bit indices; CPU and GPU copies.
function partBufferBytes(part) {
  if (!part.indices.length) return 0;
  const vertices = part.positions.length / 3;
  return 2 * (vertices * 11 * 4 + part.indices.length * (vertices > 65535 ? 4 : 2));
}

function chunkBufferBytes(chunk) {
  return [chunk, ...(chunk.transparentParts ?? [])].reduce((sum, part) => sum + partBufferBytes(part), 0);
}

function addChunk(chunk) {
  const { opaque, transparent } = state.materials;
  const parts = [[chunk, chunk.transparent ? transparent : opaque],
    ...(chunk.transparentParts ?? []).map((part) => [part, transparent])];
  const meshes = [];
  try {
    for (const [part, material] of parts) {
      const mesh = buildMesh(part, material);
      if (!mesh) continue;
      meshes.push(mesh);
      if (material === transparent) mesh.renderOrder = 1;
      else state.raycastTargets.push(mesh);
      state.pickTargets.push(mesh);
      scene.add(mesh);
      state.triangles += part.indices.length / 3;
    }
  } catch (err) {
    removeChunk(meshes);
    throw err;
  }
  state.dirty = true;
  return meshes;
}

function removeChunk(meshes) {
  const removed = new Set(meshes);
  state.raycastTargets = state.raycastTargets.filter((mesh) => !removed.has(mesh));
  state.pickTargets = state.pickTargets.filter((mesh) => !removed.has(mesh));
  for (const mesh of meshes) {
    scene.remove(mesh);
    state.triangles -= mesh.geometry.index.count / 3;
    mesh.geometry.dispose();
  }
  state.dirty = true;
}

// The detail stream and the whole-world tiles have SEPARATE limits (GEOMETRY_BUDGET each: the stream's maxBytes and
// the window cut in updateHires), so each is shown as its own used / limit, never as one sum against one limit.
// Detail = resident stream payload bytes (chunkBufferBytes / LOD meshBytesFromCounts: float32 attributes + indices,
// estimated CPU and GPU copy); world = hires.bytes (byte length of the uploaded quantised tile buffers, one copy).
const GEOMETRY_MEMORY_TITLE = 'Dva samostatné limity. Detail: odhad CPU a GPU bufferů načtených chunků. '
  + 'Svět: velikost bufferů dlaždic nahraných na GPU (CPU kopie pro výběr bloků nezapočteny). '
  + 'Nezahrnuje atlas, textury, dočasná data ani režii prohlížeče; není to celková paměť.';
function geometryMemoryText(detailBytes, worldBytes) {
  const mib = (b) => (b / 1048576).toFixed(1);
  const limit = `${GEOMETRY_BUDGET / 1048576} MiB`;
  const detail = `detail ${mib(detailBytes)} / ${limit}`;
  return worldBytes == null ? detail : `${detail} · svět ${mib(worldBytes)} / ${limit}`;
}

function showStream(snapshot) {
  noteDetailResident(snapshot);
  const s = { ...snapshot };
  for (const key of ['resident', 'desired', 'pending', 'queued', 'failed', 'limited']) s[key] = snapshot[key].length;
  const busy = s.pending + s.queued > 0;
  setChunkCounts(s.resident, s.failed, s.desired);
  // The whole-world tiles count too (their own window and limit), so the panel is right away from the spawn.
  const world = hires.index ? hiresStats() : null;
  if (world) {
    $('chunks').textContent += ` · svět ${fmtNumber.format(world.loaded)} / ${fmtNumber.format(world.wanted)} dlaždic`;
    $('triangles').textContent = fmtNumber.format(state.triangles + world.triangles);
  }
  $('geometry-memory').textContent = geometryMemoryText(s.bytes, world ? world.bytes : null);
  $('geometry-memory').title = GEOMETRY_MEMORY_TITLE;
  // retryable: LOD refinement failures of tiles that still show coverage (not counted as failed chunks).
  $('retry-chunks').hidden = s.failed === 0 && !(snapshot.retryable > 0) && !(world?.failed > 0);
  setProgress(busy ? s.resident + s.failed + s.limited : 0, busy ? s.desired : 0);
  const details = s.errors.slice(0, MAX_LISTED_ERRORS).map((e) => `${e.entry?.file ?? 'Správa mapy'}: ${e.message}`);
  if (s.limited) details.unshift(`${s.limited} chunků se nevešlo do paměťového limitu. Přibližte pohled nebo snižte dohled.`);
  // The generation refresh, the experimental LOD summary and an LOD fallback warning add lines to the stream
  // status instead of replacing it.
  const note = state.refreshNote;
  // Whole-world tiles that failed wait for their automatic retry (backoff); the retry button forces it now.
  const worldFail = world?.failed > 0
    ? `Detail světa: ${fmtNumber.format(world.failed)} dlaždic se nepodařilo načíst, další pokus proběhne automaticky.`
    : null;
  const extra = [snapshot.lodNote, state.lodNote?.text, note?.text, worldFail].filter(Boolean);
  const warnNote = Boolean(note?.warn || state.lodNote?.warn || worldFail);
  const withNote = (lines) => [...lines, ...extra];
  if (busy) setStatus('loading', `Načítám okolí · ${s.resident} / ${s.desired} chunků`, withNote(details).length ? withNote(details) : null);
  else if (!s.desired && world && world.loading) {
    setStatus('loading', `Načítám detail světa · ${world.loaded} / ${world.wanted} dlaždic`, extra.length ? extra : null);
  } else if (!s.desired && world && worldFail) {
    setStatus('warn', `Detail světa načten částečně · ${world.loaded} / ${world.wanted} dlaždic`, extra);
  } else if (!s.desired && world) setStatus('ok', `Detail světa načten · ${world.loaded} dlaždic (${fmtNumber.format(world.loaded * 16)} chunků)`, null);
  else if (!s.desired) setStatus('warn', 'V tomto okolí nejsou exportované chunky. Přesuňte se zpět nebo resetujte kameru.', extra.length ? extra : null);
  else if (s.failed || s.limited || details.length) setStatus(s.resident ? 'warn' : 'error', `Okolí načteno částečně · ${s.resident} / ${s.desired}`, withNote(details));
  else setStatus(warnNote ? 'warn' : 'ok', `Okolí načteno · ${s.resident} chunků`, extra.length ? extra : null);
}

function setRefreshNote(text, warn = false) {
  state.refreshNote = text ? { text, warn } : null;
  if (state.stream) showStream(state.stream.getSnapshot());
}

function chunkRadius() {
  const setting = $('view-distance').value;
  if (setting !== 'auto') return Number(setting);
  if (state.fly?.mode === 'fly') return FLY_AUTO_RADIUS;
  // Automatic window grows with zoom-out; count and byte caps still apply.
  const distance = camera.position.distanceTo(controls.target);
  return Math.max(2, Math.min(12, Math.ceil(distance / 32)));
}

function desiredEntries(allEntries) {
  const radius = chunkRadius();
  // Orbit loads around the look-at target; Fly loads around the camera itself.
  const { x, z } = state.fly?.mode === 'fly' ? camera.position : controls.target;
  // Quantize priority to target chunk centers to avoid reorder/retry churn while orbiting.
  return selectChunks(allEntries, Math.floor(x / 16) * 16 + 8,
    Math.floor(z / 16) * 16 + 8, radius, MAX_RESIDENT_CHUNKS);
}

function updateChunkWindow() {
  if (!state.stream) return;
  state.stream.setDesired(desiredEntries(state.chunkEntries));
}

function scheduleChunkWindow() {
  if (!state.stream || state.streamTimer !== null) return;
  // Throttle rather than debounce: a continuous pan must still load new terrain.
  state.streamTimer = setTimeout(() => {
    state.streamTimer = null;
    updateChunkWindow();
  }, 150);
}

function startChunkStream(entries) {
  state.chunkEntries = entries;
  state.stream = new ChunkStream({
    maxConcurrent: MAX_PARALLEL, maxBytes: GEOMETRY_BUDGET,
    load: async (entry, signal) => {
      // Generation mode: base captured when the load starts; bytes verified against the hash in the file name,
      // so a request started for an earlier generation is still valid for the same content key.
      const generation = state.generation;
      const raw = generation
        ? await fetchVerifiedJson(generation.base + entry.file, contentHash(entry.file, 'chunks'), TIMEOUT_MS.chunk, signal)
        : await fetchJson(DATA_URL + entry.file, TIMEOUT_MS.chunk, signal);
      const chunk = validateChunk(raw, entry.file);
      return { chunk, bytes: chunkBufferBytes(chunk) };
    },
    attach: (entry, payload) => addChunk(payload.chunk),
    detach: (entry, meshes) => removeChunk(meshes),
    onChange: showStream,
  });
  $('view-distance').disabled = false;
  updateChunkWindow();
}

// ---------------------------------------------------------------- experimental opt-in LOD
//
// Only with ?lod=1 in generation mode and a complete, valid manifest.lod (lod-contract.js). The bounded window
// and its order come from desiredEntries/selectChunks as before; selectDistanceLod picks detail near the focus
// and coarse elsewhere in that window. This is not a far-distance or full-world LOD. LodSwap owns the budget,
// coverage handoff and failures. Every generation commit in LOD mode resets all geometry (no reuse yet).

/** { lod, reserve, warning } for a generation manifest; never throws. */
function planLod(manifest) {
  let lod;
  try {
    lod = validateLodManifest(manifest);
  } catch (err) {
    return { lod: null, warning: `Experimentální LOD vypnuto (neplatná data LOD: ${err.message}); zobrazují se plné detaily.` };
  }
  if (!lod) return { lod: null, warning: 'Experimentální LOD: tato verze mapy data LOD neobsahuje; zobrazují se plné detaily.' };
  const reserve = Math.max(0, ...[...lod.values()].map((d) => d.coarseBytes));
  if (reserve > GEOMETRY_BUDGET) {
    return { lod: null, warning: 'Experimentální LOD vypnuto: hrubá geometrie chunku přesahuje paměťový limit; zobrazují se plné detaily.' };
  }
  return { lod, reserve, warning: null };
}

/** Start the stream for a manifest: LOD when opted in and valid, otherwise the unchanged ChunkStream. */
function startStream(manifest) {
  const plan = !state.lodOptIn ? null : state.generation ? planLod(manifest)
    : { lod: null, warning: 'Experimentální LOD je dostupné jen pro zveřejněné verze mapy; zobrazují se plné detaily.' };
  state.lodNote = plan?.warning ? { text: plan.warning, warn: true } : null;
  if (plan?.lod) startLodStream(manifest.chunks, plan.lod, plan.reserve);
  else startChunkStream(manifest.chunks);
}

// Vertex colours only: no atlas, no alpha test, no atlas sampling helper. Same lighting class as the textured ones.
function ensureUntexturedMaterial() {
  if (state.untexturedMaterial) return;
  const Material = state.bakedLighting ? THREE.MeshBasicMaterial : THREE.MeshLambertMaterial;
  state.untexturedMaterial = new Material({ vertexColors: true, side: THREE.FrontSide });
  applyDisplayOptions();
}

function lodDesired(entries, lod, previous) {
  const { x, z } = state.fly?.mode === 'fly' ? camera.position : controls.target;
  const rows = selectDistanceLod({
    tiles: entries.map((e) => ({ id: `${e.x},${e.z}`, minX: e.x * 16, maxX: e.x * 16 + 16, minZ: e.z * 16, maxZ: e.z * 16 + 16 })),
    focus: { x, z }, enterRadius: LOD_ENTER_RADIUS, exitRadius: LOD_EXIT_RADIUS, previousDetailIds: previous,
  });
  const level = new Map(rows.map((r) => [r.id, r.level]));
  const desired = entries.map((e) => {   // window order (nearest first) is kept
    const id = `${e.x},${e.z}`;
    const d = lod.get(id);
    return { id, generation: `${d.atlasSha256}:${d.sourceSha256}:${d.coarseSha256}`, level: level.get(id),
      coarse: { key: d.file, bytes: d.coarseBytes }, detail: { key: e.file, bytes: d.detailBytes } };
  });
  return { desired, detailIds: rows.filter((r) => r.level === 'detail').map((r) => r.id) };
}

const partCounts = (parts) => parts.map((p) => ({ vertices: p.positions.length / 3, indices: p.indices.length }));

async function loadLodRepresentation(generation, desc, d, rep, signal) {
  if (rep === 'detail') {
    const file = desc.detail.key;
    const chunk = validateChunk(await fetchVerifiedJson(generation.base + file, contentHash(file, 'chunks'),
      TIMEOUT_MS.chunk, signal), file);
    const counts = partCounts([chunk, ...(chunk.transparentParts ?? [])]);
    if (counts.length !== d.detailParts.length
      || counts.some((c, i) => c.vertices !== d.detailParts[i].vertices || c.indices !== d.detailParts[i].indices)) {
      throw new DataError(`${file}: počty geometrie neodpovídají manifestu LOD`);
    }
    return { bytes: chunkBufferBytes(chunk), data: { kind: 'detail', chunk } };
  }
  const doc = await fetchVerifiedJson(generation.base + d.file, d.coarseSha256, TIMEOUT_MS.chunk, signal);
  try {
    validateLodChunk(doc, d);
  } catch (err) {
    throw new DataError(`${d.file}: ${err.message}`);
  }
  const parts = [doc.untextured, doc.textured, ...doc.transparentParts];
  const bytes = parts.reduce((sum, part) => sum + partBufferBytes(part), 0);
  if (bytes !== meshBytesFromCounts(partCounts(parts))) throw new DataError(`${d.file}: velikost geometrie nesouhlasí`);
  return { bytes, data: { kind: 'coarse', doc } };
}

// All parts are built HIDDEN and are neither pickable nor counted until setLodVisible(resource, true).
function attachLod(data) {
  const { opaque, transparent } = state.materials;
  const parts = data.kind === 'detail'
    ? [[data.chunk, data.chunk.transparent ? transparent : opaque],
      ...(data.chunk.transparentParts ?? []).map((part) => [part, transparent])]
    : [[data.doc.untextured, state.untexturedMaterial], [data.doc.textured, data.doc.textured.transparent ? transparent : opaque],
      ...data.doc.transparentParts.map((part) => [part, transparent])];
  const resource = { meshes: [], raycast: [], triangles: 0, visible: false, disposed: false };
  try {
    for (const [part, material] of parts) {
      const mesh = buildMesh(part, material);
      if (!mesh) continue;
      mesh.visible = false;
      resource.meshes.push(mesh);
      if (material === transparent) mesh.renderOrder = 1;
      else resource.raycast.push(mesh);
      resource.triangles += part.indices.length / 3;
      scene.add(mesh);
    }
  } catch (err) {
    for (const mesh of resource.meshes) {
      scene.remove(mesh);
      mesh.geometry.dispose();
    }
    throw err;
  }
  return resource;
}

// Idempotent: picking/raycast registration and the triangle count follow visibility exactly once.
function setLodVisible(resource, visible) {
  if (resource.disposed || resource.visible === visible) return;
  resource.visible = visible;
  for (const mesh of resource.meshes) mesh.visible = visible;
  if (visible) {
    state.raycastTargets.push(...resource.raycast);
    state.pickTargets.push(...resource.meshes);
    state.triangles += resource.triangles;
  } else {
    const removed = new Set(resource.meshes);
    state.raycastTargets = state.raycastTargets.filter((mesh) => !removed.has(mesh));
    state.pickTargets = state.pickTargets.filter((mesh) => !removed.has(mesh));
    state.triangles -= resource.triangles;
  }
  state.dirty = true;
}

// Separate from removeChunk (which always subtracts triangles): unregisters only if visible, disposes once.
function detachLod(resource) {
  if (resource.disposed) return;
  setLodVisible(resource, false);
  resource.disposed = true;
  for (const mesh of resource.meshes) {
    scene.remove(mesh);
    mesh.geometry.dispose();
  }
  state.dirty = true;
}

/** ChunkStream-shaped snapshot for showStream. Coverage counts only; background refinement is in lodNote. */
function lodStreamSnapshot(s, byId) {
  const entryOf = (t) => byId.get(t.id);
  const pick = (predicate) => s.tiles.filter(predicate).map(entryOf);
  const count = (predicate) => s.tiles.filter(predicate).length;
  const detail = count((t) => t.visible?.representation === 'detail');
  const coarse = count((t) => t.visible?.representation === 'coarse');
  const refining = count((t) => t.visible && t.loading);
  const waiting = count((t) => t.visible && t.limited);
  let lodNote = `Experimentální LOD (jen v načteném okolí, ne celý svět): ${detail} detailních a ${coarse} hrubých chunků`;
  if (refining) lodNote += `, ${refining} se právě nahrazuje`;
  if (waiting) lodNote += `, ${waiting} čeká na volnou paměť`;
  return {
    desired: s.tiles.map(entryOf),
    resident: pick((t) => t.visible),
    pending: pick((t) => !t.visible && t.loading),
    queued: pick((t) => !t.visible && !t.loading && !t.failed.length && !t.limited),
    failed: pick((t) => !t.visible && !t.loading && t.failed.length > 0),
    limited: pick((t) => !t.visible && !t.loading && !t.failed.length && t.limited),
    bytes: s.bytes,
    errors: [
      ...s.tiles.flatMap((t) => t.failed.map((f) => ({ entry: entryOf(t),
        message: `${f.representation === 'coarse' ? 'hrubá' : 'detailní'} verze: ${f.message}` }))),
      ...s.errors.map((e) => ({ entry: null, message: e.message })),
    ],
    retryable: count((t) => t.failed.length > 0),
    lodNote,
  };
}

function startLodStream(entries, lod, reserve) {
  ensureUntexturedMaterial();
  state.chunkEntries = entries;
  const generation = state.generation;   // immutable base captured for every load of this stream
  const byId = new Map(entries.map((e) => [`${e.x},${e.z}`, e]));
  let previous = [];                       // previous DESIRED detail ids (hysteresis), reset with the stream
  const swap = new LodSwap({
    maxBytes: GEOMETRY_BUDGET, maxConcurrent: MAX_PARALLEL, downgradeReserveBytes: reserve,
    load: (desc, rep, signal) => loadLodRepresentation(generation, desc, lod.get(desc.id), rep, signal),
    attach: (desc, rep, payload) => attachLod(payload.data),
    setVisible: setLodVisible,
    detach: detachLod,
    onChange: (snapshot) => showStream(lodStreamSnapshot(snapshot, byId)),
  });
  state.stream = {
    kind: 'lod',
    setDesired(windowEntries) {
      const { desired, detailIds } = lodDesired(windowEntries, lod, previous);
      previous = detailIds;
      swap.setDesired(desired);
    },
    retry: () => swap.retry(),
    dispose: () => swap.dispose(),
    getSnapshot: () => lodStreamSnapshot(swap.getSnapshot(), byId),
  };
  $('view-distance').disabled = false;
  updateChunkWindow();
}

function initViewer() {
  const container = $('view');
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  } catch (err) {
    throw new DataError(['WebGL není v tomto prohlížeči dostupné.', err.message]);
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.localClippingEnabled = true;
  renderer.domElement.tabIndex = 0;
  container.append(renderer.domElement);

  scene = new THREE.Scene();
  scene.background = new THREE.Color(BACKGROUND);
  fog = new THREE.Fog(BACKGROUND, 100, 1000);
  scene.fog = fog;
  scene.add(new THREE.HemisphereLight(0xe6f0ff, 0x8a8478, LIGHT.hemisphere));
  scene.add(new THREE.AmbientLight(0xffffff, LIGHT.ambient));
  const sun = new THREE.DirectionalLight(0xfff3df, LIGHT.sun);
  sun.position.set(0.45, 1, 0.3);
  scene.add(sun);

  camera = new THREE.PerspectiveCamera(55, 1, 0.5, 4000);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.screenSpacePanning = false;
  controls.maxPolarAngle = Math.PI * 0.495;
  controls.minDistance = 4;
  controls.zoomToCursor = true;
  controls.addEventListener('start', () => { state.userMoved = true; cancelPendingGoto(); });
  renderer.domElement.addEventListener('wheel', cancelPendingGoto, { passive: true });
  // Wheel/touch handlers call update() themselves. The following RAF update can
  // return false, so its return value alone is not a reliable render invalidation.
  controls.addEventListener('change', () => { state.dirty = true; scheduleChunkWindow(); scheduleOverview(); });
  const keyboardPan = createKeyboardPan(controls, window, document,
    () => Boolean(state.bounds) && state.fly?.mode !== 'fly');

  new ResizeObserver(() => {
    const { clientWidth: w, clientHeight: h } = container;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    state.dirty = true;
  }).observe(container);

  let previousTime = null;
  const tick = (time) => {
    requestAnimationFrame(tick);
    const dt = previousTime === null ? 0 : (time - previousTime) / 1000;
    previousTime = time;
    if (state.fly?.mode === 'fly') {
      // OrbitControls stays disabled and is never updated while flying.
      if (state.fly.step(dt)) state.dirty = true;
      if (!state.dirty) return;
      state.dirty = false;
      const far = (chunkRadius() + 0.5) * 16;
      fog.near = far * 0.6;
      fog.far = far;
      const p = camera.position;
      const waiting = state.fly.walking && state.walker.waiting ? ' · načítám bloky…' : '';
      $('fly-position').textContent = `X ${p.x.toFixed(1)} · Y ${p.y.toFixed(1)} · Z ${p.z.toFixed(1)}${waiting}`;
      showCaveCoverage();
      renderer.render(scene, camera);
      return;
    }
    keyboardPan.update(dt);
    if (!controls.update() && !state.dirty) return;
    state.dirty = false;
    const distance = camera.position.distanceTo(controls.target);
    // Orbit distance measures the look-at point, which can be above the terrain
    // (e.g. a generation without focus). Close zoom must not fog out that terrain.
    fog.near = Math.max(128, distance * 0.9);
    fog.far = Math.max(256, distance * 2.6 + 64);
    if (state.bounds) {
      const t = controls.target;
      $('target').textContent = `X ${Math.round(t.x)} · Z ${Math.round(t.z)}`;
    }
    $('zoom-distance').textContent = `${Math.round(distance)} bloků`;
    showCaveCoverage();
    renderer.render(scene, camera);
  };
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------------- camera and UI

const raycaster = new THREE.Raycaster();
let pendingGoto = null;
function cancelPendingGoto() {
  const wasPending = pendingGoto?.pending;
  pendingGoto?.cancel();
  // Once navigation changes, the old destination's loading notice is no longer current.
  if (wasPending) $('goto-msg').hidden = true;
}

function terrainHeight(x, z) {
  const { min, max } = state.bounds;
  raycaster.set(new THREE.Vector3(x, max[1] + 16, z), new THREE.Vector3(0, -1, 0));
  raycaster.far = max[1] - min[1] + 32;
  const hit = firstVisibleHit(raycaster.intersectObjects(state.raycastTargets, false), state.cut);
  return hit ? hit.point.y : null;
}

// Detail has priority; outside it, raycast only the attached hires tile at this column.
// Include the translucent group so a water surface wins over the opaque seabed.
function gotoHeight(x, z) {
  const detail = terrainHeight(x, z);
  if (detail !== null) return detail;
  if (!hires.index) return null;
  const size = hires.index.tileSize;
  const entry = hires.tiles.get(`${Math.floor(x / size)},${Math.floor(z / size)}`);
  if (!entry) return null;
  raycaster.set(new THREE.Vector3(x, 320, z), new THREE.Vector3(0, -1, 0));
  raycaster.far = 400;
  const meshes = entry.meshes.filter((mesh) => mesh.visible);
  for (const mesh of meshes) mesh.updateWorldMatrix(true, false);
  const hit = firstVisibleHit(raycaster.intersectObjects(meshes, false), state.cut);
  if (hit) return hit.point.y;
  if (state.cut !== null) return null;
  const ground = hiresColumn(x, z);
  return ground !== undefined && ground >= 0 ? ground : null;
}

function resetCamera() {
  cancelPendingGoto();
  const { min, max } = state.bounds;
  const [cx, cy, cz] = state.mapView ? state.focus : [0, 1, 2].map((i) => (min[i] + max[i]) / 2);
  const span = state.mapView ? 128 : Math.max(max[0] - min[0], max[2] - min[2], 32);
  const y = state.focus?.[1] ?? terrainHeight(cx, cz) ?? cy;
  controls.target.set(cx, y, cz);
  const fit = Math.max(1, 1 / camera.aspect);
  camera.position.set(cx + span * 0.85 * fit, y + span * 0.95 * fit, cz + span * 1.1 * fit);
  camera.far = span * 8 + 2000;
  camera.updateProjectionMatrix();
  controls.maxDistance = span * 3 + 200;
  state.dirty = true;
}

function moveTo(event) {
  event.preventDefault();
  cancelPendingGoto();
  if (state.fly?.mode === 'fly') return;
  const msg = $('goto-msg');
  const say = (text, warn) => { msg.textContent = text; msg.classList.toggle('warn', warn); msg.hidden = false; };
  const rawX = $('goto-x').value.trim();
  const rawZ = $('goto-z').value.trim();
  const x = Number(rawX);
  const z = Number(rawZ);
  if (!rawX || !rawZ || !Number.isFinite(x) || !Number.isFinite(z)) return say('Zadejte čísla X i Z.', true);
  const { min, max } = state.bounds;
  const wholeWorld = Boolean(hires.index || overview.index);   // tiles/overview cover the world beyond the export
  if (!wholeWorld && (x < min[0] || x > max[0] || z < min[2] || z > max[2])) {
    return say(`Mimo exportovanou oblast (X ${min[0]} … ${max[0]}, Z ${min[2]} … ${max[2]}).`, true);
  }
  const y = gotoHeight(x, z);
  const offset = camera.position.clone().sub(controls.target);
  controls.target.set(x, y ?? controls.target.y, z);
  camera.position.copy(controls.target).add(offset);
  if (y === null) {
    const key = hires.index ? `${Math.floor(x / hires.index.tileSize)},${Math.floor(z / hires.index.tileSize)}` : null;
    if (key === null || hires.index.tiles.has(key)) {
      pendingGoto ??= createGotoTerrain();
      pendingGoto.start({ key, x, z, targetY: controls.target.y });
    }
  }
  state.userMoved = true;
  state.dirty = true;
  say(y === null ? `Přesunuto na X ${x}, Z ${z} – terén zde není načten.` : `Přesunuto na X ${x}, Z ${z}.`, y === null);
}

function finishPendingGoto(key) {
  if (!pendingGoto?.pending || state.fly?.mode === 'fly') return;
  const x = controls.target.x, z = controls.target.z;
  if (!hires.index || key !== `${Math.floor(x / hires.index.tileSize)},${Math.floor(z / hires.index.tileSize)}`) return;
  const y = gotoHeight(x, z);
  const delta = pendingGoto.resolve({ key, height: y, targetX: x, targetZ: z,
    targetY: controls.target.y, orbit: state.fly?.mode !== 'fly' });
  if (delta === null) return;
  controls.target.y += delta;
  camera.position.y += delta;
  controls.update();
  state.dirty = true;
  $('goto-msg').textContent = `Přesunuto na X ${x}, Z ${z}.`;
  $('goto-msg').classList.remove('warn');
}

// ---------------------------------------------------------------- block selection

const pickRaycaster = new THREE.Raycaster();

function showPick({ cell = null, block = null, message = null, warn = false }) {
  $('pick').hidden = false;
  $('pick-coords').textContent = cell ? `X ${cell.x} · Y ${cell.y} · Z ${cell.z}` : '–';
  $('pick-facts').hidden = !block;
  if (block) {
    $('pick-name').textContent = block.name;
    $('pick-id').textContent = String(block.id);
    $('pick-meta').textContent = String(block.meta);
  }
  $('pick-msg').textContent = message ?? '';
  $('pick-msg').classList.toggle('warn', warn);
  $('pick-msg').hidden = !message;
}

function closePick() {
  state.pickSeq += 1;  // any pending lookup is now stale
  $('pick').hidden = true;
}

// Block files are read only on click; decoded chunks are kept in a small LRU cache and failures are not cached.
// Keyed by file (content-addressed in generation mode, one file per chunk in the legacy export).
function loadBlockChunk(cx, cz, file) {
  const key = file;
  const cached = blockCache.get(key);
  if (cached) return Promise.resolve(cached);
  let pending = blockLoads.get(key);
  if (!pending) {
    const generation = state.generation;
    const request = generation
      ? Promise.resolve().then(() => fetchVerifiedJson(generation.base + file, contentHash(file, 'blocks', cx, cz),
        TIMEOUT_MS.blocks))
      : fetchJson(DATA_URL + file, TIMEOUT_MS.blocks);
    pending = request
      .then((data) => {
        const chunk = decodeBlockChunk(data, cx, cz, file);
        blockCache.set(key, chunk);
        return chunk;
      })
      .finally(() => blockLoads.delete(key));
    blockLoads.set(key, pending);
  }
  return pending;
}

async function lookupBlock({ x, y, z }) {
  if (y < 0 || y > 255) return { message: 'Mimo výškový rozsah exportu (Y 0–255).', warn: true };
  const cx = chunkCoord(x);
  const cz = chunkCoord(z);
  const file = state.blockIndex?.get(`${cx},${cz}`);
  if (!file) return { message: 'Pro toto místo export neobsahuje bloková data.', warn: true };
  let chunk;
  try {
    chunk = await loadBlockChunk(cx, cz, file);
  } catch (err) {
    return { message: `Bloková data nelze načíst: ${err.message}`, warn: true };
  }
  const block = blockAt(chunk, x, y, z);
  return block ? { block } : { message: 'Buňka leží mimo exportovanou oblast.', warn: true };
}

function pickAt(clientX, clientY) {
  const rect = renderer.domElement.getBoundingClientRect();
  const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
  pickRaycaster.setFromCamera(ndc, camera);
  pickRaycaster.near = camera.near;
  pickRaycaster.far = camera.far;
  const seq = ++state.pickSeq;
  // With the height cut on, geometry above the plane is invisible and must not be picked.
  const hit = firstVisibleHit(pickRaycaster.intersectObjects([...state.pickTargets, ...hiresPickTargets()], false), state.cut);
  if (!hit || !hit.face) {
    showPick({ message: 'Kliknutí nezasáhlo vykreslený terén.', warn: true });
    return;
  }
  const normal = hit.face.normal.clone().transformDirection(hit.object.matrixWorld).normalize();
  const cell = pickCell(hit.point, normal, pickRaycaster.ray.direction);
  if (hit.object.userData.hires) {
    // Whole-world tiles carry no block ids: name the texture of the face and give the coordinates.
    const uv = hit.uv ? hit.uv.clone() : null;
    if (HIRES_IMMUTABLE && !hires.index?.context) {
      // The mutable atlas-layout.json may belong to another atlas than the pinned immutable one: no texture name.
      showPick({ cell, block: { name: 'neznámá textura', id: '–', meta: '–' },
        message: 'Experimentální neměnné dlaždice nemají ověřený layout atlasu, název textury proto nelze spolehlivě určit.' });
      return;
    }
    showPick({ cell, message: 'Načítám…' });
    const pickedLayout = HIRES_IMMUTABLE ? hires.index?.context?.layoutSha256 : null;
    loadHiresSprites().then((spriteAt) => {
      if (seq !== state.pickSeq) return;
      if(HIRES_IMMUTABLE && pickedLayout!==hires.index?.context?.layoutSha256){
        showPick({cell,message:'Mapa se aktualizovala; vyber blok znovu.'});return;
      }
      const sprite = spriteAt && uv ? spriteAt(uv.x, uv.y) : null;
      showPick({ cell, block: { name: sprite ? `textura: ${sprite}` : 'neznámá textura', id: '–', meta: '–' },
        message: 'Mimo detail u spawnu zná mapa jen texturu plochy, ne přesné ID a metadata bloku.' });
    });
    return;
  }
  showPick({ cell, message: 'Načítám bloková data…' });
  lookupBlock(cell).then((result) => {
    if (seq === state.pickSeq) showPick({ cell, ...result });  // a newer click or close wins
  });
}

// Short left click or single tap selects; drags, orbit, pan and multi-touch pinch never do.
function wirePicking(canvas) {
  const pointers = new Set();
  let gesture = null;
  canvas.addEventListener('pointerdown', (e) => {
    // No picking in Fly: a click there only acquires the mouse.
    if (state.fly?.mode === 'fly') { gesture = null; return; }
    pointers.add(e.pointerId);
    if (pointers.size === 1 && e.button === 0 && e.isPrimary) {
      gesture = { id: e.pointerId, x: e.clientX, y: e.clientY, t: performance.now(), valid: true };
    } else if (gesture) {
      gesture.valid = false;
    }
  });
  canvas.addEventListener('pointermove', (e) => {
    if (gesture && e.pointerId === gesture.id
      && Math.hypot(e.clientX - gesture.x, e.clientY - gesture.y) > PICK_SLOP_PX) gesture.valid = false;
  });
  const end = (e) => {
    pointers.delete(e.pointerId);
    if (!gesture || e.pointerId !== gesture.id || state.fly?.mode === 'fly') return;
    const g = gesture;
    gesture = null;
    if (e.type === 'pointerup' && g.valid && performance.now() - g.t <= PICK_MAX_MS
      && Math.hypot(e.clientX - g.x, e.clientY - g.y) <= PICK_SLOP_PX) pickAt(e.clientX, e.clientY);
  };
  canvas.addEventListener('pointerup', end);
  canvas.addEventListener('pointercancel', end);
  $('pick-close').addEventListener('click', closePick);
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('pick').hidden) closePick(); });
  $('pick-help').hidden = false;
}

// Picking is wired once, for the first manifest (or later generation) that ships block data.
function ensurePicking() {
  if (state.pickingWired) return;
  state.pickingWired = true;
  wirePicking(renderer.domElement);
}

function applyDisplayOptions() {
  scene.fog = $('opt-fog').checked ? fog : null;
  const side = $('opt-double').checked ? THREE.DoubleSide : THREE.FrontSide;
  if (state.cut !== null) cutPlane.constant = clipTop(state.cut);
  const planes = state.cut === null ? null : [cutPlane];
  for (const mesh of overview.meshes.values()) mesh.material.clippingPlanes = planes;
  for (const material of [...Object.values(state.materials ?? {}), state.untexturedMaterial,
    ...Object.values(hiresAtlas?.active?.materials ?? {})].filter(Boolean)) {
    material.side = side;
    material.clippingPlanes = planes;
    material.needsUpdate = true;
  }
  state.dirty = true;
}

// ---------------------------------------------------------------- whole-world overview (UIE_OVERVIEW1)
// Coarse heightfield tiles of the whole world under the detail chunks (1 block lower, so detail wins where it
// exists). Optional: without /data/overview.json nothing changes. Tiles are swapped by camera distance.
const OVERVIEW_GRID = 128;          // quads per tile side (every 4th texel)
const overview = { index: null, base: null, build: null, meshes: new Map(), loading: new Set(), timer: null, failed: 0 };
const OVERVIEW_POLL_MS = 10 * 60 * 1000;   // the server refreshes the overview periodically (overview_update.py)

// ---------------------------------------------------------------- whole-world hires tiles (hires.js, UIH1)
// Same loading system as the detail chunk stream: the window of "Načítané okolí" (chunkRadius, around the look-at
// point; around the camera when flying), nearest first, at most GEOMETRY_BUDGET bytes; everything outside the
// window is unloaded (the overview covers it). Quads of detail-export chunks that are resident right now are hidden
// (masked per chunk and refreshed whenever the detail stream changes), so the two never overlap and nothing is
// left empty when the stream unloads a detail chunk. CPU copies are released after upload.
const HIRES_INDEX_POLL_MS = 60_000;
// Read a consistent immutable generation by default. ?immutableHires=0 is an explicit diagnostic legacy mode.
// An immutable error never falls back to legacy files (no mixed revisions); the previous set stays.
const HIRES_IMMUTABLE = new URLSearchParams(window.location?.search ?? '').get('immutableHires') !== '0';
const hires = { index: null, tiles: new Map(), requests: null, bytes: 0, chunks: new Set(), wired: false,
  indexLoading: false, sourceBase: `${DATA_URL}hires/` };
let hiresWantedSet = new Set();
// The tiles' own atlas (hires/index.json atlas + atlasSha256) and materials, never the detail generation's
// state.materials: a detail generation switch does not touch them and a hires atlas switch does not touch the
// detail atlas. Created in startHires (null before).
let hiresAtlas = null;

// Offline-meshed tiles carry baked face shading in their vertex colours and have no normals: always unlit
// (MeshBasicMaterial, as the detail materials with bakedLighting), same alpha handling and display options.
function makeHiresMaterials(texture) {
  const base = { map: texture, vertexColors: true, side: $('opt-double').checked ? THREE.DoubleSide : THREE.FrontSide,
    clippingPlanes: state.cut === null ? null : [cutPlane] };
  const materials = {
    opaque: new THREE.MeshBasicMaterial({ ...base, alphaTest: 0.5 }),
    transparent: new THREE.MeshBasicMaterial({ ...base, transparent: true, depthWrite: false, alphaTest: 0.01 }),
  };
  for (const material of Object.values(materials)) useCentroidAtlasSampling(material, THREE.ShaderChunk);
  return materials;
}

// Atlas of the geometry that is visible outside the detail export (for collision pairing); null without tiles.
function visibleHiresAtlasSha() {
  return hiresAtlas?.active?.sha ?? null;
}

function hiresBytes(group) {
  return group.positions.byteLength + group.uvs.byteLength + group.colors.byteLength + group.indices.byteLength;
}

function hiresGroupMesh(group, material, tile) {
  const [offX, offY, offZ] = tile.offset;
  const s = tile.scale;
  // Chunk of each quad (0..15 inside the 4 x 4 chunk tile) from its centre, for the per-chunk detail mask.
  const quadChunk = new Uint8Array(group.quads);
  const p = group.positions;
  const [tx0, tz0] = tile.origin;
  for (let q = 0; q < group.quads; q += 1) {
    const v = q * 12;
    const lx = Math.min(3, Math.max(0, Math.floor(((p[v] + p[v + 6]) * s / 2 + offX - tx0) / 16)));
    const lz = Math.min(3, Math.max(0, Math.floor(((p[v + 2] + p[v + 8]) * s / 2 + offZ - tz0) / 16)));
    quadChunk[q] = lz * 4 + lx;
  }
  // Quantised attributes go to the GPU as stored; the mesh matrix restores world units (scale + offset).
  const geometry = new THREE.BufferGeometry();
  const release = function release() { this.array = null; };
  // Positions, UVs and the index stay on the CPU too: block picking raycasts the tiles (~10 bytes per vertex).
  geometry.setAttribute('position', new THREE.BufferAttribute(group.positions, 3));
  geometry.setAttribute('uv', new THREE.BufferAttribute(group.uvs, 2, true));
  geometry.setAttribute('color', new THREE.BufferAttribute(group.colors, 3, true).onUpload(release));
  geometry.setIndex(new THREE.BufferAttribute(group.indices, 1));
  geometry.computeBoundingSphere();
  const mesh = new THREE.Mesh(geometry, material);
  mesh.matrixAutoUpdate = false;
  mesh.matrix.makeScale(s, s, s).setPosition(offX, offY, offZ);
  mesh.matrixWorldNeedsUpdate = true;
  mesh.userData = { quadChunk, quads: group.quads, big: group.indices instanceof Uint32Array, mask: 0, hires: true };
  return mesh;
}

// Hide the quads of chunks the detail stream shows right now (bit per local chunk); rebuild only on change.
function applyHiresMask(entry) {
  const [tx, tz] = entry.key.split(',').map(Number);
  let mask = 0;
  for (let i = 0; i < 16; i += 1) {
    if (detailResident.has(`${tx * 4 + (i & 3)},${tz * 4 + (i >> 2)}`)) mask |= 1 << i;
  }
  for (const mesh of entry.meshes) {
    const d = mesh.userData;
    if (d.mask === mask) continue;
    d.mask = mask;
    let kept = 0;
    for (let q = 0; q < d.quads; q += 1) if (!(mask & (1 << d.quadChunk[q]))) kept += 1;
    const indices = d.big ? new Uint32Array(kept * 6) : new Uint16Array(kept * 6);
    let i = 0;
    for (let q = 0; q < d.quads; q += 1) {
      if (mask & (1 << d.quadChunk[q])) continue;
      const b = q * 4;
      indices[i] = b; indices[i + 1] = b + 1; indices[i + 2] = b + 2;
      indices[i + 3] = b; indices[i + 4] = b + 2; indices[i + 5] = b + 3;
      i += 6;
    }
    mesh.geometry.setIndex(new THREE.BufferAttribute(indices, 1));   // kept for picking
    mesh.visible = kept > 0;
  }
  state.dirty = true;
}

function refreshHiresMasks() {
  for (const entry of hires.tiles.values()) applyHiresMask(entry);
  updateWorldCaves();
}

function hiresPickTargets() {
  const out = [];
  for (const entry of hires.tiles.values()) for (const mesh of entry.meshes) if (mesh.visible) out.push(mesh);
  return out;
}

let hiresSprites = null;
let hiresSpritesKey = null;
function loadHiresSprites() {
  if(HIRES_IMMUTABLE){
    const index=hires.index,key=index?.context?.layoutSha256;
    if(!key)return Promise.resolve(null);
    if(hiresSpritesKey!==key){hiresSprites=null;hiresSpritesKey=key;}
    if(!hiresSprites){
      const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),15000);
      hiresSprites=loadImmutableLayout({fetch:(url)=>fetch(url,{signal:abort.signal}),baseUrl:hires.sourceBase,index,sha256Hex})
        .then(layout=>hires.index?.context?.layoutSha256===key?spriteIndex(layout):null)
        .catch(()=>{if(hiresSpritesKey===key)hiresSprites=null;return null;})
        .finally(()=>clearTimeout(timer));
    }
    return hiresSprites;
  }
  hiresSprites ??= fetch(`${DATA_URL}hires/atlas-layout.json`)
    .then((res) => (res.ok ? res.json() : null))
    .then((layout) => (layout ? spriteIndex(layout) : null))
    .catch(() => null);
  return hiresSprites;
}

function hiresStats() {
  let triangles = 0;
  for (const entry of hires.tiles.values()) {
    for (const mesh of entry.meshes) if (mesh.visible) triangles += mesh.geometry.index.count / 3;
  }
  // Loaded = the current revision is shown (an older revision kept until its replacement arrives does not count).
  const loaded = [...hiresWantedSet].filter((k) => hires.tiles.get(k)?.sha === hires.index?.tiles.get(k)).length;
  const requests = hires.requests?.snapshot() ?? { loading: 0, failed: [] };
  return { loaded, wanted: hiresWantedSet.size, triangles, bytes: hires.bytes,
    loading: requests.loading > 0 || (requests.busyRetryInMs > 0 && loaded < hiresWantedSet.size),
    failed: requests.failed.length };
}

// Panel refresh after tile loads/unloads (throttled; the detail stream refreshes it on its own events).
let hiresPanelTimer = null;
function scheduleHiresPanel() {
  if (hiresPanelTimer !== null) return;
  hiresPanelTimer = setTimeout(() => {
    hiresPanelTimer = null;
    if (state.stream) showStream(state.stream.getSnapshot());
    else if (state.mapView) showStandaloneProgress();
  }, 250);
}

function dropHires(key) {
  const entry = hires.tiles.get(key);
  if (!entry) return;
  for (const mesh of entry.meshes) { scene.remove(mesh); mesh.geometry.dispose(); }
  hires.bytes -= entry.bytes;
  for (const c of entry.chunks) hires.chunks.delete(c);
  hires.tiles.delete(key);
  updateWorldCaves();
  scheduleOverviewMask();
  scheduleHiresPanel();
  state.dirty = true;
}

// Returns false when the tile is not attached (hires-requests then retries it with backoff while still wanted).
// `loaded` is { tile, atlasGeneration } from decodeTile: a tile requested against an earlier index/atlas set is
// never attached to the current one (its UVs belong to the atlas of the index it was listed in).
function attachHiresTile(key, sha, loaded) {
  const { tile, atlasGeneration } = loaded;
  const set = hiresAtlas?.active;
  if (!set || !hiresAtlas.isCurrent(atlasGeneration)) return false;
  if (hires.index?.tiles.get(key) !== sha) return false;
  // Immutable mode: the full object hash pinned at request start must still be the current index's object.
  if (hires.index.immutable && hires.index.objects.get(key) !== loaded.object) return false;
  if (!hiresWantedSet.has(key)) return false;   // the window moved on while it was loading
  if (hires.tiles.get(key)?.sha === sha) return true;
  let bytes = tile.groups.reduce((n, g) => n + hiresBytes(g), 0);
  const meshes = [];
  let entry;
  try {
    for (const [i,g] of tile.groups.entries()) if (g.quads) {
      meshes.push(hiresGroupMesh(g, i ? set.materials.transparent : set.materials.opaque, tile));
    }
    const chunks = tileChunks(key, hires.index.tileSize);
    const heights = surfaceHeights(tile, hires.index.tileSize);
    const walk = buildHiresWalk(tile, hires.index.tileSize);
    bytes += walk.byteLength + heights.byteLength;
    entry = { key, sha, object: loaded.object ?? null, meshes, bytes, chunks, heights, walk };
    applyHiresMask(entry);
    for (const mesh of meshes) scene.add(mesh);
  } catch (error) {
    for (const mesh of meshes) { scene.remove(mesh); mesh.geometry.dispose(); }
    throw error; // Request scheduler retries; the previous visible/walkable revision is untouched.
  }
  dropHires(key); // Commit only after the replacement's geometry, collision surface and mask are ready.
  const { chunks } = entry;
  for (const c of chunks) hires.chunks.add(c);
  hires.tiles.set(key, entry);
  hires.bytes += bytes;
  updateWorldCaves();
  try { hires.onTileAttached?.(key); }
  catch (error) { console.warn('goto terrain:', error); }
  scheduleOverviewMask();
  state.dirty = true;
  return true;
}

// Tiles of the current chunk window (the detail stream's radius and centre), nearest first.
function hiresWindow() {
  const radius = chunkRadius();
  const { x, z } = state.fly?.mode === 'fly' ? camera.position : controls.target;
  const ccx = Math.floor(x / 16);
  const ccz = Math.floor(z / 16);
  const n = hires.index.tileSize / 16;
  const tiles = new Map();
  for (let dz = -radius; dz <= radius; dz += 1) {
    for (let dx = -radius; dx <= radius; dx += 1) {
      if (dx * dx + dz * dz > radius * radius) continue;
      const key = `${Math.floor((ccx + dx) / n)},${Math.floor((ccz + dz) / n)}`;
      if (!hires.index.tiles.has(key)) continue;
      const d = Math.hypot(dx, dz);
      if (!tiles.has(key) || d < tiles.get(key)) tiles.set(key, d);
    }
  }
  return [...tiles].sort((a, b) => a[1] - b[1]).map(([key]) => key);
}

function updateHires() {
  if (!hires.index || !hiresAtlas?.active || !hires.requests) return;
  // A tile gone from the index is dropped; a tile with a new revision stays until the new one is attached.
  for (const key of [...hires.tiles.keys()]) if (!hires.index.tiles.has(key)) dropHires(key);
  // Same byte budget as the detail chunks; tiles beyond it wait like budget-limited chunks.
  let budget = 0;
  const allowed = new Set();
  for (const key of hiresWindow()) {
    const entry = hires.tiles.get(key);
    budget += entry ? entry.bytes : 4 * 1024 * 1024;
    if (budget > GEOMETRY_BUDGET) break;
    allowed.add(key);
  }
  hiresWantedSet = allowed;
  for (const key of [...hires.tiles.keys()]) if (!allowed.has(key)) dropHires(key);
  // Wanted = allowed tiles whose current revision is not shown yet (missing, evicted or an older revision).
  hires.requests.setDesired(new Map([...allowed]
    .filter((key) => hires.tiles.get(key)?.sha !== hires.index.tiles.get(key))
    .map((key) => [key, hires.index.tiles.get(key)])));
  updateWorldCaves();
}

// Underground geometry is loaded only during a height cut, from its configured store.
// It does not replace the public surface pointer or invent underground coverage.
let WORLD_CAVES = new URLSearchParams(window.location?.search ?? '').get('worldCaves') !== '0';
function desiredHiresBase(){return `${DATA_URL}${WORLD_CAVES&&HIRES_IMMUTABLE&&state.cut!==null?'cave-geometry':'hires'}/`;}
let worldCaves = null;
const worldCaveMeshes = new Map();
function removeWorldCaveMesh(key) {
  const mesh = worldCaveMeshes.get(key);
  if (!mesh) return;
  scene.remove(mesh); mesh.geometry.dispose(); mesh.material.dispose();
  worldCaveMeshes.delete(key); state.dirty = true;
}
function updateWorldCaves() {
  if (!WORLD_CAVES) return;
  worldCaves ??= createWorldCaveOverlay({fetch,baseUrl:`${DATA_URL}caves/`,sha256Hex,
    attach(key, arrays) {
      const geometry = new THREE.BufferGeometry();
      const material = new THREE.MeshBasicMaterial({vertexColors:true,side:THREE.DoubleSide});
      try {
        // Match the detail caps: capArrays returns sRGB, vertex attributes are linear.
        for(let i=0;i<arrays.colors.length;i++){
          const c=arrays.colors[i];arrays.colors[i]=c<0.04045?c/12.92:((c+0.055)/1.055)**2.4;
        }
        geometry.setAttribute('position',new THREE.BufferAttribute(arrays.positions,3));
        geometry.setAttribute('color',new THREE.BufferAttribute(arrays.colors,3));
        geometry.setIndex(new THREE.BufferAttribute(arrays.indices,1));
        const mesh = new THREE.Mesh(geometry,material);
        removeWorldCaveMesh(key); scene.add(mesh); worldCaveMeshes.set(key,mesh); state.dirty=true;
      } catch(error) { geometry.dispose(); material.dispose(); throw error; }
    },
    drop:removeWorldCaveMesh,
    onStatus(info) {
      let note=$('world-cave-state');
      if(!note){note=document.createElement('p');note.id='world-cave-state';note.className='note';$('cut-value').parentElement.after(note);}
      note.hidden=state.cut===null;
      note.dataset.state=info.state;
      note.textContent=`Podzemní vrstva: ${info.pairs} dlaždic v indexu; načtené uzávěry ${info.resident}/${info.wanted}. `
        +`Paměť dat ${(info.bytes/1048576).toFixed(1)} MiB, řezů ${(info.meshBytes/1048576).toFixed(1)} MiB. `
        +(info.state==='loading'?'Načítám index. ':info.state==='unavailable'?'Pro tuto geometrii není podzemní index. ':'')
        +(info.error?`Chyba: ${info.error}. `:'')
        +(info.budgetLimited?'Část uzávěrů čeká na volnou paměť. ':'')
        +'Částečné modované tvary mají zjednodušený řez. Pokrytí světa najdeš v panelu aktualizací.';
    },
  });
  const level=$('opt-cut-cap').checked&&Number.isInteger(state.cut)&&state.cut>=0&&state.cut<=255?state.cut:null;
  const maskedChunks=new Set([...capReady].filter(([key,ready])=>detailResident.has(key)&&ready.level===level&&ready.file===state.blockIndex?.get(key)).map(([key])=>key));
  try { worldCaves.update({geometry:hires.index,tiles:hires.tiles,keys:[...hiresWantedSet],level,maskedChunks}); }
  catch(error) { console.warn('world cave caps:',error); }
}

// One index request at a time, bounded like the tiles (the timeout also covers the JSON body). A new index becomes
// active only together with its verified atlas: while the atlas loads, and if it fails (fetch, timeout, SHA-256
// mismatch of the mutable atlas.png), the previous index, atlas, materials and tiles stay; the next poll retries.
const HIRES_INDEX_TIMEOUT_MS = 15_000;
async function loadHiresIndex() {
  if (hires.indexLoading) return;
  const sourceBase=desiredHiresBase();
  hires.indexLoading = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), HIRES_INDEX_TIMEOUT_MS);
  try {
    let index;
    if (HIRES_IMMUTABLE) {
      // Pointer + content-addressed index, both bodies inside this timeout; an unchanged pointer keeps everything.
      index = await loadImmutableHiresIndex({ baseUrl: sourceBase, sha256Hex, current:hires.sourceBase===sourceBase?hires.index:null, expectedWorld: state.mapIdentity,
        fetch: (url, init) => fetch(url, { ...init, signal: controller.signal }) });
      if (state.mapView && index.context?.caves !== false) throw new Error('Samostatný povrch vyžaduje povrchovou geometrii.');
      if(sourceBase!==desiredHiresBase())return;
      if(sourceBase.endsWith('/cave-geometry/')&&index.context?.caves!==true)throw new Error('Podzemní úložiště neobsahuje podzemní geometrii.');
      if (index === hires.index) return;
    } else {
      const res = await fetch(`${DATA_URL}hires/index.json`, { cache: 'no-store', signal: controller.signal });
      if (!res.ok) return;
      index = parseHiresIndex(await res.json());
    }
    clearTimeout(timer);   // the atlas has its own timeout (loadAtlas)
    const prepared = await hiresAtlas.prepare(index, sourceBase);
    if(sourceBase!==desiredHiresBase()){hiresAtlas.discard(prepared);return;}
    if (!prepared.switched) {
      hires.index = index;   // same atlas revision: materials and loaded tiles are reused
      hires.sourceBase=sourceBase;
    } else if (!hiresAtlas.commit(prepared, () => {
      // Atlas switch: every tile built for the previous atlas leaves before its materials/texture are released;
      // requests started against it are cancelled (never attached), the sprite layout is read again.
      hires.requests.setDesired(new Map());
      for (const key of [...hires.tiles.keys()]) dropHires(key);
      hiresSprites = null;
      hires.index = index;
      hires.sourceBase=sourceBase;
    })) {
      return;
    }
    showHiresPublication(index);
    if (state.mapView) scheduleHiresPanel();
    showCaveCoverage();
    updateHires();
  } catch (err) {
    console.warn('hires index:', err);
    if (state.mapView && !hires.index) setStatus('error', 'Dimenzi nelze načíst', [String(err.message)]);
  } finally {
    clearTimeout(timer);
    hires.indexLoading = false;
    if(sourceBase!==desiredHiresBase())queueMicrotask(loadHiresIndex);
  }
}

function showHiresPublication(index) {
  if (!index) return;
  // These badges describe the legacy detail sample, not the streamed world store.
  for (const id of ['sample-badge', 'sample-note', 'sample-flag']) $(id).hidden = true;
  if(WORLD_CAVES&&index.context?.caves===true){
    $('title').textContent='UIE · podzemí světa';document.title=$('title').textContent;
    return; // The surface publication panel continues to describe its own store.
  }
  // Detail generation refreshes must not relabel the loaded whole-world surface as a sample.
  const publication = describeHiresPublication(index, time => fmtDate.format(new Date(time)));
  $('title').textContent = state.mapView?.title ?? publication?.title ?? 'UIE · mapa světa';
  document.title = $('title').textContent;
  if (publication) {
    let note = $('hires-publication');
    if (!note) { note=document.createElement('p');note.id='hires-publication';note.className='note';$('hires-state').closest('dl').before(note); }
    note.textContent = publication.text;
  }
}

function startHires() {
  hires.onTileAttached = finishPendingGoto;
  hiresAtlas ??= createHiresAtlas({
    loadAtlas: (url, sha) => loadAtlas(url, undefined, sha),
    makeMaterials: makeHiresMaterials,
    releaseAtlas,
    disposeMaterials: (materials) => { for (const material of Object.values(materials)) material.dispose(); },
  });
  hires.requests ??= createHiresRequests({
    concurrency: HIRES_IMMUTABLE ? 3 : 6,
    // The atlas set current at request start travels with the tile, so attachHiresTile can refuse a tile that
    // was requested against an earlier index/atlas.
    fetchTile: async (key, sha, signal) => {
      const atlasGeneration = hiresAtlas.generation;
      if (HIRES_IMMUTABLE) {
        // Pin the full object of the CURRENT index now (refused if it is no longer the sha12 revision); the
        // browser inflates the gzip transfer once, decodeHiresTileCompact gets the raw UIH1.
        const object = pinImmutableObject(hires.index, key, sha);
        const res = await fetch(immutableObjectUrl(hires.sourceBase, object), { signal });
        return { ok: res.ok, status: res.status, headers: res.headers,
          arrayBuffer: async () => ({ buffer: await res.arrayBuffer(), atlasGeneration, object }) };
      }
      const res = await fetch(`${DATA_URL}hires/tiles/${key.replace(',', '_')}.uih?v=${sha}`, { signal });
      return { ok: res.ok, status: res.status,
        arrayBuffer: async () => ({ buffer: await res.arrayBuffer(), atlasGeneration }) };
    },
    decodeTile: ({ buffer, atlasGeneration, object }) => ({ tile: decodeHiresTileCompact(buffer), atlasGeneration, object }),
    onTile: attachHiresTile,
    onError: (key, _sha, error, count) => console.warn(`hires ${key} (pokus ${count}):`, error),
    onSettled: () => { updateHires(); scheduleHiresPanel(); },
  });
  if (!hires.wired) {
    hires.wired = true;
    $('view-distance').addEventListener('change', () => updateHires());   // same window as the detail chunks
    controls.addEventListener('change', () => { clearTimeout(hires.moveTimer); hires.moveTimer = setTimeout(updateHires, 200); });
    setInterval(() => { if (!document.hidden) updateHires(); }, 2000);   // fly mode moves the camera directly
    setInterval(() => { if (!document.hidden) loadHiresIndex(); }, HIRES_INDEX_POLL_MS);   // the world build grows
    // A sleeping tab may hold a retired generation. Refresh on return instead of
    // waiting for the next poll; loadHiresIndex keeps the last good map on failure.
    document.addEventListener('visibilitychange', () => { if (!document.hidden) loadHiresIndex(); });
  }
  loadHiresIndex();
}

// ---------------------------------------------------------------- players (players.js, UIE_PLAYERS1)
// Optional: /data/players.json exists only when the server is given a players file. Positions are as of the last
// world save. Labels are screen-sized sprites drawn over the terrain; the list moves the view to a player.
const PLAYERS_POLL_MS = 30_000;
const PLAYER_COLORS = { online: '#3ccf6e', recent: '#e8c547', old: '#9aa0a6' };
const players = { list: [], group: new THREE.Group(), timer: null, wired: false };

function playerSprite(player, now) {
  const text = markerLabel(player, now);
  const color = PLAYER_COLORS[staleness(player, now)];
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const font = '600 28px system-ui, sans-serif';
  ctx.font = font;
  const width = Math.ceil(ctx.measureText(text).width) + 44;
  canvas.width = width;
  canvas.height = 48;
  ctx.font = font;
  ctx.fillStyle = 'rgba(15, 18, 24, 0.82)';
  ctx.beginPath();
  ctx.roundRect(0, 0, width, 48, 12);
  ctx.fill();
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(20, 24, 8, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#ffffff';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 36, 25);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, sizeAttenuation: false, transparent: true });
  const sprite = new THREE.Sprite(material);
  sprite.center.set(0.5, 0);
  sprite.scale.set(0.045 * width / 48, 0.045, 1);
  sprite.position.set(player.x, player.y + 2.2, player.z);
  sprite.renderOrder = 10;
  return sprite;
}

function showPlayers() {
  for (const child of [...players.group.children]) {
    players.group.remove(child);
    child.material.map?.dispose();
    child.material.dispose();
  }
  const now = Date.now();
  const visible = $('opt-players').checked;
  players.group.visible = visible;
  for (const p of players.list) players.group.add(playerSprite(p, now));
  $('players-list').replaceChildren(...players.list.map((p) => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    const dot = document.createElement('span');
    dot.className = 'dot';
    dot.style.background = PLAYER_COLORS[staleness(p, now)];
    button.append(dot, `${markerLabel(p, now)} · X ${Math.round(p.x)} Z ${Math.round(p.z)}`);
    button.addEventListener('click', () => focusCell({ x: Math.floor(p.x), y: Math.floor(p.y), z: Math.floor(p.z),
      name: markerLabel(p, now), id: '–', meta: '–', player: true }));
    li.append(button);
    return li;
  }));
  $('players').hidden = false;
  state.dirty = true;
}

async function loadPlayers(force = false) {
  if (document.hidden && !force) return;   // polling pauses in a hidden tab; the first load always runs
  let doc;
  try {
    const res = await fetch(`${DATA_URL}players.json`, { cache: 'no-store' });
    if (!res.ok) { if (!players.list.length) $('players').hidden = true; return; }
    doc = await res.json();
  } catch {
    return;   // keep the last valid data
  }
  try {
    players.list = playersInDimension(parsePlayers(doc).players, Number(state.mapIdentity?.dimension ?? 0));
  } catch (err) {
    console.error(err);
    return;
  }
  showPlayers();
}

function startPlayers() {
  if (!players.wired) {
    players.wired = true;
    scene.add(players.group);
    $('opt-players').addEventListener('change', () => { players.group.visible = $('opt-players').checked; state.dirty = true; });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) loadPlayers(); });
    // Labels age ("před 5 min") even without new data.
    setInterval(() => { if (players.list.length && !document.hidden) showPlayers(); }, 60_000);
  }
  loadPlayers(true);
  clearInterval(players.timer);
  players.timer = setInterval(loadPlayers, PLAYERS_POLL_MS);
}

async function startOverview() {
  if (await loadOverviewBuild()) {
    const far = wholeWorldDistance(overview.index);
    controls.maxDistance = Math.max(controls.maxDistance, far * 1.6);
    camera.far = Math.max(camera.far, far * 5);
    camera.updateProjectionMatrix();
    $('opt-overview').disabled = false;
    $('overview-note').hidden = false;
    updateOverview();
  }
  // A newer build replaces the tiles in place: old tiles stay until the new set around the camera is loaded.
  setInterval(async () => { if (overview.index && await loadOverviewBuild()) updateOverview(); }, OVERVIEW_POLL_MS);
}

// Load the published overview pointer and index; true if a (new) build is now active.
async function loadOverviewBuild() {
  let pointer;
  try { pointer = await fetchJson(`${DATA_URL}overview.json`, TIMEOUT_MS.revision); }
  catch { return false; }   // no overview published: detail-only map as before
  if (!pointer || pointer.format !== 'UIE_OVERVIEW_POINTER1' || !/^[0-9a-f]{64}$/.test(pointer.build ?? '')) return false;
  if (pointer.build === overview.build) return false;
  const base = `${DATA_URL}overviews/${pointer.build}/`;
  try {
    overview.index = validateOverviewIndex(await fetchJson(`${base}index.json`, TIMEOUT_MS.manifest));
  } catch (err) {
    console.warn('overview unavailable:', err.message);
    return false;
  }
  overview.base = base;
  overview.build = pointer.build;
  return true;
}

function scheduleOverview() {
  if (!overview.index || overview.timer !== null) return;
  overview.timer = setTimeout(() => { overview.timer = null; updateOverview(); }, 250);
}

function removeOverviewTile(key) {
  const mesh = overview.meshes.get(key);
  if (!mesh) return;
  scene.remove(mesh);
  mesh.geometry.dispose();
  mesh.material.map?.dispose();
  mesh.material.dispose();
  overview.meshes.delete(key);
  state.dirty = true;
}

function updateOverview() {
  if (!overview.index) return;
  const enabled = $('opt-overview').checked;
  const target = controls.target;
  const wanted = enabled ? selectOverviewTiles(overview.index, target.x, target.z, camera.position.distanceTo(target)) : [];
  for (const tile of wanted) {   // a new build never reuses old tiles; a tile always loads from its own build
    tile.key = `${overview.build.slice(0, 12)}:${tile.key}`;
    tile.base = overview.base;
  }
  const keys = new Set(wanted.map((t) => t.key));
  const ready = wanted.every((t) => overview.meshes.has(t.key));
  // Keep the previous tiles until every wanted tile is shown, so zooming never leaves an empty world.
  if (ready || !enabled) for (const key of [...overview.meshes.keys()]) if (!keys.has(key)) removeOverviewTile(key);
  for (const tile of wanted) {
    if (overview.meshes.has(tile.key) || overview.loading.has(tile.key)) continue;
    overview.loading.add(tile.key);
    loadOverviewTile(tile).then((mesh) => {
      overview.loading.delete(tile.key);
      if (!mesh) return;
      overview.meshes.set(tile.key, mesh);
      scene.add(mesh);
      state.dirty = true;
      scheduleOverview();   // re-evaluate: old tiles go once the new set is complete
    }, (err) => {
      overview.loading.delete(tile.key);
      overview.failed += 1;
      console.warn('overview tile failed:', tile.key, err.message);
    });
  }
}

async function overviewImage(url) {
  const blob = await fetchWithTimeout(url, TIMEOUT_MS.atlas, (response) => response.blob());
  return createImageBitmap(blob, { premultiplyAlpha: 'none', colorSpaceConversion: 'none' });
}

async function loadOverviewTile(tile) {
  const name = `${tile.base}L${tile.level}/${tile.tx}_${tile.tz}`;
  const [color, heightImage] = await Promise.all([overviewImage(`${name}.png`), overviewImage(`${name}.h.png`)]);
  const size = overview.index.tileSize;
  const canvas = new OffscreenCanvas(size, size);
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(heightImage, 0, 0);
  const heights = ctx.getImageData(0, 0, size, size).data;
  ctx.clearRect(0, 0, size, size);
  ctx.drawImage(color, 0, 0);
  const alpha = ctx.getImageData(0, 0, size, size).data;
  heightImage.close?.();
  color.close?.();
  const { x0, z0, size: span } = tileRect(tile.level, tile.tx, tile.tz, size);
  const n = OVERVIEW_GRID;
  const step = size / n;
  let sum = 0;
  let count = 0;
  for (let i = 0; i < size * size; i += 97) if (alpha[i * 4 + 3]) { sum += heights[i * 4]; count += 1; }
  const fallbackY = count ? sum / count : 64;
  const positions = new Float32Array((n + 1) * (n + 1) * 3);
  const uvs = new Float32Array((n + 1) * (n + 1) * 2);
  const valid = new Uint8Array((n + 1) * (n + 1));   // vertex has present columns around it
  for (let j = 0; j <= n; j += 1) {
    for (let i = 0; i <= n; i += 1) {
      const px = Math.min(size - 1, Math.round(i * step));
      const pz = Math.min(size - 1, Math.round(j * step));
      // Median of the surrounding 5x5 present columns: one tree top or tower must not become a spike.
      const samples = [];
      for (let dz = -2; dz <= 2; dz += 1) {
        for (let dx = -2; dx <= 2; dx += 1) {
          const sx = px + dx;
          const sz = pz + dz;
          if (sx < 0 || sz < 0 || sx >= size || sz >= size) continue;
          const o = (sz * size + sx) * 4;
          if (alpha[o + 3]) samples.push(heights[o]);
        }
      }
      samples.sort((a, b) => a - b);
      const y = samples.length ? samples[samples.length >> 1] + 1 : fallbackY;   // top face of the column
      const v = j * (n + 1) + i;
      valid[v] = samples.length ? 1 : 0;
      positions.set([x0 + (i / n) * span, y - 1, z0 + (j / n) * span], v * 3);
      uvs.set([i / n, j / n], v * 2);
    }
  }
  // Quads with an absent corner are dropped (no stretched walls at the edge of the explored world). Each kept quad
  // remembers the detail chunk under its centre: applyOverviewMask hides it while that chunk's detail is shown.
  const quads = [];
  const quadChunks = [];
  for (let j = 0; j < n; j += 1) {
    for (let i = 0; i < n; i += 1) {
      const a = j * (n + 1) + i;
      if (!(valid[a] && valid[a + 1] && valid[a + n + 1] && valid[a + n + 2])) continue;
      quads.push(a);
      quadChunks.push(`${Math.floor((x0 + ((i + 0.5) / n) * span) / 16)},${Math.floor((z0 + ((j + 0.5) / n) * span) / 16)}`);
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
  geometry.computeBoundingSphere();
  const texture = new THREE.DataTexture(new Uint8Array(alpha.buffer, alpha.byteOffset, alpha.length), size, size,
    THREE.RGBAFormat);
  texture.flipY = false;
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.magFilter = THREE.NearestFilter;
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  texture.generateMipmaps = true;
  texture.needsUpdate = true;
  const material = new THREE.MeshBasicMaterial({ map: texture, alphaTest: 0.5, side: THREE.FrontSide,
    clippingPlanes: state.cut === null ? null : [cutPlane] });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.renderOrder = -1;
  mesh.userData.overview = tile.key;
  mesh.userData.quads = quads;
  mesh.userData.quadChunks = quadChunks;
  mesh.userData.gridN = n;
  applyOverviewMask(mesh);
  return mesh;
}

// Detail chunks currently shown by the chunk stream (plain or LOD): the overview is hidden exactly there, at every
// level, so the coarse heightfield never cuts through real geometry. Updated from showStream (debounced).
const detailResident = new Set();
let overviewMaskTimer = null;

function noteDetailResident(snapshot) {
  const keys = new Set((snapshot.resident ?? []).map((e) => `${e.x},${e.z}`));
  if (keys.size === detailResident.size && [...keys].every((k) => detailResident.has(k))) return;
  detailResident.clear();
  for (const k of keys) detailResident.add(k);
  if (state.cut !== null) scheduleCutCaps();
  refreshHiresMasks();
  scheduleOverviewMask();
}

function scheduleOverviewMask() {
  if (overviewMaskTimer !== null) return;
  overviewMaskTimer = setTimeout(() => {
    overviewMaskTimer = null;
    for (const mesh of overview.meshes.values()) applyOverviewMask(mesh);
    state.dirty = true;
  }, 100);
}

function applyOverviewMask(mesh) {
  const { quads, quadChunks, gridN: n } = mesh.userData;
  const indices = new Uint32Array(quads.length * 6);
  let k = 0;
  for (let q = 0; q < quads.length; q += 1) {
    if (detailResident.has(quadChunks[q]) || hires.chunks.has(quadChunks[q])) continue;
    const a = quads[q];
    indices[k] = a; indices[k + 1] = a + n + 1; indices[k + 2] = a + 1;
    indices[k + 3] = a + 1; indices[k + 4] = a + n + 1; indices[k + 5] = a + n + 2;
    k += 6;
  }
  mesh.geometry.setIndex(new THREE.BufferAttribute(indices.subarray(0, k), 1));
}

// Height cut UI: range from the current bounds; ?cut=<Y> turns it on at load. Kept across generation switches
// (clamped to the new bounds).
function configureCut() {
  if (!state.bounds) return;
  const range = cutRange(state.bounds);
  const slider = $('cut-level');
  slider.min = String(range.min);
  slider.max = String(range.max);
  if (state.cutRequest !== null) {
    state.cut = parseCutParam(state.cutRequest, range);
    state.cutRequest = null;
  } else if (state.cut !== null) {
    state.cut = Math.min(range.max, Math.max(range.min, state.cut));
  }
  if (state.cut !== null) slider.value = String(state.cut);
  else if (!slider.dataset.touched) slider.value = String(Math.min(range.max, Math.max(range.min, 48)));
  $('opt-cut').checked = state.cut !== null;
  $('opt-cut').disabled = false;
  if (!state.cutWired) {
    state.cutWired = true;
    const update = () => {
      slider.dataset.touched = '1';
      state.cut = $('opt-cut').checked ? Number.parseInt(slider.value, 10) : null;
      showCut();
      applyDisplayOptions();
    };
    $('opt-cut').addEventListener('change', update);
    slider.addEventListener('input', update);
    $('opt-cut-cap').addEventListener('change', scheduleCutCaps);
  }
  showCut();
  applyDisplayOptions();
}

function showCut() {
  $('cut-level').disabled = state.cut === null;
  $('cut-value').textContent = describeCut(state.cut);
  showCaveCoverage();
  scheduleCutCaps();
}

let caveCoverage = createCaveCoverage(null);
function showCaveCoverage() {
  const center = state.fly?.mode === 'fly' ? camera.position : controls.target;
  let description = caveCoverage.describe({ x: center.x, z: center.z, cut: state.cut });
  if (WORLD_CAVES && state.cut !== null && hires.index?.context?.caves === true && description.state === 'outside') {
    const key=`${Math.floor(center.x/64)},${Math.floor(center.z/64)}`;
    description={...description,state:'cave-prototype',text:hires.index.tiles.has(key)
      ? 'Podzemní geometrie je v tomto regionu dostupná.'
      : 'Mimo vykreslené podzemní regiony: zde zatím podzemní data nejsou.'};
  }
  let note = $('cave-coverage');
  if (!note) {
    note = document.createElement('p'); note.id = 'cave-coverage'; note.className = 'note';
    $('cut-value').parentElement.after(note);
  }
  note.hidden = !description.visible;
  if (note.textContent !== (description.text ?? '')) note.textContent = description.text ?? '';
  note.dataset.state = description.state;
}

// ---------------------------------------------------------------- closed height cut (cut-cap.js)
// One cap mesh per resident detail chunk with block data, rebuilt when the level, the resident set or the
// generation changes. Voxel classes are cached per block file (64 KB each), so moving the slider costs no download.
const CAP_CLASS_CHUNKS = 160;
const CAP_CONCURRENCY = 4;
const capClassCache = new LruCache(CAP_CLASS_CHUNKS);
const capMeshes = new Map();   // "cx,cz" -> mesh
const capReady = new Map();    // completed detail classifications, including empty slices
const capMaterial = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide });   // unlit: exact colours
let capTimer = null;
let capSeq = 0;

function scheduleCutCaps() {
  if (capTimer !== null) return;
  capTimer = setTimeout(() => {
    capTimer = null;
    if(WORLD_CAVES&&hires.wired&&hires.sourceBase!==desiredHiresBase())loadHiresIndex();
    updateWorldCaves();
    updateCutCaps().catch((err) => console.error(err));
  }, 60);
}

function removeCap(key) {
  capReady.delete(key);
  const mesh = capMeshes.get(key);
  if (!mesh) return;
  scene.remove(mesh);
  mesh.geometry.dispose();
  capMeshes.delete(key);
}

function setCap(key, file, entry, level) {
  removeCap(key);
  const quads = capQuads(entry.classes, entry.width, entry.depth, level);
  if (quads.length === 0) { capReady.set(key,{file,level}); return; }
  // Just below the clipping plane: exposed tops of the cut level keep their own texture.
  const { positions, colors, indices } = capArrays(entry.x, entry.z, quads, clipTop(level) - 0.002);
  // CAP_COLORS are sRGB; three.js treats vertex colours as linear.
  for (let i = 0; i < colors.length; i += 1) {
    const c = colors[i];
    colors[i] = c < 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  const normals = new Float32Array(positions.length);
  for (let i = 1; i < normals.length; i += 3) normals[i] = 1;
  geometry.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  const mesh = new THREE.Mesh(geometry, capMaterial);
  mesh.matrixAutoUpdate = false;
  mesh.userData = { file, level };
  scene.add(mesh);
  capMeshes.set(key, mesh);
  capReady.set(key,{file,level});
}

async function updateCutCaps() {
  const seq = (capSeq += 1);
  const active = state.cut !== null && $('opt-cut-cap').checked && Boolean(state.blockIndex);
  const keys = active ? [...detailResident].filter((k) => state.blockIndex.has(k)) : [];
  const keep = new Set(keys);
  for (const [key, ready] of capReady) {
    if (!keep.has(key) || ready.file !== state.blockIndex?.get(key) || ready.level !== state.cut) removeCap(key);
  }
  state.dirty = true;
  if (!active) { updateWorldCaves(); return; }
  const level = state.cut;
  const center = state.fly?.mode === 'fly' ? camera.position : controls.target;
  const distance = (key) => { const [x, z] = key.split(',').map(Number); return Math.hypot(x * 16 + 8 - center.x, z * 16 + 8 - center.z); };
  const todo = keys.filter((key) => capReady.get(key)?.level !== level).sort((a, b) => distance(a) - distance(b));
  const work = async () => {
    while (todo.length) {
      const key = todo.shift();
      const file = state.blockIndex.get(key);
      let entry = capClassCache.get(file);
      if (!entry) {
        const [cx, cz] = key.split(',').map(Number);
        let chunk;
        try {
          chunk = await loadBlockChunk(cx, cz, file);
        } catch {
          continue;   // no cap for this chunk; the next update retries
        }
        entry = { classes: capClasses(chunk, blockShape), x: chunk.x, z: chunk.z, width: chunk.width, depth: chunk.depth };
        capClassCache.set(file, entry);
      }
      if (seq !== capSeq || state.cut !== level || state.blockIndex?.get(key) !== file) return;
      setCap(key, file, entry, level);
      updateWorldCaves();
      state.dirty = true;
    }
  };
  await Promise.all(Array.from({ length: CAP_CONCURRENCY }, work));
}

function showManifest(m) {
  const title = m.title.trim() || 'UIE mapa';
  $('title').textContent = title;
  document.title = title;
  $('generated').textContent = fmtDate.format(new Date(m.generatedAt));
  $('generated').title = m.generatedAt;
  const { min, max } = m.bounds;
  $('bounds').textContent = `X ${Math.floor(min[0])} … ${Math.ceil(max[0])} · Z ${Math.floor(min[2])} … ${Math.ceil(max[2])} · Y ${Math.floor(min[1])} … ${Math.ceil(max[1])}`;
  for (const id of ['sample-badge', 'sample-note', 'sample-flag']) $(id).hidden = !m.sample;
  setChunkCounts(0, 0, m.chunks.length);

  const cov = m.coverage;
  $('cov-rendered').textContent = fmtNumber.format(cov.renderedBlocks);
  $('cov-unsupported').textContent = fmtNumber.format(cov.unsupportedBlocks);
  $('cov-unsupported').classList.toggle('warn', cov.unsupportedBlocks > 0);
  // Optional: blocks whose renderer produced no faces (not air, not culled hidden blocks).
  const hasEmpty = cov.emptyBlocks !== undefined;
  $('cov-empty-label').hidden = !hasEmpty;
  $('cov-empty').hidden = !hasEmpty;
  $('cov-empty-note').hidden = !hasEmpty || cov.emptyBlocks === 0;
  if (hasEmpty) {
    $('cov-empty').textContent = fmtNumber.format(cov.emptyBlocks);
    $('cov-empty').classList.toggle('warn', cov.emptyBlocks > 0);
  }
  const names = cov.unsupportedNames.slice(0, MAX_LISTED_NAMES);
  if (cov.unsupportedNames.length > names.length) {
    names.push(`… a dalších ${fmtNumber.format(cov.unsupportedNames.length - names.length)}`);
  }
  $('cov-list').replaceChildren(...names.map((name) => {
    const li = document.createElement('li');
    li.textContent = name;
    return li;
  }));
  $('cov-names').hidden = names.length === 0;
  state.unsupportedNames = new Set(cov.unsupportedNames);
  state.findSeq += 1;  // results of a search over the previous manifest are stale
  $('cov-hits').replaceChildren();
  $('cov-find-msg').hidden = true;
  showHiresPublication(hires.index);
}

// ---------------------------------------------------------------- locating unsupported blocks

const MAX_FIND_CHUNKS = 64;
const MAX_FIND_HITS = 40;

// Scans the block data of the detail chunks currently shown (nearest to the view first) for blocks named in the
// coverage list. Only resident chunks are read, so the search costs at most MAX_FIND_CHUNKS block downloads.
async function findUnsupported() {
  const seq = (state.findSeq += 1);
  const msg = $('cov-find-msg');
  const say = (text, warn) => { msg.textContent = text; msg.classList.toggle('warn', warn); msg.hidden = false; };
  $('cov-hits').replaceChildren();
  const names = state.unsupportedNames;
  if (!names?.size) return say('Žádné nepodporované bloky.', false);
  const t = controls.target;
  const chunks = [...detailResident]
    .map((key) => { const [x, z] = key.split(',').map(Number); return { key, x, z, file: state.blockIndex?.get(key) }; })
    .filter((c) => c.file)
    .sort((a, b) => Math.hypot(a.x * 16 + 8 - t.x, a.z * 16 + 8 - t.z) - Math.hypot(b.x * 16 + 8 - t.x, b.z * 16 + 8 - t.z))
    .slice(0, MAX_FIND_CHUNKS);
  if (chunks.length === 0) return say('Nejsou načtené žádné chunky s blokovými daty. Přibližte mapu k detailní oblasti.', true);
  say(`Prohledávám ${chunks.length} načtených chunků…`, false);
  const hits = [];
  let failed = 0;
  for (const c of chunks) {
    let chunk;
    try {
      chunk = await loadBlockChunk(c.x, c.z, c.file);
    } catch {
      failed += 1;
      continue;
    }
    if (seq !== state.findSeq) return;
    hits.push(...findBlocks(chunk, names, MAX_FIND_HITS - hits.length));
    if (hits.length >= MAX_FIND_HITS) break;
  }
  if (seq !== state.findSeq) return;
  const failNote = failed ? ` ${failed} chunků nešlo načíst.` : '';
  if (hits.length === 0) {
    return say(`V ${chunks.length} načtených chunkech nic nenalezeno; mohou ležet mimo zobrazenou část.${failNote}`, failed > 0);
  }
  say(`Nalezeno ${hits.length >= MAX_FIND_HITS ? `prvních ${MAX_FIND_HITS}` : hits.length} výskytů `
    + `(${chunks.length} chunků). Kliknutím přesunete pohled.${failNote}`, failed > 0);
  $('cov-hits').replaceChildren(...hits.map((h) => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = `${h.name}:${h.meta} · X ${h.x} Y ${h.y} Z ${h.z}`;
    button.addEventListener('click', () => focusCell(h));
    li.append(button);
    return li;
  }));
}

function focusCell(h) {
  if (state.fly?.mode === 'fly') return;
  const offset = camera.position.clone().sub(controls.target);
  const near = Math.min(offset.length(), 40);
  controls.target.set(h.x + 0.5, h.y + 0.5, h.z + 0.5);
  camera.position.copy(controls.target).add(offset.setLength(Math.max(near, 12)));
  state.userMoved = true;
  state.dirty = true;
  showPick({ cell: h, block: { name: h.name, id: h.id, meta: h.meta },
    message: h.player ? 'Poloha hráče k poslednímu uložení hry.'
      : 'Nalezeno v blokových datech; renderer tento blok kreslí jako náhradní kostku.' });
}

// ---------------------------------------------------------------- server render status

// Separate from the browser chunk download status above. Values come only from render-status.json (the spawn detail
// generation, shown under "Detail okolí spawnu"; the whole-world surface has its own status below);
// updatedAt is shown as the status heartbeat, never as the map update time (that is manifest.generatedAt).
function setRenderRow(id, text) {
  const shown = text !== null && text !== undefined;
  $(id).textContent = shown ? text : '';
  $(id).hidden = !shown;
  $(`${id}-label`).hidden = !shown;
}

function showRenderStatus(view) {
  const stateEl = $('render-state');
  const message = $('render-message');
  const say = (text) => { message.textContent = text ?? ''; message.hidden = !text; };
  $('render-bar').hidden = true;
  if (view.kind !== 'ok') {
    stateEl.textContent = 'Stav renderu není k dispozici';
    stateEl.dataset.tone = 'muted';
    for (const id of ['render-progress', 'render-eta', 'render-published', 'render-heartbeat']) setRenderRow(id, null);
    say(view.reason === 'missing' ? 'Server stav renderu nezveřejňuje (starší export).'
      : `Stav renderu nelze přečíst: ${view.message}`);
    return;
  }
  const d = describeRenderStatus(view.record, Date.now());
  stateEl.textContent = d.label;
  stateEl.dataset.tone = d.tone;
  setRenderRow('render-progress', d.progress?.text ?? null);
  setRenderRow('render-eta', d.eta);
  setRenderRow('render-published', d.published === null ? null : fmtDate.format(new Date(d.published)));
  setRenderRow('render-heartbeat', fmtDate.format(new Date(d.heartbeat)));
  const percent = d.progress?.percent ?? null;
  if (percent !== null) {
    $('render-bar').hidden = false;
    $('render-bar').setAttribute('aria-valuenow', String(percent));
    $('render-bar-fill').style.width = `${percent}%`;
  }
  say([d.note, d.message].filter(Boolean).join(' ') || null);
}

function startRenderStatus() {
  const poller = createRenderStatusPoller({
    fetchStatus: (signal) => fetchRenderStatus(RENDER_STATUS_URL, { timeoutMs: RENDER_STATUS_TIMEOUT_MS, signal }),
    onUpdate: showRenderStatus,
    document,
  });
  poller.start();
  return poller;
}

// Whole-world surface: values only from hires/status.json. Times are shown only when the file has them; the start
// of a run is not a publication, and done / total are the region jobs of this run, not coverage of the world.
function showHiresStatus(view) {
  const stateEl = $('hires-state');
  const message = $('hires-message');
  const say = (text) => { message.textContent = text ?? ''; message.hidden = !text; };
  const rows = ['hires-progress', 'hires-eta', 'hires-started', 'hires-finished'];
  if (view.kind !== 'ok') {
    stateEl.textContent = 'Stav povrchu světa není k dispozici';
    stateEl.dataset.tone = 'muted';
    for (const id of rows) setRenderRow(id, null);
    say(view.reason === 'missing' ? 'Server stav aktualizace povrchu světa neposkytuje.'
      : `Stav povrchu světa nelze přečíst: ${view.message}`);
    return;
  }
  const d = describeHiresStatus(view.record, Date.now(), view.unchangedForMs);
  stateEl.textContent = HIRES_IMMUTABLE ? `Běh rendereru: ${d.label}` : d.label;
  stateEl.dataset.tone = d.tone;
  setRenderRow('hires-progress', d.progress);
  setRenderRow('hires-eta', d.etaSeconds === null ? null : `přibližně ${formatDuration(d.etaSeconds)}`);
  setRenderRow('hires-started', fmtDate.format(new Date(d.started)));
  setRenderRow('hires-finished', d.finished === null ? null : fmtDate.format(new Date(d.finished)));
  $('hires-finished-label').textContent = d.finishedLabel;
  say([d.note, d.progress ? d.scope : null].filter(Boolean).join(' ') || null);
}

function startHiresStatus() {
  let modelLast=null;
  const model = createRenderStatusPoller({
    fetchStatus: signal => fetchRenderStatus(`${DATA_URL}model-status.json`, {timeoutMs:RENDER_STATUS_TIMEOUT_MS,signal}),
    validateStatus:data=>data,document,
    onUpdate:view=>{
      const panel=modelStatusPanel(view,modelLast);modelLast=panel.last;
      const d=panel.description;
      let note=$('model-render-progress');
      if(!note && !d)return;
      if(!note){
        note=document.createElement('p');note.id='model-render-progress';note.className='note';
        note.setAttribute('aria-live','polite');$('hires-message').after(note);
      }
      note.hidden=!d;note.textContent=d?.text??'';note.dataset.tone=d?.tone??'muted';
    },
  });
  model.start();
  const cave = WORLD_CAVES ? createRenderStatusPoller({
    fetchStatus: signal => fetchRenderStatus(`${DATA_URL}cave-status.json`, {timeoutMs:RENDER_STATUS_TIMEOUT_MS,signal}),
    validateStatus:data=>data,document,
    onUpdate:view=>{
      let note=$('cave-batch-progress');
      if(!note){
        const heading=document.createElement('h3');heading.className='note';
        heading.textContent='Podzemí celého světa';heading.style.cssText='margin:14px 0 6px;font-weight:600';
        note=document.createElement('p');note.id='cave-batch-progress';note.className='note';
        // After the surface candidate note when it already exists, so it never lands under this heading.
        note.setAttribute('aria-live','polite');($('model-render-progress')??$('hires-message')).after(heading,note);
      }
      const description=describeCaveStatus(view.kind==='ok'?view.record:null);
      note.textContent=description.text;note.dataset.tone=description.tone;
    },
  }):null;
  cave?.start();
  const publisher = HIRES_IMMUTABLE ? createRenderStatusPoller({
    fetchStatus: signal => fetchRenderStatus(`${DATA_URL}hires/publisher-status.json`,
      { timeoutMs: RENDER_STATUS_TIMEOUT_MS, signal }),
    validateStatus: data => data,
    onUpdate: view => {
      let note = $('hires-publisher-progress');
      if (!note) {
        note = document.createElement('p'); note.id = 'hires-publisher-progress'; note.className = 'note';
        $('hires-state').closest('dl').before(note);
      }
      const description = describePublisherStatus(view.kind === 'ok' ? view.record : null);
      note.textContent = description.text; note.dataset.tone = description.tone;
    },
    document,
  }) : null;
  publisher?.start();
  const poller = createHiresStatusPoller({
    fetchStatus: (signal) => fetchRenderStatus(HIRES_STATUS_URL, { timeoutMs: RENDER_STATUS_TIMEOUT_MS, signal }),
    onUpdate: showHiresStatus,
    document,
  });
  poller.start();
  return {...poller, dispose() { model.dispose(); cave?.dispose(); publisher?.dispose(); poller.dispose(); }};
}

function startCollisionPreparationStatus() {
  let last=null;
  const poller=createRenderStatusPoller({
    fetchStatus:signal=>fetchRenderStatus(`${DATA_URL}collision-status.json`,{timeoutMs:RENDER_STATUS_TIMEOUT_MS,signal}),
    validateStatus:data=>data,document,
    onUpdate:view=>{
      const panel=collisionStatusPanel(view,last);last=panel.last;
      const note=$('collision-preparation-progress');
      note.hidden=!panel.description;note.textContent=panel.description?.text??'';
      note.dataset.tone=panel.description?.tone??'muted';
    },
  });
  poller.start();return poller;
}

function startOverviewStatus() {
  const poller = createRenderStatusPoller({
    fetchStatus: signal => fetchRenderStatus(`${DATA_URL}overview-status.json`, { timeoutMs: RENDER_STATUS_TIMEOUT_MS, signal }),
    validateStatus: validateOverviewStatus,
    document,
    onUpdate(view) {
      const label = $('overview-state'), note = $('overview-message');
      const rows = ['overview-stage', 'overview-finished', 'overview-published', 'overview-counts'];
      if (view.kind !== 'ok') {
        label.textContent = 'Stav přehledu není k dispozici'; label.dataset.tone = 'muted';
        for (const id of rows) setRenderRow(id, null);
        note.textContent = 'Samotná mapa zůstává dostupná. Stav se zkusí načíst znovu.';
        return;
      }
      const d = describeOverviewStatus(view.record, Date.now());
      label.textContent = d.label; label.dataset.tone = d.tone;
      setRenderRow('overview-stage', d.stage);
      setRenderRow('overview-finished', d.finished === null ? null : fmtDate.format(new Date(d.finished)));
      setRenderRow('overview-published', d.published === null ? null : fmtDate.format(new Date(d.published)));
      setRenderRow('overview-counts', [d.changed, d.failed].filter(Boolean).join(' · ') || null);
      note.textContent = d.note ?? '';
    },
  });
  poller.start();
  return poller;
}

// ---------------------------------------------------------------- first-person fly

function setFlyMessage(text, tone = 'info') {
  $('fly-message').textContent = text ?? '';
  $('fly-message').hidden = !text;
  $('fly-message').classList.toggle('warn', tone === 'warn');
  $('fly-overlay-message').textContent = text
    ?? 'Klikněte na tlačítko nebo do mapy a ovládejte pohled myší.';
}

function showFlyUi() {
  const flying = state.fly?.mode === 'fly';
  const locked = flying && state.fly.locked;
  document.body.classList.toggle('fly-mode', flying);
  $('mode-orbit').setAttribute('aria-pressed', String(!flying));
  $('mode-fly').setAttribute('aria-pressed', String(flying));
  $('fly-panel').hidden = !flying;
  $('fly-overlay').hidden = !flying || locked || state.fly.dragMode;
  $('fly-crosshair').hidden = !locked;
  // Orbit-only actions would act on the frozen orbit state; Map is always one click away.
  for (const id of ORBIT_ONLY_CONTROLS) $(id).disabled = flying;
}

// Walk collisions read the decoded block data (same files as block picking). A missing chunk starts its download
// and counts as unknown (a wall; gravity waits on it); failed downloads are retried after a pause.
const blockRetryAt = new Map();
let collisionPilot = null;
let collisionStrict = false;

function refreshCollisionPilot() {
  if (!collisionPilot || !camera) return;
  const pair = collisionPilot.pair;
  const key = pair ? `${pair.x / 64},${pair.z / 64}` : '';
  const atlasSha=visibleHiresAtlasSha();
  collisionPilot.update({walking:state.fly?.walking && !document.hidden && (collisionStrict || state.cut===null), x:camera.position.x, z:camera.position.z,
    geometrySha:hires.tiles.get(key)?.sha,
    geometryAt:(tileKey)=>visibleHiresAtlasSha()===atlasSha?hires.tiles.get(tileKey)?.sha:undefined, atlasSha});
  if (state.fly?.walking) {
    if(!collisionStrict){
      $('walk-help').textContent = (state.cut!==null
        ? 'Podzemní kolize jsou přibližné. Plavání vyžaduje zapnuté uzávěry řezu a načtená podzemní data.'
        : collisionPilot.ready ? 'Pro toto místo jsou načtená bloková data kolizí; tvary některých modů jsou přibližné.'
          : 'Pro toto místo používám přibližné kolize z geometrie mapy. Dostupná bloková data se načtou automaticky.')
        +' WASD pohyb · Mezerník skok / plavání · G létání.';
    } else $('walk-help').textContent = collisionPilot.ready
      ? 'Zkušební chůze: bloková data oblasti jsou načtená. WASD pohyb, mezerník skok / plavání, G létání. Za hranicí zkušební oblasti se chůze zastaví.'
      : 'Zkušební chůze čeká na odpovídající bloková data. Mimo připravenou oblast použijte létání (G).';
  }
}

function startCollisionPilot() {
  const params = new URLSearchParams(window.location?.search ?? '');
  const pairHash = params.get('collisionPair'), indexHash = params.get('collisionIndex');
  collisionStrict = Boolean(pairHash || indexHash || params.get('collision')==='1');
  if(params.get('collision')==='0' && !pairHash && !indexHash)return;
  const follow = !pairHash && !indexHash;
  const factory = follow ? createCollisionFollower : indexHash ? createCollisionWorld : createCollisionSource;
  collisionPilot = factory({base:`${DATA_URL}collision/`,pairHash,indexHash,
    onChange:()=>{state.dirty=true;refreshCollisionPilot();},
    onError:(_key,_sha,error)=>console.warn('collision pilot:',error)});
  if (!follow) (indexHash ? collisionPilot.loadIndex() : collisionPilot.loadPair()).catch(error=>console.warn('collision pilot:',error));
  setInterval(refreshCollisionPilot,250);
  document.addEventListener('visibilitychange',refreshCollisionPilot);
}

// Surface height of a world column from the loaded whole-world tile: undefined while the tile is not loaded,
// -1 when the column has no opaque top face.
function hiresColumn(x, z) {
  if (!hires.index) return undefined;
  const size = hires.index.tileSize;
  const tx = Math.floor(x / size);
  const tz = Math.floor(z / size);
  const entry = hires.tiles.get(`${tx},${tz}`);
  if (!entry) return undefined;
  return entry.heights[(Math.floor(z) - tz * size) * size + (Math.floor(x) - tx * size)];
}


// Read only detail blocks from this generation; never mix a neighboring collision export into a detail shape.
function detailWalkBlockAt(x, y, z) {
  if (y < 0 || y > 255) return undefined;
  const cx=chunkCoord(x),cz=chunkCoord(z),file=state.blockIndex?.get(`${cx},${cz}`);
  if (!file) return undefined;
  const chunk=blockCache.get(file);
  if (!chunk) {
    const now=performance.now();
    if (!blockLoads.has(file) && (blockRetryAt.get(file) ?? 0) <= now) {
      loadBlockChunk(cx,cz,file).then(()=>{state.dirty=true;},
        ()=>{blockRetryAt.set(file,performance.now()+5000);});
    }
    return undefined;
  }
  return blockAt(chunk,x,y,z) ?? undefined;
}

function walkShapeAt(x, y, z) {
  const cx = chunkCoord(x);
  const cz = chunkCoord(z);
  const file = state.blockIndex?.get(`${cx},${cz}`);
  if (!file) {
    if (collisionPilot && (collisionStrict || state.cut===null)) {
      const pair=collisionPilot.pair;
      const choice=chooseCollision({source:collisionPilot,strict:collisionStrict,x,y,z,
        visibleGeometrySha:pair?hires.tiles.get(`${pair.x/64},${pair.z/64}`)?.sha:undefined,
        visibleAtlasSha:visibleHiresAtlasSha()},(_name,_meta,readNeighbor)=>blockShapeAt(readNeighbor,x,y,z));
      if(choice.handled)return choice.shape;
    }
    // Opaque surface intervals preserve outdoor air under canopies and overhangs.
    const size=hires.index?.tileSize;
    if (!size) return undefined;
    const tx=Math.floor(x/size), tz=Math.floor(z/size);
    const entry=hires.tiles.get(`${tx},${tz}`);
    // UIC1 is ambiguous for air/partial models and fallback cubes. Only its liquid
    // classes supplement the existing approximation, and only for the exact cave snapshot.
    if (WORLD_CAVES && state.cut !== null && hires.index.context?.caves === true && $('opt-cut-cap').checked) {
      const classification=worldCaves?.classAt(x,y,z,{
        geometryIndexSha256:hires.index.indexSha256,geometryObject:entry?.object,atlasSha256:hires.index.atlasSha256,
      });
      if(classification===2 || classification===3) return blockShape(classification===2?'minecraft:water':'minecraft:lava',0);
    }
    const walk=entry?.walk;
    const occupied=hiresWalkCell(walk, x-tx*size, y, z-tz*size);
    return occupied===undefined ? undefined : blockShape(occupied ? 'minecraft:stone' : 'minecraft:air');
  }
  return blockShapeAt(detailWalkBlockAt,x,y,z);
}

// Walking starts right under the camera (anywhere in the world): high above the ground the body is dropped onto
// the terrain below; near the ground it starts where it is. Ground = detail meshes, else the whole-world tile surface.
function walkStart(feet) {
  const detail = terrainHeight(feet.x, feet.z);
  const tile = hiresColumn(feet.x, feet.z);
  const g = detail ?? (tile !== undefined && tile >= 0 ? tile : null);
  if (g === null || feet.y < g + 24) return null;   // unknown yet: start in place, gravity waits for the data
  return { x: feet.x, y: g + 0.5, z: feet.z };
}

function showWalkUi() {
  const walking = Boolean(state.fly?.walking);
  $('fly-walk').setAttribute('aria-pressed', String(walking));
  $('fly-walk').textContent = walking ? 'Přepnout na létání (G)' : 'Chůze s kolizemi (G)';
  for (const id of ['fly-start', 'fly-overlay-start']) {
    $(id).textContent = walking ? 'Spustit / pokračovat v chůzi' : 'Spustit / pokračovat v létání';
  }
  $('fly-overlay').setAttribute('aria-label', walking ? 'Chůze' : 'Létání');
  $('fly-overlay-title').textContent = walking ? 'Chůze (s kolizemi)' : 'Létání (bez kolizí)';
  $('fly-help').hidden = walking;
  $('walk-help').hidden = !walking;
  $('fly-speed').textContent = walking
    ? `${fmtNumber.format(WALK_SPEED)} bloků/s · Shift ×${fmtNumber.format(SPRINT_FACTOR)}`
    : `${fmtNumber.format(state.flyMotion.speed)} bloků/s · Shift ×${fmtNumber.format(state.flyMotion.boost)}`;
}

function startFlyMode() {
  const motion = createFlyMotion({ speed: 10, boost: 4 });
  state.flyMotion = motion;
  state.walker = createWalker({ shapeAt: walkShapeAt });
  state.fly = createFirstPersonView({
    camera, controls, canvas: renderer.domElement, window, document, motion,
    walker: state.walker, eyeHeight: BODY.eye, walkStart,
    onChange: (kind) => {
      if (kind === 'move' || kind === 'walk' || kind === 'mode') cancelPendingGoto();
      state.dirty = true;
      if (kind === 'move' || kind === 'mode' || kind === 'walk') scheduleChunkWindow();
      if (kind === 'lock' || kind === 'mode') showFlyUi();
      if (kind === 'walk' || kind === 'mode') showWalkUi();
      if (kind === 'move' || kind === 'walk' || kind === 'mode') refreshCollisionPilot();
    },
    onModeChange: (mode) => { if (mode === 'fly') closePick(); showFlyUi(); },
    onMessage: setFlyMessage,
  });
  showWalkUi();
  $('fly-walk').addEventListener('click', () => state.fly.setWalking(!state.fly.walking));
  $('mode-fly').addEventListener('click', () => state.fly.enterFly());
  $('mode-orbit').addEventListener('click', () => state.fly.exitToMap());
  $('fly-overlay-map').addEventListener('click', () => state.fly.exitToMap());
  $('fly-drag').addEventListener('click', () => state.fly.enableDragLook());
  $('fly-center').addEventListener('click', () => {
    if (state.fly.mode !== 'fly') return;
    camera.position.copy(controls.target);
    camera.position.y += 8;
    camera.updateMatrixWorld();
    state.dirty = true;
    scheduleChunkWindow();
  });
  for (const id of ['fly-start', 'fly-overlay-start']) $(id).addEventListener('click', () => state.fly.requestLock());
  renderer.domElement.addEventListener('click', () => {
    if (state.fly.mode === 'fly' && !state.fly.locked && !state.fly.dragMode) state.fly.requestLock();
  });
  // Honest note only; the mode stays available for touch devices that also have a mouse.
  $('fly-touch-note').hidden = window.matchMedia?.('(any-pointer: fine)').matches !== false;
  $('mode-fly').disabled = false;
  setFlyMessage(null);
  showFlyUi();
}

function wireUi() {
  const panKeys = new Set(['KeyW', 'KeyA', 'KeyS', 'KeyD', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']);
  window.addEventListener('keydown', (event) => {
    if (!panKeys.has(event.code) || event.ctrlKey || event.metaKey || event.altKey) return;
    const editing = (node) => Boolean(node?.isContentEditable
      || node?.closest?.('input, textarea, select, [role="textbox"], [role="slider"]'));
    if (!editing(event.target) && !editing(document.activeElement)) cancelPendingGoto();
  });
  let diagnosticsLoading = false;
  async function loadWorldDiagnostics() {
    if (diagnosticsLoading) return;
    diagnosticsLoading = true;
    $('world-diagnostics-load').disabled = true;
    $('world-diagnostics-message').textContent = 'Načítám diagnostiku světa…';
    try {
      const doc = await fetchJson(`${DATA_URL}hires/diagnostics.json`, 15000);
      if (doc.scope !== 'world') throw new Error('Server neposlal souhrn světa.');
      renderWorldDiagnostics($('world-diagnostics-body'), doc, (x, y, z) => {
        if (state.fly?.mode === 'fly') {
          $('world-diagnostics-message').textContent = 'Pro přesun na příklad nejdřív přepněte na režim Mapa.';
          return;
        }
        $('goto-x').value = String(x); $('goto-z').value = String(z);
        moveTo({preventDefault(){}});
        $('world-diagnostics-message').textContent = `Příklad na X ${x}, Y ${y}, Z ${z}. Může ležet pod zemí nebo už být změněný; jde o záznam z doby kontroly.`;
      });
      $('world-diagnostics-message').textContent = typeof doc.generatedAt === 'number' && Number.isFinite(doc.generatedAt)
        ? `Souhrn vytvořen ${new Date(doc.generatedAt * 1000).toLocaleString('cs-CZ')}. Jednotlivé regiony mohou pocházet z různých běhů.`
        : 'Čas vytvoření souhrnu není známý.';
    } catch (error) {
      $('world-diagnostics-message').textContent = 'Diagnostika světa teď není dostupná. Případný předchozí seznam může být starší; zkuste načtení znovu.';
      console.warn('world diagnostics:', error);
    } finally {
      diagnosticsLoading = false;
      $('world-diagnostics-load').disabled = false;
    }
  }
  $('world-diagnostics-load').addEventListener('click', loadWorldDiagnostics);
  $('goto').addEventListener('submit', moveTo);
  $('cov-find').addEventListener('click', () => { findUnsupported().catch((err) => console.error(err)); });
  $('reset').addEventListener('click', () => { cancelPendingGoto(); if (state.fly?.mode !== 'fly') { state.userMoved = true; resetCamera(); } });
  $('opt-fog').addEventListener('change', applyDisplayOptions);
  $('opt-double').addEventListener('change', applyDisplayOptions);
  $('opt-overview').addEventListener('change', updateOverview);
  $('view-distance').addEventListener('change', () => { state.dirty = true; updateChunkWindow(); });
  $('retry-chunks').addEventListener('click', () => {
    state.stream?.retry();
    if (hires.requests?.retryFailed()) scheduleHiresPanel();   // whole-world tiles waiting for their backoff
  });
  // dollyIn/Out call controls.update(); never while flying.
  $('zoom-in').addEventListener('click', () => { cancelPendingGoto(); if (state.fly?.mode !== 'fly') { state.userMoved = true; controls.dollyIn(0.75); } });
  $('zoom-out').addEventListener('click', () => { cancelPendingGoto(); if (state.fly?.mode !== 'fly') { state.userMoved = true; controls.dollyOut(0.75); } });
  $('panel-toggle').addEventListener('click', () => {
    const collapsed = document.body.classList.toggle('panel-collapsed');
    $('panel-toggle').setAttribute('aria-expanded', String(!collapsed));
    $('panel-toggle').textContent = collapsed ? 'Zobrazit panel' : 'Skrýt panel';
  });
}

function enableNavigation() {
  for (const id of ['goto-x', 'goto-z', 'goto-submit', 'reset', 'zoom-in', 'zoom-out']) $(id).disabled = false;
}

// ---------------------------------------------------------------- published generations

// Watcher fetch: 404 means nothing published (null); every other failure is reported by the watcher.
async function fetchRevision(signal) {
  try {
    return await fetchJson(REVISION_URL, TIMEOUT_MS.revision, signal);
  } catch (err) {
    if (err?.status === 404) return null;
    throw err;
  }
}

// Boot choice. An absent, unavailable or invalid revision selects the legacy export; a valid one commits the
// page to generation mode, so a missing or corrupt generation is then shown as an error, never replaced.
async function bootRevision() {
  try {
    const raw = await fetchJson(REVISION_URL, TIMEOUT_MS.revision);
    return validateRevision(raw);
  } catch (err) {
    console.info('Revize mapy není k dispozici, zobrazuji export v data/:', err?.message ?? err);
    return null;
  }
}

// Verified generation manifest: exact bytes against revision.manifestSha256, then the normal contract and
// content-addressed chunk/block names (required for reuse by file key).
async function loadGeneration(revision, signal) {
  const base = generationBase(revision.generation);
  const url = `${base}manifest.json`;
  const manifest = validateManifest(await fetchVerifiedJson(url, revision.manifestSha256, TIMEOUT_MS.manifest, signal));
  let files;
  try {
    files = validateGenerationFiles(manifest);
  } catch (err) {
    throw new DataError([`${url}: soubory generace nemají obsahově adresovaná jména.`, err.message]);
  }
  return { id: revision.generation, base, manifest, blockIndex: files.blockIndex };
}

// Everything that can fail runs here; the current scene, camera and generation are not touched.
async function prepareGeneration(revision, signal) {
  const source = await loadGeneration(revision, signal);
  if ((source.manifest.bakedLighting === true) !== state.bakedLighting) {
    throw new DataError('Nová verze mapy mění režim osvětlení; načte se až po obnovení stránky.');
  }
  const atlas = await loadAtlas(source.base + source.manifest.atlas, signal, source.manifest.atlasSha256, collisionStrict);
  return { ...source, atlas, committed: false };
}

function discardGeneration(prepared) {
  // After the first mutation of a commit the new atlas may already be on the materials.
  if (prepared && !prepared.committed) releaseAtlas(prepared.atlas);
}

// Synchronous switch. Preflight first (selection with the current camera, counts); after the first mutation
// only assignments and calls that do not throw for validated input follow. There is no rollback.
// Unchanged chunks keep their resident meshes (same content-addressed key); changed and removed ones are
// detached synchronously by setDesired before the next frame, so no frame draws an old changed mesh with
// the new atlas. Changed chunks then reload through the normal queue (visible holes until they arrive).
function commitGeneration(prepared) {
  // Opt-in LOD: when the current or the next stream is LOD, every generation commit resets all geometry.
  const plan = state.lodOptIn ? planLod(prepared.manifest) : null;   // pure preflight, never throws
  if (state.stream?.kind === 'lod' || plan?.lod) {
    commitWithReset(prepared);
    return;
  }
  const entries = desiredEntries(prepared.manifest.chunks);
  const counts = diffEntries(state.stream.getSnapshot().resident.map((entry) => entry.file), entries);
  const previousAtlas = state.materials.opaque.map;
  prepared.committed = true;
  state.generation = { id: prepared.id, base: prepared.base };  // read by loads started from setDesired
  state.chunkEntries = prepared.manifest.chunks;
  for (const material of Object.values(state.materials)) material.map = prepared.atlas;
  if (state.lodOptIn) state.lodNote = plan.warning ? { text: plan.warning, warn: true } : null;
  state.stream.setDesired(entries);
  // A content key that failed at the old generation URL may be healthy at the new one.
  // Clear also failures outside the current window, which getSnapshot intentionally hides.
  state.stream.retry();
  applyGenerationFields(prepared);
  if (previousAtlas !== prepared.atlas) releaseAtlas(previousAtlas);
  state.dirty = true;
  state.lastSwitch = counts;
}

// Opt-in LOD commit: preflight, then synchronously dispose the old stream (every old mesh leaves the scene and
// the pick lists) BEFORE the atlas changes, then start a new stream for the new generation. No old UVs are ever
// drawn with the new atlas; geometry is not reused across generations in this mode; the camera is kept.
function commitWithReset(prepared) {
  const entries = desiredEntries(prepared.manifest.chunks);   // preflight with the current camera
  const counts = diffEntries([], entries);                    // nothing is retained
  const previousAtlas = state.materials.opaque.map;
  prepared.committed = true;
  state.stream.dispose();
  state.stream = null;
  state.generation = { id: prepared.id, base: prepared.base };
  for (const material of Object.values(state.materials)) material.map = prepared.atlas;   // textured materials only
  applyGenerationFields(prepared);
  startStream(prepared.manifest);
  if (previousAtlas !== prepared.atlas) releaseAtlas(previousAtlas);
  state.dirty = true;
  state.lastSwitch = counts;
}

function applyGenerationFields(prepared) {
  caveCoverage = createCaveCoverage(prepared.manifest);
  state.bounds = prepared.manifest.bounds;
  state.focus = prepared.manifest.focus;
  configureCut();
  const { min, max } = state.bounds;
  const span = Math.max(max[0] - min[0], max[2] - min[2], 32);
  const distance = camera.position.distanceTo(controls.target);
  controls.maxDistance = Math.max(span * 3 + 200, distance);
  camera.far = Math.max(span * 8 + 2000, distance + span * 2);
  camera.updateProjectionMatrix();
  state.blockIndex = prepared.blockIndex;
  closePick();  // a shown or pending lookup belongs to the previous generation
  if (state.blockIndex) ensurePicking();
  showManifest(prepared.manifest);
}

function showRefreshState({ state: kind, message }) {
  switch (kind) {
    case 'preparing':
      setRefreshNote('Načítám novou verzi mapy…');
      break;
    case 'committed': {
      const c = state.lastSwitch;
      setRefreshNote(`Mapa aktualizována · ${fmtNumber.format(c.retained)} chunků beze změny, `
        + `${fmtNumber.format(c.added)} určeno k novému načtení.`);
      break;
    }
    case 'current':
      setRefreshNote(null);
      break;
    case 'unavailable':
      setRefreshNote('Server teď nezveřejňuje revizi mapy; zobrazuje se naposledy načtená verze.', true);
      break;
    case 'error':
      setRefreshNote(`Stav aktualizací nelze ověřit (${message ?? 'síťová chyba'}); zobrazená mapa zůstává dostupná.`, true);
      break;
    default:  // error, invalid, identity-mismatch, prepare-failed, commit-failed
      setRefreshNote(`Novou verzi mapy nelze použít (${message ?? kind}); zobrazuje se předchozí verze.`, true);
  }
}

function startRevisionWatcher(revision) {
  state.watcher = createRevisionWatcher({
    fetchRevision,
    prepare: prepareGeneration,
    commit: commitGeneration,
    discard: discardGeneration,
    onState: showRefreshState,
    initialGeneration: revision.generation,
    identity: { world: revision.world, dimension: revision.dimension },
    document,
  });
  state.watcher.start();
}

function showStandaloneProgress() {
  if (!hires.index) return;
  const s = hiresStats();
  $('chunks').textContent = `${s.loaded} / ${s.wanted} dlaždic`;
  $('triangles').textContent = fmtNumber.format(s.triangles);
  $('geometry-memory').textContent = geometryMemoryText(0, s.bytes);
  $('retry-chunks').hidden = s.failed === 0;
  setProgress(s.loading ? s.loaded : 0, s.loading ? s.wanted : 0);
  setStatus(s.failed ? 'warn' : s.loading ? 'loading' : 'ok',
    `${s.loading ? 'Načítám dimenzi' : s.failed ? 'Dimenze načtena částečně' : 'Dimenze načtena'} · ${s.loaded} / ${s.wanted} dlaždic`);
}

function startStandaloneView(view) {
  if (!HIRES_IMMUTABLE) throw new DataError('Samostatná dimenze vyžaduje neměnné publikování.');
  state.mapView = view;
  state.mapIdentity = { world: view.world, dimension: view.dimension };
  state.bounds = view.bounds;
  state.focus = view.focus;
  state.cut = null;
  state.cutRequest = null;
  WORLD_CAVES = false; // UIE_MAP_VIEW1 currently describes a surface-only store.
  $('title').textContent = view.title;
  document.title = view.title;
  const { min, max } = view.bounds;
  $('bounds').previousElementSibling.textContent = `Dimenze ${view.dimension}`;
  $('bounds').textContent = `X ${min[0]} … ${max[0]} · Z ${min[2]} … ${max[2]} · Y ${min[1]} … ${max[1]}`;
  $('render').hidden = true;
  $('cov-rendered').closest('section').hidden = true;
  for (const id of ['opt-cut', 'cut-level', 'opt-cut-cap', 'opt-overview', 'opt-players']) $(id).disabled = true;
  const note = document.createElement('p'); note.className = 'note';
  note.textContent = `Samostatný povrch dimenze ${view.dimension}. Jeskyně, hráči a bloková data chůze zde zatím nejsou připojené; chůze používá přibližnou geometrii.`;
  $('bounds').closest('section').append(note);
  resetCamera();
  enableNavigation();
  startFlyMode();
  applyDisplayOptions();
  setStatus('loading', 'Načítám povrch dimenze…');
  startHires();
}

async function main() {
  // Experimental LOD only on explicit request; everything else behaves as before.
  state.lodOptIn = new URLSearchParams(window.location?.search ?? '').get('lod') === '1';
  state.cutRequest = new URLSearchParams(window.location?.search ?? '').get('cut');
  wireUi();
  initViewer();
  const menuController = new AbortController();
  const menuTimer = setTimeout(() => menuController.abort(), 10000);
  mountDimensionMenu({ fetch, document, location: window.location, signal: menuController.signal })
    .catch(error => console.warn('dimension menu:', error))
    .finally(() => clearTimeout(menuTimer));
  setStatus('loading', 'Načítám manifest…');
  const viewController = new AbortController();
  const viewTimer = setTimeout(() => viewController.abort(), TIMEOUT_MS.manifest);
  let view;
  try { view = await loadMapView(fetch, `${DATA_URL}map-view.json`, viewController.signal); }
  finally { clearTimeout(viewTimer); }
  if (view) { startStandaloneView(view); return; }
  const revision = await bootRevision();
  state.mapIdentity = revision ? {world:revision.world,dimension:revision.dimension} : null;
  let manifest;
  if (revision) {
    state.generationBoot = true;
    const source = await loadGeneration(revision);
    manifest = source.manifest;
    state.generation = { id: source.id, base: source.base };
    state.blockIndex = source.blockIndex;
  } else {
    manifest = validateManifest(await fetchJson(`${DATA_URL}manifest.json`, TIMEOUT_MS.manifest));
    // Block selection exists only when the export ships UIE_BLOCKS1 data; otherwise nothing is offered.
    if (manifest.blockInfo?.chunks.length) {
      state.blockIndex = new Map(manifest.blockInfo.chunks.map((c) => [`${c.x},${c.z}`, c.file]));
    }
  }
  state.bounds = manifest.bounds;
  state.focus = manifest.focus;
  caveCoverage = createCaveCoverage(manifest);
  showManifest(manifest);
  configureCut();
  resetCamera();
  enableNavigation();
  startFlyMode();
  startCollisionPilot();
  // Independent of the map: a missing or broken status file only affects the render status panel.
  startRenderStatus();
  startHiresStatus();
  startCollisionPreparationStatus();
  startOverviewStatus();
  startOverview();
  startPlayers();
  if (state.blockIndex) ensurePicking();

  setStatus('loading', 'Načítám atlas textur…');
  const atlas = await loadAtlas((state.generation?.base ?? DATA_URL) + manifest.atlas, undefined, manifest.atlasSha256, collisionStrict);
  // bakedLighting: vertex colours already contain Minecraft face shading, so scene lights are skipped.
  state.bakedLighting = manifest.bakedLighting === true;
  const Material = state.bakedLighting ? THREE.MeshBasicMaterial : THREE.MeshLambertMaterial;
  const base = { map: atlas, vertexColors: true, side: THREE.FrontSide };
  state.materials = {
    opaque: new Material({ ...base, alphaTest: 0.5 }),
    transparent: new Material({ ...base, transparent: true, depthWrite: false, alphaTest: 0.01 }),
  };
  for (const material of Object.values(state.materials)) useCentroidAtlasSampling(material, THREE.ShaderChunk);
  applyDisplayOptions();
  startHires();

  setStatus('loading', 'Načítám chunky…');
  startStream(manifest);
  // Refresh only in generation mode; a page showing the legacy export needs a reload to enter it.
  if (revision) startRevisionWatcher(revision);
}

main().catch((err) => {
  console.error(err);
  setProgress(0, 0);
  const lines = err instanceof DataError ? err.lines : ['Neočekávaná chyba aplikace.', String(err?.message ?? err)];
  const hint = !renderer ? []
    : state.generationBoot ? ['Zveřejněná verze mapy je nedostupná nebo poškozená; starší export se nenačítá místo ní.']
      : ['Ukázková data se nezobrazují – je potřeba platný export v data/.'];
  setStatus('error', 'Mapu nelze zobrazit', [...lines, ...hint]);
});
