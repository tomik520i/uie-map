// Two-resolution per-tile coverage controller (coarse/detail). No DOM or three.js: resources are created,
// shown, hidden and destroyed only through the callbacks. Not a quadtree or a full-world LOD; ChunkStream
// and chunk selection are unchanged.
//
// new LodSwap({ load, attach, detach, setVisible, maxBytes, maxConcurrent = 4, onChange })
//   load(entry, representation, signal) -> Promise<{ bytes, data }>   bytes = actual, <= the reservation
//   attach(entry, representation, payload) -> resource                 creates it HIDDEN; must not leak if it throws
//   setVisible(resource, visible)                                      expected not to throw
//   detach(resource)                                                   destroys it; called exactly once per resource
// setDesired([{ id, generation, level: 'coarse' | 'detail', coarse: { key, bytes } | null, detail: { key, bytes } | null }])
// retry(), dispose(), getSnapshot()
//
// Guarantees:
// - A visible tile stays visible until its replacement has loaded and attached; then, synchronously, the new
//   resource is shown, the old one hidden and detached. No frame without coverage for a desired tile.
// - Budget = bytes of attached resources + reserved bytes of every unsettled load, including obsolete loads
//   that ignore abort. A load starts only if that sum plus its reservation fits maxBytes, so the handoff
//   (old visible + replacement) never exceeds it. If it does not fit, the old resource stays and the tile is
//   reported as limited; nothing is evicted to make room.
// - Detail with a coarse representation loads coarse first (when the current generation is not yet visible),
//   then refines; if detail does not fit, coarse stays.
// - A new generation never receives a stale result; the old generation stays visible until a matching new
//   resource is attached. The snapshot shows which generation is visible.
// - downgradeReserveBytes (0..maxBytes, default 0): detail admission is limited to maxBytes minus this
//   reserve; coarse admission may use all of maxBytes. Set it to at least the largest coarse representation
//   so a filled detail budget can still start a coarse downgrade.
// - Coverage first, then coarse downgrades of visible detail, then refinements/replacements; desired order is
//   the priority within each phase. Failed loads/attaches/shows are not retried until retry(); limited tiles
//   start only when the budget allows, without retrying anything that failed. Only failures of the current
//   descriptors are kept (at most two per desired tile).
// - If setVisible(new, true) throws, the new resource is detached once, the old one stays visible and the
//   representation is recorded as failed.

const LEVELS = new Set(['coarse', 'detail']);
const MAX_REPORTS = 32;
const message = (err) => (err instanceof Error ? err.message : String(err));
const isBytes = (v) => Number.isSafeInteger(v) && v >= 0;
const nonEmpty = (v) => typeof v === 'string' && v !== '';

function validate(entries) {
  if (!Array.isArray(entries)) throw new TypeError('entries must be an array');
  const ids = new Set();
  const keys = new Set();
  return entries.map((e, i) => {
    if (!e || typeof e !== 'object') throw new TypeError(`entries[${i}] must be an object`);
    if (!nonEmpty(e.id) || ids.has(e.id)) throw new TypeError(`entries[${i}] needs a unique non-empty id`);
    if (!nonEmpty(e.generation)) throw new TypeError(`entries[${i}] needs a non-empty generation`);
    if (!LEVELS.has(e.level)) throw new TypeError(`entries[${i}].level must be 'coarse' or 'detail'`);
    const reps = {};
    for (const name of ['coarse', 'detail']) {
      const r = e[name];
      if (r === undefined || r === null) { reps[name] = null; continue; }
      if (typeof r !== 'object' || !nonEmpty(r.key) || !isBytes(r.bytes)) {
        throw new TypeError(`entries[${i}].${name} needs a non-empty key and safe-integer bytes >= 0`);
      }
      if (keys.has(r.key)) throw new TypeError(`duplicate content key ${r.key}`);
      keys.add(r.key);
      reps[name] = Object.freeze({ key: r.key, bytes: r.bytes });
    }
    if (!reps[e.level]) throw new TypeError(`entries[${i}] has no ${e.level} representation`);
    ids.add(e.id);
    return Object.freeze({ id: e.id, generation: e.generation, level: e.level, ...reps });
  });
}

export class LodSwap {
  #load; #attach; #detach; #setVisible; #onChange; #maxBytes; #maxConcurrent;
  #downgradeReserve;        // bytes detail admission may not use, kept free for coarse replacements
  #order = [];              // desired ids, priority order
  #tiles = new Map();       // id -> { desc, visible, active }
  #failed = new Map();      // JSON [id, generation, rep, key] -> { id, generation, rep, key, message }; current descriptors only
  #limited = new Set();
  #reports = [];
  #running = 0;             // unsettled loads, including obsolete ones
  #attachedBytes = 0;
  #reservedBytes = 0;
  #disposed = false;

