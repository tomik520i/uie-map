"""Bounded block diagnostics of the whole-world (hires) render: which registry name / ID / meta is drawn as the
magenta fallback, uses the missing texture, has no geometry, or is only an assumed cube. Stdlib only.

Not part of the renderer identity (world_hires hashes only offline_mesher/catalog/volume), so changing this module
never forces a re-render. Nothing per voxel is kept: a tile is reduced to a Counter of its distinct (id, meta).

API
  tile_diagnostics(vol, catalog, stats=None, limit=64) -> doc (scope "tile")
      vol: offline_volume.Volume; counts only the tile CORE (vol.core_chunks(): present chunks, halo excluded).
      catalog: offline_catalog.Catalog (models are classified once per distinct key).
      stats: Mesher.mesh_batch(...)["stats"] of the same volume, or None (visible counts unknown).
  empty_tile_diagnostics() -> doc for a processed tile without any present core chunk
  job_diagnostics(tile_docs, expected_tiles=64, limit=200) -> doc (scope "job"); pass one doc per SUCCESSFULLY
      processed tile; failed tiles are simply missing, so coverage.tiles < expectedTiles (complete False)
  world_diagnostics(job_docs, planned_jobs, limit=500) -> doc (scope "world"); job_docs: one entry per job record,
      None for records without diagnostics (older runs): counted as unknown, never as zero
  validate_diagnostics(doc) -> doc; raises DiagnosticsError (use it for docs read back from disk)

Doc (UIE_BLOCK_DIAGNOSTICS1, version 1)
  { format, version, scope: tile|job|world,
    coverage: { tiles, expectedTiles, coreChunks, coreChunksExpected, voxels (non-air core voxels counted),
                visibleKnown (every part had mesher stats), complete },
    jobs: { planned, withDiagnostics, withoutDiagnostics, completeJobs }          (world only)
    classes: { fallback|missingTexture|invisible|assumed: { all, visible|null, lowerBound } }  totals of ALL keys
    entries: [ { id, meta, name|null, class, flags, reason, all, visible|null, sample:[x,y,z], lowerBound } ]
             sorted by all desc, id, meta, name; at most `limit` (<= 500)
    truncated: { entries, all (omitted at this level), inputParts, inputEntries, inputAll (omitted below) } }

Meaning
  all      every core voxel with that (id, meta), any height, also buried ones
  visible  only for the fallback class: blocks the mesher actually emitted (surface mode); null when not known
           (no stats, other classes, ambiguous unknown ids, or any contributing part unknown)
  lowerBound  an input part was truncated and did not list the key: the true count may be higher
  class    highest-priority problem: fallback > missingTexture > invisible > assumed; flags keep all evidence
"""
from __future__ import annotations

from collections import Counter

FORMAT = 'UIE_BLOCK_DIAGNOSTICS1'
VERSION = 1
HEIGHT = 256
MAX_ENTRIES = 500
CLASSES = ('fallback', 'missingTexture', 'invisible', 'assumed')
FLAGS = frozenset(CLASSES) | {'unknownId'}
SCOPES = ('tile', 'job', 'world')
UNKNOWN_PREFIX = 'uie:unknown_'        # world_hires._batch names ids missing from level.dat like this


class DiagnosticsError(ValueError):
    pass


# ------------------------------------------------------------------ classification

def classify(catalog, block_id: int, meta: int, registry_name):
    """(class, flags, reason) of one (id, meta), or None for air and normally rendered blocks."""
    if block_id == 0 or registry_name == 'minecraft:air':
        return None
    name = registry_name if registry_name is not None else f'#{block_id}'   # same lookup name as the mesher
    model = catalog.model(name, meta)
    missing_uv = tuple(catalog.missing_uv)
    missing = sum(1 for f in model.faces if f is not None and tuple(f) == missing_uv)
    flags = set()
    if model.tier == 'fallback':
        flags.add('fallback')
    if model.kind == 'none':
        flags.add('invisible')
    if model.tier == 'assumed':
        flags.add('assumed')
    if missing and model.kind != 'none':
        flags.add('missingTexture')
    if registry_name is None or str(registry_name).startswith(UNKNOWN_PREFIX):
        flags.add('unknownId')            # id missing from level.dat: the catalog draws it as a fallback cube
    if not flags & set(CLASSES):
        return None
    cls = next(c for c in CLASSES if c in flags)
    reason = model.reason or model.tier
    if missing and model.kind != 'none':
        reason += f'; {missing} of {len(model.faces)} model faces use the missing texture'
    return cls, sorted(flags), reason[:300]


