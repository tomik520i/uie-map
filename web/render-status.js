// Server render pipeline status (UIE_RENDER_STATUS1): validation, Czech presentation and a polling
// controller. No DOM access; app.js owns the elements. Nothing here estimates progress on its own.
//
// Record: { format: 'UIE_RENDER_STATUS1',
//           state: 'not_configured' | 'idle' | 'queued' | 'rendering' | 'publishing' | 'complete' | 'error',
//           updatedAt: ISO 8601 (heartbeat of the status writer, NOT the last map update),
//           completedBatches: null | integer >= 0, totalBatches: null | integer >= 1 (completed <= total),
//           etaSeconds: null | finite >= 0, lastPublishedAt: null | ISO 8601 (last SUCCESSFUL publication),
//           message: string of at most 300 characters }
// Unknown extra fields are ignored.
//
// API
//   validateRenderStatus(data) -> record with parsed times (ms); throws RenderStatusError
//   describeRenderStatus(record, nowMs) -> { label, tone, stale, unreliable, progress, eta, published,
//                                            heartbeat, message, note }
//   formatDuration(seconds) -> Czech text
//   fetchRenderStatus(url, { timeoutMs, signal, fetchImpl }) -> { kind: 'missing' } | { kind: 'data', data }
//   createRenderStatusPoller({ fetchStatus, onUpdate, intervalMs, document, setTimer, clearTimer })
//       -> { start(), pollNow(), dispose() }; onUpdate receives { kind: 'ok', record } or
//          { kind: 'unavailable', reason: 'missing' | 'invalid' | 'error', message }

export const RENDER_STATUS_FORMAT = 'UIE_RENDER_STATUS1';
export const STALE_AFTER_MS = 120_000;
export const FUTURE_TOLERANCE_MS = 120_000;
export const POLL_INTERVAL_MS = 30_000;

const STATES = {
  not_configured: 'Automatický render zatím není zapojený',
  idle: 'Čeká',
  queued: 'Ve frontě',
  rendering: 'Renderuje',
  publishing: 'Zveřejňuje mapu',
  complete: 'Dokončeno',
  error: 'Chyba renderu',
};
const ACTIVE = new Set(['queued', 'rendering', 'publishing']);
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;
const fmtCount = new Intl.NumberFormat('cs-CZ');

export class RenderStatusError extends Error {}

function parseTime(value, label) {
  const ms = typeof value === 'string' && ISO.test(value) ? Date.parse(value) : NaN;
  if (!Number.isFinite(ms)) throw new RenderStatusError(`${label} není čas ISO 8601`);
  return ms;
}

export function validateRenderStatus(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new RenderStatusError('stav renderu není objekt');
  if (data.format !== RENDER_STATUS_FORMAT) throw new RenderStatusError(`nepodporovaný format ${JSON.stringify(data.format)}`);
  if (!Object.hasOwn(STATES, data.state ?? '')) throw new RenderStatusError(`neznámý stav ${JSON.stringify(data.state)}`);
  const updatedAt = parseTime(data.updatedAt, 'updatedAt');
  const { completedBatches: done, totalBatches: total, etaSeconds: eta } = data;
  if (done !== null && !(Number.isSafeInteger(done) && done >= 0)) throw new RenderStatusError('completedBatches není nezáporné celé číslo');
  if (total !== null && !(Number.isSafeInteger(total) && total >= 1)) throw new RenderStatusError('totalBatches není kladné celé číslo');
  if (done !== null && total !== null && done > total) throw new RenderStatusError('completedBatches je větší než totalBatches');
  if (eta !== null && !(typeof eta === 'number' && Number.isFinite(eta) && eta >= 0)) throw new RenderStatusError('etaSeconds není nezáporné číslo');
  const lastPublishedAt = data.lastPublishedAt === null ? null : parseTime(data.lastPublishedAt, 'lastPublishedAt');
  if (typeof data.message !== 'string' || data.message.length > 300) throw new RenderStatusError('message není text do 300 znaků');
  return { state: data.state, updatedAt, completedBatches: done, totalBatches: total, etaSeconds: eta, lastPublishedAt,
    message: data.message };
}

