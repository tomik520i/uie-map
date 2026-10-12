// Tests for web/render-status.js (server render status). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_render_status.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// web/ has no package.json, so load the real module source as ESM independent of Node's .js detection.
const source = readFileSync(new URL('./web/render-status.js', import.meta.url), 'utf8');
const rs = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const NOW = Date.parse('2026-09-30T12:00:00Z');
const iso = (ms) => new Date(ms).toISOString();

function status(overrides = {}) {
  return { format: 'UIE_RENDER_STATUS1', state: 'rendering', updatedAt: iso(NOW - 5000), completedBatches: 12,
    totalBatches: 40, etaSeconds: 600, lastPublishedAt: '2026-09-29T22:15:00Z', message: '', ...overrides };
}
const describe = (overrides, now = NOW) => rs.describeRenderStatus(rs.validateRenderStatus(status(overrides)), now);

// ---------------------------------------------------------------- validation

test('valid records are parsed; not_configured with all unknowns is accepted', () => {
  const r = rs.validateRenderStatus(status());
  assert.equal(r.state, 'rendering');
  assert.equal(r.updatedAt, NOW - 5000);
  assert.equal(r.lastPublishedAt, Date.parse('2026-09-29T22:15:00Z'));
  const blank = rs.validateRenderStatus(status({ state: 'not_configured', completedBatches: null, totalBatches: null,
    etaSeconds: null, lastPublishedAt: null }));
  assert.equal(blank.lastPublishedAt, null);
  rs.validateRenderStatus(status({ updatedAt: '2026-09-30T14:00:00+02:00', extra: 'ignored' }));
});

test('malformed records are rejected', () => {
  const cases = {
    root: null, array: [], format: status({ format: 'UIE_RENDER_STATUS2' }), state: status({ state: 'paused' }),
    'state proto': status({ state: 'toString' }), 'updatedAt missing': status({ updatedAt: undefined }),
    'updatedAt date only': status({ updatedAt: '2026-09-30' }), 'updatedAt garbage': status({ updatedAt: 'včera' }),
    'completed negative': status({ completedBatches: -1 }), 'completed fraction': status({ completedBatches: 1.5 }),
    'completed string': status({ completedBatches: '3' }), 'completed missing': status({ completedBatches: undefined }),
    'total zero': status({ totalBatches: 0 }), 'completed > total': status({ completedBatches: 41 }),
    'eta negative': status({ etaSeconds: -1 }), 'eta NaN': status({ etaSeconds: NaN }),
    'eta infinite': status({ etaSeconds: Infinity }), 'eta string': status({ etaSeconds: '60' }),
    'lastPublishedAt bad': status({ lastPublishedAt: 'dnes' }), 'message long': status({ message: 'x'.repeat(301) }),
    'message missing': status({ message: undefined }), 'message number': status({ message: 5 }),
  };
  for (const [label, data] of Object.entries(cases)) {
    assert.throws(() => rs.validateRenderStatus(data), rs.RenderStatusError, label);
  }
  rs.validateRenderStatus(status({ message: 'x'.repeat(300) }));
});

// ---------------------------------------------------------------- presentation

test('partial and complete progress use the reported counts only', () => {
  const partial = describe({});
  assert.equal(partial.label, 'Renderuje');
  assert.equal(partial.tone, 'active');
  assert.deepEqual(partial.progress, { text: '12 / 40 dávek (30 %)', percent: 30 });
  assert.equal(partial.eta, 'přibližně 10 min');
  assert.equal(describe({ completedBatches: 39 }).progress.percent, 97, 'never rounded up to 100 before done');
  const done = describe({ state: 'complete', completedBatches: 40, etaSeconds: null });
  assert.equal(done.label, 'Dokončeno');
  assert.equal(done.tone, 'ok');
  assert.equal(done.progress.percent, 100);
  assert.equal(done.eta, null, 'no ETA outside active states');
});

test('unknown totals give a counter or nothing, and an honest missing estimate', () => {
  const counter = describe({ totalBatches: null, etaSeconds: null });
  assert.deepEqual(counter.progress, { text: '12 dávek hotovo', percent: null });
  assert.equal(counter.eta, 'Odhad zatím není k dispozici');
  const unknown = describe({ completedBatches: null, totalBatches: null, etaSeconds: null });
  assert.equal(unknown.progress, null);
  assert.equal(describe({ completedBatches: null, totalBatches: 40 }).progress, null, 'total alone is no progress');
  assert.equal(describe({ state: 'idle', etaSeconds: null }).eta, null, 'no estimate text where it is not useful');
});

test('state labels', () => {
  const labels = { not_configured: 'Automatický render zatím není zapojený', idle: 'Čeká', queued: 'Ve frontě',
    rendering: 'Renderuje', publishing: 'Zveřejňuje mapu', complete: 'Dokončeno', error: 'Chyba renderu' };
  for (const [state, label] of Object.entries(labels)) assert.equal(describe({ state }).label, label, state);
});