# ------------------------------------------------------------------ tile

def _core_columns(vol):
    """(x, z, base) of every present core column, deterministic order (chunk z, chunk x, z, x)."""
    for cx, cz in vol.core_chunks():
        for z in range(cz * 16, cz * 16 + 16):
            for x in range(cx * 16, cx * 16 + 16):
                col = vol.column_index(x, z)
                if col >= 0:
                    yield x, z, col * HEIGHT


def _empty_classes():
    return {c: {'all': 0, 'visible': 0, 'lowerBound': False} for c in CLASSES}


def _check_limit(limit):
    if not isinstance(limit, int) or not 1 <= limit <= MAX_ENTRIES:
        raise DiagnosticsError(f'limit must be 1..{MAX_ENTRIES}')


def _finish(scope, coverage, entries, classes, limit, inputs=(0, 0, 0), extra=None):
    entries.sort(key=lambda e: (-e['all'], e['id'], e['meta'], e['name'] or ''))
    kept, omitted = entries[:limit], entries[limit:]
    doc = {'format': FORMAT, 'version': VERSION, 'scope': scope, 'coverage': coverage, 'classes': classes,
           'entries': kept,
           'truncated': {'entries': len(omitted), 'all': sum(e['all'] for e in omitted),
                         'inputParts': inputs[0], 'inputEntries': inputs[1], 'inputAll': inputs[2]}}
    if extra:
        doc.update(extra)
    return doc


