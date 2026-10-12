import test from 'node:test';
import assert from 'node:assert/strict';
import { validateWorldDiagnostics, describeWorldDiagnostics, filterWorldDiagnosticsEntries } from './web/world-diagnostics.js';

function fixture() {
  const classes = Object.fromEntries(['fallback', 'missingTexture', 'invisible', 'assumed']
    .map(key => [key, { all: 0, visible: 0, lowerBound: false }]));
  classes.fallback = { all: 12, visible: 3, lowerBound: true };
  return {
    format: 'UIE_BLOCK_DIAGNOSTICS1', version: 1, scope: 'world',
    coverage: { tiles: 2, expectedTiles: 64, coreChunks: 32, coreChunksExpected: 1024,
      voxels: 800, visibleKnown: true, complete: false },
    jobs: { planned: 2, withDiagnostics: 1, withoutDiagnostics: 1, completeJobs: 0 },
    classes,
    entries: [{ id: 53, meta: 2, name: 'mod:<unsafe>', class: 'fallback', flags: ['fallback'],
      reason: '<script>not a model</script>', all: 12, visible: 3, sample: [12, 64, -9], lowerBound: true }],
    truncated: { entries: 4, all: 20, inputParts: 1, inputEntries: 2, inputAll: 9 },
  };
}

test('world diagnostics preserves coverage and lower bound meaning', () => {
  const doc = fixture();
  assert.equal(validateWorldDiagnostics(doc), doc);
  const model = describeWorldDiagnostics(doc);
  assert.match(model.coverageText, /1 z 2/);
  assert.match(model.coverageText, /2 z 64/);
  assert.match(model.explanation, /podzemí/);
  assert.match(model.unknownText, /neznámá/);
  assert.match(model.truncatedText, /6 záznamů/);
  assert.match(model.truncatedText, /opakovat/);
  assert.equal(model.classRows[0].all, '≥ 12');
  assert.equal(model.entries[0].all, '≥ 12');
  assert.deepEqual(model.entries[0].sample, [12, 64, -9]);
});

test('missing diagnostics with no entries is unknown, while complete empty coverage is known zero', () => {
  const unknown = fixture();
  unknown.entries = [];
  const model = describeWorldDiagnostics(unknown);
  assert.match(model.emptyText, /neznámý/);
  const complete = fixture();
  complete.entries = [];
  complete.coverage = { ...complete.coverage, tiles: 64, expectedTiles: 64, complete: true };
  complete.jobs = { planned: 1, withDiagnostics: 1, withoutDiagnostics: 0, completeJobs: 1 };
  const known = describeWorldDiagnostics(complete);
  assert.match(known.emptyText, /nebyl zjištěn/);
  assert.equal(known.unknownText, null);
});

test('untrusted fields and oversized or invalid numeric values are rejected', () => {
  const bad = mutate => { const doc = fixture(); mutate(doc); assert.throws(() => validateWorldDiagnostics(doc), TypeError); };
  bad(doc => { doc.entries[0].sample[0] = 30_000_001; });
  bad(doc => { doc.entries[0].sample[1] = -1; });
  bad(doc => { doc.entries[0].id = 65536; });
  bad(doc => { doc.entries[0].meta = 256; });
  bad(doc => { doc.entries[0].all = Number.MAX_SAFE_INTEGER + 1; });
  bad(doc => { doc.entries[0].name = 'x'.repeat(201); });
  bad(doc => { doc.entries[0].reason = 'x'.repeat(301); });
  bad(doc => { doc.entries = Array(501).fill(doc.entries[0]); });
  bad(doc => { doc.jobs.withoutDiagnostics = 0; });
});

test('null visible counts remain explicitly unknown', () => {
  const doc = fixture();
  doc.entries[0].visible = null;
  doc.classes.fallback.visible = null;
  const model = describeWorldDiagnostics(doc);
  assert.equal(model.entries[0].visible, 'neznámé');
  assert.equal(model.classRows[0].visible, 'neznámé');
});

test('class and text filters search only the displayed entries without changing summary totals', () => {
  const doc = fixture();
  doc.entries.push({ id: 700, meta: 0, name: 'mod:HiddenStone', class: 'assumed', flags: ['assumed'],
    reason: 'assumed cube', all: 30, visible: null, sample: [1, 20, 1], lowerBound: false });
  doc.entries.push({ id: 701, meta: 4, name: 'mod:MissingFace', class: 'missingTexture', flags: ['missingTexture'],
    reason: 'missing texture', all: 5, visible: null, sample: [2, 20, 1], lowerBound: false });
  const model = describeWorldDiagnostics(doc);
  assert.deepEqual(filterWorldDiagnosticsEntries(model.entries, { classKey: 'missingTexture' }).map(e => e.id), [701]);
  assert.deepEqual(filterWorldDiagnosticsEntries(model.entries, { query: 'hiddenstone' }).map(e => e.id), [700]);
  assert.deepEqual(filterWorldDiagnosticsEntries(model.entries, { query: '701:4' }).map(e => e.id), [701]);
  assert.deepEqual(filterWorldDiagnosticsEntries(model.entries, { classKey: 'fallback', query: '700' }), []);
  assert.equal(filterWorldDiagnosticsEntries(model.entries).length, 3);
  assert.equal(model.classRows[0].all, '≥ 12', 'filtering does not recompute measured totals');
  assert.throws(() => filterWorldDiagnosticsEntries(model.entries, { classKey: 'unknown' }), TypeError);
});
