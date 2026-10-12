// Tests for web/collision-follow.js: pointer following while walking, validated swaps, bounded polling.
// Run from the work directory:  node --test uie-map/test_collision_follow.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createCollisionFollower, parseCollisionPointer, MAX_BACKOFF_MS } from './web/collision-follow.js';

const hash = (b) => createHash('sha256').update(b).digest('hex');
const flush = async () => { for (let i = 0; i < 20; i += 1) await new Promise(setImmediate); };
const A = 'a'.repeat(64), B = 'b'.repeat(64), C = 'c'.repeat(64);
const pointerBytes = (indexSha256) => Buffer.from(JSON.stringify({ format: 'UIE_COLLISION_POINTER1', indexSha256 }));
const toArrayBuffer = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.length);
const WALK = { walking: true, x: 32, z: 32, geometryAt: () => 'b'.repeat(12), atlasSha: 'a'.repeat(64) };
const FLY = { ...WALK, walking: false };

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  promise.catch(() => {});
  return { promise, resolve, reject };
}

function clock() {
  let t = 0, id = 0;
  const timers = new Map();
  return {
    now: () => t,
    setTimer: (fn, ms) => { id += 1; timers.set(id, { fn, at: t + ms }); return id; },
    clearTimer: (k) => { timers.delete(k); },
    pending: () => timers.size,
    async advance(ms) {
      const end = t + ms;
      for (;;) {
        const due = [...timers].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        t = due[1].at;
        timers.delete(due[0]);
        due[1].fn();
        await flush();
      }
      t = end;
      await flush();
    },
  };
}

// Pointer server: mode 'ok' | 'error' | 'hang' (ignores abort) | 'body' (body never arrives) | 'manual' | raw bytes.
function pointerServer() {
  const s = { hash: A, mode: 'ok', calls: [], manual: [] };
  s.fetcher = async (url, options) => {
    s.calls.push({ url, options });
    if (s.mode === 'hang') return new Promise(() => {});
    if (s.mode === 'body') return { ok: true, status: 200, arrayBuffer: () => new Promise(() => {}) };
    if (s.mode === 'error') return { ok: false, status: 503 };
    if (s.mode === 'manual') { const d = deferred(); s.manual.push({ d, signal: options.signal }); return d.promise; }
    const raw = s.mode instanceof Buffer ? s.mode : pointerBytes(s.hash);
    return { ok: true, status: 200, arrayBuffer: async () => toArrayBuffer(raw) };
  };
  s.respond = (hash = s.hash) => s.manual.shift().d.resolve(
    { ok: true, status: 200, arrayBuffer: async () => toArrayBuffer(pointerBytes(hash)) });
  return s;
}

// Lifecycle stand-in for createCollisionWorld: controllable loadIndex, records views, 100 bytes while walking.
function worldFactory({ rejectOnDispose = true } = {}) {
  const f = { worlds: [], log: [] };
  f.create = ({ indexHash }) => {
    const d = deferred();
    const w = {
      hash: indexHash, views: [], disposed: false, walkingNow: false, loadCalls: 0, d,
      loadIndex() { w.loadCalls += 1; return d.promise; },
      update(view) {
        assert.equal(w.disposed, false, 'a disposed world is never updated');
        w.views.push(view);
        w.walkingNow = Boolean(view?.walking);
        if (w.walkingNow) {
          f.log.push({ walk: indexHash, otherWalking: f.worlds.filter((o) => o !== w && o.walkingNow && !o.disposed).length });
        }
      },
      blockAt() { return w.walkingNow ? { name: indexHash } : undefined; },
      get bytes() { return w.walkingNow ? 100 : 0; },
      get ready() { return w.walkingNow; },
      get resident() { return w.walkingNow ? 1 : 0; },
      dispose() { w.disposed = true; w.walkingNow = false; if (rejectOnDispose) d.reject(new Error('Collision index cancelled')); },
    };
    f.worlds.push(w);
    return w;
  };
  return f;
}

function setup(options = {}) {
  const c = clock(), server = pointerServer(), factory = worldFactory(options);
  const errors = [], changes = [];
  const follower = createCollisionFollower({ base: '/collision/', fetcher: server.fetcher, timeoutMs: 1000,
    now: c.now, setTimer: c.setTimer, clearTimer: c.clearTimer, createWorld: factory.create,
    onError: (kind, sha, error) => errors.push({ kind, sha, message: error.message }), onChange: () => changes.push(1) });
  return { c, server, factory, follower, errors, changes };
}

