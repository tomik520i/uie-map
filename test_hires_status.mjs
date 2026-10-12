// Tests for web/hires-status.js (whole-world surface build status) and its panel wiring. Node built-ins only.
// Run from the work directory:  node --test uie-map/test_hires_status.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// web/ has no package.json, so load the real module source as ESM independent of Node's .js detection.
const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const hs = await import(`data:text/javascript;base64,${Buffer.from(read('./web/hires-status.js')).toString('base64')}`);

const NOW = Date.parse('2026-10-03T12:00:00Z');
const S = 1000;
const MIN = 60 * S;
const H = 60 * MIN;
const secs = (ms) => ms / 1000;

// Shapes exactly as world_hires.py build() writes them (epoch seconds as floats, etaSeconds/finishedAt optional).
function status(overrides = {}) {
  return { format: 'UIE_HIRES_STATUS1', startedAt: secs(NOW - 10 * MIN), total: 40, done: 10, skippedUnchanged: 2300,
    chunks: 5000, bytes: 123456, state: 'running', etaSeconds: 1800, ...overrides };
}
const describe = (overrides, now = NOW, unchanged = 0) =>
  hs.describeHiresStatus(hs.validateHiresStatus(status(overrides)), now, unchanged);

// ---------------------------------------------------------------- validation

test('writer records are accepted: start, mid-run, done, stopped; epoch seconds become ms', () => {
  const start = hs.validateHiresStatus(status({ done: 0, etaSeconds: undefined, startedAt: 1759492800.123 }));
  assert.equal(start.startedAt, 1759492800123);
  assert.equal(start.etaSeconds, null);
  assert.equal(start.finishedAt, null);
  const done = hs.validateHiresStatus(status({ state: 'done', done: 40, etaSeconds: 0, finishedAt: secs(NOW) }));
  assert.equal(done.finishedAt, NOW);
  const stopped = hs.validateHiresStatus(status({ state: 'stopped: free disk below 20 GiB', finishedAt: secs(NOW) }));
  assert.equal(stopped.state, 'stopped');
  assert.equal(stopped.reason, 'free disk below 20 GiB');
  hs.validateHiresStatus(status({ extra: 'ignored' }));
});

test('malformed records are rejected', () => {
  const cases = {
    root: null, array: [], format: status({ format: 'UIE_RENDER_STATUS1' }), state: status({ state: 'paused' }),
    'state number': status({ state: 1 }), 'started missing': status({ startedAt: undefined }),
    'started ISO': status({ startedAt: '2026-10-03T12:00:00Z' }), 'started zero': status({ startedAt: 0 }),
    'finished bad': status({ finishedAt: 'dnes' }), 'total missing': status({ total: undefined }),
    'done negative': status({ done: -1 }), 'done fraction': status({ done: 1.5 }), 'done > total': status({ done: 41 }),
    'skipped missing': status({ skippedUnchanged: undefined }), 'eta negative': status({ etaSeconds: -1 }),
    'eta string': status({ etaSeconds: '60' }), 'eta NaN': status({ etaSeconds: NaN }),
    'epoch overflow': status({ startedAt: 1e300 }),
    'reversed times': status({ finishedAt: secs(NOW - H) }),
    'incomplete done': status({ state: 'done', done: 1 }),
  };
  for (const [label, data] of Object.entries(cases)) assert.throws(() => hs.validateHiresStatus(data), hs.HiresStatusError, label);
});

// ---------------------------------------------------------------- presentation

test('fresh running run: progress of this run only, never rounded up, estimate, no finish time', () => {
  const d = describe({});
  assert.equal(d.label, 'Probíhá aktualizace');
  assert.equal(d.tone, 'active');
  assert.equal(d.stale, false);
  assert.equal(d.progress, '10 / 40 regionů (25 %)');
  assert.equal(d.etaSeconds, 1800);
  assert.equal(d.started, NOW - 10 * MIN);
  assert.equal(d.finished, null);
  assert.match(d.scope, /jen pro regiony zpracovávané tímto během/);
  assert.match(d.scope, /2\s300 nezměněných regionů/);
  assert.match(d.scope, /Nejde o pokrytí celého světa/);
  assert.equal(describe({ done: 39 }).percent, 97);
  assert.equal(describe({ done: 0, etaSeconds: undefined }).etaSeconds, null, 'no estimate before the first job');
  assert.equal(describe({ done: 0, etaSeconds: undefined }).progress, '0 / 40 regionů (0 %)');
});

