// Tests for lod-coverage.js. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_lod_coverage.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// No package.json declares ESM here, so load the real module source as ESM independent of Node's .js detection.
const source = readFileSync(new URL('./web/lod-coverage.js', import.meta.url), 'utf8');
const { LodCoverageError, selectCoverage } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const GEN = 'g2';

/** Full quadtree of `depth` levels below `root`; ids are root + quadrant digits ("r", "r0", "r03", ...). */
function quadtree(root, depth, empty = new Set()) {
  const nodes = [];
  const build = (id, level) => {
    if (level === depth) { nodes.push({ id }); return; }
    const kids = [0, 1, 2, 3].map((q) => (empty.has(`${id}${q}`) ? null : `${id}${q}`));
    nodes.push({ id, children: kids });
    for (const kid of kids) if (kid) build(kid, level + 1);
  };
  build(root, 0);
  return nodes;
}

const res = (ids, generation = GEN) => ids.map((id) => ({ id, generation }));
const select = ({ nodes, roots = ['r'], ready = [], stale = [], desired = [] }) =>
  selectCoverage({ nodes, roots, resident: [...res(ready), ...res(stale, 'g1')], desired, generation: GEN });

/** Independent invariant check: antichain, exact cover of covered roots, nothing inside uncovered roots. */
function checkInvariants(nodes, roots, result, readyIds) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const selected = new Set(result.selected);
  assert.equal(selected.size, result.selected.length, 'no duplicates');
  for (const id of selected) assert.ok(readyIds.has(id), `${id} selected but not ready`);
  // Exactly one selected tile on every path from a covered root to a leaf; known-empty quadrants need none.
  const covered = (id, above) => {
    const node = byId.get(id);
    const total = above + (selected.has(id) ? 1 : 0);
    assert.ok(total <= 1, `overlap at ${id}`);
    if (total === 1) {
      for (const kid of (node.children ?? []).filter(Boolean)) covered(kid, total);
      return true;
    }
    if (!node.children) return false;
    return node.children.every((kid) => kid === null || covered(kid, 0));
  };
  for (const root of roots) {
    if (result.uncovered.includes(root)) {
      const inside = [];
      const walk = (id) => { if (selected.has(id)) inside.push(id); (byId.get(id).children ?? []).filter(Boolean).forEach(walk); };
      walk(root);
      assert.deepEqual(inside, [], `uncovered root ${root} must not contain selections`);
    } else {
      assert.ok(covered(root, 0), `root ${root} reported covered but has a hole`);
    }
  }
}

// ---------------------------------------------------------------- basic choices

test('unrefined root prefers the ready root', () => {
  const nodes = quadtree('r', 1);
  assert.deepEqual(select({ nodes, ready: ['r', 'r0', 'r1', 'r2', 'r3'] }), { selected: ['r'], uncovered: [] });
});

test('refined parent stays until all four children are ready', () => {
  const nodes = quadtree('r', 1);
  const steps = [[], ['r0'], ['r0', 'r1'], ['r0', 'r1', 'r3']];
  for (const kids of steps) {
    assert.deepEqual(select({ nodes, ready: ['r', ...kids], desired: ['r'] }).selected, ['r'], `with ${kids}`);
  }
  assert.deepEqual(select({ nodes, ready: ['r', 'r0', 'r1', 'r2', 'r3'], desired: ['r'] }).selected, ['r0', 'r1', 'r2', 'r3']);
});

test('a missing child is covered by its ready descendants', () => {
  const nodes = quadtree('r', 2);
  const grandchildren = ['r30', 'r31', 'r32', 'r33'];
  const result = select({ nodes, ready: ['r', 'r0', 'r1', 'r2', ...grandchildren], desired: ['r'] });
  assert.deepEqual(result.selected, ['r0', 'r1', 'r2', 'r30', 'r31', 'r32', 'r33']);
  // One grandchild failed: r3's quadrant is incomplete, so the whole parent area stays on r.
  const partial = select({ nodes, ready: ['r', 'r0', 'r1', 'r2', 'r30', 'r31', 'r33'], desired: ['r'] });
  assert.deepEqual(partial.selected, ['r']);
});

test('refinement of a child keeps that child until its four quadrants are ready', () => {
  const nodes = quadtree('r', 2);
  const base = ['r', 'r0', 'r1', 'r2', 'r3'];
  assert.deepEqual(select({ nodes, ready: [...base, 'r20', 'r21', 'r22'], desired: ['r', 'r2'] }).selected,
    ['r0', 'r1', 'r2', 'r3']);
  assert.deepEqual(select({ nodes, ready: [...base, 'r20', 'r21', 'r22', 'r23'], desired: ['r', 'r2'] }).selected,
    ['r0', 'r1', 'r20', 'r21', 'r22', 'r23', 'r3']);
  // Refining r2 without refining r: coverage never reaches r2 while r is ready.
  assert.deepEqual(select({ nodes, ready: [...base, 'r20', 'r21', 'r22', 'r23'], desired: ['r2'] }).selected, ['r']);
});

