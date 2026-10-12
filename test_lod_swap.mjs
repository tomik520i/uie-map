// Tests for web/lod-swap.js. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_lod_swap.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('./web/lod-swap.js', import.meta.url), 'utf8');
const { LodSwap } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}
const tick = async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve(); };

function tile(id, { generation = 'g1', level = 'detail', coarse = 100, detail = 400 } = {}) {
  return { id, generation, level,
    coarse: coarse === null ? null : { key: `${id}-${generation}-c`, bytes: coarse },
    detail: detail === null ? null : { key: `${id}-${generation}-d`, bytes: detail } };
}

function harness({ maxBytes = 1000, maxConcurrent = 4, attachFails = () => false, showFails = () => false,
  detachFails = false, onChangeFails = false, downgradeReserveBytes } = {}) {
  const events = [];
  const loads = [];
  const snapshots = [];
  let serial = 0;
  const lod = new LodSwap({
    maxBytes, maxConcurrent, ...(downgradeReserveBytes === undefined ? {} : { downgradeReserveBytes }),
    load: (entry, rep, signal) => {
      const d = deferred();
      loads.push({ id: entry.id, generation: entry.generation, rep, key: entry[rep].key, bytes: entry[rep].bytes,
        signal, ...d });
      events.push(['load', entry.id, rep, entry.generation]);
      return d.promise;
    },
    attach: (entry, rep, payload) => {
      if (attachFails(entry, rep)) throw new Error('attach boom');
      const resource = { name: `${entry.id}:${rep}:${entry.generation}#${serial += 1}`, payload };
      events.push(['attach', resource.name]);
      return resource;
    },
    setVisible: (resource, visible) => {
      events.push([visible ? 'show' : 'hide', resource.name]);
      if (visible && showFails(resource)) throw new Error('show boom');
    },
    detach: (resource) => {
      events.push(['detach', resource.name]);
      if (detachFails) throw new Error('detach boom');
    },
    onChange: (snapshot) => {
      snapshots.push(snapshot);
      if (onChangeFails) throw new Error('observer boom');
    },
  });
  const settle = async (i, bytes) => { loads[i].resolve({ bytes, data: `payload ${i}` }); await tick(); };
  const view = (id) => lod.getSnapshot().tiles.find((t) => t.id === id);
  const h = { lod, events, loads, snapshots, settle, view, done: 0 };
  /** Settle every load (including ones started by settling) in order, each at its full reservation. */
  h.drain = async () => {
    while (h.done < loads.length) {
      const i = h.done++;
      await settle(i, loads[i].bytes);
    }
  };
  return h;
}

// Two west tiles with larger coarse resources, two east tiles with smaller ones; detail 60 each.
function area(levels) {
  return [['w0', 40], ['w1', 40], ['e0', 20], ['e1', 20]]
    .map(([id, coarse]) => tile(id, { level: levels[id[0]], coarse, detail: 60 }));
}

function assertBudget(h, maxBytes) {
  for (const s of h.snapshots) assert.ok(s.bytes <= maxBytes, `budget exceeded: ${s.bytes} > ${maxBytes}`);
}

test('coarse first, then detail; the switch shows the new resource before hiding the old one', async () => {
  const h = harness();
  h.lod.setDesired([tile('a')]);
  await h.settle(0, 90);
  assert.equal(h.view('a').visible.representation, 'coarse');
  await h.settle(1, 380);
  assert.deepEqual(h.events, [
    ['load', 'a', 'coarse', 'g1'], ['attach', 'a:coarse:g1#1'], ['show', 'a:coarse:g1#1'],
    ['load', 'a', 'detail', 'g1'], ['attach', 'a:detail:g1#2'], ['show', 'a:detail:g1#2'],
    ['hide', 'a:coarse:g1#1'], ['detach', 'a:coarse:g1#1'],
  ]);
  const s = h.lod.getSnapshot();
  assert.equal(s.tiles[0].settled, true);
  assert.deepEqual([s.attachedBytes, s.reservedBytes, s.running], [380, 0, 0], 'actual bytes are accounted');
  assertBudget(h, 1000);
});