async function walkingOn(h, hash = A) {
  h.server.hash = hash;
  h.follower.update(WALK);
  await flush();
  const world = h.factory.worlds.at(-1);
  world.d.resolve();
  await flush();
  return world;
}

test('first pointer loads the index without a view, then activates the latest view; same pointer rebuilds nothing', async () => {
  const h = setup();
  h.follower.update(WALK);
  await flush();
  assert.equal(h.server.calls.length, 1);
  assert.equal(h.server.calls[0].url, '/collision/current.json');
  assert.equal(h.server.calls[0].options.cache, 'no-store');
  const [world] = h.factory.worlds;
  assert.equal(world.hash, A);
  assert.equal(world.loadCalls, 1);
  assert.deepEqual(world.views, [], 'no block window while the candidate prepares');
  const latest = { ...WALK, x: 40 };
  h.follower.update(latest);
  world.d.resolve();
  await flush();
  assert.deepEqual(world.views, [latest], 'only the latest view after the swap');
  assert.equal(h.follower.blockAt(1, 2, 3).name, A);
  assert.equal(h.follower.ready, true);
  assert.equal(h.follower.indexSha256, A);
  for (let i = 0; i < 20; i += 1) h.follower.update(WALK);
  await h.c.advance(29_999);
  assert.equal(h.server.calls.length, 1, 'at most one poll per 30 s, updates do not poll');
  await h.c.advance(1);
  assert.equal(h.server.calls.length, 2);
  assert.equal(h.factory.worlds.length, 1, 'same index hash: no new world');
  assert.equal(h.follower.stats.swaps, 1);
  h.follower.dispose();
});

test('a changed pointer prepares without blocks and swaps after disposing the old world (bounded bytes)', async () => {
  const h = setup();
  const old = await walkingOn(h, A);
  h.server.hash = B;
  await h.c.advance(30_000);
  const next = h.factory.worlds.at(-1);
  assert.equal(next.hash, B);
  assert.deepEqual(next.views, []);
  assert.equal(h.follower.bytes, 100, 'only the active window while the candidate prepares');
  assert.equal(h.follower.blockAt(0, 0, 0).name, A, 'old world serves meanwhile');
  next.d.resolve();
  await flush();
  assert.equal(old.disposed, true);
  assert.deepEqual(h.factory.log.at(-1), { walk: B, otherWalking: 0 }, 'old disposed before the new window starts');
  assert.equal(h.follower.blockAt(0, 0, 0).name, B);
  assert.equal(h.follower.bytes, 100);
  assert.equal(h.follower.stats.indexSha256, B);
  h.follower.dispose();
});

test('failed or corrupt candidate index keeps the usable old world and backs off', async () => {
  const h = setup();
  await walkingOn(h, A);
  h.server.hash = B;
  await h.c.advance(30_000);
  h.factory.worlds.at(-1).d.reject(new Error('Collision index identity mismatch'));
  await flush();
  assert.equal(h.factory.worlds.at(-1).disposed, true);
  assert.equal(h.follower.blockAt(0, 0, 0).name, A);
  assert.equal(h.follower.stats.indexErrors, 1);
  assert.deepEqual(h.errors.at(-1), { kind: 'index', sha: B, message: 'Collision index identity mismatch' });
  const worlds = h.factory.worlds.length;
  for (let i = 0; i < 30; i += 1) h.follower.update(WALK);
  await h.c.advance(29_999);
  assert.equal(h.factory.worlds.length, worlds, 'no hot retry');
  await h.c.advance(1);
  assert.equal(h.factory.worlds.length, worlds + 1, 'retried after the backoff');
  h.follower.dispose();
});

