// Progressive chunk loading: selection around a target and a budgeted, cancellable load/attach queue.
// No DOM or three.js; resources are created and destroyed only through the attach/detach callbacks.

const CHUNK = 16;
const MAX_REPORTS = 32;

/**
 * Chunks whose centre lies within Chebyshev distance `radius` (in chunks) of the target's chunk,
 * ordered by squared Euclidean distance from the chunk centre to the target, ties by x then z.
 * Returns at most `limit` of the original entry objects; `entries` is not modified.
 */
export function selectChunks(entries, targetX, targetZ, radius, limit) {
  if (!Array.isArray(entries)) throw new TypeError('entries must be an array');
  if (!Number.isFinite(targetX) || !Number.isFinite(targetZ)) throw new RangeError('target must be finite');
  if (!Number.isSafeInteger(radius) || radius < 0) throw new RangeError('radius must be a nonnegative integer');
  if (!Number.isSafeInteger(limit) || limit < 1) throw new RangeError('limit must be a positive integer');
  const cx = Math.floor(targetX / CHUNK);
  const cz = Math.floor(targetZ / CHUNK);
  const picked = [];
  for (const entry of entries) {
    if (!entry || !Number.isSafeInteger(entry.x) || !Number.isSafeInteger(entry.z)) {
      throw new TypeError('entry x/z must be safe integers');
    }
    if (Math.max(Math.abs(entry.x - cx), Math.abs(entry.z - cz)) > radius) continue;
    const dx = entry.x * CHUNK + CHUNK / 2 - targetX;
    const dz = entry.z * CHUNK + CHUNK / 2 - targetZ;
    picked.push({ entry, d: dx * dx + dz * dz });
  }
  picked.sort((a, b) => a.d - b.d || a.entry.x - b.entry.x || a.entry.z - b.entry.z);
  return picked.slice(0, limit).map((p) => p.entry);
}

const message = (err) => (err instanceof Error ? err.message : String(err));

/**
 * Loads the desired chunks nearest first with at most `maxConcurrent` running loads, attaches results
 * while the sum of resident payload.bytes stays <= maxBytes, and detaches what is no longer desired.
 * Entries are keyed by entry.file.
 */
export class ChunkStream {
  #load; #attach; #detach; #onChange; #maxConcurrent; #maxBytes;
  #desired = [];
  #rank = new Map();         // key -> index in #desired
  #resident = new Map();     // key -> { entry, resource, bytes }
  #active = new Map();       // key -> current (non-obsolete) load record
  #running = 0;              // all unsettled loads, including obsolete ones that ignore abort
  #failed = new Map();       // key -> { entry, message }; only retry() clears
  #limited = new Map();      // key -> bytes; blocked until the budget could fit it, or retry()
  #oversize = new Map();     // key -> bytes > maxBytes; only retry() clears
  #reports = [];             // callback failures not tied to a failed chunk (detach, onChange)
  #bytes = 0;
  #disposed = false;

