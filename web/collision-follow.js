// Follows the mutable collision pointer (base + 'current.json', UIE_COLLISION_POINTER1) while walking and swaps the
// walking collision world (createCollisionWorld) only to a fully validated immutable index. Pure: no DOM.
//
// createCollisionFollower({ base, fetcher, timeoutMs, intervalMs, onChange, onError, now, setTimer, clearTimer,
//                           createWorld })
//   .update(view)          latest view { walking, x, z, geometryAt, atlasSha }; polling runs only while walking
//   .blockAt(x, y, z)      from the active world (its revision + atlas checks apply); undefined otherwise
//   .ready, .bytes, .resident, .stats, .dispose()
//
// Rules
// - The pointer is fetched with cache 'no-store', one request at a time, at most every intervalMs (30 s), with a
//   deadline over headers AND body (also when the fetch ignores the abort). Failures back off 30 s, 60 s, ... 5 min;
//   update() never polls earlier than the schedule.
// - A new index hash becomes a candidate world: loadIndex() WITHOUT a view (no block downloads, ~0 bytes). Only after
//   its immutable index was fetched, hash-checked and validated is it swapped in: the old world is disposed first,
//   then the candidate receives the latest view. Pointer, network or index failures keep the old world.
// - Same hash as the active world: nothing is rebuilt.
// - Leaving walking stops polling, cancels the pointer request and discards a pending candidate; the active world
//   gets the non-walking view (it releases its block sources) and its index is kept for the next walk.
// - Every asynchronous completion carries a token; completions after stop, dispose or a newer request are ignored.

import { createCollisionWorld } from './collision-world.js';

export const POINTER_FORMAT = 'UIE_COLLISION_POINTER1';
export const POLL_INTERVAL_MS = 30_000;
export const MAX_BACKOFF_MS = 300_000;
export const MAX_POINTER_BYTES = 4096;
const HEX = /^[a-f0-9]{64}$/;

export function parseCollisionPointer(raw) {
  if (!(raw instanceof ArrayBuffer) || raw.byteLength > MAX_POINTER_BYTES) throw new Error('Collision pointer too large');
  const doc = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw));
  if (!doc || typeof doc !== 'object' || Array.isArray(doc) || Object.keys(doc).length !== 2
      || doc.format !== POINTER_FORMAT || typeof doc.indexSha256 !== 'string' || !HEX.test(doc.indexSha256)) {
    throw new Error('Invalid collision pointer');
  }
  return doc.indexSha256;
}

