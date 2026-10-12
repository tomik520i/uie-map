// UIE_OVERVIEW_UPDATE1 status, independent of the hires surface and spawn-detail renderers.
// Pure validation and Czech presentation; app.js owns fetching, polling and DOM updates.
export const OVERVIEW_STATUS_FORMAT = 'UIE_OVERVIEW_UPDATE1';
export const OVERVIEW_STALE_AFTER_MS = 120_000;
const HEX64 = /^[0-9a-f]{64}$/;
const STAGES = { surface: 'Čte povrch regionů', tiles: 'Skládá hrubé dlaždice',
  publish: 'Zveřejňuje přehled', finished: 'Dokončeno' };
const fmt = new Intl.NumberFormat('cs-CZ');

export class OverviewStatusError extends Error {}

function epoch(value, key) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    throw new OverviewStatusError(`${key} není platný čas`);
  }
  const ms = Math.round(value * 1000);
  if (!Number.isFinite(ms) || ms > 8_640_000_000_000_000) throw new OverviewStatusError(`${key} není platný čas`);
  return ms;
}

function optionalEpoch(value, key) {
  return value == null ? null : epoch(value, key);
}

function count(value, key) {
  if (value == null) return null;
  if (!Number.isSafeInteger(value) || value < 0) throw new OverviewStatusError(`${key} není nezáporné celé číslo`);
  return value;
}

function iso(value) {
  const ms = typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(value)
    ? Date.parse(value) : NaN;
  if (!Number.isFinite(ms)) throw new OverviewStatusError('lastRunAt není čas ISO 8601');
  return ms;
}

export function validateOverviewStatus(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data) || data.format !== OVERVIEW_STATUS_FORMAT) {
    throw new OverviewStatusError('neplatný formát stavu přehledu');
  }
  const legacy = data.state === undefined;
  let state, stage, startedAt, updatedAt, finishedAt;
  if (legacy) {
    state = 'legacy';
    stage = null;
    startedAt = null;
    updatedAt = null;
    finishedAt = null;
    if (data.lastRunAt === undefined) throw new OverviewStatusError('starý stav nemá lastRunAt');
  } else {
    if (!['running', 'done', 'error'].includes(data.state)) throw new OverviewStatusError('neznámý stav přehledu');
    if (!Object.hasOwn(STAGES, data.stage)) throw new OverviewStatusError('neznámá fáze přehledu');
    state = data.state;
    stage = data.stage;
    startedAt = epoch(data.startedAt, 'startedAt');
    updatedAt = epoch(data.updatedAt, 'updatedAt');
    finishedAt = optionalEpoch(data.finishedAt, 'finishedAt');
    if (updatedAt < startedAt || (finishedAt !== null && finishedAt < startedAt)) {
      throw new OverviewStatusError('časy běhu nejsou v pořadí');
    }
    if (state !== 'running' && finishedAt === null) throw new OverviewStatusError('dokončený běh nemá finishedAt');
  }
  const lastRunAt = data.lastRunAt == null ? null : iso(data.lastRunAt);
  const changedRegions = count(data.changedRegions, 'changedRegions');
  const failedRegions = count(data.failedRegions, 'failedRegions');
  const surfaceExitCode = count(data.surfaceExitCode, 'surfaceExitCode');
  const chunkErrors = count(data.chunkErrors, 'chunkErrors');
  const published = data.published == null ? null : data.published;
  if (published !== null && typeof published !== 'boolean') throw new OverviewStatusError('published není true/false');
  const build = data.build == null ? null : data.build;
  if (build !== null && (typeof build !== 'string' || !HEX64.test(build))) throw new OverviewStatusError('neplatný build');
  const reason = data.reason == null ? null : data.reason;
  if (reason !== null && (typeof reason !== 'string' || reason.length > 300)) throw new OverviewStatusError('neplatný důvod');
  const elapsedSeconds = data.elapsedSeconds == null ? null : data.elapsedSeconds;
  if (elapsedSeconds !== null && (typeof elapsedSeconds !== 'number' || !Number.isFinite(elapsedSeconds)
      || elapsedSeconds < 0)) throw new OverviewStatusError('neplatná doba běhu');
  const lastPublishedAt = optionalEpoch(data.lastPublishedAt, 'lastPublishedAt');
  const publication = data.lastPublication == null ? null : data.lastPublication;
  if (publication !== null && (typeof publication !== 'object' || Array.isArray(publication)
      || typeof publication.build !== 'string' || !HEX64.test(publication.build))) {
    throw new OverviewStatusError('neplatná poslední publikace');
  }
  const publicationAt = publication ? optionalEpoch(publication.publishedAt, 'lastPublication.publishedAt') : null;
  const publicationLegacyAt = publication?.lastRunAt == null ? null : iso(publication.lastRunAt);
  const currentBuild = data.currentBuild == null ? null : data.currentBuild;
  if (currentBuild !== null && (typeof currentBuild !== 'string' || !HEX64.test(currentBuild))) {
    throw new OverviewStatusError('neplatný aktuální build');
  }
  const error = data.error == null ? null : data.error;
  const warning = data.warning == null ? null : data.warning;
  for (const [key, value] of [['error', error], ['warning', warning]]) {
    if (value !== null && (typeof value !== 'string' || value.length > 300)) throw new OverviewStatusError(`neplatné ${key}`);
  }
  const heartbeatSeconds = data.heartbeatSeconds == null ? null : data.heartbeatSeconds;
  if (heartbeatSeconds !== null && (typeof heartbeatSeconds !== 'number' || !Number.isFinite(heartbeatSeconds)
      || heartbeatSeconds <= 0 || heartbeatSeconds > 3600)) throw new OverviewStatusError('neplatný heartbeat');
  return { state, stage, legacy, startedAt, updatedAt, finishedAt, lastRunAt, lastPublishedAt,
    heartbeatSeconds, changedRegions, failedRegions, surfaceExitCode, chunkErrors, published, build,
    reason, elapsedSeconds, publicationAt, publicationLegacyAt, currentBuild, error, warning };
}