test('a tight budget keeps coarse visible, reports limited and does not churn', async () => {
  const h = harness({ maxBytes: 450 });
  h.lod.setDesired([tile('a')]);
  await h.settle(0, 100);
  // coarse 100 visible + detail reservation 400 = 500 > 450: keep coarse.
  assert.equal(h.loads.length, 1);
  assert.equal(h.view('a').visible.representation, 'coarse');
  assert.equal(h.view('a').limited, true);
  h.lod.setDesired([tile('a')]);
  h.lod.retry();
  await tick();
  assert.equal(h.loads.length, 1, 'no repeated attempts while it cannot fit');
  assertBudget(h, 450);
});

test('generation change: no stale result, old generation visible until the new one is attached', async () => {
  const h = harness({ maxBytes: 500 });
  h.lod.setDesired([tile('a')]);
  await h.settle(0, 100);                      // g1 coarse visible; g1 detail loading (100 + 400 = 500)
  assert.equal(h.loads[1].rep, 'detail');
  h.lod.setDesired([tile('a', { generation: 'g2' })]);
  assert.equal(h.loads[1].signal.aborted, true, 'the stale load is aborted');
  // The aborted load ignores the abort and stays reserved: g2 coarse (100) cannot start yet.
  assert.equal(h.loads.length, 2);
  assert.equal(h.lod.getSnapshot().reservedBytes, 400);
  assert.equal(h.view('a').limited, true);
  assert.equal(h.view('a').visible.generation, 'g1');
  await h.settle(1, 400);                      // late stale result: dropped, never attached
  assert.equal(h.events.filter(([e, n]) => e === 'attach' && n.startsWith('a:detail:g1')).length, 0);
  assert.equal(h.loads[2].generation, 'g2');
  assert.equal(h.loads[2].rep, 'coarse', 'the new generation starts with coarse');
  assert.equal(h.view('a').visible.generation, 'g1', 'old generation still visible while g2 loads');
  await h.settle(2, 100);
  assert.equal(h.view('a').visible.generation, 'g2');
  // Show new, hide and detach old in one turn; then the refinement to g2 detail starts (100 + 400 <= 500).
  assert.deepEqual(h.events.slice(-4), [['show', 'a:coarse:g2#2'], ['hide', 'a:coarse:g1#1'], ['detach', 'a:coarse:g1#1'],
    ['load', 'a', 'detail', 'g2']]);
  assert.equal(h.loads[3].rep, 'detail');
  assertBudget(h, 500);
});

test('reversal with an abort-ignoring load: pending cap includes it, only the new result attaches', async () => {
  const h = harness({ maxConcurrent: 1 });
  h.lod.setDesired([tile('a')]);
  await h.settle(0, 100);
  h.lod.setDesired([tile('a', { level: 'coarse' })]);   // detail no longer wanted
  assert.equal(h.loads[1].signal.aborted, true);
  h.lod.setDesired([tile('a')]);                        // wanted again before the old load settled
  assert.equal(h.loads.length, 2, 'the obsolete load still occupies the only slot');
  await h.settle(1, 400);                               // obsolete result ignored
  assert.equal(h.loads.length, 3);
  await h.settle(2, 400);
  const attached = h.events.filter(([e, n]) => e === 'attach' && n.startsWith('a:detail'));
  assert.equal(attached.length, 1, 'exactly one detail resource');
  assert.equal(h.view('a').visible.representation, 'detail');
});

test('switching down to coarse keeps detail visible until coarse is attached', async () => {
  const h = harness();
  h.lod.setDesired([tile('a', { coarse: null, level: 'detail' })]);
  await h.settle(0, 400);
  h.lod.setDesired([tile('a', { coarse: 100, level: 'coarse' })]);
  assert.equal(h.view('a').visible.representation, 'detail');
  await h.settle(1, 100);
  assert.deepEqual(h.events.slice(-4), [['attach', 'a:coarse:g1#2'], ['show', 'a:coarse:g1#2'],
    ['hide', 'a:detail:g1#1'], ['detach', 'a:detail:g1#1']]);
});

test('removal detaches, dispose detaches every resource exactly once and drops late results', async () => {
  const h = harness();
  h.lod.setDesired([tile('a', { level: 'coarse' }), tile('b', { level: 'coarse' }), tile('c', { level: 'coarse' })]);
  await h.settle(0, 100);
  await h.settle(1, 100);
  h.lod.setDesired([tile('b', { level: 'coarse' }), tile('c', { level: 'coarse' })]);
  assert.deepEqual(h.events.slice(-2), [['hide', 'a:coarse:g1#1'], ['detach', 'a:coarse:g1#1']]);
  h.lod.dispose();
  h.lod.dispose();
  await h.settle(2, 100);                   // c was still loading
  const detached = h.events.filter(([e]) => e === 'detach').map(([, n]) => n);
  assert.deepEqual(detached, ['a:coarse:g1#1', 'b:coarse:g1#2']);
  assert.equal(h.events.filter(([e, n]) => e === 'attach' && n.startsWith('c')).length, 0);
  assert.equal(h.loads[2].signal.aborted, true);
  h.lod.setDesired([tile('d')]);
  assert.equal(h.loads.length, 3, 'nothing happens after dispose');
});

