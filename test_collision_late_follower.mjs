// Late browser follower after GC removed old immutable collision files (HTTP 404). Real collision-follow.js,
// collision-world.js and collision-source.js; only the transport (an in-memory store) and the follower's clock are
// mocked. Old revision = air, new revision = stone, so a stale or mixed answer is visible as "air".
// Run from the work directory:  node --test uie-map/test_collision_late_follower.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createCollisionFollower } from './web/collision-follow.js';

const hash = (b) => createHash('sha256').update(b).digest('hex');
const flush = async () => { for (let i = 0; i < 80; i += 1) await new Promise(setImmediate); };
const toArrayBuffer = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.length);
const COUNT = 64 * 64 * 256;
const TILE_BYTES = COUNT * 3;
const ATLAS = 'a'.repeat(64);
const G_OLD = 'b'.repeat(64), G_NEW = 'c'.repeat(64);
const BASE = '/data/collision/';

function deferred() {
  let resolve;
  const promise = new Promise((a) => { resolve = a; });
  return { promise, resolve };
}

function clock() {
  let t = 0, id = 0;
  const timers = new Map();
  return {
    now: () => t,
    setTimer: (fn, ms) => { id += 1; timers.set(id, { fn, at: t + ms }); return id; },
    clearTimer: (k) => { timers.delete(k); },
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

function uib(tx, tz, stone) {
  const registry = Buffer.from(stone ? '{"0":"minecraft:air","1":"minecraft:stone"}' : '{"0":"minecraft:air"}');
  const blocks = Buffer.alloc(20 + registry.length + TILE_BYTES);
  blocks.write('UIB1');
  blocks.writeInt32LE(tx * 64, 4); blocks.writeInt32LE(tz * 64, 8);
  blocks.writeUInt16LE(65535, 12); blocks.writeUInt32LE(registry.length, 16);
  registry.copy(blocks, 20);
  if (stone) for (let i = 0, off = 20 + registry.length; i < COUNT; i += 1) blocks[off + 2 * i] = 1;
  return blocks;
}

// In-memory collision store with the HTTP layout; a deleted (GC'd) file answers 404.
function store() {
  const files = new Map();
  const holds = new Map();
  const s = {
    pointer: null, calls: [],
    pair(tx, tz, { stone, geometry }) {
      const blocks = uib(tx, tz, stone);
      const blocksSha = hash(blocks);
      files.set(`objects/${blocksSha}.uib`, blocks);
      const raw = Buffer.from(JSON.stringify({ format: 'UIE_COLLISION_PAIR1', x: tx * 64, z: tz * 64,
        atlasSha256: ATLAS, geometrySha256: geometry, blocksSha256: blocksSha }));
      const pairHash = hash(raw);
      files.set(`pairs/${pairHash}.json`, raw);
      return { pairHash, blocksSha };
    },
    index(pairs) {
      const raw = Buffer.from(JSON.stringify({ format: 'UIE_COLLISION_INDEX1', tileSize: 64, pairs }));
      files.set(`indexes/${hash(raw)}.json`, raw);
      return hash(raw);
    },
    gc(...paths) { for (const p of paths) assert.ok(files.delete(p), `fixture: ${p} existed`); },
    hold(path) { const d = deferred(); holds.set(path, d); return d; },
    async fetcher(url) {
      const rel = url.slice(BASE.length);
      s.calls.push(rel);
      if (holds.has(rel)) await holds.get(rel).promise;
      const raw = rel === 'current.json'
        ? Buffer.from(JSON.stringify({ format: 'UIE_COLLISION_POINTER1', indexSha256: s.pointer }))
        : files.get(rel);
      return raw ? { ok: true, status: 200, arrayBuffer: async () => toArrayBuffer(raw) }
        : { ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0) };
    },
  };
  return s;
}

function setup({ x = 32, z = 32 } = {}) {
  const c = clock(), s = store(), errors = [];
  const visible = {};                                    // hires geometry the browser shows per tile (sha12)
  const view = { walking: true, x, z, atlasSha: ATLAS, geometryAt: (key) => visible[key] };
  const follower = createCollisionFollower({ base: BASE, fetcher: s.fetcher, now: c.now, setTimer: c.setTimer,
    clearTimer: c.clearTimer, onError: (kind, sha, error) => errors.push({ kind, sha, message: error.message }) });
  return { c, s, errors, visible, view, follower };
}

const name = (h, x, z) => h.follower.blockAt(x, 64, z)?.name;

// Old revision O (air) is published, followed and loaded; returns the hashes needed for GC.
async function followOld(h, keys = ['0,0']) {
  const old = {};
  for (const key of keys) {
    const [tx, tz] = key.split(',').map(Number);
    old[key] = h.s.pair(tx, tz, { stone: false, geometry: G_OLD });
    h.visible[key] = G_OLD.slice(0, 12);
  }
  const O = h.s.index(Object.fromEntries(keys.map((k) => [k, old[k].pairHash])));
  h.s.pointer = O;
  h.follower.update(h.view);
  await flush();
  assert.equal(h.follower.indexSha256, O);
  for (const key of keys) {
    const [tx, tz] = key.split(',').map(Number);
    assert.equal(name(h, tx * 64 + 1, tz * 64 + 1), 'minecraft:air', `fixture: old ${key} loaded`);
  }
  return { O, old };
}

function publishNew(h, keys = ['0,0'], { visibleNow = true } = {}) {
  const neu = {};
  for (const key of keys) {
    const [tx, tz] = key.split(',').map(Number);
    neu[key] = h.s.pair(tx, tz, { stone: true, geometry: G_NEW });
    if (visibleNow) h.visible[key] = G_NEW.slice(0, 12);
  }
  const N = h.s.index(Object.fromEntries(keys.map((k) => [k, neu[k].pairHash])));
  h.s.pointer = N;
  return { N, neu };
}

test('loaded old tile after GC: unknown once the visible revision moves on, then recovery to the new index', async () => {
  const h = setup();
  try {
    const { O, old } = await followOld(h);
    const { N, neu } = publishNew(h, ['0,0'], { visibleNow: false });
    h.s.gc(`indexes/${O}.json`, `pairs/${old['0,0'].pairHash}.json`, `objects/${old['0,0'].blocksSha}.uib`);
    // Existence: the old files are gone, but the tile decoded before GC is still in memory and still matches the
    // visible (old) hires revision, so it may keep answering for that revision.
    assert.equal(name(h, 1, 1), 'minecraft:air');
    h.visible['0,0'] = G_NEW.slice(0, 12);                 // hires now shows the new revision
    assert.equal(h.follower.blockAt(1, 64, 1), undefined, 'stale collision is unknown, never air');
    const object = h.s.hold(`objects/${neu['0,0'].blocksSha}.uib`);
    await h.c.advance(30_000);                             // next pointer poll: N is validated and swapped in
    assert.equal(h.follower.indexSha256, N);
    assert.equal(h.follower.blockAt(1, 64, 1), undefined, 'new tile not loaded yet: unknown, not the old air');
    object.resolve();
    await flush();
    assert.equal(name(h, 1, 1), 'minecraft:stone', 'recovered with the new revision');
    assert.equal(h.follower.bytes, TILE_BYTES, 'only the new window is resident');
  } finally {
    h.follower.dispose();
  }
});

test('old pair or object 404 while the late follower still holds the old index: unknown, then recovery', async () => {
  for (const removed of ['pair', 'object']) {
    const h = setup();
    try {
      const { O, old } = await followOld(h);
      h.follower.update({ ...h.view, walking: false });   // walker leaves: the old tile is released
      assert.equal(h.follower.bytes, 0);
      const { N } = publishNew(h, ['0,0'], { visibleNow: false });
      h.s.gc(`indexes/${O}.json`, removed === 'pair' ? `pairs/${old['0,0'].pairHash}.json`
        : `objects/${old['0,0'].blocksSha}.uib`);
      h.follower.update(h.view);                           // back before the next poll: index O still active
      await flush();
      assert.equal(h.follower.indexSha256, O, removed);
      assert.equal(h.follower.blockAt(1, 64, 1), undefined, `${removed} 404: unknown, never air`);
      assert.equal(h.follower.ready, false);
      assert.ok(h.errors.some((e) => /404/.test(e.message)), `${removed} 404 is reported`);
      h.visible['0,0'] = G_NEW.slice(0, 12);
      await h.c.advance(30_000);
      assert.equal(h.follower.indexSha256, N, removed);
      assert.equal(name(h, 1, 1), 'minecraft:stone', `${removed}: recovered`);
    } finally {
      h.follower.dispose();
    }
  }
});

test('candidate index 404 keeps the old world; a later valid pointer recovers', async () => {
  const h = setup();
  try {
    await followOld(h);
    h.s.pointer = 'd'.repeat(64);                          // a pointer to an index that no longer exists
    await h.c.advance(30_000);
    assert.equal(h.follower.stats.indexErrors, 1);
    assert.match(h.follower.stats.lastError.message, /404/);
    assert.equal(name(h, 1, 1), 'minecraft:air', 'the old, still matching world keeps serving');
    const { N } = publishNew(h);
    await h.c.advance(30_000);                             // first failure backs off 30 s
    assert.equal(h.follower.indexSha256, N);
    assert.equal(name(h, 1, 1), 'minecraft:stone');
  } finally {
    h.follower.dispose();
  }
});

test('no mixture of old and new data: preparing candidate and partially loaded new world', async () => {
  const h = setup({ x: 60, z: 32 });                       // window covers tiles 0,0 and 1,0
  try {
    const { O, old } = await followOld(h, ['0,0', '1,0']);
    const { N, neu } = publishNew(h, ['0,0', '1,0']);       // hires already shows the new revision
    h.s.gc(`objects/${neu['1,0'].blocksSha}.uib`);          // new tile 1,0 cannot load (e.g. a 404)
    h.s.gc(`indexes/${O}.json`, `pairs/${old['0,0'].pairHash}.json`, `objects/${old['0,0'].blocksSha}.uib`);
    const index = h.s.hold(`indexes/${N}.json`);
    await h.c.advance(30_000);                             // candidate N is preparing; O is still active
    assert.equal(h.follower.stats.candidateSha256, N);
    assert.equal(h.follower.blockAt(1, 64, 1), undefined, 'old tile vs new visible revision: unknown');
    assert.equal(h.follower.blockAt(65, 64, 1), undefined, 'old tile vs new visible revision: unknown');
    index.resolve();
    await flush();
    assert.equal(h.follower.indexSha256, N);
    assert.equal(name(h, 1, 1), 'minecraft:stone');
    assert.equal(h.follower.blockAt(65, 64, 1), undefined, 'missing new data stays unknown, not the old air');
    assert.equal(h.follower.bytes, TILE_BYTES, 'the old window was released at the swap');
    for (let x = 0; x < 128; x += 7) {
      assert.notEqual(name(h, x, 1), 'minecraft:air', `no old answer anywhere after the swap (x=${x})`);
    }
  } finally {
    h.follower.dispose();
  }
});