test('stale active heartbeat is not presented as running', () => {
  for (const state of ['queued', 'rendering', 'publishing']) {
    const d = describe({ state, updatedAt: iso(NOW - 120_001) });
    assert.equal(d.label, 'Stav renderu je zastaralý', state);
    assert.equal(d.tone, 'warn');
    assert.ok(d.stale && d.unreliable);
    assert.equal(d.eta, null);
    assert.equal(d.progress.percent, null);
    assert.match(d.progress.text, /12 \/ 40 dávek$/);
    assert.match(d.note, /Poslední hlášený stav/);
  }
  assert.equal(describe({ updatedAt: iso(NOW - 120_000) }).label, 'Renderuje', 'exactly 120 s is still fresh');
  const idle = describe({ state: 'idle', updatedAt: iso(NOW - 86_400_000) });
  assert.equal(idle.label, 'Čeká', 'inactive states do not go stale');
  assert.equal(idle.stale, false);
});

test('future heartbeats beyond the clock tolerance are not trusted', () => {
  assert.equal(describe({ updatedAt: iso(NOW + 60_000) }).label, 'Renderuje', 'small clock skew is tolerated');
  const future = describe({ updatedAt: iso(NOW + 600_000) });
  assert.equal(future.label, 'Stav renderu nelze ověřit');
  assert.equal(future.eta, null);
  assert.equal(future.progress.percent, null);
  assert.match(future.note, /v budoucnosti/);
  const complete = describe({ state: 'complete', completedBatches: 40, updatedAt: iso(NOW + 600_000) });
  assert.equal(complete.label, 'Dokončeno');
  assert.match(complete.note, /v budoucnosti/);
});

test('error state keeps message and last publication; heartbeat is separate from publication', () => {
  const d = describe({ state: 'error', message: '  Dávka 13 selhala: chybí region r.0.1.mca  ', etaSeconds: null });
  assert.equal(d.label, 'Chyba renderu');
  assert.equal(d.tone, 'error');
  assert.equal(d.message, 'Dávka 13 selhala: chybí region r.0.1.mca');
  assert.equal(d.published, Date.parse('2026-09-29T22:15:00Z'));
  assert.equal(d.heartbeat, NOW - 5000);
  assert.notEqual(d.published, d.heartbeat);
  assert.equal(describe({ state: 'not_configured', lastPublishedAt: null }).published, null);
  assert.equal(describe({ message: '   ' }).message, null);
});

test('formatDuration', () => {
  const cases = [[0, 'méně než minuta'], [59, 'méně než minuta'], [90, '2 min'], [600, '10 min'], [3600, '1 h'],
    [5400, '1 h 30 min'], [86_400, '1 d'], [90_000, '1 d 1 h'], [7 * 86_400 + 3 * 3600, '7 d 3 h']];
  for (const [seconds, text] of cases) assert.equal(rs.formatDuration(seconds), text, String(seconds));
});

// ---------------------------------------------------------------- fetchRenderStatus

function abortable(signal) {
  return new Promise((_, reject) => {
    const fail = () => reject(new DOMException('aborted', 'AbortError'));
    if (signal.aborted) fail();
    else signal.addEventListener('abort', fail, { once: true });
  });
}

test('fetchRenderStatus: data, 404 as missing, HTTP errors, JSON errors, body timeout, caller abort', { timeout: 3000 }, async () => {
  const ok = await rs.fetchRenderStatus('s.json', { fetchImpl: async () => ({ ok: true, status: 200, text: async () => '{"a":1}' }) });
  assert.deepEqual(ok, { kind: 'data', data: { a: 1 } });
  let cancelled = 0;
  const body = { cancel: () => { cancelled += 1; return Promise.resolve(); } };
  assert.deepEqual(await rs.fetchRenderStatus('s.json', { fetchImpl: async () => ({ ok: false, status: 404, body }) }),
    { kind: 'missing' });
  await assert.rejects(rs.fetchRenderStatus('s.json', { fetchImpl: async () => ({ ok: false, status: 500, body }) }),
    (err) => err instanceof rs.RenderStatusError && /HTTP 500/.test(err.message));
  assert.equal(cancelled, 2);
  await assert.rejects(rs.fetchRenderStatus('s.json', { fetchImpl: async () => ({ ok: true, status: 200, text: async () => '<html>' }) }),
    /platný JSON/);
  await assert.rejects(rs.fetchRenderStatus('s.json', { fetchImpl: async () => { throw new TypeError('offline'); } }),
    /síťová chyba \(offline\)/);
  const hangingBody = async (url, { signal }) => ({ ok: true, status: 200, text: () => abortable(signal) });
  await assert.rejects(rs.fetchRenderStatus('s.json', { timeoutMs: 30, fetchImpl: hangingBody }), /časový limit/);
  const caller = new AbortController();
  const pending = rs.fetchRenderStatus('s.json', { timeoutMs: 5000, signal: caller.signal,
    fetchImpl: (url, { signal }) => abortable(signal) });
  caller.abort(new Error('closed'));
  await assert.rejects(pending, /closed/);
});