  constructor({ load, attach, detach, setVisible, onChange = () => {}, maxBytes, maxConcurrent = 4,
    downgradeReserveBytes = 0 } = {}) {
    for (const [name, fn] of [['load', load], ['attach', attach], ['detach', detach], ['setVisible', setVisible],
      ['onChange', onChange]]) {
      if (typeof fn !== 'function') throw new TypeError(`${name} must be a function`);
    }
    if (!isBytes(maxBytes)) throw new RangeError('maxBytes must be a safe integer >= 0');
    if (!Number.isSafeInteger(maxConcurrent) || maxConcurrent < 1) throw new RangeError('maxConcurrent must be a positive integer');
    if (!isBytes(downgradeReserveBytes) || downgradeReserveBytes > maxBytes) {
      throw new RangeError('downgradeReserveBytes must be a safe integer 0..maxBytes');
    }
    this.#load = load; this.#attach = attach; this.#detach = detach; this.#setVisible = setVisible;
    this.#onChange = onChange; this.#maxBytes = maxBytes; this.#maxConcurrent = maxConcurrent;
    this.#downgradeReserve = downgradeReserveBytes;
  }

  setDesired(entries) {
    if (this.#disposed) return;
    const descs = validate(entries);   // everything is validated before any state changes
    const wanted = new Set(descs.map((d) => d.id));
    for (const [id, tile] of [...this.#tiles]) {
      if (wanted.has(id)) continue;
      this.#abandon(tile);
      if (tile.visible) this.#release(tile.visible);
      this.#tiles.delete(id);
    }
    for (const d of descs) {
      const tile = this.#tiles.get(d.id);
      if (tile) tile.desc = d;
      else this.#tiles.set(d.id, { desc: d, visible: null, active: null });
    }
    // Keep only failures of the current descriptors (at most coarse + detail per desired tile).
    for (const [key, f] of [...this.#failed]) {
      const desc = this.#tiles.get(f.id)?.desc;
      if (!desc || desc.generation !== f.generation || desc[f.rep]?.key !== f.key) this.#failed.delete(key);
    }
    this.#order = descs.map((d) => d.id);
    this.#refresh();
  }

  /** Clear failures and callback reports and try again. */
  retry() {
    if (this.#disposed) return;
    this.#failed.clear();
    this.#reports = [];
    this.#refresh();
  }

  /** Abort loads and detach every resource exactly once. Idempotent; late results are dropped. */
  dispose() {
    if (this.#disposed) return;
    this.#disposed = true;
    for (const tile of this.#tiles.values()) {
      this.#abandon(tile);
      if (tile.visible) this.#release(tile.visible);
      tile.visible = null;
    }
    this.#tiles.clear();
    this.#order = [];
    this.#failed.clear();
    this.#limited.clear();
    this.#notify();
  }

  getSnapshot() {
    const failures = [...this.#failed.values()].map((f) => ({
      id: f.id, generation: f.generation, representation: f.rep, key: f.key, message: f.message }));
    return {
      maxBytes: this.#maxBytes, downgradeReserveBytes: this.#downgradeReserve,
      bytes: this.#attachedBytes + this.#reservedBytes,
      attachedBytes: this.#attachedBytes, reservedBytes: this.#reservedBytes, running: this.#running,
      tiles: this.#order.map((id) => {
        const { desc, visible, active } = this.#tiles.get(id);
        return {
          id, generation: desc.generation, level: desc.level,
          visible: visible && { generation: visible.generation, representation: visible.rep, key: visible.key, bytes: visible.bytes },
          loading: active && { generation: active.generation, representation: active.rep, key: active.key, reservedBytes: active.reserved },
          settled: this.#isCurrent(desc, visible) && visible.rep === desc.level,
          limited: this.#limited.has(id),
          failed: failures.filter((f) => f.id === id),
        };
      }),
      errors: this.#reports.map((r) => ({ ...r })),
    };
  }

  // ------------------------------------------------------------ internals

  #failKey(id, generation, rep, key) { return JSON.stringify([id, generation, rep, key]); }

  #isCurrent(desc, visible) {
    return Boolean(visible) && visible.generation === desc.generation && desc[visible.rep]?.key === visible.key;
  }

  /** Next representation to load for a tile, or null (settled, or everything still useful has failed). */
  #next(tile) {
    const { desc, visible } = tile;
    const current = this.#isCurrent(desc, visible);
    if (current && visible.rep === desc.level) return null;
    const steps = desc.level === 'detail' && desc.coarse && !current ? ['coarse', 'detail'] : [desc.level];
    return steps.find((rep) => !this.#failed.has(this.#failKey(desc.id, desc.generation, rep, desc[rep].key))) ?? null;
  }