export function createCollisionFollower({ base, fetcher = fetch, timeoutMs = 15_000, intervalMs = POLL_INTERVAL_MS,
  onChange = () => {}, onError = () => {}, now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout,
  createWorld = createCollisionWorld } = {}) {
  if (typeof base !== 'string') throw new TypeError('base must be a string');
  let view = null;
  let walking = false;
  let disposed = false;
  let active = null;            // { hash, world }
  let candidate = null;         // { hash, world }
  let request = null;           // { token, controller, rejectStop }
  let timer = null;
  let nextPollAt = 0;
  let failures = 0;
  let token = 0;
  const counts = { polls: 0, pointerErrors: 0, indexErrors: 0, swaps: 0, lastError: null };
  const quiet = (fn) => { try { fn(); } catch { /* callbacks must not break the follower */ } };

  function backoff() {
    failures += 1;
    return Math.min(MAX_BACKOFF_MS, intervalMs * 2 ** (failures - 1));
  }

  function report(kind, hash, error) {
    counts.lastError = { kind, message: String(error?.message ?? error) };
    quiet(() => onError(kind, hash, error));
  }

  function cancelTimer() {
    if (timer !== null) { clearTimer(timer); timer = null; }
  }

  function schedule() {
    cancelTimer();
    if (disposed || !walking || request) return;
    const wait = nextPollAt - now();
    if (wait <= 0) { poll(); return; }
    timer = setTimer(() => { timer = null; schedule(); }, wait);
  }

  function cancelRequest() {
    if (!request) return;
    request.controller.abort();
    request.rejectStop(new Error('Collision pointer request cancelled'));
    request = null;
  }

  function discardCandidate() {
    if (!candidate) return;
    const { world } = candidate;
    candidate = null;
    quiet(() => world.dispose());
  }

  function poll() {
    const mine = ++token;
    const controller = new AbortController();
    let deadlineTimer = null;
    let rejectStop;
    const stop = new Promise((_resolve, reject) => { rejectStop = reject; });
    const deadline = new Promise((_resolve, reject) => {
      deadlineTimer = setTimer(() => {
        controller.abort();
        reject(new Error('Collision pointer timeout'));
      }, timeoutMs);
    });
    request = { token: mine, controller, rejectStop };
    counts.polls += 1;
    const work = (async () => {
      const response = await fetcher(`${base}current.json`, { cache: 'no-store', signal: controller.signal });
      if (!response?.ok) throw new Error(`Collision pointer HTTP ${response?.status ?? 'none'}`);
      return parseCollisionPointer(await response.arrayBuffer());
    })();
    stop.catch(() => {});
    Promise.race([work, deadline, stop]).then((hash) => settle(mine, deadlineTimer, null, hash),
      (error) => settle(mine, deadlineTimer, error, null));
  }

  function settle(mine, deadlineTimer, error, hash) {
    clearTimer(deadlineTimer);
    if (disposed || request?.token !== mine) return;      // stopped, disposed or superseded: ignore
    request = null;
    if (error) {
      counts.pointerErrors += 1;
      nextPollAt = now() + backoff();
      report('pointer', null, error);
    } else {
      nextPollAt = now() + intervalMs;
      if (hash === active?.hash) {
        failures = 0;
        discardCandidate();                                 // the pointer went back to the active index
      } else if (hash !== candidate?.hash) {
        startCandidate(hash);
      }
    }
    schedule();
  }

  function startCandidate(hash) {
    discardCandidate();
    let world;
    try {
      world = createWorld({ base, indexHash: hash, fetcher, timeoutMs, now,
        onChange: () => { if (active?.world === world) quiet(onChange); },
        onError: (key, sha, error) => { if (active?.world === world || candidate?.world === world) report(key, sha, error); } });
    } catch (error) {
      counts.indexErrors += 1;
      nextPollAt = now() + backoff();
      report('index', hash, error);
      return;
    }
    candidate = { hash, world };
    // No view: the candidate downloads only its immutable index, never a block window.
    world.loadIndex().then(() => {
      if (disposed || candidate?.world !== world) return;   // discarded meanwhile (its world is already disposed)
      swap();
    }, (error) => {
      if (disposed || candidate?.world !== world) return;
      discardCandidate();
      counts.indexErrors += 1;
      nextPollAt = Math.max(nextPollAt, now() + backoff());
      report('index', hash, error);
      schedule();
    });
  }

  function swap() {
    const next = candidate;
    candidate = null;
    const old = active;
    active = next;
    failures = 0;
    counts.swaps += 1;
    if (old) quiet(() => old.world.dispose());            // release the old block window BEFORE the new one loads
    if (view) active.world.update(view);
    quiet(onChange);
  }

  function stopPolling() {
    token += 1;                                            // late completions of anything started before are ignored
    cancelTimer();
    cancelRequest();
    discardCandidate();
  }

  return {
    update(next) {
      if (disposed) return;
      view = next ?? null;
      const was = walking;
      walking = Boolean(view?.walking);
      active?.world.update(view);
      if (walking && !was) schedule();
      else if (!walking && was) stopPolling();
      else if (walking && !request && timer === null) schedule();
    },
    blockAt(x, y, z) {
      return walking && active ? active.world.blockAt(x, y, z) : undefined;
    },
    get ready() { return Boolean(walking && active?.world.ready); },
    get bytes() { return (active?.world.bytes ?? 0) + (candidate?.world.bytes ?? 0); },
    get resident() { return active?.world.resident ?? 0; },
    get indexSha256() { return active?.hash ?? null; },
    get stats() {
      return { indexSha256: active?.hash ?? null, candidateSha256: candidate?.hash ?? null, polling: walking && !disposed,
        requestInFlight: Boolean(request), nextPollInMs: Math.max(0, nextPollAt - now()), consecutiveFailures: failures,
        ...counts, lastError: counts.lastError && { ...counts.lastError } };
    },
    dispose() {
      if (disposed) return;
      stopPolling();
      disposed = true;
      if (active) { const { world } = active; active = null; quiet(() => world.dispose()); }
      view = null;
      walking = false;
    },
  };
}
