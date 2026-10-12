// Deterministic tests for web/hires-requests.js (Q5): virtual clock and timers, fake fetch/decoders, no network.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createHiresRequests, retryDelay, RETRY_CAP_MS, SLOW_PROBE_MS, capacityRetryAfterMs } from './web/hires-requests.js';

const flush = async () => { for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const response = (body = new ArrayBuffer(1)) => ({ ok: true, status: 200, arrayBuffer: async () => body });
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};

function clock() {
  let time = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    now: () => time,
    setTimer: (fn, delay) => {
      const id = nextId++;
      timers.set(id, { at: time + delay, fn });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
    pending: () => timers.size,
    advance(ms) {
      const end = time + ms;
      while (true) {
        const due = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!due) break;
        time = due[1].at;
        timers.delete(due[0]);
        due[1].fn();
      }
      time = end;
    },
  };
}

// Same filter as app.js updateHires: wanted = window keys whose CURRENT revision is not shown yet.
const wantedFrom = (index, windowKeys, shown) =>
  new Map(windowKeys.filter((k) => index.has(k) && shown.get(k) !== index.get(k)).map((k) => [k, index.get(k)]));

test('retryDelay: exponential, capped at 30 s, then a slow 5 min probe', () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6].map(retryDelay), [1000, 2000, 4000, 8000, 16000, RETRY_CAP_MS]);
  assert.deepEqual([7, 8, 50].map(retryDelay), [SLOW_PROBE_MS, SLOW_PROBE_MS, SLOW_PROBE_MS]);
  assert.equal(retryDelay(0), 1000);
});

test('due failed tile does not spin timers while every download slot is occupied', async () => {
  const time=clock();
  let scheduled=0;
  const slow=deferred();
  const requests=createHiresRequests({ ...time, concurrency:1,
    setTimer:(fn,delay)=>{scheduled++;return time.setTimer(fn,delay);},
    fetchTile:async(key)=>key==='failed'?{ok:false,status:503}:slow.promise,
    decodeTile:raw=>raw,onTile:()=>{},
  });
  requests.setDesired(new Map([['failed','v1'],['slow','v1']]));
  await flush();
  const before=scheduled;
  time.advance(5000);await flush();
  assert.equal(scheduled,before,'no 1ms retry timer while the slot is occupied');
  assert.equal(requests.loading,1);
  slow.resolve(response());await flush();
  assert.equal(requests.snapshot().failed[0].count,2,'due retry runs when a slot becomes available');
  requests.dispose();
});

test('HTTP 503 backs off instead of hot-looping and eventually probes slowly', async () => {
  const time = clock();
  const calls = [];
  const requests = createHiresRequests({
    ...time,
    fetchTile: async () => { calls.push(time.now()); return { ok: false, status: 503 }; },
    decodeTile: (raw) => raw,
    onTile: () => assert.fail('503 cannot attach'),
  });
  requests.setDesired(new Map([['0,0', 'rev1']]));
  await flush();
  assert.deepEqual(calls, [0]);
  for (let i = 0; i < 30; i += 1) requests.setDesired(new Map([['0,0', 'rev1']]));
  assert.deepEqual(calls, [0], 'repeated view updates cannot bypass backoff');
  time.advance(999); await flush();
  assert.deepEqual(calls, [0]);
  time.advance(1); await flush();
  assert.deepEqual(calls, [0, 1000]);
  time.advance(2000); await flush();
  assert.deepEqual(calls, [0, 1000, 3000]);
  time.advance(4000); await flush();
  time.advance(8000); await flush();
  time.advance(16000); await flush();
  time.advance(30000); await flush();
  assert.equal(calls.length, 7);
  time.advance(299999); await flush();
  assert.equal(calls.length, 7);
  time.advance(1); await flush();
  assert.equal(calls.length, 8, 'persistent failure gets a slow recovery probe');
  assert.equal(requests.snapshot().failed[0].count, 8);
  assert.equal(requests.snapshot().failed[0].message, 'HTTP 503');
  requests.dispose();
});