def tile_diagnostics(vol, catalog, stats=None, limit=64) -> dict:
    _check_limit(limit)
    ids, metas, registry = vol.ids, vol.metas, vol.registry
    counts = Counter()
    seen = set()
    problems = {}                       # (id, meta) -> (class, flags, reason)
    samples = {}
    columns = 0
    for x, z, base in _core_columns(vol):
        columns += 1
        pairs = list(zip(ids[base:base + HEIGHT], metas[base:base + HEIGHT]))
        counts.update(pairs)
        new = set(pairs) - seen
        if not new:
            continue
        seen |= new
        for key in new:
            info = classify(catalog, key[0], key[1], registry.get(key[0]))
            if info is not None:
                problems[key] = info
                samples[key] = [x, pairs.index(key), z]        # first voxel in scan order (lowest y)
    core = vol.core
    expected_chunks = (core.width // 16) * (core.depth // 16)
    visible_known = isinstance(stats, dict) and hasattr(stats.get('fallbackBlocks'), 'get')
    fallback_visible = stats['fallbackBlocks'] if visible_known else {}
    # The mesher keys its counter by (registry.get(id), meta): several unknown ids share (None, meta).
    by_stats_key = Counter((registry.get(i), m) for (i, m), (cls, _f, _r) in problems.items() if cls == 'fallback')
    entries, classes = [], _empty_classes()
    for (block_id, meta), (cls, flags, reason) in problems.items():
        visible = None
        if visible_known and cls == 'fallback':
            stats_key = (registry.get(block_id), meta)
            if by_stats_key[stats_key] == 1:
                visible = int(fallback_visible.get(stats_key, 0))
        total = counts[(block_id, meta)]
        entries.append({'id': block_id, 'meta': meta, 'name': registry.get(block_id), 'class': cls, 'flags': flags,
                        'reason': reason, 'all': total, 'visible': visible, 'sample': samples[(block_id, meta)],
                        'lowerBound': False})
        classes[cls]['all'] += total
        if visible is None:
            classes[cls]['visible'] = None
        elif classes[cls]['visible'] is not None:
            classes[cls]['visible'] += visible
    for cls, c in classes.items():
        if c['all'] == 0:
            c['visible'] = 0                 # no such block in the core at all: none can be visible
        elif cls != 'fallback' or not visible_known:
            c['visible'] = None              # the mesher reports emitted counts only for fallback blocks
    voxels = sum(c for (block_id, _m), c in counts.items() if block_id != 0)
    coverage = {'tiles': 1, 'expectedTiles': 1, 'coreChunks': columns // 256, 'coreChunksExpected': expected_chunks,
                'voxels': voxels, 'visibleKnown': visible_known, 'complete': True}
    return _finish('tile', coverage, entries, classes, limit)


def empty_tile_diagnostics(core_chunks_expected=16) -> dict:
    """A processed tile whose core has no present chunk (never generated): covered, nothing to report."""
    classes = _empty_classes()                # nothing present: every count, visible included, is exactly 0
    coverage = {'tiles': 1, 'expectedTiles': 1, 'coreChunks': 0, 'coreChunksExpected': core_chunks_expected,
                'voxels': 0, 'visibleKnown': True, 'complete': True}
    return _finish('tile', coverage, [], classes, 1)


# ------------------------------------------------------------------ validation

def _int(value, lo=0, hi=None):
    return type(value) is int and value >= lo and (hi is None or value <= hi)


def validate_diagnostics(doc) -> dict:
    def need(ok, message):
        if not ok:
            raise DiagnosticsError(message)
    need(isinstance(doc, dict) and doc.get('format') == FORMAT and doc.get('version') == VERSION, 'not a diagnostics doc')
    need(doc.get('scope') in SCOPES, 'invalid scope')
    cov = doc.get('coverage')
    need(isinstance(cov, dict) and all(_int(cov.get(k)) for k in
                                       ('tiles', 'expectedTiles', 'coreChunks', 'coreChunksExpected', 'voxels'))
         and isinstance(cov.get('visibleKnown'), bool) and isinstance(cov.get('complete'), bool), 'invalid coverage')
    classes = doc.get('classes')
    need(isinstance(classes, dict) and set(classes) == set(CLASSES), 'invalid classes')
    for c in classes.values():
        need(isinstance(c, dict) and _int(c.get('all')) and (c.get('visible') is None or _int(c.get('visible')))
             and isinstance(c.get('lowerBound'), bool), 'invalid class totals')
    entries = doc.get('entries')
    need(isinstance(entries, list) and len(entries) <= MAX_ENTRIES, 'invalid entries')
    for e in entries:
        need(isinstance(e, dict) and _int(e.get('id'), 0, 65535) and _int(e.get('meta'), 0, 255)
             and (e.get('name') is None or (isinstance(e['name'], str) and 0 < len(e['name']) <= 200))
             and e.get('class') in CLASSES and isinstance(e.get('flags'), list) and set(e['flags']) <= FLAGS
             and e['class'] in e['flags'] and isinstance(e.get('reason'), str) and len(e['reason']) <= 300
             and _int(e.get('all')) and (e.get('visible') is None or _int(e.get('visible')))
             and isinstance(e.get('sample'), list) and len(e['sample']) == 3 and all(type(v) is int for v in e['sample'])
             and isinstance(e.get('lowerBound'), bool), 'invalid entry')
    t = doc.get('truncated')
    need(isinstance(t, dict) and all(_int(t.get(k)) for k in ('entries', 'all', 'inputParts', 'inputEntries', 'inputAll')),
         'invalid truncation')
    return doc


# ------------------------------------------------------------------ aggregation

def _merge(docs, scope, limit, coverage_override=None, extra=None):
    """Order-independent merge of validated docs (sums, union of flags, smallest sample)."""
    _check_limit(limit)
    docs = [validate_diagnostics(d) for d in docs]
    acc = {}
    truncated_parts = [i for i, d in enumerate(docs) if d['truncated']['entries'] or d['truncated']['inputEntries']]
    for i, d in enumerate(docs):
        for e in d['entries']:
            key = (e['id'], e['meta'], e['name'])
            a = acc.get(key)
            if a is None:
                a = acc[key] = {'id': e['id'], 'meta': e['meta'], 'name': e['name'], 'class': e['class'],
                                'flags': set(), 'reason': e['reason'], 'all': 0, 'visible': 0,
                                'sample': e['sample'], 'lowerBound': False, 'parts': set()}
            a['all'] += e['all']
            a['visible'] = None if a['visible'] is None or e['visible'] is None else a['visible'] + e['visible']
            a['flags'] |= set(e['flags'])
            a['class'] = min(a['class'], e['class'], key=CLASSES.index)
            a['reason'] = min(a['reason'], e['reason'])
            a['sample'] = min(a['sample'], e['sample'])
            a['lowerBound'] |= e['lowerBound']
            a['parts'].add(i)
    entries = []
    for a in acc.values():
        if any(i not in a['parts'] for i in truncated_parts):
            a['lowerBound'] = True        # a truncated part may contain this key without listing it
        a.pop('parts')
        a['flags'] = sorted(a['flags'])
        entries.append(a)
    classes = _empty_classes()
    for c in CLASSES:
        classes[c]['all'] = sum(d['classes'][c]['all'] for d in docs)
        vis = [d['classes'][c]['visible'] for d in docs]
        classes[c]['visible'] = None if any(v is None for v in vis) else sum(vis)
        classes[c]['lowerBound'] = any(d['classes'][c]['lowerBound'] for d in docs)
    coverage = {k: sum(d['coverage'][k] for d in docs)
                for k in ('tiles', 'expectedTiles', 'coreChunks', 'coreChunksExpected', 'voxels')}
    coverage['visibleKnown'] = all(d['coverage']['visibleKnown'] for d in docs)
    coverage['complete'] = all(d['coverage']['complete'] for d in docs)
    if coverage_override:
        coverage.update(coverage_override)
    inputs = (len(truncated_parts),
              sum(d['truncated']['entries'] + d['truncated']['inputEntries'] for d in docs),
              sum(d['truncated']['all'] + d['truncated']['inputAll'] for d in docs))
    return _finish(scope, coverage, entries, classes, limit, inputs, extra)


def job_diagnostics(tile_docs, expected_tiles=64, limit=200) -> dict:
    """One job (region): tile docs of the successfully processed tiles only."""
    if not _int(expected_tiles, 1, 4096) or len(tile_docs) > expected_tiles:
        raise DiagnosticsError('invalid expected tile count')
    if any(d.get('scope') != 'tile' for d in tile_docs):
        raise DiagnosticsError('job diagnostics take tile docs')
    tiles = len(tile_docs)
    merged = _merge(tile_docs, 'job', limit, {'tiles': tiles, 'expectedTiles': expected_tiles})
    merged['coverage']['complete'] = tiles == expected_tiles and all(d['coverage']['complete'] for d in tile_docs)
    return merged


def world_diagnostics(job_docs, planned_jobs, limit=500) -> dict:
    """All job records of the plan: a doc per record with diagnostics, None for records without (older runs)."""
    if not _int(planned_jobs) or len(job_docs) > planned_jobs:
        raise DiagnosticsError('more job docs than planned jobs')
    present = [d for d in job_docs if d is not None]
    if any(d.get('scope') != 'job' for d in present):
        raise DiagnosticsError('world diagnostics take job docs')
    complete_jobs = sum(1 for d in present if d['coverage']['complete'])
    jobs = {'planned': planned_jobs, 'withDiagnostics': len(present),
            'withoutDiagnostics': planned_jobs - len(present), 'completeJobs': complete_jobs}
    merged = _merge(present, 'world', limit, extra={'jobs': jobs})
    merged['coverage']['complete'] = jobs['withoutDiagnostics'] == 0 and complete_jobs == planned_jobs
    if jobs['withoutDiagnostics']:
        for c in merged['classes'].values():
            c['lowerBound'] = True        # jobs without diagnostics may contain any of these blocks
        for e in merged['entries']:
            e['lowerBound'] = True
    return merged