test('coarsening returns to the parent; an evicted parent falls back to complete children', () => {
  const nodes = quadtree('r', 1);
  const all = ['r', 'r0', 'r1', 'r2', 'r3'];
  assert.deepEqual(select({ nodes, ready: all, desired: [] }).selected, ['r']);
  assert.deepEqual(select({ nodes, ready: ['r0', 'r1', 'r2', 'r3'], desired: [] }).selected, ['r0', 'r1', 'r2', 'r3']);
});

test('uncovered roots are reported and contain no partial selection', () => {
  const nodes = quadtree('r', 2);
  const result = select({ nodes, ready: ['r0', 'r1', 'r2', 'r30'], desired: ['r'] });
  assert.deepEqual(result, { selected: [], uncovered: ['r'] });
  assert.deepEqual(select({ nodes, ready: [] }), { selected: [], uncovered: ['r'] });
});

test('generation mismatch never counts as ready', () => {
  const nodes = quadtree('r', 1);
  assert.deepEqual(select({ nodes, ready: ['r', 'r0', 'r1', 'r2'], stale: ['r3'], desired: ['r'] }).selected, ['r']);
  assert.deepEqual(select({ nodes, ready: ['r0', 'r1', 'r2', 'r3'], stale: ['r'] }).selected, ['r0', 'r1', 'r2', 'r3']);
  assert.deepEqual(select({ nodes, stale: ['r', 'r0', 'r1', 'r2', 'r3'], desired: ['r'] }),
    { selected: [], uncovered: ['r'] });
});

test('known-empty quadrants need no tile; all-empty children cover with nothing', () => {
  const nodes = quadtree('r', 1, new Set(['r1', 'r2']));
  assert.deepEqual(nodes[0].children, ['r0', null, null, 'r3']);
  assert.deepEqual(select({ nodes, ready: ['r', 'r0', 'r3'], desired: ['r'] }).selected, ['r0', 'r3']);
  assert.deepEqual(select({ nodes, ready: ['r0'], desired: ['r'] }), { selected: [], uncovered: ['r'] });
  const empty = [{ id: 'e', children: [null, null, null, null] }];
  assert.deepEqual(selectCoverage({ nodes: empty, roots: ['e'], resident: [], desired: [], generation: GEN }),
    { selected: [], uncovered: [] });
});

test('a leaf in desired is treated as unrefinable', () => {
  const nodes = quadtree('r', 1);
  assert.deepEqual(select({ nodes, ready: ['r0', 'r1', 'r2', 'r3'], desired: ['r', 'r2'] }).selected, ['r0', 'r1', 'r2', 'r3']);
});

test('forest with arbitrary ids (negative coordinates) is ordered by root id', () => {
  const a = quadtree('-3,-2/', 1);
  const b = quadtree('4,-1/', 1);
  const c = quadtree('-10,7/', 1);
  const nodes = [...b, ...c, ...a];
  const roots = ['4,-1/', '-10,7/', '-3,-2/'];
  const ready = ['-3,-2/', '4,-1/0', '4,-1/1', '4,-1/2', '4,-1/3', '4,-1/'];
  const result = selectCoverage({ nodes, roots, resident: res(ready), desired: ['4,-1/'], generation: GEN });
  assert.deepEqual(result, { selected: ['-3,-2/', '4,-1/0', '4,-1/1', '4,-1/2', '4,-1/3'], uncovered: ['-10,7/'] });
});

test('output is independent of input order and inputs are not modified', () => {
  const nodes = quadtree('r', 2);
  const ready = ['r', 'r0', 'r1', 'r2', 'r3', 'r30', 'r31', 'r32', 'r33'];
  const input = { nodes, roots: ['r'], resident: res(ready), desired: ['r', 'r3'], generation: GEN };
  const frozen = JSON.stringify(input);
  const deepFreeze = (v) => { if (v && typeof v === 'object') { Object.values(v).forEach(deepFreeze); Object.freeze(v); } return v; };
  const expected = selectCoverage(deepFreeze(structuredClone(input)));
  assert.equal(JSON.stringify(input), frozen);
  const shuffled = { ...input, nodes: [...nodes].reverse(), resident: [...input.resident].reverse(), desired: ['r3', 'r'] };
  assert.deepEqual(selectCoverage(shuffled), expected);
  assert.deepEqual(expected.selected, ['r0', 'r1', 'r2', 'r30', 'r31', 'r32', 'r33']);
});