test('404, rejected fetch (network error) and a thrown fetch also back off', async () => {
  for (const [label, fetchTile] of [
    ['404', async () => ({ ok: false, status: 404 })],
    ['network', async () => { throw new TypeError('Failed to fetch'); }],
    ['sync throw', () => { throw new Error('boom'); }],
  ]) {
    const time = clock();
    let calls = 0;
    const requests = createHiresRequests({ ...time, fetchTile: (...a) => { calls += 1; return fetchTile(...a); },
      decodeTile: (raw) => raw, onTile: () => assert.fail('cannot attach') });
    requests.setDesired(new Map([['1,1', 'a']]));
    await flush();
    requests.setDesired(new Map([['1,1', 'a']]));
    await flush();
    assert.equal(calls, 1, `${label}: no immediate repeat`);
    time.advance(1000); await flush();
    assert.equal(calls, 2, `${label}: retried after 1 s`);
    requests.dispose();
  }
});

test('timeout covers a body that never completes and frees the slot', async () => {
  const time = clock();
  const signals = [];
  let calls = 0;
  const requests = createHiresRequests({
    ...time, timeoutMs: 100,
    fetchTile: async (_key, _sha, signal) => {
      calls += 1;
      signals.push(signal);
      return { ok: true, arrayBuffer: () => new Promise(() => {}) };
    },
    decodeTile: (raw) => raw,
    onTile: () => assert.fail('hanging body cannot attach'),
  });
  requests.setDesired(new Map([['0,0', 'rev1']]));
  await flush();
  assert.equal(requests.loading, 1);
  time.advance(100); await flush();
  assert.equal(signals[0].aborted, true);
  assert.equal(requests.loading, 0, 'ignored abort still releases the concurrency slot');
  assert.match(requests.snapshot().failed[0].message, /timed out/);
  assert.equal(calls, 1);
  time.advance(999); await flush();
  assert.equal(calls, 1);
  time.advance(1); await flush();
  assert.equal(calls, 2);
  requests.dispose();
});

test('a hanging fetch (headers never arrive, abort ignored) cannot hold a slot: the next tile starts', async () => {
  const time = clock();
  const started = [];
  const requests = createHiresRequests({
    ...time, concurrency: 1, timeoutMs: 500,
    fetchTile: (key) => { started.push(key); return key === 'hang' ? new Promise(() => {}) : Promise.resolve(response()); },
    decodeTile: (raw) => raw,
    onTile: () => true,
  });
  requests.setDesired(new Map([['hang', 'x'], ['next', 'y']]));
  await flush();
  assert.deepEqual(started, ['hang'], 'concurrency 1 respected');
  time.advance(500); await flush();
  assert.deepEqual(started, ['hang', 'next'], 'the timed-out request released its slot');
  assert.equal(requests.loading, 0);
  requests.dispose();
});

test('decode failure retries after backoff and success stops further requests', async () => {
  const time = clock();
  let calls = 0;
  const attached = [];
  const requests = createHiresRequests({
    ...time,
    fetchTile: async () => { calls += 1; return response(); },
    decodeTile: () => { if (calls === 1) throw new Error('bad UIH'); return { good: true }; },
    onTile: (key, sha, tile) => { attached.push([key, sha, tile]); },
  });
  requests.setDesired(new Map([['0,0', 'rev1']]));
  await flush();
  assert.equal(calls, 1);
  assert.deepEqual(attached, []);
  time.advance(1000); await flush();
  assert.equal(calls, 2);
  assert.deepEqual(attached, [['0,0', 'rev1', { good: true }]]);
  time.advance(600000); await flush();
  assert.equal(calls, 2);
  assert.equal(time.pending(), 0, 'no timer left once everything is delivered');
  requests.dispose();
});

test('leaving the window and changing revision abort stale requests without attaching them', async () => {
  const time = clock();
  const bodies = [];
  const signals = [];
  const attached = [];
  const requests = createHiresRequests({
    ...time, concurrency: 1,
    fetchTile: async (_key, _sha, signal) => {
      const body = deferred();
      bodies.push(body);
      signals.push(signal);
      return { ok: true, arrayBuffer: () => body.promise };
    },
    decodeTile: (raw) => raw,
    onTile: (key, sha) => { attached.push([key, sha]); },
  });
  requests.setDesired(new Map([['0,0', 'rev1']]));
  await flush();
  requests.setDesired(new Map());
  assert.equal(signals[0].aborted, true);
  assert.equal(requests.loading, 0, 'the slot is free synchronously');
  await flush();
  requests.setDesired(new Map([['0,0', 'rev1']]));
  await flush();
  requests.setDesired(new Map([['0,0', 'rev2']]));
  await flush();
  assert.equal(signals[1].aborted, true);
  assert.equal(bodies.length, 3, 'new revision starts even if the old body ignores abort');
  bodies[0].resolve(new ArrayBuffer(1));
  bodies[1].resolve(new ArrayBuffer(1));
  await flush();
  assert.deepEqual(attached, []);
  bodies[2].resolve(new ArrayBuffer(1));
  await flush();
  assert.deepEqual(attached, [['0,0', 'rev2']]);
  requests.dispose();
});

