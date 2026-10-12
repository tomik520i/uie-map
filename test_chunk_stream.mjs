// Tests for web/chunk-stream.js (chunk selection and the budgeted load/attach queue). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_chunk_stream.mjs
// Loads are controlled deferred promises; no network, no timing races.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// web/ has no package.json, so load the real module source as ESM independent of Node's .js detection.
const source = readFileSync(new URL('./web/chunk-stream.js', import.meta.url), 'utf8');
const { ChunkStream, selectChunks } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const E = (x, z) => ({ x, z, file: `chunks/c_${x}_${z}.json` });
const files = (entries) => entries.map((e) => e.file);
// Let settled deferreds run their continuations inside the stream (setImmediate runs after all microtasks).
const flush = () => new Promise((resolve) => { setImmediate(resolve); });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

/** A stream whose load/attach/detach are recorded; checks concurrency and budget on every event. */
function harness({ maxConcurrent = 4, maxBytes = 1000, attachThrows = new Set(), detachThrows = new Set(),
  onChangeThrows = false } = {}) {
  const h = { loads: [], attaches: [], detaches: [], snapshots: [], running: 0, maxRunning: 0 };
  h.stream = new ChunkStream({
    maxConcurrent,
    maxBytes,
    load(entry, signal) {
      const d = deferred();
      h.running += 1;
      h.maxRunning = Math.max(h.maxRunning, h.running);
      assert.ok(h.running <= maxConcurrent, `more than ${maxConcurrent} loads running`);
      const call = { entry, signal, settled: false };
      const settle = (fn) => (value) => { if (!call.settled) { call.settled = true; h.running -= 1; fn(value); } };
      call.resolve = settle(d.resolve);
      call.reject = settle(d.reject);
      h.loads.push(call);
      return d.promise;
    },
    attach(entry, payload) {
      if (attachThrows.has(entry.file)) throw new Error('attach boom');
      const resource = { file: entry.file, payload, detached: 0 };
      h.attaches.push(resource);
      return resource;
    },
    detach(entry, resource) {
      assert.equal(resource.file, entry.file, 'detach receives the resource attached for that entry');
      resource.detached += 1;
      h.detaches.push(entry.file);
      if (detachThrows.has(entry.file)) throw new Error('detach boom');
    },
    onChange(snapshot) {
      h.snapshots.push(snapshot);
      assert.ok(snapshot.bytes <= maxBytes, `resident bytes ${snapshot.bytes} exceed ${maxBytes}`);
      if (onChangeThrows) throw new Error('listener boom');
    },
  });
  h.callsFor = (entry) => h.loads.filter((c) => c.entry.file === entry.file);
  h.last = (entry) => h.callsFor(entry).at(-1);
  h.attachedFiles = () => h.attaches.map((r) => r.file);
  return h;
}

// ---------------------------------------------------------------- selectChunks

test('selectChunks: negative coordinates, distance order and limit', () => {
  const grid = [];
  for (let x = -5; x <= 1; x += 1) for (let z = -4; z <= 2; z += 1) grid.push(E(x, z));
  const before = JSON.stringify(grid);
  // target (-17.5, -0.2) lies in chunk (-2, -1); squared distances to chunk centres computed by hand.
  const expected = [[-2, -1], [-2, 0], [-1, -1], [-1, 0], [-3, -1], [-3, 0], [-2, -2], [-1, -2], [-3, -2]];
  const picked = selectChunks(grid, -17.5, -0.2, 1, 100);
  assert.deepEqual(picked.map((e) => [e.x, e.z]), expected);
  assert.ok(picked.every((e) => grid.includes(e)), 'returns the original entry objects');
  assert.deepEqual(selectChunks(grid, -17.5, -0.2, 1, 4).map((e) => [e.x, e.z]), expected.slice(0, 4));
  assert.equal(JSON.stringify(grid), before, 'entries are not modified');
});

