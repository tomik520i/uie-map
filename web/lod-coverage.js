// LOD coverage selection over a quadtree forest. Pure and deterministic: no DOM, three.js or I/O.
//
// selectCoverage({ nodes, roots, resident, desired, generation }) -> { selected, uncovered }
//
//   nodes      [{ id: string, children?: [c0, c1, c2, c3] }]
//              children omitted (undefined) = leaf. Otherwise exactly four entries, each a node id or
//              null. null = the quadrant is known to be empty (nothing to draw, needs no tile). A child
//              array of another length, or any other value, is malformed and never read as "empty".
//   roots      root ids of the forest. Every node is reachable from exactly one root; roots have no parent.
//   resident   [{ id, generation }] tiles whose geometry is FULLY ATTACHED to the scene (not merely
//              fetched or decoded). A tile counts as ready only if its generation === `generation`.
//              Ids must be nodes of this tree; stale tiles of other trees must be filtered by the caller.
//   desired    ids the caller wants refined, i.e. shown through their children instead of themselves.
//              Refinement of a node is only considered where coverage reaches that node, so a deep
//              target needs its ancestors in `desired` as well.
//   generation non-empty string identifying the current data generation.
//
// Per node, bottom-up (each node evaluated once, linear in the tree size):
//   children coverable  = every child is null (known empty) or itself coverable
//   refined node        -> its children if they are coverable, else itself if ready, else not coverable
//   not refined / leaf  -> itself if ready, else its children if coverable, else not coverable
// So a ready coarse tile stays selected over its whole area until ALL four quadrants can be covered by
// ready descendants; there is never a partial child overlay on top of a parent. A missing tile falls back
// to the nearest complete coverage: its own descendants, otherwise an ancestor that is ready.
//
// Result: `selected` is an antichain (no selected id is an ancestor of another) that exactly covers every
// root listed in `uncovered`'s complement. `uncovered` lists roots with no complete coverage; nothing is
// selected inside them (no partial coverage is presented as covered). Roots are processed in id order
// (code-unit comparison), children in array order, so the output does not depend on input ordering.
//
// Limitations: no budget, fetch scheduling or eviction policy. The caller must keep every selected tile
// pinned (attached) until the tiles replacing it are resident and selected by a later call; this module
// cannot see in-flight work. It checks tree structure and ids, not geometry (bounds, quadrant placement).
// Inputs are not modified. Invalid input throws LodCoverageError.

export class LodCoverageError extends Error {}

const fail = (message) => { throw new LodCoverageError(message); };
const isId = (v) => typeof v === 'string' && v.length > 0;

function uniqueIds(list, label, known) {
  if (!Array.isArray(list)) fail(`${label} must be an array`);
  const set = new Set();
  for (const id of list) {
    if (!isId(id)) fail(`${label} contains an invalid id ${JSON.stringify(id)}`);
    if (set.has(id)) fail(`${label} contains ${id} twice`);
    if (known && !known.has(id)) fail(`${label} references unknown node ${id}`);
    set.add(id);
  }
  return set;
}

function buildTree(nodes, roots) {
  if (!Array.isArray(nodes)) fail('nodes must be an array');
  const children = new Map();
  for (const node of nodes) {
    if (!node || typeof node !== 'object' || Array.isArray(node)) fail('every node must be an object');
    if (!isId(node.id)) fail(`node id ${JSON.stringify(node.id)} is not a non-empty string`);
    if (children.has(node.id)) fail(`duplicate node id ${node.id}`);
    const list = node.children;
    if (list === undefined) {
      children.set(node.id, null);
      continue;
    }
    if (!Array.isArray(list) || list.length !== 4) fail(`node ${node.id}: children must be omitted or exactly 4 entries`);
    for (const child of list) {
      if (child !== null && !isId(child)) fail(`node ${node.id}: child ${JSON.stringify(child)} is neither an id nor null`);
    }
    children.set(node.id, [...list]);
  }
  const parent = new Map();
  for (const [id, list] of children) {
    if (!list) continue;
    for (const child of list) {
      if (child === null) continue;
      if (!children.has(child)) fail(`node ${id}: unknown child ${child}`);
      if (child === id) fail(`node ${id} is its own child`);
      if (parent.has(child)) fail(`node ${child} has more than one parent (${parent.get(child)}, ${id})`);
      parent.set(child, id);
    }
  }
  const rootSet = uniqueIds(roots, 'roots', children);
  for (const root of rootSet) if (parent.has(root)) fail(`root ${root} has a parent ${parent.get(root)}`);
  // Post-order from the roots, iteratively (no recursion depth limit). Unreached nodes are orphans or cycles.
  const order = [];
  const reached = new Set();
  const stack = [...rootSet].map((id) => [id, false]);
  while (stack.length) {
    const [id, expanded] = stack.pop();
    if (expanded) { order.push(id); continue; }
    reached.add(id);
    stack.push([id, true]);
    for (const child of children.get(id) ?? []) if (child !== null) stack.push([child, false]);
  }
  for (const id of children.keys()) {
    if (!reached.has(id)) fail(`node ${id} is not reachable from a root (orphan or cycle)`);
  }
  return { children, order, rootSet };
}

function readySet(resident, generation, known) {
  if (!Array.isArray(resident)) fail('resident must be an array');
  const seen = new Set();
  const ready = new Set();
  for (const entry of resident) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) fail('resident entries must be objects');
    if (!isId(entry.id) || !known.has(entry.id)) fail(`resident references unknown node ${JSON.stringify(entry.id)}`);
    if (!isId(entry.generation)) fail(`resident ${entry.id}: generation must be a non-empty string`);
    if (seen.has(entry.id)) fail(`resident lists ${entry.id} twice`);
    seen.add(entry.id);
    if (entry.generation === generation) ready.add(entry.id);
  }
  return ready;
}

export function selectCoverage({ nodes, roots, resident, desired, generation } = {}) {
  if (!isId(generation)) fail('generation must be a non-empty string');
  const { children, order, rootSet } = buildTree(nodes, roots);
  const ready = readySet(resident, generation, children);
  const refine = uniqueIds(desired, 'desired', children);

  const plan = new Map();  // id -> 'self' | 'children' | null
  for (const id of order) {  // children before parents
    const list = children.get(id);
    const childrenOk = list !== null && list.every((child) => child === null || plan.get(child) !== null);
    let choice = null;
    if (list !== null && refine.has(id)) choice = childrenOk ? 'children' : (ready.has(id) ? 'self' : null);
    else choice = ready.has(id) ? 'self' : (childrenOk ? 'children' : null);
    plan.set(id, choice);
  }

  const selected = [];
  const uncovered = [];
  for (const root of [...rootSet].sort()) {
    if (plan.get(root) === null) { uncovered.push(root); continue; }
    const stack = [root];
    while (stack.length) {
      const id = stack.pop();
      if (plan.get(id) === 'self') { selected.push(id); continue; }
      const list = children.get(id);
      for (let i = list.length - 1; i >= 0; i -= 1) if (list[i] !== null) stack.push(list[i]);
    }
  }
  return { selected, uncovered };
}