test('cancelled requests are not failures: no backoff, immediate reload when wanted again', async () => {
  const time = clock();
  const errors = [];
  let calls = 0;
  const requests = createHiresRequests({ ...time,
    fetchTile: () => { calls += 1; return new Promise(() => {}); },
    decodeTile: (raw) => raw, onTile: () => true, onError: (...a) => errors.push(a) });
  requests.setDesired(new Map([['0,0', 'a']]));
  requests.setDesired(new Map());
  await flush();
  requests.setDesired(new Map([['0,0', 'a']]));
  assert.equal(calls, 2);
  assert.deepEqual(errors, []);
  assert.deepEqual(requests.snapshot().failed, []);
  requests.dispose();
});

test('a new revision of a failed tile starts fresh; leaving the window keeps the backoff', async () => {
  const time = clock();
  const calls = [];
  const requests = createHiresRequests({ ...time,
    fetchTile: async (key, sha) => { calls.push(sha); return { ok: false, status: 503 }; },
    decodeTile: (raw) => raw, onTile: () => true });
  requests.setDesired(new Map([['0,0', 'a']]));
  await flush();
  requests.setDesired(new Map());          // left the window
  requests.setDesired(new Map([['0,0', 'a']]));   // back again: still waiting
  await flush();
  assert.deepEqual(calls, ['a']);
  requests.setDesired(new Map([['0,0', 'b']]));   // the index published a new revision
  await flush();
  assert.deepEqual(calls, ['a', 'b'], 'new revision is fetched at once');
  assert.equal(requests.snapshot().failed[0].count, 1, 'its own failure count starts at 1');
  requests.dispose();
});

test('a refused attach (onTile false) is retried with backoff, not marked done', async () => {
  const time = clock();
  let calls = 0;
  let accept = false;
  const requests = createHiresRequests({ ...time,
    fetchTile: async () => { calls += 1; return response(); },
    decodeTile: (raw) => raw,
    onTile: () => accept });
  requests.setDesired(new Map([['0,0', 'a']]));
  await flush();
  assert.equal(calls, 1);
  assert.equal(requests.snapshot().failed[0].message, 'tile was not attached');
  requests.setDesired(new Map([['0,0', 'a']]));
  await flush();
  assert.equal(calls, 1, 'no hot loop');
  accept = true;
  time.advance(1000); await flush();
  assert.equal(calls, 2);
  assert.deepEqual(requests.snapshot().failed, []);
  time.advance(600000); await flush();
  assert.equal(calls, 2);
  requests.dispose();
});

test('an exception thrown by onTile counts as a failure and does not break the scheduler', async () => {
  const time = clock();
  let calls = 0;
  const requests = createHiresRequests({ ...time,
    fetchTile: async () => { calls += 1; return response(); },
    decodeTile: (raw) => raw,
    onTile: () => { if (calls === 1) throw new Error('gpu upload'); return true; } });
  requests.setDesired(new Map([['0,0', 'a']]));
  await flush();
  assert.equal(requests.snapshot().failed[0].message, 'gpu upload');
  time.advance(1000); await flush();
  assert.equal(calls, 2);
  assert.deepEqual(requests.snapshot().failed, []);
  requests.dispose();
});

test('eviction and revision change through the app filter reload exactly once each', async () => {
  const time = clock();
  const fetched = [];
  const shown = new Map();
  const requests = createHiresRequests({ ...time,
    fetchTile: async (key, sha) => { fetched.push(`${key}@${sha}`); return response(); },
    decodeTile: (raw) => raw,
    onTile: (key, sha) => { shown.set(key, sha); return true; } });
  let index = new Map([['0,0', 'a'], ['1,0', 'b']]);
  const update = (windowKeys) => {
    for (const key of [...shown.keys()]) if (!windowKeys.includes(key) || !index.has(key)) shown.delete(key);   // eviction
    requests.setDesired(wantedFrom(index, windowKeys, shown));
  };
  update(['0,0', '1,0']);
  await flush();
  update(['0,0', '1,0']);
  await flush();
  assert.deepEqual(fetched, ['0,0@a', '1,0@b'], 'shown tiles are not fetched again');
  update(['1,0']);                 // 0,0 evicted (out of the window)
  await flush();
  update(['0,0', '1,0']);          // back in the window
  await flush();
  assert.deepEqual(fetched, ['0,0@a', '1,0@b', '0,0@a'], 'an evicted tile is downloaded again');
  index = new Map([['0,0', 'a2'], ['1,0', 'b']]);   // new hires index revision for 0,0
  update(['0,0', '1,0']);
  assert.equal(shown.get('0,0'), 'a', 'the old revision stays shown until the replacement arrives');
  await flush();
  assert.equal(shown.get('0,0'), 'a2');
  update(['0,0', '1,0']);
  await flush();
  assert.deepEqual(fetched, ['0,0@a', '1,0@b', '0,0@a', '0,0@a2'], 'nothing else is fetched');
  requests.dispose();
});

