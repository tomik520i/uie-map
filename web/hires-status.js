// Whole-world surface (hires) build status, /data/hires/status.json written by world_hires.py build():
// validation, Czech presentation and a polling controller. No DOM access; app.js owns the elements.
//
// Record (UIE_HIRES_STATUS1, times are Unix epoch SECONDS as floats):
//   { format, state: 'running' | 'done' | 'stopped: <reason>', startedAt, total, done, skippedUnchanged,
//     chunks, bytes, etaSeconds? (after the first finished job), finishedAt? (only at the end) }
// total/done count the region jobs of THIS run only (changed or not yet complete regions); skippedUnchanged
// regions were taken over unchanged. Neither is coverage of the whole world.
// Optional (world_hires.py since task 106; older files lack them and they stay UNKNOWN, never zero):
//   updatedAt (heartbeat, epoch s, at least every heartbeatSeconds while running), heartbeatSeconds,
//   failedTiles/failedJobs (tile failures of this run), plannedJobs, notScheduled (changed regions cut by
//   --max-jobs), planComplete (every planned region has a current error-free record), staleTiles.
// With a heartbeat, a running record is trusted while updatedAt is younger than max(3 x heartbeat, 120 s); the
// heartbeat proves the build process is alive, not that its workers progress. Without one (older writers) the
// file is rewritten only at the start, after a finished job at most every 30 s and at the end, so a running
// record is trusted only while it plausibly can still be current (heuristics below).
//
// API
//   validateHiresStatus(data) -> record (times in ms); throws HiresStatusError
//   describeHiresStatus(record, nowMs, unchangedForMs = 0) -> { label, tone, stale, progress, percent, etaSeconds,
//                                                               started, finished, finishedLabel, note, scope }
//   createHiresStatusPoller({ fetchStatus, onUpdate, intervalMs, document, now, setTimer, clearTimer })
//       -> { start(), pollNow(), dispose() }; onUpdate receives { kind: 'ok', record, unchangedForMs } or
//          { kind: 'unavailable', reason: 'missing' | 'invalid' | 'error', message }
//   fetchStatus(signal) -> { kind: 'missing' } | { kind: 'data', data } (app.js passes fetchRenderStatus)

export const HIRES_STATUS_FORMAT = 'UIE_HIRES_STATUS1';
export const POLL_INTERVAL_MS = 30_000;
export const UNCHANGED_STALE_MS = 30 * 60_000;      // running record not rewritten while watched for 30 min
export const NO_PROGRESS_STALE_MS = 2 * 3_600_000;  // running, no job finished 2 h after the start
export const ETA_GRACE_MS = 30 * 60_000;            // past the estimated end by max(30 min, half the run)
export const FUTURE_TOLERANCE_MS = 120_000;
export const DEFAULT_HEARTBEAT_S = 30;
export const MIN_HEARTBEAT_STALE_MS = 120_000;     // stale after max(3 x heartbeat, 120 s) without a new heartbeat
export const MAX_HEARTBEAT_S = 3600;

export function heartbeatStaleAfterMs(heartbeatSeconds) {
  return Math.max(3 * (heartbeatSeconds ?? DEFAULT_HEARTBEAT_S) * 1000, MIN_HEARTBEAT_STALE_MS);
}

const fmtCount = new Intl.NumberFormat('cs-CZ');
const STOP_REASONS = [[/free disk/i, 'na serveru dochází místo na disku']];

export class HiresStatusError extends Error {}

const count = (value, label) => {
  if (!Number.isSafeInteger(value) || value < 0) throw new HiresStatusError(`${label} není nezáporné celé číslo`);
  return value;
};
const epoch = (value, label) => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) throw new HiresStatusError(`${label} není čas`);
  const ms = Math.round(value * 1000);
  if (!Number.isFinite(ms) || Math.abs(ms) > 8640000000000000) throw new HiresStatusError(`${label} není platný čas`);
  return ms;
};

