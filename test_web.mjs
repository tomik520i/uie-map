import { createCaveCoverage } from './web/cave-coverage.js';
// Regression tests for the web loader in web/app.js. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_web.mjs
//
// app.js imports three.js and touches the DOM, so it cannot be imported in Node. The pure
// declarations under test are cut out of the real source text and evaluated in a vm context
// with a fake fetch; nothing is copied, so a change in app.js is what gets tested.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const SOURCE = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');

// ---------------------------------------------------------------- extraction

function skipQuoted(src, i) {
  const quote = src[i];
  for (i += 1; i < src.length; i += 1) {
    if (src[i] === '\\') i += 1;
    else if (src[i] === quote) return i + 1;
  }
  throw new Error('unterminated string literal');
}

// Returns the end index of a declaration starting at `start`. mode 'block' ends at the brace
// closing the body, mode 'statement' at the first top-level semicolon.
function findEnd(src, start, mode) {
  const stack = [];
  const pairs = { ')': '(', ']': '[', '}': '{' };
  let i = start;
  while (i < src.length) {
    const c = src[i];
    if (stack.at(-1) === '`') {
      if (c === '\\') i += 2;
      else if (c === '`') { stack.pop(); i += 1; }
      else if (c === '$' && src[i + 1] === '{') { stack.push('${'); i += 2; }
      else i += 1;
      continue;
    }
    if (c === '"' || c === "'") { i = skipQuoted(src, i); continue; }
    if (c === '`') { stack.push('`'); i += 1; continue; }
    if (c === '/' && src[i + 1] === '/') { i = src.indexOf('\n', i); continue; }
    if (c === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 2; continue; }
    if ('([{'.includes(c)) { stack.push(c); i += 1; continue; }
    if (c in pairs) {
      const open = stack.pop();
      i += 1;
      if (c === '}' && open === '${') continue;
      if (open !== pairs[c]) throw new Error(`unbalanced ${c} at offset ${i - 1}`);
      if (mode === 'block' && c === '}' && stack.length === 0) return i;
      continue;
    }
    if (mode === 'statement' && c === ';' && stack.length === 0) return i + 1;
    i += 1;
  }
  throw new Error(`declaration at offset ${start} does not terminate`);
}

