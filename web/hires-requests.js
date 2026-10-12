// Bounded, cancellable downloads of the currently wanted whole-world tiles (hires UIH1). Pure: no DOM, no three.js.
//
// createHiresRequests({ fetchTile, decodeTile, onTile, onError, onSettled, concurrency, timeoutMs,
//                       maxFailureRecords, now, setTimer, clearTimer })
//   fetchTile(key, sha, signal) -> Promise<Response-like { ok, status, arrayBuffer(), headers? }>
//   decodeTile(arrayBuffer)     -> tile (may throw; a throw counts as a failed download)
//   onTile(key, sha, tile)      -> false when the caller did NOT attach it (then it is retried with backoff);
//                                  anything else means attached. Called only for the still-wanted key+sha.
//   onError(key, sha, error, failureCount), onSettled() after every finished, failed, timed out or cancelled request.
//   .setDesired(Map key -> sha) wanted tiles in priority order (nearest first). Keys that are already shown must not be
//                               listed. A request whose key left the map or whose sha changed is cancelled at once and
//                               its slot is free immediately (also when the transport ignores the abort).
//   .retryFailed() -> n         makes the failed wanted tiles due now (manual retry); their failure count is kept.
//   .snapshot()                 { loading, desired, failed: [{ key, sha, count, retryInMs, message }] (wanted only),
//                                 busyRetryInMs }  (server capacity cooldown left, 0 when none)
//   .loading, .dispose()
//
// Rules: the timeout covers the whole request (headers, body and decoding). A failure (HTTP error, network error,
// timeout, decode error, refused attach) waits retryDelay(count) before the next attempt of the same key+sha:
// 1, 2, 4, 8, 16, 30 s, then a slow probe every 5 min. Changing view or re-listing a key never bypasses the wait;
// leaving the window keeps the record; a new sha starts fresh. At most maxFailureRecords records of tiles that are
// NOT wanted are kept (oldest forgotten first); records of wanted tiles are never dropped.
//
// Server capacity: HTTP 503 with a valid Retry-After of 1..60 whole seconds (headers.get('retry-after'), e.g. the
// immutable hires endpoint's "Retry-After: 1") is NOT a tile failure: no failure record, no count, no onError, no
// 5 min slowdown. Instead the whole queue starts no new request until that cooldown ends (requests already in
// flight continue; the latest of several cooldowns wins). setDesired, onSettled-driven pumps and retryFailed never
// start a request during the cooldown. A 503 without, or with a malformed / out-of-range, Retry-After is an ordinary
// failure as before. Earlier real failure records keep their count and due time.

export const HIRES_REQUEST_TIMEOUT_MS = 20_000;
export const MAX_FAILURE_RECORDS = 512;
export const RETRY_BASE_MS = 1000;
export const RETRY_CAP_MS = 30_000;
export const SLOW_PROBE_AFTER = 7;
export const SLOW_PROBE_MS = 300_000;

export function retryDelay(failures) {
  if (!Number.isInteger(failures) || failures < 1) return RETRY_BASE_MS;
  if (failures >= SLOW_PROBE_AFTER) return SLOW_PROBE_MS;   // keep probing slowly after persistent failures
  return Math.min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** (failures - 1));
}

export const MAX_CAPACITY_COOLDOWN_S = 60;

/** Milliseconds of a valid capacity Retry-After (whole seconds 1..60), or null for missing/malformed/out of range. */
export function capacityRetryAfterMs(response) {
  if (response?.status !== 503) return null;
  let value;
  try { value = response.headers?.get?.('retry-after'); } catch { return null; }
  if (typeof value !== 'string' || !/^[0-9]{1,2}$/.test(value.trim())) return null;
  const seconds = Number(value.trim());
  return seconds >= 1 && seconds <= MAX_CAPACITY_COOLDOWN_S ? seconds * 1000 : null;
}

export class HiresCapacityError extends Error {
  constructor(retryAfterMs) {
    super(`server busy, retry after ${retryAfterMs / 1000} s`);
    this.name = 'HiresCapacityError';
    this.retryAfterMs = retryAfterMs;
  }
}