test('attach failure, oversize result and load failure keep the old resource and are not retried until retry()', async () => {
  const h = harness({ maxBytes: 2000, attachFails: (entry, rep) => rep === 'detail' && entry.id === 'a' });
  h.lod.setDesired([tile('a'), tile('b'), tile('c')]);
  for (let i = 0; i < 3; i += 1) await h.settle(i, 100);   // coarse for a, b, c
  await h.settle(3, 400);                                    // a detail: attach throws
  await h.settle(4, 401);                                    // b detail: actual > reserved 400
  h.loads[5].reject(new Error('network'));                   // c detail: load fails
  await tick();
  for (const id of ['a', 'b', 'c']) {
    assert.equal(h.view(id).visible.representation, 'coarse', `${id} keeps coarse`);
    assert.equal(h.view(id).failed.length, 1);
  }
  assert.match(h.view('b').failed[0].message, /exceeds the reserved 400 B/);
  assert.match(h.view('c').failed[0].message, /network/);
  assert.equal(h.lod.getSnapshot().attachedBytes, 300, 'rejected payloads are not accounted');
  h.lod.setDesired([tile('a'), tile('b'), tile('c')]);
  await tick();
  assert.equal(h.loads.length, 6, 'no requeue of failures');
  h.lod.retry();
  assert.equal(h.loads.length, 9, 'retry tries each failed detail once more');
});

test('desired order is the priority and the budget holds for every snapshot', async () => {
  const h = harness({ maxConcurrent: 1, maxBytes: 600 });
  h.lod.setDesired([tile('b', { level: 'coarse' }), tile('a', { level: 'coarse' }), tile('c')]);
  assert.deepEqual(h.loads.map((l) => l.id), ['b']);
  await h.settle(0, 100);
  assert.deepEqual(h.loads.map((l) => l.id), ['b', 'a']);
  await h.settle(1, 100);
  await h.settle(2, 100);       // c coarse
  // c detail: 300 attached + 400 reserved > 600 -> limited, c keeps coarse.
  assert.equal(h.view('c').limited, true);
  assert.equal(h.loads.length, 3);
  assertBudget(h, 600);
});

test('coverage first: every tile gets coarse before the first detail refinement', async () => {
  // Budget: all five coarse (5 x 100) plus one detail handoff (400).
  const h = harness({ maxConcurrent: 1, maxBytes: 900 });
  const ids = ['t0', 't1', 't2', 't3', 't4'];
  h.lod.setDesired(ids.map((id) => tile(id)));
  for (let i = 0; i < 5; i += 1) await h.settle(i, 100);
  assert.deepEqual(h.loads.slice(0, 5).map((l) => [l.id, l.rep]), ids.map((id) => [id, 'coarse']));
  assert.deepEqual([h.loads[5].id, h.loads[5].rep], ['t0', 'detail'], 'refinement only after full coverage');
  await h.settle(5, 400);
  assert.equal(h.view('t0').visible.representation, 'detail');
  for (const id of ids.slice(1)) {
    assert.equal(h.view(id).visible.representation, 'coarse', `${id} keeps coverage`);
    assert.equal(h.view(id).limited, true);
  }
  assert.equal(h.loads.length, 6, 'no further attempts while nothing fits');
  assertBudget(h, 900);
});

test('refinement waits while an uncovered tile only needs pending reservations to settle', async () => {
  const h = harness({ maxBytes: 500 });
  const y = (level) => tile('y', { level, coarse: 100, detail: 100 });
  const w = tile('w', { level: 'coarse', coarse: 250 });
  const x = tile('x', { level: 'coarse', coarse: 200 });
  h.lod.setDesired([y('coarse')]);
  await h.settle(0, 100);                              // y coarse visible (100)
  h.lod.setDesired([y('coarse'), w]);                  // w coarse loading (250 reserved)
  h.lod.setDesired([y('detail'), w, x]);               // x: 100 + 250 + 200 > 500, but fits once w settles
  assert.equal(h.loads.length, 2, 'y detail would fit now, but it must not take the room x is waiting for');
  assert.equal(h.view('x').limited, true);
  assert.equal(h.view('y').limited, true);
  await h.settle(1, 250);                              // now x can never fit (350 + 200 > 500)...
  assert.equal(h.view('x').limited, true);
  assert.deepEqual([h.loads[2].id, h.loads[2].rep], ['y', 'detail'], '...so it no longer blocks refinement');
  assertBudget(h, 500);
});