function extract(header) {
  const matches = [...SOURCE.matchAll(new RegExp(`^${header.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'gm'))];
  assert.equal(matches.length, 1, `expected exactly one top-level "${header}" in app.js`);
  const start = matches[0].index;
  return SOURCE.slice(start, findEnd(SOURCE, start, header.startsWith('const ') ? 'statement' : 'block'));
}

const DECLARATIONS = [
  'const isNum =', 'const safePath =', 'class DataError ', 'async function fetchWithTimeout(',
  'function fetchJson(', 'function validateManifest(', 'function validatePart(', 'function validateChunk(',
];

let fetchImpl = () => { throw new Error('fetch not stubbed'); };
const context = vm.createContext({ createCaveCoverage, URLSearchParams,
  fetch: (...args) => fetchImpl(...args), AbortController, setTimeout, clearTimeout,
});
const api = vm.runInContext(`${DECLARATIONS.map(extract).join('\n')}
({ DataError, safePath, fetchWithTimeout, fetchJson, validateManifest, validatePart, validateChunk });`,
context, { filename: 'app.js (extracted)' });

// ---------------------------------------------------------------- helpers

const isDataError = (pattern) => (err) => {
  assert.ok(err instanceof api.DataError, `expected DataError, got ${err?.constructor?.name}: ${err?.message}`);
  if (pattern) assert.match(err.message, pattern);
  return true;
};
const CATEGORIES = { http: /HTTP \d{3}/, timeout: /časový limit/, network: /síťová chyba/, json: /není platný JSON/ };
function assertCategory(err, expected) {
  for (const [name, pattern] of Object.entries(CATEGORIES)) {
    assert.equal(pattern.test(err.message), name === expected,
      `"${err.message}" should ${name === expected ? '' : 'not '}be classified as ${name}`);
  }
  return true;
}

function abortable(signal) {
  return new Promise((_, reject) => {
    const fail = () => reject(new DOMException('The operation was aborted.', 'AbortError'));
    if (signal.aborted) fail();
    else signal.addEventListener('abort', fail, { once: true });
  });
}

function stubFetch(makeResponse) {
  const calls = [];
  fetchImpl = (url, options) => {
    calls.push({ url, options });
    return makeResponse(options?.signal, url);
  };
  return calls;
}

function response({ ok = true, status = 200, statusText = 'OK', text = '{}', body = null }) {
  return { ok, status, statusText, body, text: async () => text, blob: async () => ({ size: text.length }) };
}

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// ---------------------------------------------------------------- fetchWithTimeout / fetchJson

test('successful JSON load passes an AbortSignal and clears the timer', { timeout: 2000 }, async () => {
  const calls = stubFetch(async () => response({ text: '{"a":1}' }));
  const value = await api.fetchJson('data/manifest.json', 40);
  assert.equal(value.a, 1);
  assert.equal(calls.length, 1);
  const { signal } = calls[0].options;
  assert.ok(signal instanceof AbortSignal, 'fetch must receive an AbortSignal');
  await sleep(80);
  assert.equal(signal.aborted, false, 'timer must be cleared after success');
});

test('hanging headers end in a timeout error', { timeout: 2000 }, async () => {
  const calls = stubFetch((signal) => abortable(signal));
  const started = Date.now();
  await assert.rejects(api.fetchJson('data/manifest.json', 50), (err) => isDataError()(err) && assertCategory(err, 'timeout'));
  assert.ok(Date.now() - started < 1000, 'timeout must fire near the configured limit');
  assert.equal(calls.length, 1, 'no retry after timeout');
});

test('hanging response body is covered by the timeout', { timeout: 2000 }, async () => {
  const calls = stubFetch(async (signal) => ({ ...response({}), text: () => abortable(signal), blob: () => abortable(signal) }));
  await assert.rejects(api.fetchJson('data/chunk_0_0.json', 50), (err) => isDataError(/chunk_0_0/)(err) && assertCategory(err, 'timeout'));
  await assert.rejects(api.fetchWithTimeout('data/atlas.png', 50, (r) => r.blob()),
    (err) => isDataError(/atlas\.png/)(err) && assertCategory(err, 'timeout'));
  assert.equal(calls.length, 2, 'no retry after timeout');
});

test('HTTP errors are reported with status and distinguished from other failures', { timeout: 2000 }, async () => {
  let cancelled = 0;
  const body = { cancel: () => { cancelled += 1; return Promise.resolve(); } };
  for (const [status, statusText] of [[404, 'Not Found'], [500, 'Internal Server Error'], [503, '']]) {
    let bodyRead = false;
    const calls = stubFetch(async () => ({ ...response({ ok: false, status, statusText, body }),
      text: async () => { bodyRead = true; return '{}'; } }));
    await assert.rejects(api.fetchJson('data/manifest.json', 1000), (err) => {
      isDataError(new RegExp(`data/manifest\\.json: HTTP ${status}`))(err);
      return assertCategory(err, 'http');
    });
    assert.equal(bodyRead, false, 'an error response must not be parsed as data');
    assert.equal(calls.length, 1, 'no retry after HTTP error');
  }
  assert.equal(cancelled, 3, 'error response bodies are released');
  stubFetch(async () => response({ ok: false, status: 404, statusText: 'Not Found', body: null }));
  await assert.rejects(api.fetchJson('x.json', 1000), (err) => assertCategory(err, 'http'));
});

test('network failure and invalid JSON are separate categories', { timeout: 2000 }, async () => {
  stubFetch(async () => { throw new TypeError('Failed to fetch'); });
  await assert.rejects(api.fetchJson('data/manifest.json', 1000),
    (err) => isDataError(/Failed to fetch/)(err) && assertCategory(err, 'network'));
  stubFetch(async () => response({ text: '<html>not json' }));
  await assert.rejects(api.fetchJson('data/manifest.json', 1000),
    (err) => isDataError(/data\/manifest\.json/)(err) && assertCategory(err, 'json'));
});

test('obsolete streaming fetch aborts its body without being reported as timeout and removes listener', async () => {
  const owner = new AbortController();
  let added = 0, removed = 0;
  const add = owner.signal.addEventListener.bind(owner.signal);
  const remove = owner.signal.removeEventListener.bind(owner.signal);
  owner.signal.addEventListener = (...args) => { added++; return add(...args); };
  owner.signal.removeEventListener = (...args) => { removed++; return remove(...args); };
  stubFetch(async (signal) => ({ ...response({}), text: () => abortable(signal) }));
  const loading = api.fetchJson('chunk.json', 1000, owner.signal);
  owner.abort();
  await assert.rejects(loading, (err) => err.name === 'AbortError' && !/časový limit/.test(err.message));
  assert.equal(added, 1);
  assert.equal(removed, 1);
});

// ---------------------------------------------------------------- validateManifest

function manifest(overrides = {}) {
  return {
    format: 1, title: 'UIE', generatedAt: '2026-09-25T10:00:00Z', sample: true,
    bounds: { min: [-32, 0, -16], max: [32, 255, 48] }, atlas: 'atlas.png',
    chunks: [{ file: 'chunk_-2_-1.json', x: -2, z: -1 }, { file: 'chunks/c1.json', x: 0, z: 0 }],
    coverage: { renderedBlocks: 10, unsupportedBlocks: 1, unsupportedNames: ['mod:x'] },
    ...overrides,
  };
}

test('valid manifests are accepted and returned unchanged', () => {
  const m = manifest();
  assert.equal(api.validateManifest(m), m);
  assert.equal(api.validateManifest(manifest({ bakedLighting: true })).bakedLighting, true);
  assert.equal(api.validateManifest(manifest({ atlas: 'textures/atlas-1.png' })).atlas, 'textures/atlas-1.png');
});

test('coverage counts are nonnegative integers and emptyBlocks remains optional', () => {
  api.validateManifest(manifest());
  const coverage = { renderedBlocks: 10, unsupportedBlocks: 1, unsupportedNames: [], emptyBlocks: 3 };
  api.validateManifest(manifest({ coverage }));
  for (const field of ['renderedBlocks', 'unsupportedBlocks', 'emptyBlocks']) {
    for (const value of [-1, 0.5, '2', null, NaN, Infinity]) {
      assert.throws(() => api.validateManifest(manifest({ coverage: { ...coverage, [field]: value } })),
        isDataError(), `${field}: ${value}`);
    }
    api.validateManifest(manifest({ coverage: { ...coverage, [field]: 0 } }));
  }
});

test('unsafe atlas paths are rejected', () => {
  const unsafe = ['../atlas.png', 'a/../../atlas.png', 'sub/../atlas.png', '/atlas.png', 'https://evil.example/atlas.png',
    '//evil.example/atlas.png', 'C:\\atlas.png', '..\\atlas.png', 'atlas.png?x=1', 'atlas png', '', null, 42];
  for (const atlas of unsafe) {
    assert.throws(() => api.validateManifest(manifest({ atlas })), isDataError(/atlas/), `atlas ${JSON.stringify(atlas)}`);
  }
});

test('manifest contract violations are rejected', () => {
  const cases = {
    format: { format: 2 }, title: { title: 5 }, date: { generatedAt: 'včera' }, sample: { sample: 'yes' },
    'bounds order': { bounds: { min: [0, 0, 10], max: [16, 255, 0] } },
    'bounds length': { bounds: { min: [0, 0], max: [16, 255] } },
    'bounds NaN': { bounds: { min: [0, NaN, 0], max: [16, 255, 16] } },
    'baked string': { bakedLighting: 'true' },
    'chunks object': { chunks: {} }, 'chunks empty': { chunks: [] }, 'chunk null': { chunks: [null] },
    'chunk path': { chunks: [{ file: '../secret.json', x: 0, z: 0 }] },
    'chunk x float': { chunks: [{ file: 'c.json', x: 0.5, z: 0 }] },
    'chunk z string': { chunks: [{ file: 'c.json', x: 0, z: '0' }] },
    'chunk duplicate file': { chunks: [{ file: 'c.json', x: 0, z: 0 }, { file: 'c.json', x: 1, z: 0 }] },
    'chunk duplicate coordinates': { chunks: [{ file: 'c.json', x: 0, z: 0 }, { file: 'd.json', x: 0, z: 0 }] },
    'chunk unsafe integer': { chunks: [{ file: 'c.json', x: Number.MAX_SAFE_INTEGER + 1, z: 0 }] },
    coverage: { coverage: undefined },
    'coverage names': { coverage: { renderedBlocks: 1, unsupportedBlocks: 0, unsupportedNames: [1] } },
  };
  for (const [label, overrides] of Object.entries(cases)) {
    assert.throws(() => api.validateManifest(manifest(overrides)), isDataError(/manifest\.json/), label);
  }
  for (const root of [null, [], 'manifest', 1]) {
    assert.throws(() => api.validateManifest(root), isDataError(/kořen/), JSON.stringify(root));
  }
  assert.throws(() => api.validateManifest(manifest({ format: 2, sample: 'x' })),
    (err) => isDataError()(err) && err.lines.length === 3, 'all violations are listed');
});

test('optional blockInfo: old manifests still load and the block index is validated', () => {
  const old = manifest();
  assert.equal('blockInfo' in old, false);
  assert.equal(api.validateManifest(old), old);
  const blockInfo = { format: 'UIE_BLOCKS1', chunks: [{ x: -1, z: -2, file: 'blocks/chunk_-1_-2.json' },
    { x: 0, z: 0, file: 'blocks/chunk_0_0.json' }] };
  assert.equal(api.validateManifest(manifest({ blockInfo })).blockInfo, blockInfo);
  api.validateManifest(manifest({ blockInfo: { format: 'UIE_BLOCKS1', chunks: [] } }));
  const chunk = { x: 0, z: 0, file: 'blocks/chunk_0_0.json' };
  const cases = {
    null: null, array: [], string: 'blocks', 'wrong format': { format: 'UIE_BLOCKS2', chunks: [chunk] },
    'chunks missing': { format: 'UIE_BLOCKS1' }, 'chunks object': { format: 'UIE_BLOCKS1', chunks: {} },
    'chunk null': { format: 'UIE_BLOCKS1', chunks: [null] },
    'unsafe path': { format: 'UIE_BLOCKS1', chunks: [{ ...chunk, file: '../blocks/x.json' }] },
    'absolute path': { format: 'UIE_BLOCKS1', chunks: [{ ...chunk, file: '/blocks/x.json' }] },
    'x float': { format: 'UIE_BLOCKS1', chunks: [{ ...chunk, x: 0.5 }] },
    'z string': { format: 'UIE_BLOCKS1', chunks: [{ ...chunk, z: '0' }] },
    duplicate: { format: 'UIE_BLOCKS1', chunks: [chunk, { ...chunk, file: 'blocks/other.json' }] },
  };
  for (const [label, value] of Object.entries(cases)) {
    assert.throws(() => api.validateManifest(manifest({ blockInfo: value })), isDataError(/blockInfo/), label);
  }
});

// ---------------------------------------------------------------- validatePart / validateChunk

function quad(overrides = {}) {
  return {
    positions: [0, 64, 0, 1, 64, 0, 1, 64, 1, 0, 64, 1],
    uvs: [0, 0, 1, 0, 1, 1, 0, 1],
    colors: [1, 1, 1, 0.8, 0.8, 0.8, 0.6, 0.6, 0.6, 0.5, 0.5, 0.5],
    indices: [0, 1, 2, 0, 2, 3],
    ...overrides,
  };
}

test('valid and empty geometry parts are accepted', () => {
  api.validatePart(quad(), 'c.json');
  api.validatePart(quad({ transparent: true }), 'c.json');
  api.validatePart({ positions: [], uvs: [], colors: [], indices: [] }, 'empty.json');
});

test('out-of-range and malformed indices are rejected', () => {
  for (const indices of [[0, 1, 4], [0, 1, 2, 0, 2, 999], [-1, 1, 2], [0, 1.5, 2], [0, '1', 2], [0, null, 2], [0, 1, 2, 3]]) {
    assert.throws(() => api.validatePart(quad({ indices }), 'c.json'), isDataError(/c\.json/), JSON.stringify(indices));
  }
});

test('inconsistent or non-numeric vertex data is rejected', () => {
  const cases = {
    'positions not multiple of 3': { positions: [0, 0, 0, 1, 1] },
    'uvs short': { uvs: [0, 0, 1, 0, 1, 1] },
    'colors short': { colors: [1, 1, 1] },
    'position NaN': { positions: [0, NaN, 0, 1, 64, 0, 1, 64, 1, 0, 64, 1] },
    'position Infinity': { positions: [0, Infinity, 0, 1, 64, 0, 1, 64, 1, 0, 64, 1] },
    'uv string': { uvs: ['0', 0, 1, 0, 1, 1, 0, 1] },
    'color null': { colors: [null, 1, 1, 0.8, 0.8, 0.8, 0.6, 0.6, 0.6, 0.5, 0.5, 0.5] },
    'indices missing': { indices: undefined },
    'indices object': { indices: { length: 3 } },
    'transparent string': { transparent: 'yes' },
  };
  for (const [label, overrides] of Object.entries(cases)) {
    assert.throws(() => api.validatePart(quad(overrides), 'c.json'), isDataError(/c\.json/), label);
  }
  for (const part of [null, [], 'x']) {
    assert.throws(() => api.validatePart(part, 'c.json'), isDataError(/c\.json/), JSON.stringify(part));
  }
});

test('validateChunk checks transparentParts', () => {
  const chunk = quad({ transparentParts: [quad(), quad({ transparent: true })] });
  assert.equal(api.validateChunk(chunk, 'c.json'), chunk);
  assert.equal(api.validateChunk(quad(), 'c.json').transparentParts, undefined);
  assert.throws(() => api.validateChunk(quad({ transparentParts: quad() }), 'c.json'), isDataError(/transparentParts/));
  assert.throws(() => api.validateChunk(quad({ transparentParts: [quad({ indices: [0, 1, 7] })] }), 'c.json'),
    isDataError(/transparentParts\[0\]/));
  assert.throws(() => api.validateChunk(quad({ transparentParts: [quad({ transparentParts: [] })] }), 'c.json'),
    isDataError(/vnořené/));
  assert.throws(() => api.validateChunk(quad({ indices: [0, 1, 4] }), 'c.json'), isDataError(/c\.json/));
});