test('done and stopped runs show only the times the file provides', () => {
  const done = describe({ state: 'done', done: 40, etaSeconds: 0, finishedAt: secs(NOW) });
  assert.equal(done.label, 'Poslední běh dokončen');
  assert.match(done.note, /nepotvrzuje bezchybný render/);
  assert.equal(done.tone, 'ok');
  assert.equal(done.percent, 100);
  assert.equal(done.finished, NOW);
  assert.equal(done.finishedLabel, 'Dokončeno');
  assert.equal(done.etaSeconds, null);
  assert.equal(describe({ state: 'done', done: 40 }).finished, null, 'no finishedAt -> no finish time invented');
  const nothing = describe({ state: 'done', total: 0, done: 0, finishedAt: secs(NOW) });
  assert.equal(nothing.progress, null, 'no 0 / 0 or 100 % for a run without jobs');
  assert.equal(nothing.percent, null);
  assert.match(nothing.note, /Žádný region se od předchozí aktualizace nezměnil/);
  const stopped = describe({ state: 'stopped: free disk below 20 GiB', finishedAt: secs(NOW) });
  assert.equal(stopped.label, 'Poslední aktualizace byla zastavena');
  assert.equal(stopped.tone, 'warn');
  assert.equal(stopped.finishedLabel, 'Zastaveno');
  assert.match(stopped.note, /dochází místo na disku/);
  assert.equal(stopped.progress, '10 / 40 regionů (25 %)');
  assert.match(describe({ state: 'stopped: worker crashed' }).note, /Důvod: worker crashed\./);
  assert.match(describe({ state: 'stopped' }).note, /Důvod: neuveden\./);
});

test('a running record that was not rewritten for 30 min while watched is stale, not running', () => {
  assert.equal(describe({}, NOW, 30 * MIN).label, 'Probíhá aktualizace', 'exactly 30 min is still trusted');
  const d = describe({}, NOW, 30 * MIN + 1);
  assert.equal(d.label, 'Stav aktualizace je zastaralý');
  assert.equal(d.tone, 'warn');
  assert.ok(d.stale && d.unreliable);
  assert.equal(d.percent, null);
  assert.equal(d.progress, '10 / 40 regionů', 'last reported counts without a percent');
  assert.equal(d.etaSeconds, null);
  assert.match(d.note, /nelze potvrdit/);
  assert.equal(describe({ state: 'done', done: 40 }, NOW, 10 * H).stale, false, 'finished states do not go stale');
});

test('no finished job 2 h after the start, or an estimated end long past, makes a running record stale', () => {
  const noJob = { done: 0, etaSeconds: undefined };
  assert.equal(describe({ ...noJob, startedAt: secs(NOW - 2 * H) }).stale, false);
  assert.equal(describe({ ...noJob, startedAt: secs(NOW - 2 * H - S) }).stale, true);
  // done 10 / 40, eta 1800 s -> written 600 s after the start, run 2400 s, grace max(1800, 1200) s = 4200 s.
  assert.equal(describe({ startedAt: secs(NOW - 4200 * S) }).stale, false);
  const late = describe({ startedAt: secs(NOW - 4201 * S) });
  assert.equal(late.stale, true);
  assert.equal(late.label, 'Stav aktualizace je zastaralý');
});

test('a start time in the future is not trusted', () => {
  assert.equal(describe({ startedAt: secs(NOW + MIN) }).label, 'Probíhá aktualizace', 'small clock skew tolerated');
  const d = describe({ startedAt: secs(NOW + 10 * MIN) });
  assert.equal(d.label, 'Stav aktualizace nelze ověřit');
  assert.equal(d.percent, null);
  assert.equal(d.etaSeconds, null);
  assert.match(d.note, /v budoucnosti/);
});
test('last-good stale tiles are reported without calling them fresh or proving a specific error',()=>{
 const done=describe({state:'done',done:40,staleTiles:7});
 assert.match(done.label,/zůstává starší/);assert.equal(done.tone,'warn');assert.match(done.note,/7 dlaždic/);
 const pending=describe({staleTiles:2});assert.match(pending.note,/nová data ještě nejsou připravená/);
 assert.equal(hs.validateHiresStatus(status()).staleTiles,null);
 assert.throws(()=>hs.validateHiresStatus(status({staleTiles:-1})),hs.HiresStatusError);
});

// ---------------------------------------------------------------- task 107: heartbeat, failures, plan