test('setVisible failure on the replacement keeps the old resource visible and detaches the new one once', async () => {
  const h = harness({ showFails: (r) => r.name.startsWith('a:detail') });
  h.lod.setDesired([tile('a')]);
  await h.settle(0, 100);
  await h.settle(1, 400);
  assert.deepEqual(h.events, [
    ['load', 'a', 'coarse', 'g1'], ['attach', 'a:coarse:g1#1'], ['show', 'a:coarse:g1#1'],
    ['load', 'a', 'detail', 'g1'], ['attach', 'a:detail:g1#2'], ['show', 'a:detail:g1#2'], ['detach', 'a:detail:g1#2'],
  ], 'the old coarse resource is never hidden or detached');
  const view = h.view('a');
  assert.equal(view.visible.representation, 'coarse');
  assert.equal(view.visible.key, 'a-g1-c');
  assert.match(view.failed[0].message, /setVisible failed/);
  assert.deepEqual([h.lod.getSnapshot().attachedBytes, h.lod.getSnapshot().reservedBytes], [100, 0]);
  h.lod.setDesired([tile('a')]);
  await tick();
  assert.equal(h.loads.length, 2, 'no automatic retry');
});

test('failure history keeps only the current descriptors and ignores stale results', async () => {
  const h = harness();
  const a = (g) => tile('a', { generation: g, level: 'coarse', detail: null });
  for (let g = 1; g <= 20; g += 1) {
    h.lod.setDesired([a(`g${g}`)]);
    h.loads[g - 1].reject(new Error(`fail g${g}`));
    await tick();
  }
  const failed = h.view('a').failed;
  assert.equal(failed.length, 1, 'older generations are pruned');
  assert.equal(failed[0].generation, 'g20');
  h.lod.setDesired([a('g20')]);
  assert.equal(h.loads.length, 20, 'the current failure still prevents a requeue');
  h.lod.retry();
  assert.equal(h.loads.length, 21);
  // A stale load that fails after its generation was replaced is not recorded.
  h.lod.setDesired([a('g21')]);
  assert.equal(h.loads[20].signal.aborted, true);
  h.loads[20].reject(new Error('stale'));
  await tick();
  assert.deepEqual(h.view('a').failed, []);
});

test('without a downgrade reserve a filled detail budget deadlocks (the observed browser case)', async () => {
  const h = harness({ maxBytes: 230, maxConcurrent: 1 });
  h.lod.setDesired(area({ w: 'detail', e: 'detail' }));
  await h.drain();
  // coarse 120; w0, w1, e0 detail (200 attached); e1 detail would need 260.
  assert.equal(h.lod.getSnapshot().attachedBytes, 200);
  const before = h.loads.length;
  h.lod.setDesired(area({ w: 'coarse', e: 'detail' }));
  await h.drain();
  assert.equal(h.loads.length, before, 'no load can start: 30 B free, every west coarse needs 40');
  for (const id of ['w0', 'w1']) {
    assert.equal(h.view(id).visible.representation, 'detail', 'coverage is kept...');
    assert.equal(h.view(id).limited, true, '...but the downgrade is stuck');
  }
  assert.equal(h.lod.getSnapshot().downgradeReserveBytes, 0, 'default reserve is 0 (previous behaviour)');
});