export function describeOverviewStatus(record, nowMs = Date.now()) {
  const running = record.state === 'running';
  const future = running && record.updatedAt - nowMs > OVERVIEW_STALE_AFTER_MS;
  const stale = running && nowMs - record.updatedAt > OVERVIEW_STALE_AFTER_MS;
  const unreliable = stale || future;
  const failures = (record.failedRegions ?? 0) > 0 || (record.surfaceExitCode ?? 0) !== 0 || (record.chunkErrors ?? 0) > 0;
  let label, tone;
  if (record.legacy) { label = 'Poslední známý běh přehledu'; tone = 'muted'; }
  else if (running) {
    label = stale ? 'Stav přehledu je zastaralý' : future ? 'Stav přehledu nelze ověřit' : 'Přehled se aktualizuje';
    tone = unreliable ? 'warn' : 'active';
  } else if (record.state === 'error') { label = 'Aktualizace přehledu selhala'; tone = 'error'; }
  else if (failures) { label = 'Přehled aktualizován s chybami'; tone = 'warn'; }
  else { label = 'Poslední běh přehledu dokončen'; tone = 'ok'; }
  const notes = [];
  if (record.legacy) notes.push('Starší záznam nepotvrzuje, zda právě běží aktualizace.');
  if (stale) notes.push('Server neobnovil hlášení déle než dvě minuty; průběh nelze potvrdit.');
  if (future) notes.push('Čas hlášení je v budoucnosti; zkontrolujte hodiny serveru.');
  if (record.failedRegions > 0) notes.push(`${fmt.format(record.failedRegions)} regionů se nepodařilo zpracovat.`);
  if (record.chunkErrors > 0) notes.push(`${fmt.format(record.chunkErrors)} chunků se nepodařilo přečíst.`);
  if (record.surfaceExitCode > 0 && !record.failedRegions) notes.push('Čtení povrchu skončilo chybou.');
  if (record.error) notes.push(record.error);
  if (record.warning) notes.push(record.warning);
  if (record.reason && record.reason !== 'no region changed') notes.push(record.reason);
  if (record.reason === 'no region changed' && !failures) notes.push('Žádný region se od minulého běhu nezměnil.');
  const changed = record.changedRegions === null ? null : `${fmt.format(record.changedRegions)} změněných regionů`;
  const failed = record.failedRegions === null ? null : `${fmt.format(record.failedRegions)} chybných regionů`;
  return { label, tone, stale, unreliable, stage: running && !unreliable ? STAGES[record.stage] : null,
    changed, failed, started: record.startedAt, updated: record.updatedAt,
    finished: running ? null : (record.finishedAt ?? record.lastRunAt),
    published: record.publicationAt ?? record.lastPublishedAt,
    publishedThisRun: record.published, note: notes.join(' ') || null };
}