test('optional backend fields: absent is unknown, malformed is rejected', () => {
  const old = hs.validateHiresStatus(status());
  for (const name of ['updatedAt', 'heartbeatSeconds', 'failedTiles', 'failedJobs', 'plannedJobs', 'notScheduled', 'planComplete']) {
    assert.equal(old[name], null, `${name} unknown, never 0/false`);
  }
  const full = hs.validateHiresStatus(status({ updatedAt: secs(NOW - 5 * S), heartbeatSeconds: 30, failedTiles: 3,
    failedJobs: 1, plannedJobs: 2400, notScheduled: 0, planComplete: false }));
  assert.equal(full.updatedAt, NOW - 5 * S);
  assert.deepEqual([full.failedTiles, full.failedJobs, full.plannedJobs, full.planComplete], [3, 1, 2400, false]);
  const bad = {
    'updatedAt string': { updatedAt: 'now' }, 'updatedAt before start': { updatedAt: secs(NOW - H) },
    'heartbeat zero': { heartbeatSeconds: 0 }, 'heartbeat huge': { heartbeatSeconds: 1e6 },
    'heartbeat string': { heartbeatSeconds: '30' }, 'failedTiles negative': { failedTiles: -1 },
    'failedTiles fraction': { failedTiles: 0.5 }, 'failedJobs > failedTiles': { failedTiles: 1, failedJobs: 2 },
    'failedJobs > done': { failedJobs: 11 }, 'plannedJobs string': { plannedJobs: '1' },
    'notScheduled null': { notScheduled: null }, 'planComplete number': { planComplete: 1 },
  };
  for (const [label, override] of Object.entries(bad)) {
    assert.throws(() => hs.validateHiresStatus(status(override)), hs.HiresStatusError, label);
  }
});

test('heartbeat: fresh means the process is alive, stale after max(3 x interval, 120 s), future is untrusted', () => {
  assert.equal(hs.heartbeatStaleAfterMs(30), 120 * S);
  assert.equal(hs.heartbeatStaleAfterMs(60), 180 * S);
  const fresh = describe({ updatedAt: secs(NOW - 20 * S), heartbeatSeconds: 30 });
  assert.equal(fresh.label, 'Probíhá aktualizace');
  assert.match(fresh.note, /Server se ozval před 20 s/);
  assert.equal(fresh.heartbeatAgeMs, 20 * S);
  // An old start, no finished job for hours and no rewrite while watched do not override a fresh heartbeat.
  const slow = describe({ startedAt: secs(NOW - 10 * H), done: 0, etaSeconds: undefined,
    updatedAt: secs(NOW - 10 * S), heartbeatSeconds: 30 }, NOW, 45 * MIN);
  assert.equal(slow.label, 'Probíhá aktualizace');
  assert.equal(slow.stale, false);
  assert.match(slow.note, /45 min nezměnil; signál potvrzuje běžící proces, ne postup/);
  assert.equal(describe({ updatedAt: secs(NOW - 120 * S), heartbeatSeconds: 30 }).stale, false, 'exactly 120 s');
  const stale = describe({ updatedAt: secs(NOW - 121 * S), heartbeatSeconds: 30 });
  assert.equal(stale.label, 'Stav aktualizace je zastaralý');
  assert.equal(stale.tone, 'warn');
  assert.equal(stale.percent, null);
  assert.match(stale.note, /naposledy před 2 min/);
  assert.equal(describe({ updatedAt: secs(NOW - 170 * S), heartbeatSeconds: 60 }).stale, false, '3 x 60 s');
  const future = describe({ updatedAt: secs(NOW + 10 * MIN) });
  assert.equal(future.label, 'Stav aktualizace nelze ověřit');
  assert.equal(future.etaSeconds, null);
  assert.match(future.note, /v budoucnosti/);
  assert.equal(describe({ state: 'done', done: 40, startedAt: secs(NOW - 11 * H), updatedAt: secs(NOW - 10 * H) }).stale,
    false, 'finished runs do not go stale');
});