export function formatDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return 'méně než minuta';
  const minutes = Math.round(s / 60);
  if (minutes < 60) return `${minutes} min`;
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const rest = minutes % 60;
  if (days) return hours ? `${days} d ${hours} h` : `${days} d`;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

/** Presentation of a validated record at time nowMs. Stale or future-dated active states get no percent/ETA. */
export function describeRenderStatus(record, nowMs) {
  const active = ACTIVE.has(record.state);
  const age = nowMs - record.updatedAt;
  const stale = active && age > STALE_AFTER_MS;
  const future = -age > FUTURE_TOLERANCE_MS;
  const unreliable = stale || (active && future);
  let label = STATES[record.state];
  let note = null;
  if (stale) {
    label = 'Stav renderu je zastaralý';
    note = `Poslední hlášený stav: ${STATES[record.state]}. Průběh ani odhad nelze potvrdit.`;
  } else if (future) {
    if (active) label = 'Stav renderu nelze ověřit';
    note = 'Čas hlášení stavu je v budoucnosti – zkontrolujte hodiny serveru.';
  }
  const tone = unreliable ? 'warn'
    : { complete: 'ok', error: 'error', idle: 'muted', not_configured: 'muted' }[record.state] ?? 'active';

  const { completedBatches: done, totalBatches: total } = record;
  let progress = null;
  if (done !== null && total !== null) {
    const percent = unreliable ? null : Math.floor((done / total) * 100);
    progress = { text: `${fmtCount.format(done)} / ${fmtCount.format(total)} dávek`
      + (percent === null ? '' : ` (${percent} %)`), percent };
  } else if (done !== null) {
    progress = { text: `${fmtCount.format(done)} dávek hotovo`, percent: null };
  }
  let eta = null;
  if (active && !unreliable) {
    eta = record.etaSeconds === null ? 'Odhad zatím není k dispozici' : `přibližně ${formatDuration(record.etaSeconds)}`;
  }
  return { label, tone, stale, unreliable, progress, eta, published: record.lastPublishedAt,
    heartbeat: record.updatedAt, message: record.message.trim() || null, note };
}

/** One request with a timeout covering headers and body. 404 means "not published" (older exports). */
export async function fetchRenderStatus(url, { timeoutMs = 10_000, signal, fetchImpl = globalThis.fetch } = {}) {
  const controller = new AbortController();
  const forward = () => controller.abort();
  signal?.addEventListener('abort', forward, { once: true });
  if (signal?.aborted) controller.abort();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, { cache: 'no-store', signal: controller.signal });
    if (response.status === 404) {
      response.body?.cancel?.().catch(() => {});
      return { kind: 'missing' };
    }
    if (!response.ok) {
      response.body?.cancel?.().catch(() => {});
      throw new RenderStatusError(`HTTP ${response.status}`);
    }
    const text = await response.text();
    try {
      return { kind: 'data', data: JSON.parse(text) };
    } catch {
      throw new RenderStatusError('odpověď není platný JSON');
    }
  } catch (err) {
    if (signal?.aborted) throw signal.reason ?? err;
    if (err instanceof RenderStatusError) throw err;
    if (controller.signal.aborted) throw new RenderStatusError(`vypršel časový limit ${timeoutMs / 1000} s`);
    throw new RenderStatusError(`síťová chyba (${err?.message ?? err})`);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', forward);
  }
}

/**
 * Polls without overlap: the next request is scheduled only after the previous one settled. Polling pauses
 * while `document.hidden` and refreshes immediately when the page becomes visible again. Callback errors
 * are swallowed so they cannot stop polling. dispose() aborts the request and removes all listeners.
 */
export function createRenderStatusPoller({ fetchStatus, onUpdate, intervalMs = POLL_INTERVAL_MS, document: doc = null,
  setTimer = setTimeout, clearTimer = clearTimeout, validateStatus = validateRenderStatus } = {}) {
  if (typeof fetchStatus !== 'function' || typeof onUpdate !== 'function') {
    throw new TypeError('fetchStatus and onUpdate must be functions');
  }
  let timer = null;
  let inflight = null;
  let started = false;
  let disposed = false;

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
      if (result?.kind === 'missing') view = { kind: 'unavailable', reason: 'missing', message: 'Server stav renderu nezveřejňuje.' };
      else {
        try {
          view = { kind: 'ok', record: validateStatus(result?.data) };
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
