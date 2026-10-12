// Browser-side coordinator for switching to a newly published immutable generation (UIE_REVISION1).
// Pure: no DOM mutation, no Three.js, no camera. The owner supplies fetch/prepare/commit/discard.
//
// createRevisionWatcher({ fetchRevision(signal), prepare(revision, signal), commit(prepared, revision),
//                         discard(prepared), onState(snapshot), intervalMs = 30000, initialGeneration = null,
//                         identity = null, document = null, setTimer, clearTimer })
//   -> { start(), stop(), dispose(), checkNow(), currentGeneration }
//
// One cycle at a time: fetchRevision -> validateRevision -> identity check -> (new generation only) prepare ->
// commit. The next cycle is scheduled intervalMs after the previous one settled; there is never an overlap,
// even when prepare ignores its AbortSignal. checkNow() during a cycle requests at most one immediate follow-up.
//
// - fetchRevision may resolve null: no published revision (e.g. 404), nothing changes.
// - identity {world, dimension} is given or captured from the first valid revision; a revision for another
//   world or dimension is rejected and the map is not switched. pipelineKey may change between generations.
// - prepare failure keeps the current generation; the next poll retries.
// - commit(prepared, revision) is the synchronous, atomic application hook. It runs only while the watcher is
//   started, not disposed and the page visible. currentGeneration changes only after commit returns.
//   If commit throws, prepared is discarded and the old generation is kept.
// - stop(), dispose() or a hidden page abort the running cycle; a result that arrives later is passed to
//   discard exactly once and never committed. No timer runs while stopped, disposed or hidden; becoming
//   visible again checks immediately (after a still-settling aborted cycle, without overlap).
// Callback exceptions from onState and discard are swallowed.

export const REVISION_FORMAT = 'UIE_REVISION1';
const HEX64 = /^[0-9a-f]{64}$/;

export class RevisionError extends Error {}

const nonEmpty = (v) => typeof v === 'string' && v.trim().length > 0;

export function validateRevision(doc) {
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) throw new RevisionError('revision is not an object');
  if (doc.format !== REVISION_FORMAT) throw new RevisionError(`unsupported format ${JSON.stringify(doc.format)}`);
  if (typeof doc.generation !== 'string' || !HEX64.test(doc.generation)) throw new RevisionError('invalid generation');
  if (doc.manifest !== `generations/${doc.generation}/manifest.json`) throw new RevisionError('invalid manifest path');
  if (typeof doc.manifestSha256 !== 'string' || !HEX64.test(doc.manifestSha256)) throw new RevisionError('invalid manifestSha256');
  if (!nonEmpty(doc.world) || !nonEmpty(doc.dimension)) throw new RevisionError('missing world or dimension');
  if (typeof doc.pipelineKey !== 'string' || !HEX64.test(doc.pipelineKey)) throw new RevisionError('invalid pipelineKey');
  const { format, world, dimension, pipelineKey, generation, manifest, manifestSha256 } = doc;
  return Object.freeze({ format, world, dimension, pipelineKey, generation, manifest, manifestSha256 });
}

export function createRevisionWatcher({ fetchRevision, prepare, commit, discard = () => {}, onState = () => {},
  intervalMs = 30000, initialGeneration = null, identity = null, document: doc = null,
  setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  for (const [name, fn] of [['fetchRevision', fetchRevision], ['prepare', prepare], ['commit', commit],
    ['discard', discard], ['onState', onState]]) {
    if (typeof fn !== 'function') throw new TypeError(`${name} must be a function`);
  }
  if (!Number.isFinite(intervalMs) || intervalMs <= 0) throw new RangeError('intervalMs must be positive');
  if (initialGeneration !== null && (typeof initialGeneration !== 'string' || !HEX64.test(initialGeneration))) {
    throw new RangeError('initialGeneration must be null or 64 lowercase hex');
  }
  if (identity !== null && !(identity && nonEmpty(identity.world) && nonEmpty(identity.dimension))) {
    throw new TypeError('identity must be null or { world, dimension }');
  }

  let currentGeneration = initialGeneration;
  let expected = identity ? { world: identity.world, dimension: identity.dimension } : null;
  let started = false;
  let disposed = false;
  let busy = false;
  let followUp = false;
  let active = null;   // AbortController of the running cycle
  let timer = null;

  const hidden = () => Boolean(doc?.hidden);
  const report = (state, extra = {}) => {
    try { onState({ state, currentGeneration, ...extra }); } catch { /* observers cannot break the watcher */ }
  };
  const safeDiscard = (prepared) => {
    try { discard(prepared); } catch { /* the prepared resources are the owner's; nothing else to do */ }
  };
  const cancelTimer = () => { if (timer !== null) { clearTimer(timer); timer = null; } };
  const abortActive = () => { if (active) { active.abort(); active = null; } };
  const live = (controller) => active === controller && !controller.signal.aborted && started && !disposed && !hidden();

  function schedule() {
    cancelTimer();
    if (!started || disposed || hidden()) return;
    if (followUp) {
      followUp = false;
      runCycle();
      return;
    }
    timer = setTimer(() => { timer = null; runCycle(); }, intervalMs);
  }

  async function runCycle() {
    if (!started || disposed || hidden()) return;
    if (busy) { followUp = true; return; }
    busy = true;
    cancelTimer();
    const controller = new AbortController();
    active = controller;
    try {
      let raw;
      try {
        raw = await fetchRevision(controller.signal);
      } catch (err) {
        if (live(controller)) report('error', { message: String(err?.message ?? err) });
        return;
      }
      if (!live(controller)) return;
      if (raw === null || raw === undefined) { report('unavailable'); return; }
      let revision;
      try {
        revision = validateRevision(raw);
      } catch (err) {
        report('invalid', { message: err.message });
        return;
      }
      if (!expected) expected = { world: revision.world, dimension: revision.dimension };
      if (revision.world !== expected.world || revision.dimension !== expected.dimension) {
        report('identity-mismatch', { revision });
        return;
      }
      if (revision.generation === currentGeneration) { report('current', { revision }); return; }
      report('preparing', { revision });
      let prepared;
      try {
        prepared = await prepare(revision, controller.signal);
      } catch (err) {
        if (live(controller)) report('prepare-failed', { revision, message: String(err?.message ?? err) });
        return;
      }
      if (!live(controller)) { safeDiscard(prepared); return; }
      try {
        commit(prepared, revision);
      } catch (err) {
        safeDiscard(prepared);
        report('commit-failed', { revision, message: String(err?.message ?? err) });
        return;
      }
      currentGeneration = revision.generation;
      report('committed', { revision });
    } finally {
      if (active === controller) active = null;
      busy = false;
      schedule();
    }
  }

  const onVisibility = () => {
    if (!started || disposed) return;
    if (hidden()) {
      cancelTimer();
      followUp = false;
      abortActive();
    } else if (busy) {
      followUp = true;  // the aborted cycle is still settling; check right after it
    } else {
      runCycle();
    }
  };

  function stop() {
    if (!started) return;
    started = false;
    followUp = false;
    cancelTimer();
    abortActive();
    doc?.removeEventListener('visibilitychange', onVisibility);
  }

  return {
    get currentGeneration() { return currentGeneration; },
    start() {
      if (started || disposed) return;
      started = true;
      doc?.addEventListener('visibilitychange', onVisibility);
      if (!hidden()) runCycle();
    },
    stop,
    dispose() {
      if (disposed) return;
      stop();
      disposed = true;
    },
    checkNow() {
      if (!started || disposed || hidden()) return;
      if (busy) followUp = true;
      else runCycle();
    },
  };
}