// ---------------------------------------------------------------- poller

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}
const flush = () => new Promise((resolve) => { setImmediate(resolve); });

function harness({ hidden = false } = {}) {
  const h = { calls: [], updates: [], timers: new Map(), nextTimer: 1 };
  h.doc = new EventTarget();
  h.doc.hidden = hidden;
  h.poller = rs.createRenderStatusPoller({
    fetchStatus: (signal) => { const d = deferred(); h.calls.push({ d, signal }); return d.promise; },
    onUpdate: (view) => { h.updates.push(view); if (h.throwInUpdate) throw new Error('view broke'); },
    intervalMs: 30_000, document: h.doc,
    setTimer: (fn, ms) => { const id = h.nextTimer++; h.timers.set(id, { fn, ms }); return id; },
    clearTimer: (id) => { h.timers.delete(id); },
  });
  h.fireTimer = () => { const [[id, t]] = h.timers; h.timers.delete(id); t.fn(); };
  h.setHidden = (value) => { h.doc.hidden = value; h.doc.dispatchEvent(new Event('visibilitychange')); };
  return h;
}

test('poller never overlaps requests and schedules the next one only after completion', async () => {
  const h = harness();
  h.poller.start();
  h.poller.start();
  h.poller.pollNow();
  assert.equal(h.calls.length, 1, 'a second poll while one is in flight is ignored');
  assert.equal(h.timers.size, 0, 'no timer while a request is running');
  h.calls[0].d.resolve({ kind: 'data', data: status() });
  await flush();
  assert.equal(h.updates.length, 1);
  assert.equal(h.updates[0].kind, 'ok');
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].ms, 30_000);
  h.fireTimer();
  assert.equal(h.calls.length, 2);
  h.poller.dispose();
});

test('unavailable, invalid and failing status never crash and keep polling', async () => {
  const h = harness();
  h.throwInUpdate = true;
  h.poller.start();
  h.calls[0].d.resolve({ kind: 'missing' });
  await flush();
  assert.deepEqual(h.updates[0].kind, 'unavailable');
  assert.equal(h.updates[0].reason, 'missing');
  assert.equal(h.timers.size, 1, 'a throwing view does not stop polling');
  h.fireTimer();
  h.calls[1].d.resolve({ kind: 'data', data: status({ state: 'paused' }) });
  await flush();
  assert.equal(h.updates[1].reason, 'invalid');
  assert.match(h.updates[1].message, /neznámý stav/);
  h.fireTimer();
  h.calls[2].d.reject(new Error('vypršel časový limit 10 s'));
  await flush();
  assert.equal(h.updates[2].reason, 'error');
  assert.equal(h.timers.size, 1);
  h.poller.dispose();
});

test('polling pauses while hidden and refreshes immediately when visible', async () => {
  const h = harness({ hidden: true });
  h.poller.start();
  assert.equal(h.calls.length, 0, 'no request while hidden');
  h.setHidden(false);
  assert.equal(h.calls.length, 1, 'visible again: immediate refresh');
  h.calls[0].d.resolve({ kind: 'data', data: status() });
  await flush();
  assert.equal(h.timers.size, 1);
  h.setHidden(true);
  assert.equal(h.timers.size, 0, 'hidden: pending timer cleared');
  h.setHidden(false);
  assert.equal(h.calls.length, 2);
  h.setHidden(true);
  h.calls[1].d.resolve({ kind: 'data', data: status() });
  await flush();
  assert.equal(h.timers.size, 0, 'request finishing while hidden schedules nothing');
  h.setHidden(false);
  assert.equal(h.calls.length, 3);
  h.setHidden(false);
  assert.equal(h.calls.length, 3, 'visibility event during a request does not overlap');
  h.poller.dispose();
});

test('dispose aborts the request, drops late results and removes the listener', async () => {
  const h = harness();
  h.poller.start();
  const first = h.calls[0];
  h.poller.dispose();
  h.poller.dispose();
  assert.ok(first.signal.aborted);
  first.d.resolve({ kind: 'data', data: status() });
  await flush();
  assert.equal(h.updates.length, 0);
  assert.equal(h.timers.size, 0);
  h.setHidden(false);
  h.poller.pollNow();
  h.poller.start();
  assert.equal(h.calls.length, 1);
  assert.throws(() => rs.createRenderStatusPoller({ fetchStatus: null, onUpdate() {} }), TypeError);
});