test('pointer errors, corrupt pointers, timeouts and hung bodies keep the old world; backoff up to 5 min', async () => {
  const h = setup();
  await walkingOn(h, A);
  const fetches = () => h.server.calls.length;
  h.server.mode = 'error';
  await h.c.advance(30_000);
  assert.equal(fetches(), 2);
  await h.c.advance(29_999);
  assert.equal(fetches(), 2, 'first failure waits 30 s');
  await h.c.advance(1);
  assert.equal(fetches(), 3);
  await h.c.advance(59_999);
  assert.equal(fetches(), 3, 'second failure waits 60 s');
  await h.c.advance(1);
  assert.equal(fetches(), 4);
  const nextPoll = () => h.c.advance(h.follower.stats.nextPollInMs);   // exactly to the scheduled poll
  for (const mode of [Buffer.from('{"format":"UIE_COLLISION_POINTER1"}'), Buffer.from('not json'),
    Buffer.from(JSON.stringify({ format: 'UIE_COLLISION_POINTER1', indexSha256: B, extra: 1 })), Buffer.alloc(5000, 32)]) {
    h.server.mode = mode;
    const errors = h.follower.stats.pointerErrors;
    await nextPoll();
    assert.equal(h.follower.stats.pointerErrors, errors + 1, `refused pointer ${mode.length} bytes`);
    assert.ok(h.follower.stats.nextPollInMs <= MAX_BACKOFF_MS, 'backoff capped at 5 min');
  }
  for (const mode of ['hang', 'body']) {
    h.server.mode = mode;
    await nextPoll();
    assert.equal(h.follower.stats.requestInFlight, true, mode);
    await h.c.advance(999);
    assert.equal(h.follower.stats.requestInFlight, true);
    await h.c.advance(1);
    assert.equal(h.follower.stats.requestInFlight, false, `${mode}: deadline also when the fetch ignores the abort`);
    assert.match(h.errors.at(-1).message, /timeout/);
  }
  assert.equal(h.follower.stats.nextPollInMs, MAX_BACKOFF_MS);
  assert.equal(h.factory.worlds.length, 1, 'no candidate from a broken pointer');
  assert.equal(h.follower.blockAt(0, 0, 0).name, A);
  assert.ok(h.errors.every((e) => e.kind === 'pointer'));
  h.follower.dispose();
});

test('no overlap: a pending pointer request is never duplicated by updates or time', async () => {
  const h = setup();
  h.server.mode = 'manual';
  h.follower.update(WALK);
  for (let i = 0; i < 50; i += 1) h.follower.update({ ...WALK, x: i });
  await h.c.advance(999);
  assert.equal(h.server.calls.length, 1);
  h.server.respond(A);
  await flush();
  assert.equal(h.server.calls.length, 1);
  assert.equal(h.factory.worlds.length, 1);
  h.follower.dispose();
});

test('orbit/fly never touches the network; leaving walking cancels and ignores late completions', async () => {
  const h = setup();
  for (let i = 0; i < 10; i += 1) h.follower.update(FLY);
  await h.c.advance(10 * MAX_BACKOFF_MS);
  assert.equal(h.server.calls.length, 0);
  // Pointer request in flight -> stop: aborted, the late answer is ignored.
  h.server.mode = 'manual';
  h.follower.update(WALK);
  const first = h.server.manual[0];
  h.follower.update(FLY);
  assert.equal(first.signal.aborted, true);
  h.server.respond(A);
  await flush();
  assert.equal(h.factory.worlds.length, 0);
  assert.equal(h.c.pending(), 0, 'no timers while not walking');
  // Candidate preparing -> stop: discarded; reactivation starts over, the late old completion is ignored.
  h.follower.update(WALK);
  h.server.respond(A);
  await flush();
  const discarded = h.factory.worlds[0];
  h.follower.update(FLY);
  assert.equal(discarded.disposed, true);
  h.follower.update(WALK);
  await flush();
  assert.equal(h.server.calls.length, 2, 'reactivation keeps the 30 s interval of the last successful poll');
  await h.c.advance(h.follower.stats.nextPollInMs);
  assert.equal(h.server.calls.length, 3);
  h.server.respond(A);
  await flush();
  const fresh = h.factory.worlds[1];
  discarded.d.resolve();
  await flush();
  assert.equal(h.follower.indexSha256, null, 'the discarded candidate never activates');
  fresh.d.resolve();
  await flush();
  assert.equal(h.follower.indexSha256, A);
  assert.equal(h.follower.blockAt(0, 0, 0).name, A);
  h.follower.update(FLY);
  assert.equal(h.follower.blockAt(0, 0, 0), undefined);
  assert.equal(h.follower.bytes, 0, 'the active world released its window, its index is kept');
  assert.equal(fresh.disposed, false);
  h.follower.dispose();
});

test('a late candidate completion after dispose or after a newer pointer is ignored', async () => {
  const h = setup({ rejectOnDispose: false });
  await walkingOn(h, A);
  h.server.hash = B;
  await h.c.advance(30_000);
  const b = h.factory.worlds.at(-1);
  h.server.hash = C;
  await h.c.advance(30_000);
  const c = h.factory.worlds.at(-1);
  assert.equal(b.disposed, true, 'superseded candidate discarded');
  b.d.resolve();
  await flush();
  assert.equal(h.follower.indexSha256, A);
  h.follower.dispose();
  c.d.resolve();
  await flush();
  assert.equal(h.follower.indexSha256, null);
  assert.ok(h.factory.worlds.every((w) => w.disposed));
  assert.equal(h.c.pending(), 0);
  h.follower.update(WALK);
  await h.c.advance(MAX_BACKOFF_MS);
  assert.equal(h.server.calls.length, 3, 'nothing after dispose');
});

