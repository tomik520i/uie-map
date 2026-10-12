// Tests for web/revision-watch.js with a fake clock and deferred promises. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_revision_watch.mjs
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// web/ has no package.json, so load the real module source as ESM independent of Node's .js detection.
const source = readFileSync(new URL('./web/revision-watch.js', import.meta.url), 'utf8');
const rw = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const sha = (text) => createHash('sha256').update(text).digest('hex');
const flush = () => new Promise((resolve) => { setImmediate(resolve); });
const G0 = sha('g0');
const G1 = sha('g1');
const G2 = sha('g2');

function revision(generation, overrides = {}) {
  return { format: 'UIE_REVISION1', world: 'uie', dimension: 'overworld', pipelineKey: sha('pipeline'), generation,
    manifest: `generations/${generation}/manifest.json`, manifestSha256: sha(`m ${generation}`), ...overrides };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

function harness(options = {}) {
  const h = { fetches: [], prepares: [], commits: [], discards: [], states: [], timers: new Map(), nextTimer: 1 };
  h.doc = new EventTarget();
  h.doc.hidden = false;
  h.watcher = rw.createRevisionWatcher({
    fetchRevision: (signal) => { const d = deferred(); h.fetches.push({ signal, ...d }); return d.promise; },
    prepare: (rev, signal) => { const d = deferred(); h.prepares.push({ rev, signal, ...d }); return d.promise; },
    commit: (prepared, rev) => {
      if (h.commitThrows) throw new Error('scene swap failed');
      h.commits.push([prepared, rev.generation]);
    },
    discard: (prepared) => h.discards.push(prepared),
    onState: (s) => h.states.push(s),
    document: h.doc,
    setTimer: (fn, ms) => { const id = h.nextTimer++; h.timers.set(id, { fn, ms }); return id; },
    clearTimer: (id) => { h.timers.delete(id); },
    intervalMs: 30000,
    ...options,
  });
  h.fire = () => {
    assert.equal(h.timers.size, 1, 'exactly one pending timer');
    const [[id, t]] = h.timers;
    h.timers.delete(id);
    t.fn();
  };
  h.lastState = () => h.states.at(-1)?.state;
  h.setHidden = (value) => { h.doc.hidden = value; h.doc.dispatchEvent(new Event('visibilitychange')); };
  return h;
}

test('validateRevision accepts the publisher pointer and rejects malformed ones', () => {
  assert.equal(rw.validateRevision(revision(G1)).generation, G1);
  const cases = {
    array: [], 'null': null, format: revision(G1, { format: 'UIE_REVISION2' }),
    'upper generation': revision(G1, { generation: G1.toUpperCase() }),
    'manifest path': revision(G1, { manifest: `generations/${G2}/manifest.json` }),
    'manifest traversal': revision(G1, { manifest: `generations/${G1}/../manifest.json` }),
    'manifest sha': revision(G1, { manifestSha256: 'abc' }), 'pipeline': revision(G1, { pipelineKey: 5 }),
    'empty world': revision(G1, { world: '  ' }), 'missing dimension': revision(G1, { dimension: undefined }),
  };
  for (const [label, doc] of Object.entries(cases)) assert.throws(() => rw.validateRevision(doc), rw.RevisionError, label);
});

test('a new generation is prepared then committed; the same generation is not prepared again', async () => {
  const h = harness();
  h.watcher.start();
  assert.equal(h.fetches.length, 1, 'immediate first check');
  h.fetches[0].resolve(revision(G1));
  await flush();
  assert.equal(h.prepares.length, 1);
  assert.equal(h.commits.length, 0, 'no commit before prepare finished');
  assert.equal(h.watcher.currentGeneration, null);
  h.prepares[0].resolve('P1');
  await flush();
  assert.deepEqual(h.commits, [['P1', G1]]);
  assert.equal(h.watcher.currentGeneration, G1);
  assert.equal(h.lastState(), 'committed');
  assert.equal([...h.timers.values()][0].ms, 30000);
  h.fire();
  h.fetches[1].resolve(revision(G1));
  await flush();
  assert.equal(h.prepares.length, 1, 'unchanged generation skips prepare');
  assert.equal(h.lastState(), 'current');
  assert.deepEqual(h.discards, []);
  h.watcher.dispose();
});

test('prepare failure keeps the old generation and the next poll retries', async () => {
  const h = harness({ initialGeneration: G0 });
  h.watcher.start();
  h.fetches[0].resolve(revision(G1));
  await flush();
  h.prepares[0].reject(new Error('chunk 3 failed validation'));
  await flush();
  assert.equal(h.watcher.currentGeneration, G0);
  assert.equal(h.commits.length, 0);
  assert.equal(h.lastState(), 'prepare-failed');
  h.fire();
  h.fetches[1].resolve(revision(G1));
  await flush();
  h.prepares[1].resolve('P1');
  await flush();
  assert.equal(h.watcher.currentGeneration, G1);
  h.watcher.dispose();
});

test('missing endpoint, invalid pointer and fetch errors change nothing and retry', async () => {
  const h = harness({ initialGeneration: G0 });
  h.watcher.start();
  h.fetches[0].resolve(null);
  await flush();
  assert.equal(h.lastState(), 'unavailable');
  h.fire();
  h.fetches[1].resolve({ format: 'UIE_REVISION1', generation: 'bad' });
  await flush();
  assert.equal(h.lastState(), 'invalid');
  h.fire();
  h.fetches[2].reject(new Error('HTTP 503'));
  await flush();
  assert.equal(h.lastState(), 'error');
  assert.equal(h.timers.size, 1, 'retry on the next poll');
  assert.equal(h.prepares.length, 0);
  assert.equal(h.watcher.currentGeneration, G0);
  h.watcher.dispose();
});

test('a revision for another world or dimension is rejected without switching', async () => {
  const given = harness({ identity: { world: 'uie', dimension: 'overworld' }, initialGeneration: G0 });
  given.watcher.start();
  given.fetches[0].resolve(revision(G1, { world: 'other-world' }));
  await flush();
  assert.equal(given.lastState(), 'identity-mismatch');
  assert.equal(given.prepares.length, 0);
  assert.equal(given.watcher.currentGeneration, G0);
  given.watcher.dispose();

  const captured = harness();
  captured.watcher.start();
  captured.fetches[0].resolve(revision(G1));
  await flush();
  captured.prepares[0].resolve('P1');
  await flush();
  captured.fire();
  captured.fetches[1].resolve(revision(G2, { dimension: 'nether' }));
  await flush();
  assert.equal(captured.lastState(), 'identity-mismatch');
  assert.equal(captured.prepares.length, 1);
  assert.equal(captured.watcher.currentGeneration, G1);
  captured.fire();
  captured.fetches[2].resolve(revision(G2, { pipelineKey: sha('pipeline 2') }));
  await flush();
  assert.equal(captured.prepares.length, 2, 'a pipeline change within the same world is allowed');
  captured.watcher.dispose();
});

test('checkNow never overlaps and coalesces to one follow-up', async () => {
  const h = harness();
  h.watcher.start();
  h.watcher.checkNow();
  h.watcher.checkNow();
  h.watcher.checkNow();
  assert.equal(h.fetches.length, 1);
  h.fetches[0].resolve(revision(G1));
  await flush();
  h.watcher.checkNow();
  assert.equal(h.fetches.length, 1, 'still busy while preparing');
  h.prepares[0].resolve('P1');
  await flush();
  assert.equal(h.fetches.length, 2, 'exactly one immediate follow-up');
  assert.equal(h.timers.size, 0, 'no timer while the follow-up runs');
  h.fetches[1].resolve(revision(G1));
  await flush();
  assert.equal(h.fetches.length, 2);
  assert.equal(h.timers.size, 1);
  h.watcher.dispose();
});

test('commit failure discards exactly once and keeps the old generation', async () => {
  const h = harness({ initialGeneration: G0 });
  h.commitThrows = true;
  h.watcher.start();
  h.fetches[0].resolve(revision(G1));
  await flush();
  h.prepares[0].resolve('P1');
  await flush();
  assert.deepEqual(h.discards, ['P1']);
  assert.equal(h.watcher.currentGeneration, G0);
  assert.equal(h.lastState(), 'commit-failed');
  h.commitThrows = false;
  h.fire();
  h.fetches[1].resolve(revision(G1));
  await flush();
  h.prepares[1].resolve('P1b');
  await flush();
  assert.deepEqual(h.commits, [['P1b', G1]]);
  assert.deepEqual(h.discards, ['P1'], 'discard only once for the failed commit');
  h.watcher.dispose();
});

test('hiding aborts; a late prepare result is discarded; visible again checks after the old cycle settles', async () => {
  const h = harness({ initialGeneration: G0 });
  h.watcher.start();
  h.fetches[0].resolve(revision(G1));
  await flush();
  h.setHidden(true);
  assert.ok(h.prepares[0].signal.aborted);
  assert.equal(h.timers.size, 0);
  h.setHidden(false);
  assert.equal(h.fetches.length, 1, 'no overlap while the aborted cycle has not settled');
  h.prepares[0].resolve('LATE');  // prepare ignored the abort signal
  await flush();
  assert.deepEqual(h.discards, ['LATE']);
  assert.equal(h.commits.length, 0);
  assert.equal(h.watcher.currentGeneration, G0);
  assert.equal(h.fetches.length, 2, 'immediate check after becoming visible');
  h.fetches[1].resolve(revision(G1));
  await flush();
  h.setHidden(true);
  h.prepares[1].resolve('HIDDEN');
  await flush();
  assert.deepEqual(h.discards, ['LATE', 'HIDDEN'], 'result arriving while hidden is discarded');
  assert.equal(h.timers.size, 0, 'no timer while hidden');
  assert.equal(h.fetches.length, 2);
  h.setHidden(false);
  assert.equal(h.fetches.length, 3, 'resumes immediately when visible');
  h.watcher.dispose();
});

test('stop and dispose abort, discard late results once and leave no timers', async () => {
  const h = harness();
  h.watcher.start();
  h.fetches[0].resolve(revision(G1));
  await flush();
  h.watcher.dispose();
  h.watcher.dispose();
  assert.ok(h.prepares[0].signal.aborted);
  h.prepares[0].resolve('LATE');
  await flush();
  assert.deepEqual(h.discards, ['LATE']);
  assert.equal(h.commits.length, 0);
  assert.equal(h.timers.size, 0);
  h.watcher.checkNow();
  h.watcher.start();
  h.setHidden(false);
  assert.equal(h.fetches.length, 1, 'nothing after dispose');

  const s = harness();
  s.watcher.start();
  s.fetches[0].resolve(null);
  await flush();
  s.watcher.stop();
  assert.equal(s.timers.size, 0, 'stop clears the timer');
  s.watcher.start();
  assert.equal(s.fetches.length, 2, 'start again checks immediately');
  s.watcher.dispose();
});

test('late fetch after stop is ignored and restart does not overlap', async () => {
  const h = harness();
  h.watcher.start();
  h.watcher.stop();
  h.watcher.start();
  assert.equal(h.fetches.length, 1, 'restart waits for the aborted fetch to settle');
  h.fetches[0].resolve(revision(G1));
  await flush();
  assert.equal(h.prepares.length, 0, 'stale fetch result is not used');
  assert.equal(h.fetches.length, 2, 'then the restarted watcher checks');
  h.watcher.dispose();
});

test('observer and discard exceptions do not stop the watcher; constructor validation', async () => {
  const h = harness({ onState: () => { throw new Error('ui broke'); }, discard: () => { throw new Error('dispose broke'); } });
  h.commitThrows = true;
  h.watcher.start();
  h.fetches[0].resolve(revision(G1));
  await flush();
  h.prepares[0].resolve('P1');
  await flush();
  assert.equal(h.timers.size, 1);
  h.watcher.dispose();
  const ok = { fetchRevision: async () => null, prepare: async () => null, commit() {} };
  assert.throws(() => rw.createRevisionWatcher({ ...ok, commit: null }), TypeError);
  assert.throws(() => rw.createRevisionWatcher({ ...ok, intervalMs: 0 }), RangeError);
  assert.throws(() => rw.createRevisionWatcher({ ...ok, initialGeneration: 'abc' }), RangeError);
  assert.throws(() => rw.createRevisionWatcher({ ...ok, identity: { world: 'uie' } }), TypeError);
});
