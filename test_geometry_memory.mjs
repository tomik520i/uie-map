import { createCaveCoverage } from './web/cave-coverage.js';
// Q7: the panel shows the detail stream and the whole-world tiles as two separate used / limit values.
// Run from the work directory:  node --test uie-map/test_geometry_memory.mjs
//
// The declarations are cut out of the real web/app.js source (as in test_web.mjs) and evaluated in a vm context.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const SOURCE = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');

// End of a declaration: the brace closing a function body, or the first top-level semicolon of a const.
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
    if (c === '"' || c === "'") {
      for (i += 1; src[i] !== c; i += src[i] === '\\' ? 2 : 1) if (i >= src.length) throw new Error('unterminated string');
      i += 1;
      continue;
    }
    if (c === '`') { stack.push('`'); i += 1; continue; }
    if (c === '/' && src[i + 1] === '/') { i = src.indexOf('\n', i); continue; }
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

const api = vm.runInContext(`${['const GEOMETRY_BUDGET =', 'const GEOMETRY_MEMORY_TITLE =',
  'function geometryMemoryText('].map(extract).join('\n')}
({ GEOMETRY_BUDGET, GEOMETRY_MEMORY_TITLE, geometryMemoryText });`, vm.createContext({ createCaveCoverage, URLSearchParams,}), { filename: 'app.js (extracted)' });
const MIB = 1048576;

test('detail and world each show their own used / limit, never a sum against one limit', () => {
  assert.equal(api.GEOMETRY_BUDGET, 500 * MIB);
  const text = api.geometryMemoryText(0, 20.7 * MIB);
  assert.equal(text, 'detail 0.0 / 500 MiB · svět 20.7 / 500 MiB');
  assert.doesNotMatch(text, /\+/, 'no "detail + svět / limit" form');
  assert.equal(api.geometryMemoryText(157.6 * MIB, 94.2 * MIB), 'detail 157.6 / 500 MiB · svět 94.2 / 500 MiB');
});

test('without a world index only the detail budget is shown; zero world bytes are still shown', () => {
  assert.equal(api.geometryMemoryText(12.34 * MIB, null), 'detail 12.3 / 500 MiB');
  assert.equal(api.geometryMemoryText(12.34 * MIB, undefined), 'detail 12.3 / 500 MiB');
  assert.equal(api.geometryMemoryText(0, 0), 'detail 0.0 / 500 MiB · svět 0.0 / 500 MiB');
});

test('the tooltip states separate limits and does not claim total browser or GPU memory', () => {
  assert.match(api.GEOMETRY_MEMORY_TITLE, /samostatné limity/);
  assert.match(api.GEOMETRY_MEMORY_TITLE, /není to celková paměť/);
  assert.match(api.GEOMETRY_MEMORY_TITLE, /Nezahrnuje atlas/);
});

test('showStream uses the helper for the memory label in both branches and sets the tooltip', () => {
  const body = extract('function showStream(');
  assert.match(body, /\$\('geometry-memory'\)\.textContent = geometryMemoryText\(s\.bytes, world \? world\.bytes : null\);/);
  assert.match(body, /\$\('geometry-memory'\)\.title = GEOMETRY_MEMORY_TITLE;/);
  assert.equal((body.match(/geometry-memory'\)\.textContent/g) ?? []).length, 1, 'one place writes the label');
  assert.doesNotMatch(body, /\+ svět/);
});