test('a downgrade reserve keeps west/east swaps moving with full coverage inside the budget', async () => {
  const h = harness({ maxBytes: 230, maxConcurrent: 1, downgradeReserveBytes: 40 });
  assert.equal(h.lod.getSnapshot().downgradeReserveBytes, 40);
  h.lod.setDesired(area({ w: 'detail', e: 'detail' }));
  await h.drain();
  // Detail admission is limited to 230 - 40 = 190: only w0 refines (120 coarse -> 140 attached).
  assert.equal(h.view('w0').visible.representation, 'detail');
  assert.ok(h.lod.getSnapshot().attachedBytes <= 190);
  const covered = h.snapshots.length;

  const cycle = async (levels, downgraded, refined) => {
    const start = h.loads.length;
    h.lod.setDesired(area(levels));
    await h.drain();
    const started = h.loads.slice(start).map((l) => `${l.id}:${l.rep}`);
    assert.equal(started[0], `${downgraded}:coarse`, 'the downgrade starts before any upgrade');
    assert.ok(started.includes(`${refined}:detail`), `${refined} refines after the downgrade freed room`);
    assert.equal(h.view(downgraded).visible.representation, 'coarse');
    assert.equal(h.view(refined).visible.representation, 'detail');
  };
  await cycle({ w: 'coarse', e: 'detail' }, 'w0', 'e0');
  await cycle({ w: 'detail', e: 'coarse' }, 'e0', 'w0');
  await cycle({ w: 'coarse', e: 'detail' }, 'w0', 'e0');
  for (const snapshot of h.snapshots.slice(covered)) {
    assert.ok(snapshot.tiles.every((t) => t.visible), 'no tile ever loses coverage');
  }
  assertBudget(h, 230);
});

test('downgrade reserve option is validated and coarse may still use the whole budget', async () => {
  const fn = () => {};
  const base = { load: fn, attach: fn, detach: fn, setVisible: fn, maxBytes: 100 };
  for (const bad of [-1, 1.5, 101, true, '10', NaN]) {
    assert.throws(() => new LodSwap({ ...base, downgradeReserveBytes: bad }), RangeError, String(bad));
  }
  assert.equal(new LodSwap({ ...base, downgradeReserveBytes: 100 }).getSnapshot().downgradeReserveBytes, 100);
  // With the whole budget reserved, coarse coverage still loads and detail never starts.
  const h = harness({ maxBytes: 100, downgradeReserveBytes: 100 });
  h.lod.setDesired([tile('a', { coarse: 100, detail: 50 })]);
  await h.drain();
  assert.equal(h.view('a').visible.representation, 'coarse');
  assert.equal(h.view('a').limited, true);
  assert.equal(h.loads.length, 1);
});

test('callback failures are reported (bounded) without breaking the controller', async () => {
  const h = harness({ detachFails: true, onChangeFails: true });
  h.lod.setDesired([tile('a', { level: 'coarse' })]);
  await h.settle(0, 100);
  h.lod.setDesired([]);
  const s = h.lod.getSnapshot();
  assert.equal(s.attachedBytes, 0, 'bytes are released even if detach throws');
  assert.ok(s.errors.some((e) => /detach failed/.test(e.message)));
  assert.ok(s.errors.some((e) => /onChange failed/.test(e.message)));
  assert.ok(s.errors.length <= 32);
});

test('validation of descriptors and options', () => {
  const h = harness();
  const bad = [
    'x', [null], [{ ...tile('a'), id: '' }], [tile('a'), tile('a')],
    [{ ...tile('a'), generation: '' }], [{ ...tile('a'), level: 'medium' }],
    [{ ...tile('a'), detail: null }], [{ ...tile('a', { level: 'coarse' }), coarse: null }],
    [{ ...tile('a'), coarse: { key: 'k', bytes: -1 } }], [{ ...tile('a'), coarse: { key: 'k', bytes: 1.5 } }],
    [{ ...tile('a'), coarse: { key: '', bytes: 1 } }],
    [{ ...tile('a'), coarse: { key: 'same', bytes: 1 }, detail: { key: 'same', bytes: 1 } }],
    [tile('a'), { ...tile('b'), coarse: { key: 'a-g1-c', bytes: 1 } }],
  ];
  for (const entries of bad) assert.throws(() => h.lod.setDesired(entries), TypeError, JSON.stringify(entries).slice(0, 60));
  assert.equal(h.loads.length, 0, 'invalid input changes nothing');
  const fn = () => {};
  const base = { load: fn, attach: fn, detach: fn, setVisible: fn, maxBytes: 10 };
  assert.throws(() => new LodSwap({ ...base, maxBytes: -1 }), RangeError);
  assert.throws(() => new LodSwap({ ...base, maxBytes: undefined }), RangeError);
  assert.throws(() => new LodSwap({ ...base, maxConcurrent: 0 }), RangeError);
  assert.throws(() => new LodSwap({ ...base, setVisible: null }), TypeError);
});