export function validateHiresStatus(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new HiresStatusError('stav povrchu světa není objekt');
  if (data.format !== HIRES_STATUS_FORMAT) throw new HiresStatusError(`nepodporovaný format ${JSON.stringify(data.format)}`);
  const raw = data.state;
  let state;
  let reason = null;
  if (raw === 'running' || raw === 'done') state = raw;
  else if (typeof raw === 'string' && raw.length <= 300 && /^stopped(:|$)/.test(raw)) {
    state = 'stopped';
    reason = raw.slice('stopped'.length).replace(/^:\s*/, '').trim() || null;
  } else throw new HiresStatusError(`neznámý stav ${JSON.stringify(raw)}`);
  const total = count(data.total, 'total');
  const done = count(data.done, 'done');
  if (done > total) throw new HiresStatusError('done je větší než total');
  const skippedUnchanged = count(data.skippedUnchanged, 'skippedUnchanged');
  const staleTiles = data.staleTiles === undefined ? null : count(data.staleTiles, 'staleTiles');
  const startedAt = epoch(data.startedAt, 'startedAt');
  const finishedAt = data.finishedAt === undefined ? null : epoch(data.finishedAt, 'finishedAt');
  if (finishedAt !== null && finishedAt < startedAt) throw new HiresStatusError('finishedAt předchází startedAt');
  if (state === 'done' && done !== total) throw new HiresStatusError('dokončený běh má neúplný počet regionů');
  const eta = data.etaSeconds;
  if (eta !== undefined && !(typeof eta === 'number' && Number.isFinite(eta) && eta >= 0)) {
    throw new HiresStatusError('etaSeconds není nezáporné číslo');
  }
  // Optional fields: absent = unknown (null), present = strictly validated.
  const optional = (name) => (data[name] === undefined ? null : count(data[name], name));
  const updatedAt = data.updatedAt === undefined ? null : epoch(data.updatedAt, 'updatedAt');
  if (updatedAt !== null && updatedAt < startedAt) throw new HiresStatusError('updatedAt předchází startedAt');
  const hb = data.heartbeatSeconds;
  if (hb !== undefined && !(typeof hb === 'number' && Number.isFinite(hb) && hb > 0 && hb <= MAX_HEARTBEAT_S)) {
    throw new HiresStatusError('heartbeatSeconds není kladné číslo do 3600');
  }
  const failedTiles = optional('failedTiles');
  const failedJobs = optional('failedJobs');
  if (failedJobs !== null && ((failedTiles !== null && failedJobs > failedTiles) || failedJobs > done)) {
    throw new HiresStatusError('failedJobs neodpovídá failedTiles nebo done');
  }
  const plannedJobs = optional('plannedJobs');
  const notScheduled = optional('notScheduled');
  const plan = data.planComplete;
  if (plan !== undefined && typeof plan !== 'boolean') throw new HiresStatusError('planComplete není true/false');
  return { state, reason, startedAt, finishedAt, total, done, skippedUnchanged, staleTiles, etaSeconds: eta ?? null,
    updatedAt, heartbeatSeconds: hb ?? null, failedTiles, failedJobs, plannedJobs, notScheduled,
    planComplete: plan ?? null };
}

function ago(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 120) return `${s} s`;
  const min = Math.round(s / 60);
  return min < 120 ? `${min} min` : `${Math.round(min / 60)} h`;
}

// A running record cannot confirm itself (no heartbeat). It is stale when it was not rewritten while watched,
// when no job finished long after the start, or when its own estimated end is long past.
function runningStale(r, nowMs, unchangedForMs) {
  if (unchangedForMs > UNCHANGED_STALE_MS) return true;
  if (r.done === 0) return nowMs - r.startedAt > NO_PROGRESS_STALE_MS;
  if (r.etaSeconds === null || r.done >= r.total) return false;
  // writer: eta = elapsed / done * (total - done)  =>  elapsed at the write = eta * done / (total - done)
  const elapsedMs = (r.etaSeconds * r.done / (r.total - r.done)) * 1000;
  const runMs = elapsedMs + r.etaSeconds * 1000;
  return nowMs > r.startedAt + runMs + Math.max(ETA_GRACE_MS, runMs / 2);
}