test('failure records are bounded for unwanted tiles; wanted records are never dropped (no ping-pong)', async () => {
  const time = clock();
  const calls = [];
  const requests = createHiresRequests({ ...time, maxFailureRecords: 2,
    fetchTile: async (key) => { calls.push(key); return { ok: false, status: 500 }; },
    decodeTile: (raw) => raw, onTile: () => true });
  const count = (key) => calls.filter((k) => k === key).length;
  requests.setDesired(new Map([['a', '1'], ['b', '1'], ['c', '1']]));
  await flush();
  requests.setDesired(new Map([['a', '1'], ['b', '1'], ['c', '1']]));
  await flush();
  assert.deepEqual(calls, ['a', 'b', 'c'], 'three wanted failures exceed the limit of 2 but none is retried at once');
  assert.equal(requests.snapshot().failed.length, 3);
  requests.setDesired(new Map([['d', '1']]));   // a, b, c are unwanted now: the oldest are forgotten
  await flush();
  requests.setDesired(new Map([['a', '1'], ['c', '1'], ['d', '1']]));
  await flush();
  assert.equal(count('a'), 2, 'the forgotten (oldest unwanted) record of a no longer delays it');
  assert.equal(count('c'), 1, 'the newest unwanted record (c) was kept and still waits');
  assert.equal(count('d'), 1, 'the wanted record of d is kept');
  requests.dispose();
});

test('retryFailed forces the failed wanted tiles now and keeps their count', async () => {
  const time = clock();
  let calls = 0;
  const requests = createHiresRequests({ ...time,
    fetchTile: async () => { calls += 1; return { ok: false, status: 503 }; },
    decodeTile: (raw) => raw, onTile: () => true });
  requests.setDesired(new Map([['0,0', 'a']]));
  await flush();
  assert.equal(requests.retryFailed(), 1);
  await flush();
  assert.equal(calls, 2);
  assert.equal(requests.snapshot().failed[0].count, 2);
  assert.equal(requests.snapshot().failed[0].retryInMs, 2000, 'next automatic retry follows the kept count');
  assert.equal(requests.retryFailed(), 1);
  await flush();
  assert.equal(calls, 3);
  requests.dispose();
});

test('dispose cancels requests, ignores late results and leaves no timers', async () => {
  const time = clock();
  const body = deferred();
  let attached = 0;
  const signals = [];
  const requests = createHiresRequests({ ...time,
    fetchTile: async (_k, _s, signal) => { signals.push(signal); return { ok: true, arrayBuffer: () => body.promise }; },
    decodeTile: (raw) => raw, onTile: () => { attached += 1; return true; } });
  requests.setDesired(new Map([['0,0', 'a']]));
  await flush();
  requests.dispose();
  assert.equal(signals[0].aborted, true);
  body.resolve(new ArrayBuffer(1));
  await flush();
  assert.equal(attached, 0);
  assert.equal(time.pending(), 0);
  requests.setDesired(new Map([['1,1', 'b']]));
  assert.equal(requests.loading, 0, 'a disposed scheduler starts nothing');
});

// ---------------------------------------------------------------- server capacity (503 + Retry-After)

const busy = (retryAfter = '1') => ({ ok: false, status: 503,
  headers: new Headers(retryAfter === null ? {} : { 'Retry-After': retryAfter }) });

test('capacityRetryAfterMs accepts only 503 with whole seconds 1..60', () => {
  assert.equal(capacityRetryAfterMs(busy('1')), 1000);
  assert.equal(capacityRetryAfterMs(busy(' 60 ')), 60000);
  for (const value of [null, '', '0', '61', '1.5', '-1', 'abc', '1e1', '100', 'Wed, 21 Oct 2015 07:28:00 GMT']) {
    assert.equal(capacityRetryAfterMs(busy(value)), null, String(value));
  }
  assert.equal(capacityRetryAfterMs({ ok: false, status: 503 }), null, 'no headers');
  assert.equal(capacityRetryAfterMs({ ok: false, status: 503, headers: { get() { throw new Error('x'); } } }), null);
  assert.equal(capacityRetryAfterMs({ ok: false, status: 500, headers: new Headers({ 'Retry-After': '1' }) }), null);
});