test('zero errors vs unknown errors vs a run that finished with errors', () => {
  const finished = { state: 'done', done: 40, finishedAt: secs(NOW) };
  const unknown = describe(finished);
  assert.equal(unknown.label, 'Poslední běh dokončen');
  assert.match(unknown.note, /neobsahuje počet chyb/);
  const clean = describe({ ...finished, failedTiles: 0, failedJobs: 0 });
  assert.equal(clean.tone, 'ok');
  assert.match(clean.note, /bez chyby/);
  const errors = describe({ ...finished, failedTiles: 12, failedJobs: 3 });
  assert.equal(errors.label, 'Běh dokončen s chybami');
  assert.equal(errors.tone, 'warn');
  assert.match(errors.note, /12 dlaždic se v tomto běhu nepodařilo vykreslit \(3 regionů\)/);
  assert.doesNotMatch(errors.note, /bez chyby/);
  const both = describe({ ...finished, failedTiles: 2, failedJobs: 1, staleTiles: 2 });
  assert.equal(both.label, 'Běh dokončen s chybami', 'the error label is not overwritten by the stale tiles label');
  assert.match(both.note, /2 dlaždic zatím používá poslední dobrou starší verzi/);
  assert.match(describe({ failedTiles: 4 }).note, /Zatím 4 dlaždic/);
  assert.match(describe({ state: 'stopped: build failed: RuntimeError', failedTiles: 1, failedJobs: 1 }).note,
    /Do zastavení 1 dlaždic/);
});

test('partial plan: changed regions not scheduled or plan incomplete warn; complete plan is not a zero-error claim', () => {
  const finished = { state: 'done', done: 40, finishedAt: secs(NOW), failedTiles: 0, failedJobs: 0 };
  const cut = describe({ ...finished, notScheduled: 7, planComplete: false });
  assert.equal(cut.label, 'Běh dokončen, část světa čeká');
  assert.equal(cut.tone, 'warn');
  assert.match(cut.note, /7 změněných regionů čeká na další běh/);
  const incomplete = describe({ ...finished, notScheduled: 0, planComplete: false });
  assert.match(incomplete.note, /Ne všechny regiony mají aktuální bezchybná data/);
  const complete = describe({ ...finished, notScheduled: 0, planComplete: true });
  assert.equal(complete.tone, 'ok');
  assert.match(complete.note, /Všechny regiony z plánu mají aktuální data; pozdější změny ve světě přinese až další běh/);
  const withErrors = describe({ ...finished, failedTiles: 1, failedJobs: 1, planComplete: false });
  assert.equal(withErrors.label, 'Běh dokončen s chybami', 'errors outrank the partial plan label');
  const unknownPlan = describe({ state: 'done', done: 40, finishedAt: secs(NOW) });
  assert.doesNotMatch(unknownPlan.note, /Všechny regiony|čeká na další běh/, 'missing fields claim nothing');
  const keys = Object.keys(complete).sort();
  assert.deepEqual(keys, ['etaSeconds', 'finished', 'finishedLabel', 'heartbeatAgeMs', 'label', 'note', 'percent',
    'progress', 'scope', 'stale', 'started', 'tone', 'unreliable'], 'describe shape used by app.js is unchanged');
});

// ---------------------------------------------------------------- poller

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}
const flush = () => new Promise((resolve) => { setImmediate(resolve); });

function harness({ hidden = false } = {}) {
  const h = { calls: [], updates: [], timers: new Map(), nextTimer: 1, time: NOW };
  h.doc = new EventTarget();
  h.doc.hidden = hidden;
  h.poller = hs.createHiresStatusPoller({
    fetchStatus: (signal) => { const d = deferred(); h.calls.push({ d, signal }); return d.promise; },
    onUpdate: (view) => { h.updates.push(view); if (h.throwInUpdate) throw new Error('view broke'); },
    intervalMs: 30_000, document: h.doc, now: () => h.time,
    setTimer: (fn, ms) => { const id = h.nextTimer++; h.timers.set(id, { fn, ms }); return id; },
    clearTimer: (id) => { h.timers.delete(id); },
  });
  h.fireTimer = () => { const [[id, t]] = h.timers; h.timers.delete(id); t.fn(); };
  h.setHidden = (value) => { h.doc.hidden = value; h.doc.dispatchEvent(new Event('visibilitychange')); };
  h.answer = async (value) => { h.calls.at(-1).d.resolve(value); await flush(); return h.updates.at(-1); };
  return h;
}