  constructor({ load, attach, detach, onChange = () => {}, maxConcurrent = 4, maxBytes = 192 * 1024 * 1024 } = {}) {
    for (const [name, fn] of [['load', load], ['attach', attach], ['detach', detach], ['onChange', onChange]]) {
      if (typeof fn !== 'function') throw new TypeError(`${name} must be a function`);
    }
    if (!Number.isSafeInteger(maxConcurrent) || maxConcurrent < 1) throw new RangeError('maxConcurrent must be a positive integer');
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 0) throw new RangeError('maxBytes must be a nonnegative integer');
    this.#load = load;
    this.#attach = attach;
    this.#detach = detach;
    this.#onChange = onChange;
    this.#maxConcurrent = maxConcurrent;
    this.#maxBytes = maxBytes;
  }

  /** Replace the desired set, nearest first. An identical key order is a no-op. */
  setDesired(orderedEntries) {
    if (this.#disposed) return;
    if (!Array.isArray(orderedEntries)) throw new TypeError('orderedEntries must be an array');
    const rank = new Map();
    orderedEntries.forEach((entry, i) => {
      if (!entry || typeof entry.file !== 'string' || entry.file === ''
        || !Number.isSafeInteger(entry.x) || !Number.isSafeInteger(entry.z)) {
        throw new TypeError(`orderedEntries[${i}] needs integer x/z and a file`);
      }
      if (rank.has(entry.file)) throw new TypeError(`duplicate entry ${entry.file}`);
      rank.set(entry.file, i);
    });
    if (orderedEntries.length === this.#desired.length
      && orderedEntries.every((entry, i) => entry.file === this.#desired[i].file)) return;
    this.#desired = [...orderedEntries];
    this.#rank = rank;
    for (const [key, record] of [...this.#active]) {
      if (rank.has(key)) continue;
      record.obsolete = true;
      this.#active.delete(key);
      record.controller.abort();
    }
    for (const key of [...this.#resident.keys()]) if (!rank.has(key)) this.#remove(key);
    // Unblock budget-limited chunks only where the new situation could actually fit them.
    for (const [key, bytes] of [...this.#limited]) {
      if (!rank.has(key)) this.#limited.delete(key);
      else if (bytes <= this.#available(rank.get(key))) this.#limited.delete(key);
    }
    this.#pump();
    this.#notify();
  }

  /** Re-queue failed and budget-limited chunks and clear reported callback errors. */
  retry() {
    if (this.#disposed) return;
    this.#failed.clear();
    this.#limited.clear();
    this.#oversize.clear();
    this.#reports = [];
    this.#pump();
    this.#notify();
  }

  /** Abort all loads and detach all resources. Idempotent; late results are ignored. */
  dispose() {
    if (this.#disposed) return;
    this.#disposed = true;
    for (const record of this.#active.values()) {
      record.obsolete = true;
      record.controller.abort();
    }
    this.#active.clear();
    for (const key of [...this.#resident.keys()]) this.#remove(key);
    this.#desired = [];
    this.#rank = new Map();
    this.#failed.clear();
    this.#limited.clear();
    this.#oversize.clear();
    this.#notify();
  }

  getSnapshot() {
    const pick = (predicate) => this.#desired.filter((entry) => predicate(entry.file));
    const failed = pick((key) => this.#failed.has(key));
    return {
      desired: [...this.#desired],
      resident: pick((key) => this.#resident.has(key)),
      pending: pick((key) => this.#active.has(key)),
      queued: pick((key) => this.#isQueued(key)),
      failed,
      bytes: this.#bytes,
      limited: pick((key) => this.#limited.has(key) || this.#oversize.has(key)),
      errors: [...failed.map((entry) => ({ entry, message: this.#failed.get(entry.file).message })),
        ...this.#reports.map((r) => ({ ...r }))],
    };
  }

  // ------------------------------------------------------------ internals

  #isQueued(key) {
    return !this.#resident.has(key) && !this.#active.has(key) && !this.#failed.has(key)
      && !this.#limited.has(key) && !this.#oversize.has(key);
  }

  /** Free budget plus bytes of residents that rank behind `rank` (evictable for it). */
  #available(rank) {
    let free = this.#maxBytes - this.#bytes;
    for (const [key, r] of this.#resident) if (this.#rank.get(key) > rank) free += r.bytes;
    return free;
  }

  #report(entry, text) {
    this.#reports.push({ entry, message: text });
    if (this.#reports.length > MAX_REPORTS) this.#reports.shift();
  }

  #remove(key) {
    const r = this.#resident.get(key);
    this.#resident.delete(key);
    this.#bytes -= r.bytes;
    try {
      this.#detach(r.entry, r.resource);
    } catch (err) {
      this.#report(r.entry, `detach selhal: ${message(err)}`);
    }
  }

  #pump() {
    if (this.#disposed) return;
    for (const entry of this.#desired) {
      if (this.#running >= this.#maxConcurrent) break;
      const key = entry.file;
      // Payload size is unknown until loaded; #accept decides (0 B fits a full budget, others become limited).
      if (this.#isQueued(key)) this.#start(entry);
    }
  }

  #start(entry) {
    const record = { entry, controller: new AbortController(), obsolete: false };
    this.#active.set(entry.file, record);
    this.#running += 1;
    let promise;
    try {
      promise = Promise.resolve(this.#load(entry, record.controller.signal));
    } catch (err) {
      promise = Promise.reject(err);
    }
    promise.then((payload) => this.#finish(record, true, payload), (err) => this.#finish(record, false, err));
  }

  #finish(record, ok, value) {
    this.#running -= 1;
    const key = record.entry.file;
    if (this.#active.get(key) === record) this.#active.delete(key);
    if (record.obsolete || this.#disposed) {
      this.#pump();
      this.#notify();
      return;
    }
    if (!ok) this.#failed.set(key, { entry: record.entry, message: `načtení selhalo: ${message(value)}` });
    else this.#accept(record.entry, value);
    this.#pump();
    this.#notify();
  }

  #accept(entry, payload) {
    const key = entry.file;
    const bytes = payload?.bytes;
    if (!payload || typeof payload !== 'object' || !Number.isSafeInteger(bytes) || bytes < 0) {
      this.#failed.set(key, { entry, message: 'neplatný payload: bytes musí být nezáporné celé číslo' });
      return;
    }
    if (bytes > this.#maxBytes) {
      this.#oversize.set(key, bytes);
      return;
    }
    const rank = this.#rank.get(key);
    if (bytes > this.#available(rank)) {
      this.#limited.set(key, bytes);
      return;
    }
    // Evict the farthest residents behind this entry until it fits; they become limited, not re-queued.
    const farther = [...this.#resident.entries()]
      .filter(([k]) => this.#rank.get(k) > rank)
      .sort(([a], [b]) => this.#rank.get(b) - this.#rank.get(a));
    for (const [k, r] of farther) {
      if (this.#bytes + bytes <= this.#maxBytes) break;
      this.#remove(k);
      this.#limited.set(k, r.bytes);
    }
    let resource;
    try {
      resource = this.#attach(entry, payload);
    } catch (err) {
      this.#failed.set(key, { entry, message: `attach selhal: ${message(err)}` });
      return;
    }
    this.#resident.set(key, { entry, resource, bytes });
    this.#bytes += bytes;
  }

  #notify() {
    try {
      this.#onChange(this.getSnapshot());
    } catch (err) {
      this.#report(null, `onChange selhal: ${message(err)}`);
    }
  }
}