test('a transition sequence never overlaps and never loses coverage while a parent is ready', () => {
  const nodes = quadtree('r', 2);
  const desired = ['r', 'r0', 'r1', 'r2', 'r3'];
  const arrivals = ['r0', 'r1', 'r2', 'r3', 'r00', 'r01', 'r02', 'r10', 'r11', 'r12', 'r13', 'r03',
    'r20', 'r21', 'r22', 'r23', 'r30', 'r31', 'r33', 'r32'];
  const ready = new Set(['r']);
  const seen = [];
  for (const id of arrivals) {
    ready.add(id);
    const result = select({ nodes, ready: [...ready], desired });
    checkInvariants(nodes, ['r'], result, ready);
    assert.deepEqual(result.uncovered, []);
    seen.push(result.selected.join(' '));
  }
  assert.equal(seen[2], 'r', 'three children ready: parent kept');
  assert.equal(seen[3], 'r0 r1 r2 r3', 'fourth child replaces the parent');
  assert.equal(seen[6], 'r0 r1 r2 r3', 'three of r0 quadrants: r0 kept');
  assert.equal(seen[11], 'r00 r01 r02 r03 r10 r11 r12 r13 r2 r3');
  assert.equal(seen.at(-1), 'r00 r01 r02 r03 r10 r11 r12 r13 r20 r21 r22 r23 r30 r31 r32 r33');
});

test('randomized states satisfy the coverage invariants', () => {
  let seed = 12345;
  const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
  const full = quadtree('r', 3).map((n) => n.id);
  for (let round = 0; round < 300; round += 1) {
    const empty = new Set(full.filter((id) => id.length > 1 && rnd() < 0.1));
    const nodes = quadtree('r', 3, empty);
    const ids = nodes.map((n) => n.id);
    const ready = new Set(ids.filter(() => rnd() < 0.6));
    const stale = ids.filter((id) => !ready.has(id) && rnd() < 0.3);
    const desired = ids.filter(() => rnd() < 0.5);
    checkInvariants(nodes, ['r'], select({ nodes, ready: [...ready], stale, desired }), ready);
  }
});

test('deep chains are handled without recursion limits', () => {
  const nodes = [];
  for (let i = 0; i < 20000; i += 1) nodes.push({ id: `n${i}`, children: [`n${i + 1}`, null, null, null] });
  nodes.push({ id: 'n20000' });
  const result = selectCoverage({ nodes, roots: ['n0'], resident: res(['n20000']), desired: [], generation: GEN });
  assert.deepEqual(result, { selected: ['n20000'], uncovered: [] });
});

// ---------------------------------------------------------------- validation

test('malformed trees and inputs are rejected', () => {
  const ok = () => ({ nodes: quadtree('r', 1), roots: ['r'], resident: [], desired: [], generation: GEN });
  const cases = {
    'duplicate id': (i) => { i.nodes.push({ id: 'r0' }); },
    'three children': (i) => { i.nodes[0] = { id: 'r', children: ['r0', 'r1', 'r2'] }; },
    'children null': (i) => { i.nodes[0] = { id: 'r', children: null }; },
    'children object': (i) => { i.nodes[0] = { id: 'r', children: { 0: 'r0' } }; },
    'child number': (i) => { i.nodes[0] = { id: 'r', children: ['r0', 'r1', 'r2', 3] }; },
    'unknown child': (i) => { i.nodes[0] = { id: 'r', children: ['r0', 'r1', 'r2', 'x'] }; },
    'same child twice': (i) => { i.nodes[0] = { id: 'r', children: ['r0', 'r0', 'r2', 'r3'] }; },
    'self child': (i) => { i.nodes[1] = { id: 'r0', children: ['r0', null, null, null] }; },
    'shared child': (i) => { i.nodes[1] = { id: 'r0', children: ['r1', null, null, null] }; },
    orphan: (i) => { i.nodes.push({ id: 'lost' }); },
    cycle: (i) => { i.nodes.push({ id: 'a', children: ['b', null, null, null] }, { id: 'b', children: ['a', null, null, null] }); },
    'root with parent': (i) => { i.roots = ['r', 'r0']; },
    'unknown root': (i) => { i.roots = ['x']; },
    'duplicate root': (i) => { i.roots = ['r', 'r']; },
    'empty id': (i) => { i.nodes.push({ id: '' }); },
    'node not object': (i) => { i.nodes.push('r9'); },
    'resident unknown': (i) => { i.resident = res(['x']); },
    'resident duplicate': (i) => { i.resident = [...res(['r0']), ...res(['r0'], 'g1')]; },
    'resident no generation': (i) => { i.resident = [{ id: 'r0' }]; },
    'resident number generation': (i) => { i.resident = [{ id: 'r0', generation: 2 }]; },
    'resident not array': (i) => { i.resident = new Set(['r0']); },
    'desired unknown': (i) => { i.desired = ['x']; },
    'desired duplicate': (i) => { i.desired = ['r', 'r']; },
    'generation empty': (i) => { i.generation = ''; },
    'generation number': (i) => { i.generation = 2; },
    'nodes not array': (i) => { i.nodes = {}; },
  };
  for (const [label, mutate] of Object.entries(cases)) {
    const input = ok();
    mutate(input);
    assert.throws(() => selectCoverage(input), LodCoverageError, label);
  }
  assert.throws(() => selectCoverage(), LodCoverageError);
  assert.deepEqual(selectCoverage(ok()), { selected: [], uncovered: ['r'] });
});