test('selectChunks: Chebyshev boundaries and radius 0', () => {
  const grid = [];
  for (let x = -5; x <= 1; x += 1) for (let z = -4; z <= 2; z += 1) grid.push(E(x, z));
  assert.deepEqual(selectChunks(grid, -17.5, -0.2, 0, 10).map((e) => [e.x, e.z]), [[-2, -1]]);
  const r2 = selectChunks(grid, -17.5, -0.2, 2, 1000);
  assert.equal(r2.length, 25);
  assert.ok(r2.every((e) => e.x >= -4 && e.x <= 0 && e.z >= -3 && e.z <= 1));
  // Target exactly on a chunk corner (-16): floor gives chunk -1; chunk -2 is still inside radius 1.
  assert.deepEqual(selectChunks([E(-2, 0), E(-1, 0), E(0, 0), E(1, 0)], -16, 8, 1, 10).map((e) => e.x).sort(), [-1, -2, 0]);
});

test('selectChunks: equal distances break ties by x then z', () => {
  const scrambled = [E(0, 0), E(-1, 0), E(0, -1), E(-1, -1)];
  assert.deepEqual(selectChunks(scrambled, 0, 0, 1, 10).map((e) => [e.x, e.z]), [[-1, -1], [-1, 0], [0, -1], [0, 0]]);
});

test('selectChunks: invalid arguments are rejected', () => {
  const grid = [E(0, 0)];
  for (const radius of [-1, 1.5, NaN, '1', Infinity]) assert.throws(() => selectChunks(grid, 0, 0, radius, 1), RangeError);
  for (const limit of [0, -1, 1.5, NaN, '1']) assert.throws(() => selectChunks(grid, 0, 0, 1, limit), RangeError);
  for (const [x, z] of [[NaN, 0], [0, Infinity], ['0', 0]]) assert.throws(() => selectChunks(grid, x, z, 1, 1), RangeError);
  assert.throws(() => selectChunks([{ x: 0.5, z: 0, file: 'a' }], 0, 0, 1, 1), TypeError);
  assert.throws(() => selectChunks('x', 0, 0, 1, 1), TypeError);
});

// ---------------------------------------------------------------- ChunkStream

test('never more than maxConcurrent loads; nearest first', async () => {
  const h = harness();
  const desired = Array.from({ length: 10 }, (_, i) => E(i, 0));
  h.stream.setDesired(desired);
  assert.deepEqual(files(h.loads.map((c) => c.entry)), files(desired.slice(0, 4)), 'nearest four start first');
  h.last(desired[2]).resolve({ bytes: 1 });
  await flush();
  assert.equal(h.loads.length, 5);
  assert.equal(h.loads[4].entry.file, desired[4].file);
  for (let i = 0; i < 40 && h.loads.some((c) => !c.settled); i += 1) {
    h.loads.find((c) => !c.settled).resolve({ bytes: 1 });
    await flush();
  }
  assert.equal(h.maxRunning, 4);
  assert.equal(h.stream.getSnapshot().resident.length, 10);
  assert.equal(h.loads.length, 10, 'every chunk loaded exactly once');
});

test('obsolete loads that ignore abort keep their slot and are never attached', async () => {
  const h = harness();
  const [a, b, c, d, e, f] = [E(0, 0), E(1, 0), E(2, 0), E(3, 0), E(4, 0), E(5, 0)];
  h.stream.setDesired([a, b, c, d]);
  h.stream.setDesired([e, f]);
  assert.ok([a, b, c, d].every((x) => h.last(x).signal.aborted), 'obsolete loads are aborted');
  assert.equal(h.callsFor(e).length, 0, 'aborted loads still occupy all four slots');
  h.last(a).resolve({ bytes: 5 });  // ignores the abort and resolves late
  await flush();
  assert.equal(h.callsFor(e).length, 1);
  assert.equal(h.callsFor(f).length, 0);
  h.last(b).reject(new Error('aborted'));
  await flush();
  assert.equal(h.callsFor(f).length, 1);
  h.last(c).resolve({ bytes: 5 });
  h.last(d).resolve({ bytes: 5 });
  h.last(e).resolve({ bytes: 5 });
  h.last(f).resolve({ bytes: 5 });
  await flush();
  assert.deepEqual(h.attachedFiles(), [e.file, f.file]);
  const snap = h.stream.getSnapshot();
  assert.equal(snap.failed.length, 0, 'aborted loads are not failures');
  assert.equal(snap.bytes, 10);
});