const positiveInteger = (value) => Number.isInteger(value) && value > 0;

export function createHiresRequests({ fetchTile, decodeTile, onTile, onError = () => {}, onSettled = () => {},
  concurrency = 6, timeoutMs = HIRES_REQUEST_TIMEOUT_MS, maxFailureRecords = MAX_FAILURE_RECORDS,
  now = () => Date.now(), setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  for (const [name, fn] of Object.entries({ fetchTile, decodeTile, onTile, onError, onSettled, now, setTimer, clearTimer })) {
    if (typeof fn !== 'function') throw new TypeError(`${name} must be a function`);
  }
  if (!positiveInteger(concurrency) || !positiveInteger(timeoutMs) || !positiveInteger(maxFailureRecords)) {
    throw new RangeError('concurrency, timeoutMs and maxFailureRecords must be positive integers');
  }

  let desired = new Map();
  const active = new Map();     // key -> request in flight (always for the wanted sha)
  const failures = new Map();   // key -> { sha, count, nextAt, message }; insertion order = oldest failure first
  const done = new Map();       // key -> sha delivered and attached, until the key leaves the wanted map
  let retryTimer = null;
  let busyUntil = -Infinity;    // no new request before this time (server capacity cooldown)
  let disposed = false;

  const quiet = (fn) => { try { fn(); } catch { /* callback errors must not break the scheduler */ } };

  function clearRetryTimer() {
    if (retryTimer !== null) clearTimer(retryTimer);
    retryTimer = null;
  }

  // Records of tiles that are no longer wanted are forgotten oldest first. A wanted tile's record is never dropped:
  // that would retry it at once and could ping-pong between wanted tiles (the wanted set is bounded by the window).
  function trimFailures() {
    if (failures.size <= maxFailureRecords) return;
    for (const key of [...failures.keys()]) {
      if (failures.size <= maxFailureRecords) break;
      if (!desired.has(key)) failures.delete(key);
    }
  }

  function recordFailure(key, sha, error) {
    const previous = failures.get(key);
    const count = (previous?.sha === sha ? previous.count : 0) + 1;
    failures.delete(key);   // re-insert: the newest failure is the last one forgotten
    failures.set(key, { sha, count, nextAt: now() + retryDelay(count), message: String(error?.message ?? error) });
    trimFailures();
    quiet(() => onError(key, sha, error, count));
  }

  function cancel(request) {
    if (request.settled || request.cancelled) return;
    request.cancelled = true;
    if (active.get(request.key) === request) active.delete(request.key);   // the slot is free right now
    request.controller.abort();
    request.rejectCancel(new DOMException('Tile no longer wanted', 'AbortError'));
  }

  function settle(request, ok, value) {
    if (request.settled) return;
    request.settled = true;
    if (request.timer !== null) { clearTimer(request.timer); request.timer = null; }
    if (active.get(request.key) === request) active.delete(request.key);
    const { key, sha } = request;
    if (!request.cancelled && !disposed && desired.get(key) === sha) {
      let failure = ok ? null : value;
      if (ok) {
        let attached;
        try { attached = onTile(key, sha, value); } catch (error) { failure = error; }
        if (!failure && attached === false) failure = new Error('tile was not attached');
      }
      if (failure instanceof HiresCapacityError) {
        // Temporary server capacity, not tile data: the record (if any) keeps its count and due time.
        busyUntil = Math.max(busyUntil, now() + failure.retryAfterMs);
      } else if (failure) recordFailure(key, sha, failure);
      else { done.set(key, sha); failures.delete(key); }
    }
    if (disposed) return;
    quiet(onSettled);
    pump();
  }

  function start(key, sha) {
    const controller = new AbortController();
    const request = { key, sha, controller, cancelled: false, settled: false, timer: null, rejectCancel: null };
    active.set(key, request);
    const cancellation = new Promise((_resolve, reject) => { request.rejectCancel = reject; });
    const deadline = new Promise((_resolve, reject) => {
      request.timer = setTimer(() => {
        request.timer = null;
        const error = new Error(`hires tile ${key} timed out after ${timeoutMs} ms`);
        error.name = 'TimeoutError';
        controller.abort();
        reject(error);
      }, timeoutMs);
    });
    // The timeout races the whole job: headers, body and decoding.
    const work = (async () => {
      const response = await fetchTile(key, sha, controller.signal);
      if (!response?.ok) {
        const retryAfterMs = capacityRetryAfterMs(response);
        if (retryAfterMs !== null) throw new HiresCapacityError(retryAfterMs);
        throw new Error(`HTTP ${response?.status ?? 'bez odpovědi'}`);
      }
      return decodeTile(await response.arrayBuffer());
    })();
    Promise.race([work, deadline, cancellation]).then((tile) => settle(request, true, tile),
      (error) => settle(request, false, error));
  }

  function armRetry() {
    clearRetryTimer();
    // Completion/timeout/cancellation will pump again. A due retry cannot use a full pool;
    // scheduling it every millisecond here would create a CPU hot loop despite fetch backoff.
    if (disposed || active.size >= concurrency) return;
    const t = now();
    let next = Infinity;
    for (const [key, sha] of desired) {
      if (active.has(key) || done.get(key) === sha) continue;
      const failure = failures.get(key);
      // A tile without a due failure is due now; it is only left over while the capacity cooldown runs.
      const due = failure?.sha === sha ? failure.nextAt : t;
      next = Math.min(next, Math.max(due, busyUntil));
    }
    if (Number.isFinite(next)) retryTimer = setTimer(() => { retryTimer = null; pump(); }, Math.max(1, next - t));
  }

  function pump() {
    if (disposed) return;
    const t = now();
    if (t >= busyUntil) {             // during a capacity cooldown nothing new starts, whoever pumps
      for (const [key, sha] of desired) {
        if (active.size >= concurrency) break;
        if (active.has(key) || done.get(key) === sha) continue;
        const failure = failures.get(key);
        if (failure?.sha === sha && failure.nextAt > t) continue;
        start(key, sha);
      }
    }
    armRetry();
  }

  return {
    get loading() { return active.size; },
    setDesired(next) {
      if (disposed) return;
      const wanted = new Map(next);
      for (const [key, sha] of wanted) {
        if (typeof key !== 'string' || typeof sha !== 'string') throw new TypeError('desired tiles must be string key -> string sha');
      }
      desired = wanted;
      for (const request of [...active.values()]) if (desired.get(request.key) !== request.sha) cancel(request);
      // A new revision of a tile starts fresh; a tile that only left the window keeps its backoff.
      for (const [key, failure] of [...failures]) {
        if (desired.has(key) && desired.get(key) !== failure.sha) failures.delete(key);
      }
      for (const [key, sha] of [...done]) if (desired.get(key) !== sha) done.delete(key);
      trimFailures();
      pump();
    },
    retryFailed() {
      if (disposed) return 0;
      const t = now();
      let n = 0;
      for (const [key, sha] of desired) {
        const failure = failures.get(key);
        if (failure?.sha === sha && !active.has(key) && failure.nextAt > t) { failure.nextAt = t; n += 1; }
      }
      pump();
      return n;
    },
    snapshot() {
      const t = now();
      const failed = [];
      for (const [key, sha] of desired) {
        const failure = failures.get(key);
        if (failure?.sha !== sha || active.has(key) || done.get(key) === sha) continue;
        failed.push({ key, sha, count: failure.count, retryInMs: Math.max(0, failure.nextAt - t), message: failure.message });
      }
      return { loading: active.size, desired: desired.size, failed, busyRetryInMs: Math.max(0, busyUntil - t) };
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      clearRetryTimer();
      for (const request of [...active.values()]) cancel(request);
      desired = new Map();
      failures.clear();
      done.clear();
    },
  };
}