export function describeHiresStatus(record, nowMs, unchangedForMs = 0) {
  const r = record;
  const running = r.state === 'running';
  const hasHeartbeat = r.updatedAt !== null;
  const heartbeatAgeMs = hasHeartbeat ? nowMs - r.updatedAt : null;
  const future = r.startedAt - nowMs > FUTURE_TOLERANCE_MS || (hasHeartbeat && -heartbeatAgeMs > FUTURE_TOLERANCE_MS);
  // With a heartbeat only its age decides (no progress heuristics); without one the older heuristics apply.
  const stale = running && !future && (hasHeartbeat
    ? heartbeatAgeMs > heartbeatStaleAfterMs(r.heartbeatSeconds)
    : runningStale(r, nowMs, unchangedForMs));
  const unreliable = running && (stale || future);
  let label;
  let tone;
  const notes = [];
  const failed = r.failedTiles > 0
    ? `${fmtCount.format(r.failedTiles)} dlaždic se v tomto běhu nepodařilo vykreslit`
      + (r.failedJobs > 0 ? ` (${fmtCount.format(r.failedJobs)} regionů)` : '') : null;
  if (running) {
    label = stale ? 'Stav aktualizace je zastaralý' : future ? 'Stav aktualizace nelze ověřit' : 'Probíhá aktualizace';
    tone = unreliable ? 'warn' : 'active';
    if (stale && hasHeartbeat) {
      notes.push(`Server hlásil běh naposledy před ${ago(heartbeatAgeMs)}; mohl skončit bez zápisu. Průběh ani odhad nelze potvrdit.`);
    } else if (stale) {
      notes.push('Poslední zápis hlásil běžící aktualizaci, ale nic nenasvědčuje tomu, že stále běží '
        + '(mohla skončit bez zápisu). Průběh ani odhad nelze potvrdit.');
    } else if (!future && hasHeartbeat) {
      notes.push(`Server se ozval před ${ago(heartbeatAgeMs)}.`);
      if (unchangedForMs > UNCHANGED_STALE_MS) {
        notes.push(`Počet hotových regionů se ${ago(unchangedForMs)} nezměnil; signál potvrzuje běžící proces, ne postup.`);
      }
    }
    if (failed) notes.push(`Zatím ${failed}.`);
  } else if (r.state === 'done') {
    label = 'Poslední běh dokončen';
    tone = 'ok';
    if (r.total === 0) notes.push('Žádný region se od předchozí aktualizace nezměnil.');
    if (failed) {
      label = 'Běh dokončen s chybami';
      tone = 'warn';
      notes.push(`${failed}; zkusí se znovu při další aktualizaci.`);
    } else if (r.failedTiles === null && r.total > 0) {
      notes.push('Stav neobsahuje počet chyb jednotlivých dlaždic; dokončení běhu nepotvrzuje bezchybný render.');
    } else if (r.failedTiles === 0 && r.total > 0) {
      notes.push('Všechny dlaždice tohoto běhu se vykreslily bez chyby.');
    }
    const partial = r.planComplete === false || r.notScheduled > 0;
    if (partial) {
      if (!failed) { label = 'Běh dokončen, část světa čeká'; tone = 'warn'; }
      notes.push(r.notScheduled > 0
        ? `${fmtCount.format(r.notScheduled)} změněných regionů čeká na další běh.`
        : 'Ne všechny regiony mají aktuální bezchybná data.');
    } else if (r.planComplete === true) {
      notes.push('Všechny regiony z plánu mají aktuální data; pozdější změny ve světě přinese až další běh.');
    }
  } else {
    label = 'Poslední aktualizace byla zastavena';
    tone = 'warn';
    const known = STOP_REASONS.find(([pattern]) => r.reason && pattern.test(r.reason));
    notes.push(`Důvod: ${known ? known[1] : r.reason ?? 'neuveden'}.`);
    if (failed) notes.push(`Do zastavení ${failed}.`);
  }
  if (future) notes.push('Čas na serveru je v budoucnosti – zkontrolujte hodiny serveru.');
  if (r.staleTiles > 0) {
    notes.push(`${fmtCount.format(r.staleTiles)} dlaždic zatím používá poslední dobrou starší verzi; nová data ještě nejsou připravená.`);
    if (r.state === 'done' && label === 'Poslední běh dokončen') { label = 'Běh dokončen, část dlaždic zůstává starší'; tone = 'warn'; }
  }

  let progress = null;
  let percent = null;
  if (r.total > 0) {
    percent = unreliable ? null : Math.floor((r.done / r.total) * 100);
    progress = `${fmtCount.format(r.done)} / ${fmtCount.format(r.total)} regionů` + (percent === null ? '' : ` (${percent} %)`);
  }
  const scope = 'Počty platí jen pro regiony zpracovávané tímto během (změněné nebo dosud neúplné)'
    + (r.skippedUnchanged ? `; ${fmtCount.format(r.skippedUnchanged)} nezměněných regionů bylo převzato beze změny` : '')
    + '. Nejde o pokrytí celého světa.';
  return {
    label, tone, stale, unreliable, progress, percent,
    etaSeconds: running && !unreliable && r.done < r.total ? r.etaSeconds : null,
    started: r.startedAt,
    finished: running ? null : r.finishedAt,
    finishedLabel: r.state === 'stopped' ? 'Zastaveno' : 'Dokončeno',
    note: notes.join(' ') || null,
    scope,
    heartbeatAgeMs,                 // extra (app.js does not need it): null without a heartbeat
  };
}