test('a stale result for a key that became desired again is not attached', async () => {
  const h = harness();
  const [a, b] = [E(0, 0), E(1, 0)];
  h.stream.setDesired([a]);
  h.stream.setDesired([b]);
  h.stream.setDesired([a]);
  assert.equal(h.callsFor(a).length, 2, 'a new load replaces the obsolete one');
  const [first, second] = h.callsFor(a);
  assert.ok(first.signal.aborted);
  assert.ok(!second.signal.aborted);
  first.resolve({ bytes: 1, tag: 'old' });
  await flush();
  assert.equal(h.attaches.length, 0);
  second.resolve({ bytes: 1, tag: 'new' });
  await flush();
  assert.deepEqual(h.attaches.map((r) => r.payload.tag), ['new']);
  h.last(b).resolve({ bytes: 1 });
  await flush();
  assert.deepEqual(h.attachedFiles(), [a.file]);
});

test('removed chunks are detached exactly once', async () => {
  const h = harness();
  const [a, b] = [E(0, 0), E(1, 0)];
  h.stream.setDesired([a, b]);
  h.last(a).resolve({ bytes: 3 });
  h.last(b).resolve({ bytes: 4 });
  await flush();
  h.stream.setDesired([b]);
  assert.deepEqual(h.detaches, [a.file]);
  assert.equal(h.stream.getSnapshot().bytes, 4);
  h.stream.setDesired([]);
  h.stream.dispose();
  h.stream.dispose();
  assert.deepEqual(h.detaches, [a.file, b.file]);
  assert.ok(h.attaches.every((r) => r.detached === 1));
});

test('an identical desired order is a no-op; reordering does not reload', async () => {
  const h = harness();
  const [a, b] = [E(0, 0), E(1, 0)];
  h.stream.setDesired([a, b]);
  h.last(b).resolve({ bytes: 2 });
  await flush();
  const notified = h.snapshots.length;
  h.stream.setDesired([{ ...a }, { ...b }]);
  assert.equal(h.snapshots.length, notified, 'no notification for an identical order');
  assert.equal(h.loads.length, 2);
  assert.ok(!h.last(a).signal.aborted);
  h.stream.setDesired([b, a]);
  assert.equal(h.loads.length, 2, 'reordering keeps pending and resident chunks');
  assert.ok(!h.last(a).signal.aborted);
  assert.deepEqual(h.detaches, []);
});

test('failed chunks are only reloaded by retry()', async () => {
  const h = harness();
  const [a, b, c] = [E(0, 0), E(1, 0), E(2, 0)];
  h.stream.setDesired([a, b]);
  h.last(a).reject(new Error('HTTP 500'));
  h.last(b).resolve({ bytes: -1 });
  await flush();
  let snap = h.stream.getSnapshot();
  assert.deepEqual(files(snap.failed), [a.file, b.file]);
  assert.match(snap.errors.find((e) => e.entry.file === a.file).message, /HTTP 500/);
  assert.match(snap.errors.find((e) => e.entry.file === b.file).message, /bytes/);
  h.stream.setDesired([a, b, c]);
  await flush();
  assert.equal(h.callsFor(a).length, 1, 'no automatic reload after a new desired set');
  assert.equal(h.callsFor(b).length, 1);
  h.last(c).resolve({ bytes: 1 });
  await flush();
  assert.equal(h.callsFor(a).length, 1, 'no reload after another request completes');
  h.stream.retry();
  assert.equal(h.callsFor(a).length, 2);
  h.last(a).resolve({ bytes: 1 });
  h.last(b).resolve({ bytes: 1 });
  await flush();
  snap = h.stream.getSnapshot();
  assert.deepEqual(snap.failed, []);
  assert.deepEqual(snap.errors, []);
  assert.equal(snap.resident.length, 3);
});

test('strict budget: a closer result evicts farther residents, never exceeding maxBytes', async () => {
  const h = harness({ maxBytes: 100 });
  const [a, b, c] = [E(0, 0), E(1, 0), E(2, 0)];
  h.stream.setDesired([a, b, c]);
  h.last(c).resolve({ bytes: 60 });
  await flush();
  h.last(b).resolve({ bytes: 50 });
  await flush();
  assert.deepEqual(h.detaches, [c.file], 'farther C evicted to fit closer B');
  let snap = h.stream.getSnapshot();
  assert.deepEqual(files(snap.resident), [b.file]);
  assert.deepEqual(files(snap.limited), [c.file]);
  h.last(a).resolve({ bytes: 50 });
  await flush();
  snap = h.stream.getSnapshot();
  assert.deepEqual(files(snap.resident), [a.file, b.file]);
  assert.equal(snap.bytes, 100);
  assert.equal(h.callsFor(c).length, 1, 'evicted chunk is not reloaded by completion of other requests');
  assert.ok(h.snapshots.every((s) => s.bytes <= 100));
});