test('repeated valid 503 (more than 7) is no tile failure: no count, no slow probe, then success', async () => {
  const time = clock();
  const calls = [];
  const errors = [];
  const attached = [];
  const requests = createHiresRequests({ ...time, concurrency: 1,
    fetchTile: async () => { calls.push(time.now()); return calls.length <= 9 ? busy('1') : response(); },
    decodeTile: (raw) => raw, onTile: (key, sha) => { attached.push([key, sha]); return true; },
    onError: (...args) => errors.push(args) });
  requests.setDesired(new Map([['0,0', 'a']]));
  await flush();
  assert.equal(requests.snapshot().busyRetryInMs, 1000);
  assert.deepEqual(requests.snapshot().failed, [], 'capacity is not shown as a failed tile');
  for (let i = 0; i < 9; i += 1) { time.advance(1000); await flush(); }
  assert.deepEqual(calls, [0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000], 'retried every Retry-After, never 5 min');
  assert.deepEqual(attached, [['0,0', 'a']]);
  assert.deepEqual(errors, [], 'onError is not called for capacity');
  assert.deepEqual(requests.snapshot().failed, []);
  assert.equal(time.pending(), 0);
  requests.dispose();
});

test('503 with missing, malformed or out-of-range Retry-After stays an ordinary failure', async () => {
  for (const value of [null, '', 'abc', '0', '61', '1.5', 'Wed, 21 Oct 2015 07:28:00 GMT']) {
    const time = clock();
    const calls = [];
    const errors = [];
    const requests = createHiresRequests({ ...time,
      fetchTile: async () => { calls.push(time.now()); return busy(value); },
      decodeTile: (raw) => raw, onTile: () => assert.fail('cannot attach'), onError: (...args) => errors.push(args) });
    requests.setDesired(new Map([['0,0', 'a']]));
    await flush();
    const snap = requests.snapshot();
    assert.equal(snap.busyRetryInMs, 0, String(value));
    assert.deepEqual(snap.failed.map((f) => [f.count, f.message, f.retryInMs]), [[1, 'HTTP 503', 1000]], String(value));
    assert.equal(errors.length, 1);
    time.advance(1000); await flush();
    time.advance(2000); await flush();
    assert.deepEqual(calls, [0, 1000, 3000], `${value}: ordinary exponential backoff`);
    requests.dispose();
  }
});

test('a capacity cooldown holds the whole queue: no flood from setDesired or onSettled', async () => {
  const time = clock();
  const calls = [];
  let requests;
  requests = createHiresRequests({ ...time, concurrency: 3,
    fetchTile: async (key) => { calls.push([key, time.now()]); return calls.length <= 3 ? busy('2') : response(); },
    decodeTile: (raw) => raw, onTile: () => true,
    // like app.js: every settle triggers updateHires -> setDesired with the same wanted map
    onSettled: () => requests.setDesired(wanted) });
  const wanted = new Map(Array.from({ length: 10 }, (_, i) => [`${i},0`, 'a']));
  requests.setDesired(wanted);
  await flush();
  assert.equal(calls.length, 3, 'one wave of concurrency requests');
  for (let i = 0; i < 20; i += 1) requests.setDesired(wanted);
  time.advance(1999); await flush();
  assert.equal(calls.length, 3, 'nothing new starts during the 2 s cooldown');
  assert.equal(requests.snapshot().busyRetryInMs, 1);
  time.advance(1); await flush();
  assert.ok(calls.length > 3, 'the queue resumes after the cooldown');
  assert.deepEqual(calls.slice(3, 6).map(([, at]) => at), [2000, 2000, 2000]);
  assert.deepEqual(calls.slice(0, 3).map(([key]) => key), ['0,0', '1,0', '2,0'], 'retried keys keep their priority');
  requests.dispose();
});