/**
 * Same contract as the render status poller: no overlapping requests, the next one only after the previous
 * settled, paused while document.hidden (immediate refresh when visible again), callback errors swallowed,
 * dispose() aborts the request. Additionally reports how long the validated record has not changed while watched.
 */
export function createHiresStatusPoller({ fetchStatus, onUpdate, intervalMs = POLL_INTERVAL_MS, document: doc = null,
  now = () => Date.now(), setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  if (typeof fetchStatus !== 'function' || typeof onUpdate !== 'function') {
    throw new TypeError('fetchStatus and onUpdate must be functions');
  }
  let timer = null;
  let inflight = null;
  let started = false;
  let disposed = false;
  let lastKey = null;
  let lastChangeAt = 0;

  const cancelTimer = () => { if (timer !== null) { clearTimer(timer); timer = null; } };
  const schedule = () => {
    cancelTimer();
    if (!disposed && !doc?.hidden) timer = setTimer(pollNow, intervalMs);
  };
  const deliver = (view) => {
    try { onUpdate(view); } catch { /* a broken view must not stop polling */ }
  };

  async function pollNow() {
    if (disposed || inflight) return;
    cancelTimer();
    const controller = new AbortController();
    inflight = controller;
    let view;
    try {
      const result = await fetchStatus(controller.signal);
      if (result?.kind === 'missing') {
        view = { kind: 'unavailable', reason: 'missing', message: 'Server stav povrchu světa neposkytuje.' };
      } else {
        try {
          const record = validateHiresStatus(result?.data);
          // A heartbeat alone is not a change: unchangedForMs measures the reported progress.
          const { updatedAt: _heartbeat, ...progress } = record;
          const key = JSON.stringify(progress);
          if (key !== lastKey) { lastKey = key; lastChangeAt = now(); }
          view = { kind: 'ok', record, unchangedForMs: now() - lastChangeAt };
        } catch (err) {
          view = { kind: 'unavailable', reason: 'invalid', message: err.message };
        }
      }
    } catch (err) {
      view = { kind: 'unavailable', reason: 'error', message: err?.message ?? String(err) };
    } finally {
      if (inflight === controller) inflight = null;
    }
    if (disposed) return;
    deliver(view);
    schedule();
  }

  const onVisibility = () => {
    if (disposed) return;
    if (doc.hidden) cancelTimer();
    else if (!inflight) pollNow();
  };

  return {
    start() {
      if (started || disposed) return;
      started = true;
      doc?.addEventListener('visibilitychange', onVisibility);
      if (!doc?.hidden) pollNow();
    },
    pollNow,
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelTimer();
      inflight?.abort();
      inflight = null;
      doc?.removeEventListener('visibilitychange', onVisibility);
    },
  };
}