  #refresh() {
    for (const tile of this.#tiles.values()) {
      const a = tile.active;
      if (!a) continue;
      const rep = this.#next(tile);
      if (rep !== a.rep || a.generation !== tile.desc.generation || tile.desc[rep]?.key !== a.key) this.#abandon(tile);
    }
    this.#pump();
    this.#notify();
  }

  #abandon(tile) {
    const a = tile.active;
    if (!a) return;
    a.obsolete = true;    // still counted in #running and #reservedBytes until it settles
    tile.active = null;
    a.controller.abort();
  }

  #release(res) {
    try { this.#setVisible(res.resource, false); } catch (err) { this.#report(res.key, `setVisible failed: ${message(err)}`); }
    try { this.#detach(res.resource); } catch (err) { this.#report(res.key, `detach failed: ${message(err)}`); }
    this.#attachedBytes -= res.bytes;
  }

  #report(key, text) {
    this.#reports.push({ key, message: text });
    if (this.#reports.length > MAX_REPORTS) this.#reports.shift();
  }

  /** Admission limit: detail may not use the downgrade reserve; coarse may use the whole budget. */
  #limitFor(rep) {
    return rep === 'detail' ? this.#maxBytes - this.#downgradeReserve : this.#maxBytes;
  }

  #pump() {
    if (this.#disposed) return;
    this.#limited.clear();
    // Three phases, each in desired order:
    //   0. coverage: tiles with nothing visible;
    //   1. downgrades: a coarse replacement of a visible detail resource (frees memory once switched);
    //   2. everything else: refinement to detail, generation replacement.
    // While a phase has a tile that waits only for pending reservations to settle (it fits with the
    // attached resources alone), later phases start nothing, so upgrades cannot take reclaimed room from a
    // downgrade and nothing takes room from coverage. A tile that cannot fit even then does not block.
    const phases = [[], [], []];
    for (const id of this.#order) {
      const tile = this.#tiles.get(id);
      if (tile.active) continue;
      const rep = this.#next(tile);
      if (!rep) continue;
      phases[!tile.visible ? 0 : tile.visible.rep === 'detail' && rep === 'coarse' ? 1 : 2].push([tile, rep]);
    }
    let blocked = false;
    for (const entries of phases) {
      let waiting = false;
      for (const [tile, rep] of entries) {
        if (this.#running >= this.#maxConcurrent) return;
        const need = tile.desc[rep].bytes;
        const limit = this.#limitFor(rep);
        if (blocked || this.#attachedBytes + this.#reservedBytes + need > limit) {
          this.#limited.add(tile.desc.id);   // the visible resource stays; nothing is evicted for room
          if (!blocked && this.#attachedBytes + need <= limit) waiting = true;
          continue;
        }
        this.#start(tile, rep);
      }
      blocked = blocked || waiting;
    }
  }

  #start(tile, rep) {
    const desc = tile.desc;
    const record = { id: desc.id, generation: desc.generation, rep, key: desc[rep].key, reserved: desc[rep].bytes,
      entry: desc, controller: new AbortController(), obsolete: false };
    tile.active = record;
    this.#running += 1;
    this.#reservedBytes += record.reserved;
    let promise;
    try {
      promise = Promise.resolve(this.#load(desc, rep, record.controller.signal));
    } catch (err) {
      promise = Promise.reject(err);
    }
    promise.then((value) => this.#finish(record, true, value), (err) => this.#finish(record, false, err));
  }

  #finish(record, ok, value) {
    this.#running -= 1;
    this.#reservedBytes -= record.reserved;
    const tile = this.#tiles.get(record.id);
    if (tile?.active === record) tile.active = null;
    if (!record.obsolete && !this.#disposed && tile) {
      if (!ok) this.#fail(record, `load failed: ${message(value)}`);
      else this.#accept(tile, record, value);
    }
    this.#pump();
    this.#notify();
  }

  #fail(record, text) {
    // Only a failure of the current descriptor is kept; a stale record never re-enters the history.
    const desc = this.#tiles.get(record.id)?.desc;
    if (!desc || desc.generation !== record.generation || desc[record.rep]?.key !== record.key) return;
    this.#failed.set(this.#failKey(record.id, record.generation, record.rep, record.key),
      { id: record.id, generation: record.generation, rep: record.rep, key: record.key, message: text });
  }

  #accept(tile, record, payload) {
    const bytes = payload?.bytes;
    if (!payload || typeof payload !== 'object' || !isBytes(bytes)) {
      this.#fail(record, 'invalid payload: bytes must be a safe integer >= 0');
      return;
    }
    if (bytes > record.reserved) {
      this.#fail(record, `actual ${bytes} B exceeds the reserved ${record.reserved} B`);
      return;
    }
    let resource;
    try {
      resource = this.#attach(record.entry, record.rep, payload);
    } catch (err) {
      this.#fail(record, `attach failed: ${message(err)}`);   // the old resource stays visible
      return;
    }
    // bytes <= reserved, and the reservation was just released: the sum never exceeds maxBytes.
    this.#attachedBytes += bytes;
    try {
      this.#setVisible(resource, true);
    } catch (err) {
      // Could not show the replacement: destroy it once, keep the old resource as it is, no automatic retry.
      this.#fail(record, `setVisible failed: ${message(err)}`);
      try { this.#detach(resource); } catch (cleanup) { this.#report(record.key, `detach failed: ${message(cleanup)}`); }
      this.#attachedBytes -= bytes;
      return;
    }
    const old = tile.visible;
    tile.visible = { generation: record.generation, rep: record.rep, key: record.key, resource, bytes };
    if (old) this.#release(old);   // same synchronous turn: no frame without coverage
  }

  #notify() {
    try {
      this.#onChange(this.getSnapshot());
    } catch (err) {
      this.#report(null, `onChange failed: ${message(err)}`);
    }
  }
}