test('a farther result that cannot fit is discarded without evicting closer chunks or looping', async () => {
  const h = harness({ maxBytes: 100 });
  const [a, b, c] = [E(0, 0), E(1, 0), E(2, 0)];
  h.stream.setDesired([a, b]);
  h.last(a).resolve({ bytes: 100 });
  await flush();
  h.last(b).resolve({ bytes: 30 });
  await flush();
  let snap = h.stream.getSnapshot();
  assert.deepEqual(files(snap.resident), [a.file]);
  assert.deepEqual(files(snap.limited), [b.file]);
  assert.deepEqual(h.detaches, []);
  assert.equal(h.attaches.length, 1);
  h.stream.setDesired([{ ...a }, { ...b }]);
  h.stream.setDesired([a, b, c]);  // relevant change, but B (30 bytes) still cannot fit behind A (100)
  await flush();
  assert.equal(h.callsFor(b).length, 1, 'still-unfit limited chunk is not reloaded');
  assert.equal(h.callsFor(c).length, 1, 'unknown payload size: C is loaded once even with a full budget');
  h.last(c).resolve({ bytes: 10 });
  await flush();
  assert.deepEqual(files(h.stream.getSnapshot().limited), [b.file, c.file]);
  h.stream.setDesired([a, c, b]);  // still nothing can fit behind A
  await flush();
  assert.equal(h.callsFor(b).length, 1);
  assert.equal(h.callsFor(c).length, 1);
  h.stream.setDesired([b, c]);  // A leaves, budget freed
  await flush();
  assert.equal(h.callsFor(b).length, 2, 'unblocked once the budget can fit it');
  assert.equal(h.callsFor(c).length, 2);
  h.last(b).resolve({ bytes: 30 });
  h.last(c).resolve({ bytes: 10 });
  await flush();
  snap = h.stream.getSnapshot();
  assert.deepEqual(files(snap.resident), [b.file, c.file]);
  assert.equal(snap.bytes, 40);
});

test('a full nonzero budget still tries unknown chunks once: 0 B attaches, others end limited', async () => {
  const h = harness({ maxConcurrent: 1, maxBytes: 10 });
  const [a, b, c] = [E(0, 0), E(1, 0), E(2, 0)];
  h.stream.setDesired([a, b, c]);
  h.last(a).resolve({ bytes: 10 });
  await flush();
  assert.equal(h.callsFor(b).length, 1, 'next chunk is loaded although the budget is exactly full');
  h.last(b).resolve({ bytes: 0 });
  await flush();
  assert.equal(h.callsFor(c).length, 1);
  h.last(c).resolve({ bytes: 1 });
  await flush();
  const snap = h.stream.getSnapshot();
  assert.deepEqual(files(snap.resident), [a.file, b.file]);
  assert.deepEqual(files(snap.limited), [c.file]);
  assert.deepEqual(snap.pending, []);
  assert.deepEqual(snap.queued, []);
  assert.equal(snap.bytes, 10);
  const loads = h.loads.length;
  await flush();
  h.stream.setDesired([{ ...a }, { ...b }, { ...c }]);
  await flush();
  assert.equal(h.loads.length, loads, 'no further loads without a relevant change or retry()');
});

test('a chunk larger than the whole budget is limited until retry()', async () => {
  const h = harness({ maxBytes: 100 });
  const [a, b] = [E(0, 0), E(1, 0)];
  h.stream.setDesired([a]);
  h.last(a).resolve({ bytes: 150 });
  await flush();
  assert.equal(h.attaches.length, 0);
  assert.deepEqual(files(h.stream.getSnapshot().limited), [a.file]);
  h.stream.setDesired([a, b]);
  await flush();
  assert.equal(h.callsFor(a).length, 1, 'oversize is not retried by a new desired set');
  h.stream.retry();
  assert.equal(h.callsFor(a).length, 2);
});

test('zero-byte geometry is allowed, also with a zero budget', async () => {
  const h = harness({ maxBytes: 0 });
  const [a, b] = [E(0, 0), E(1, 0)];
  h.stream.setDesired([a, b]);
  h.last(a).resolve({ bytes: 0 });
  h.last(b).resolve({ bytes: 1 });
  await flush();
  const snap = h.stream.getSnapshot();
  assert.deepEqual(files(snap.resident), [a.file]);
  assert.deepEqual(files(snap.limited), [b.file]);
  assert.equal(snap.bytes, 0);
});