test('retryFailed and a new revision cannot bypass the cooldown; earlier real failures keep their count', async () => {
  const time = clock();
  const calls = [];
  const requests = createHiresRequests({ ...time, concurrency: 2,
    fetchTile: async (key, sha) => {
      calls.push([key, sha, time.now()]);
      if (key === 'busy') return busy('5');
      return { ok: false, status: 500 };
    },
    decodeTile: (raw) => raw, onTile: () => true });
  requests.setDesired(new Map([['real', 'a'], ['busy', 'a']]));
  await flush();
  assert.equal(calls.length, 2);
  assert.deepEqual(requests.snapshot().failed.map((f) => [f.key, f.count]), [['real', 1]]);
  assert.equal(requests.retryFailed(), 1, 'the real failure is marked due');
  await flush();
  assert.equal(calls.length, 2, 'but nothing starts while the server asked for 5 s');
  requests.setDesired(new Map([['real', 'b'], ['busy', 'a']]));   // new revision of the failed tile
  await flush();
  assert.equal(calls.length, 2, 'a new revision also waits for the server cooldown');
  time.advance(4999); await flush();
  assert.equal(calls.length, 2);
  time.advance(1); await flush();
  assert.deepEqual(calls.slice(2).map(([key, sha, at]) => [key, sha, at]), [['real', 'b', 5000], ['busy', 'a', 5000]]);
  requests.dispose();
});

test('capacity between real failures keeps the count and waits for both deadlines', async () => {
  const time = clock();
  const calls = [];
  const plan = ['500', '500', '500', 'busy', '500'];
  const requests = createHiresRequests({ ...time,
    fetchTile: async () => {
      calls.push(time.now());
      return plan[calls.length - 1] === 'busy' ? busy('3') : { ok: false, status: 500 };
    },
    decodeTile: (raw) => raw, onTile: () => true });
  requests.setDesired(new Map([['0,0', 'a']]));
  await flush();                                           // fail 1 at 0 -> due 1000
  time.advance(1000); await flush();                       // fail 2 at 1000 -> due 3000
  time.advance(2000); await flush();                       // fail 3 at 3000 -> due 7000
  assert.equal(requests.snapshot().failed[0].count, 3);
  time.advance(4000); await flush();                       // capacity at 7000 -> busy until 10000
  assert.deepEqual(calls, [0, 1000, 3000, 7000]);
  assert.equal(requests.snapshot().failed[0].count, 3, 'capacity does not add to the count');
  assert.equal(requests.snapshot().busyRetryInMs, 3000);
  time.advance(2999); await flush();
  assert.equal(calls.length, 4);
  time.advance(1); await flush();
  assert.deepEqual(calls, [0, 1000, 3000, 7000, 10000]);
  assert.equal(requests.snapshot().failed[0].count, 4, 'the next real failure continues the count');
  requests.dispose();
});

test('cancellation and dispose during a cooldown: slot freed at once, no timers left', async () => {
  const time = clock();
  const signals = [];
  let first = true;
  const requests = createHiresRequests({ ...time, concurrency: 2,
    fetchTile: async (key, _sha, signal) => {
      signals.push([key, signal]);
      if (first) { first = false; return busy('10'); }
      return new Promise(() => {});                        // the other request hangs in flight
    },
    decodeTile: (raw) => raw, onTile: () => true });
  requests.setDesired(new Map([['a', '1'], ['b', '1']]));
  await flush();
  assert.equal(requests.loading, 1, 'b is still in flight during the cooldown');
  requests.setDesired(new Map([['a', '1']]));             // b leaves the window
  assert.equal(signals[1][1].aborted, true);
  assert.equal(requests.loading, 0, 'cancellation frees the slot synchronously');
  await flush();                                           // the cancelled request settles (its timeout timer goes)
  assert.equal(time.pending(), 1, 'only the cooldown timer is armed');
  assert.equal(signals.length, 2, 'the freed slot is not used during the cooldown');
  requests.dispose();
  assert.equal(time.pending(), 0, 'dispose clears the cooldown timer');
  time.advance(20000); await flush();
  assert.equal(signals.length, 2, 'nothing starts after dispose');
});

test('invalid arguments are refused', () => {
  assert.throws(() => createHiresRequests({ decodeTile: () => {}, onTile: () => {} }), TypeError);
  assert.throws(() => createHiresRequests({ fetchTile: () => {}, decodeTile: () => {}, onTile: () => {}, concurrency: 0 }),
    RangeError);
  const requests = createHiresRequests({ fetchTile: () => new Promise(() => {}), decodeTile: () => {}, onTile: () => {},
    setTimer: () => 1, clearTimer: () => {} });
  assert.throws(() => requests.setDesired(new Map([['0,0', 5]])), TypeError);
  requests.dispose();
});
