import test from 'node:test';
import assert from 'node:assert/strict';
import { validateOverviewStatus, describeOverviewStatus, OverviewStatusError,
  OVERVIEW_STALE_AFTER_MS } from './web/overview-status.js';

const build = 'a'.repeat(64);
const running = () => ({ format: 'UIE_OVERVIEW_UPDATE1', state: 'running', stage: 'surface',
  startedAt: 1000, updatedAt: 1030, finishedAt: null, heartbeatSeconds: 30,
  changedRegions: null, failedRegions: null, surfaceExitCode: null, published: false,
  build, lastPublishedAt: 900 });

test('a recent heartbeat reports the actual running stage, without invented progress', () => {
  const record = validateOverviewStatus(running());
  const view = describeOverviewStatus(record, 1_050_000);
  assert.equal(view.label, 'Přehled se aktualizuje');
  assert.match(view.stage, /povrch/);
  assert.equal(view.changed, null);
  assert.equal(view.published, 900_000);
  assert.equal(view.tone, 'active');
  const withRunTimestamp = validateOverviewStatus({...running(), lastRunAt: '2026-10-03T10:00:00Z'});
  assert.equal(describeOverviewStatus(withRunTimestamp, 1_050_000).finished, null);
});

test('running state becomes uncertain when the heartbeat is old or future dated', () => {
  const record = validateOverviewStatus(running());
  const stale = describeOverviewStatus(record, record.updatedAt + OVERVIEW_STALE_AFTER_MS + 1);
  assert.equal(stale.stale, true);
  assert.equal(stale.stage, null);
  assert.match(stale.note, /průběh nelze potvrdit/);
  const future = describeOverviewStatus(record, record.updatedAt - OVERVIEW_STALE_AFTER_MS - 1);
  assert.equal(future.unreliable, true);
  assert.match(future.label, /nelze ověřit/);
});

test('completed surface failures remain partial even when no region changed', () => {
  const data = { ...running(), state: 'done', stage: 'finished', updatedAt: 1060, finishedAt: 1060,
    changedRegions: 0, failedRegions: 2, surfaceExitCode: 1, reason: 'no region changed' };
  const view = describeOverviewStatus(validateOverviewStatus(data), 2_000_000);
  assert.equal(view.tone, 'warn');
  assert.match(view.label, /s chybami/);
  assert.match(view.note, /2 regionů/);
  assert.doesNotMatch(view.note, /Žádný region se/);
  assert.equal(view.finished, 1_060_000);
});

test('failed publication is distinct from a successful published build', () => {
  const data = { ...running(), state: 'error', stage: 'finished', updatedAt: 1060, finishedAt: 1060,
    published: false, reason: 'publication failed' };
  const view = describeOverviewStatus(validateOverviewStatus(data), 2_000_000);
  assert.equal(view.tone, 'error');
  assert.equal(view.publishedThisRun, false);
  assert.equal(view.published, 900_000);
  assert.match(view.note, /publication failed/);
});

test('individual unreadable chunks prevent a clean success label', () => {
  const data = { ...running(), state: 'done', stage: 'finished', updatedAt: 1060, finishedAt: 1060,
    failedRegions: 0, surfaceExitCode: 0, chunkErrors: 3 };
  const view = describeOverviewStatus(validateOverviewStatus(data), 2_000_000);
  assert.equal(view.tone, 'warn'); assert.match(view.note, /3 chunků/);
});

test('backend nested publication survives a later failed run', () => {
  const data = { ...running(), state: 'error', stage: 'publish', updatedAt: 1060,
    finishedAt: 1060, lastPublishedAt: undefined, currentBuild: build,
    lastPublication: { publishedAt: 900, build }, error: 'OverviewPublishError: stale base' };
  const view = describeOverviewStatus(validateOverviewStatus(data), 2_000_000);
  assert.equal(view.published, 900_000);
  assert.match(view.note, /stale base/);
  assert.equal(view.tone, 'error');
});

test('legacy end-only status never claims a live run', () => {
  const data = { format: 'UIE_OVERVIEW_UPDATE1', lastRunAt: '2026-10-03T10:00:00Z',
    changedRegions: 0, failedRegions: 0, surfaceExitCode: 0,
    published: false, build, reason: 'no region changed', elapsedSeconds: 4.1 };
  const view = describeOverviewStatus(validateOverviewStatus(data), Date.now());
  assert.match(view.label, /Poslední známý běh/);
  assert.equal(view.stage, null);
  assert.match(view.note, /nepotvrzuje/);
  assert.equal(view.finished, Date.parse(data.lastRunAt));
});

test('malformed status cannot become a reassuring display', () => {
  for (const mutate of [
    data => { data.state = 'done'; },
    data => { data.stage = 'other'; },
    data => { data.updatedAt = data.startedAt - 1; },
    data => { data.failedRegions = -1; },
    data => { data.build = 'not-a-hash'; },
    data => { data.heartbeatSeconds = 0; },
  ]) {
    const data = running(); mutate(data);
    assert.throws(() => validateOverviewStatus(data), OverviewStatusError);
  }
});