test('poller: no overlap, next poll only after completion, unchanged time measured while watched', async () => {
  const h = harness();
  h.poller.start();
  h.poller.start();
  h.poller.pollNow();
  assert.equal(h.calls.length, 1, 'a second poll while one is in flight is ignored');
  assert.equal(h.timers.size, 0);
  let view = await h.answer({ kind: 'data', data: status() });
  assert.equal(view.kind, 'ok');
  assert.equal(view.unchangedForMs, 0);
  assert.equal([...h.timers.values()][0].ms, 30_000);
  h.time += 20 * MIN;
  h.fireTimer();
  view = await h.answer({ kind: 'data', data: status() });
  assert.equal(view.unchangedForMs, 20 * MIN, 'same record: unchanged since the first observation');
  h.time += 5 * MIN;
  h.fireTimer();
  view = await h.answer({ kind: 'data', data: status({ done: 11, etaSeconds: 1700 }) });
  assert.equal(view.unchangedForMs, 0, 'rewritten record resets the clock');
  h.poller.dispose();
});

test('poller: missing, invalid and failing status are unavailable (never a fake value) and polling continues', async () => {
  const h = harness();
  h.throwInUpdate = true;
  h.poller.start();
  let view = await h.answer({ kind: 'missing' });
  assert.deepEqual([view.kind, view.reason], ['unavailable', 'missing']);
  assert.equal(h.timers.size, 1, 'a throwing view does not stop polling');
  h.fireTimer();
  view = await h.answer({ kind: 'data', data: status({ state: 'paused' }) });
  assert.equal(view.reason, 'invalid');
  assert.match(view.message, /neznámý stav/);
  h.fireTimer();
  h.calls.at(-1).d.reject(new Error('vypršel časový limit 10 s'));
  await flush();
  assert.equal(h.updates.at(-1).reason, 'error');
  assert.equal(h.timers.size, 1);
  h.poller.dispose();
});

test('poller: paused while hidden, immediate refresh when visible, dispose aborts and drops late results', async () => {
  const h = harness({ hidden: true });
  h.poller.start();
  assert.equal(h.calls.length, 0, 'no request while hidden');
  h.setHidden(false);
  assert.equal(h.calls.length, 1);
  await h.answer({ kind: 'data', data: status() });
  h.setHidden(true);
  assert.equal(h.timers.size, 0, 'hidden: pending timer cleared');
  h.setHidden(false);
  assert.equal(h.calls.length, 2);
  h.setHidden(false);
  assert.equal(h.calls.length, 2, 'visibility event during a request does not overlap');
  const pending = h.calls[1];
  h.poller.dispose();
  assert.ok(pending.signal.aborted);
  pending.d.resolve({ kind: 'data', data: status() });
  await flush();
  assert.equal(h.updates.length, 1);
  assert.equal(h.timers.size, 0);
  h.poller.pollNow();
  assert.equal(h.calls.length, 2);
  assert.throws(() => hs.createHiresStatusPoller({ fetchStatus: null, onUpdate() {} }), TypeError);
});

// ---------------------------------------------------------------- panel wiring (source checks)

test('panel: separate labelled world-surface and spawn-detail sections, relative URL, bounded fetch', () => {
  const html = read('./web/index.html');
  const app = read('./web/app.js');
  const world = html.indexOf('Povrch celého světa');
  const spawn = html.indexOf('Detail okolí spawnu');
  assert.ok(world > 0 && spawn > world, 'world surface section first, spawn detail second');
  for (const id of ['hires-state', 'hires-message', 'hires-progress', 'hires-eta', 'hires-started', 'hires-finished']) {
    assert.equal(html.split(`id="${id}"`).length, 2, `exactly one #${id}`);
  }
  for (const id of ['hires-progress', 'hires-eta', 'hires-started', 'hires-finished']) {
    assert.ok(html.includes(`id="${id}-label"`), `#${id}-label for setRenderRow`);
  }
  const generated = html.indexOf('id="generated"');
  assert.ok(generated > spawn, 'the detail export date belongs to the spawn section');
  assert.match(html, /Data detailu vytvořena/);
  assert.doesNotMatch(html, /Data mapy vytvořena/);
  assert.match(app, /const HIRES_STATUS_URL = `\$\{DATA_URL\}hires\/status\.json`;/);
  assert.match(app, /fetchRenderStatus\(HIRES_STATUS_URL, \{ timeoutMs: RENDER_STATUS_TIMEOUT_MS, signal \}\)/);
  assert.match(app, /^ {2}startHiresStatus\(\);\r?$/m);
  assert.doesNotMatch(app.slice(app.indexOf('function showHiresStatus('), app.indexOf('function startHiresStatus(')),
    /publik|zveřejn/i, 'the world status never claims a publication time');
});