test('attach, load and listener exceptions do not block the queue', async () => {
  const h = harness({ maxConcurrent: 1, attachThrows: new Set([E(0, 0).file]), onChangeThrows: true });
  const [a, b, c] = [E(0, 0), E(1, 0), E(2, 0)];
  h.stream.setDesired([a, b, c]);
  h.last(a).resolve({ bytes: 1 });
  await flush();
  assert.equal(h.callsFor(b).length, 1, 'the slot is released after attach throws');
  h.last(b).resolve({ bytes: 1 });
  await flush();
  h.last(c).resolve({ bytes: 1 });
  await flush();
  const snap = h.stream.getSnapshot();
  assert.deepEqual(files(snap.failed), [a.file]);
  assert.deepEqual(files(snap.resident), [b.file, c.file]);
  assert.match(snap.errors.find((e) => e.entry?.file === a.file).message, /attach boom/);
  assert.ok(snap.errors.some((e) => /listener boom/.test(e.message)), 'listener failures are reported');
  assert.equal(snap.bytes, 2, 'a failed attach adds no bytes');

  const sync = new ChunkStream({ load: () => { throw new Error('sync boom'); }, attach: () => ({}), detach() {}, maxConcurrent: 1 });
  sync.setDesired([a, b]);
  await flush();
  assert.deepEqual(files(sync.getSnapshot().failed), [a.file, b.file], 'synchronous throws free the slot too');
});

test('detach failures are reported and the chunk leaves resident accounting', async () => {
  const h = harness({ detachThrows: new Set([E(0, 0).file]) });
  const [a, b] = [E(0, 0), E(1, 0)];
  h.stream.setDesired([a, b]);
  h.last(a).resolve({ bytes: 7 });
  h.last(b).resolve({ bytes: 3 });
  await flush();
  h.stream.setDesired([b]);
  const snap = h.stream.getSnapshot();
  assert.equal(snap.bytes, 3);
  assert.ok(snap.errors.some((e) => e.entry.file === a.file && /detach boom/.test(e.message)));
});

test('dispose aborts pending loads, detaches residents and ignores late results', async () => {
  const h = harness();
  const [a, b, c] = [E(0, 0), E(1, 0), E(2, 0)];
  h.stream.setDesired([a, b, c]);
  h.last(c).resolve({ bytes: 5 });
  await flush();
  h.stream.dispose();
  assert.ok(h.last(a).signal.aborted && h.last(b).signal.aborted);
  assert.deepEqual(h.detaches, [c.file]);
  h.last(a).resolve({ bytes: 1 });
  h.last(b).reject(new Error('aborted'));
  await flush();
  assert.deepEqual(h.attachedFiles(), [c.file], 'no attach after dispose');
  h.stream.dispose();
  h.stream.setDesired([E(9, 9)]);
  h.stream.retry();
  await flush();
  assert.deepEqual(h.detaches, [c.file]);
  assert.equal(h.loads.length, 3, 'no loads after dispose');
  const snap = h.stream.getSnapshot();
  assert.deepEqual([snap.desired, snap.resident, snap.pending, snap.bytes], [[], [], [], 0]);
});

test('constructor and setDesired validate their input', () => {
  const ok = { load: async () => ({ bytes: 0 }), attach: () => ({}), detach() {} };
  assert.throws(() => new ChunkStream({ ...ok, load: undefined }), TypeError);
  assert.throws(() => new ChunkStream({ ...ok, maxConcurrent: 0 }), RangeError);
  assert.throws(() => new ChunkStream({ ...ok, maxBytes: -1 }), RangeError);
  const stream = new ChunkStream(ok);
  assert.throws(() => stream.setDesired([E(0, 0), E(0, 0)]), TypeError);
  assert.throws(() => stream.setDesired([{ x: 0.5, z: 0, file: 'a' }]), TypeError);
  assert.throws(() => stream.setDesired([{ x: 0, z: 0, file: '' }]), TypeError);
  assert.deepEqual(stream.getSnapshot().desired, [], 'rejected input leaves the state unchanged');
  stream.dispose();
});