test('pointer parser is strict', () => {
  assert.equal(parseCollisionPointer(toArrayBuffer(pointerBytes(A))), A);
  for (const raw of [Buffer.from('[]'), Buffer.from('{}'), pointerBytes('A'.repeat(64)), Buffer.alloc(4097, 32)]) {
    assert.throws(() => parseCollisionPointer(toArrayBuffer(raw)));
  }
});

// ---------------------------------------------------------------- with the real createCollisionWorld

function collisionFixture() {
  const files = new Map(), pairs = {};
  for (const [x, z] of [[0, 0], [1, 0]]) {
    const registry = Buffer.from('{"0":"minecraft:air"}');
    const blocks = Buffer.alloc(20 + registry.length + 64 * 64 * 256 * 3);
    blocks.write('UIB1'); blocks.writeInt32LE(x * 64, 4); blocks.writeInt32LE(z * 64, 8); blocks.writeUInt16LE(65535, 12);
    blocks.writeUInt32LE(registry.length, 16); registry.copy(blocks, 20);
    const bsha = hash(blocks); files.set(`/objects/${bsha}.uib`, blocks);
    const pair = Buffer.from(JSON.stringify({ format: 'UIE_COLLISION_PAIR1', x: x * 64, z: z * 64, blocksSha256: bsha,
      geometrySha256: 'b'.repeat(64), atlasSha256: 'a'.repeat(64) }));
    pairs[`${x},${z}`] = hash(pair); files.set(`/pairs/${hash(pair)}.json`, pair);
  }
  const index = (p) => { const raw = Buffer.from(JSON.stringify({ format: 'UIE_COLLISION_INDEX1', tileSize: 64, pairs: p }));
    files.set(`/indexes/${hash(raw)}.json`, raw); return hash(raw); };
  const one = index({ '0,0': pairs['0,0'] });
  const both = index(pairs);
  const corrupt = 'd'.repeat(64);
  files.set(`/indexes/${corrupt}.json`, Buffer.from('{"format":"UIE_COLLISION_INDEX1"}'));   // hash does not match
  let pointer = one;
  const calls = [];
  const fetcher = async (url) => {
    calls.push(url);
    const raw = url === '/current.json' ? pointerBytes(pointer) : files.get(url);
    return { ok: !!raw, status: raw ? 200 : 404, arrayBuffer: async () => toArrayBuffer(raw) };
  };
  return { one, both, corrupt, calls, fetcher, setPointer: (h) => { pointer = h; } };
}

test('real collision world: first index, corrupt candidate refused, valid swap, bytes bounded', async () => {
  const f = collisionFixture(), c = clock();
  const follower = createCollisionFollower({ base: '/', fetcher: f.fetcher, now: c.now, setTimer: c.setTimer, clearTimer: c.clearTimer });
  try {
    const view = { ...WALK, x: 32, z: 32 };
    follower.update(view);
    await flush();
    assert.equal(follower.indexSha256, f.one);
    assert.equal(follower.blockAt(32, 64, 32).name, 'minecraft:air');
    assert.equal(follower.blockAt(96, 64, 32), undefined, 'not in the first index');
    f.setPointer(f.corrupt);
    await c.advance(30_000);
    assert.equal(follower.indexSha256, f.one, 'hash-mismatched index never activates');
    assert.equal(follower.blockAt(32, 64, 32).name, 'minecraft:air');
    f.setPointer(f.both);
    await c.advance(30_000);
    assert.equal(follower.indexSha256, f.both);
    follower.update({ ...view, x: 96 });
    await flush();
    assert.equal(follower.blockAt(96, 64, 32).name, 'minecraft:air');
    assert.ok(follower.bytes <= 4 * 3 * 1024 * 1024);
    follower.update({ ...view, walking: false });
    assert.equal(follower.bytes, 0);
    const before = f.calls.length;
    await c.advance(10 * MAX_BACKOFF_MS);
    assert.equal(f.calls.length, before, 'no network while not walking');
  } finally {
    follower.dispose();
  }
});
