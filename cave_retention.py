"""Private READ-ONLY retention planner for the cave companion store out/cave-data.

Caller owns the store and holds the exclusive out/.build.lock (the same lock the producer, the publisher and any
future executor take). Nothing is written or deleted; there is no executor, scheduler or service. The plan is a
dry-run hint whose candidate identities a separate, guarded executor would have to re-check.

API:
    plan_retention(out, *, now=None) -> dict
        Raises hb.BundleError (a ValueError) or OSError (e.g. FileNotFoundError) whenever protection cannot be
        proven completely. Never returns candidates before the complete protection closure is validated.

Protection (computed completely, or the call fails):
- Root geometry roots: root current.json, publication-history current/previous/records >= now-GRACE, and root index
  files younger than GRACE. Each is validated with hires_retention._index and must be cave geometry
  (context.caves true). Root objects are never scanned, read or proposed.
- Cap index roots: actual cave-data/current.json, cap-history current/previous/records >= now-GRACE, every cap
  index file younger than GRACE, and EVERY cap index (all are scanned and validated) whose geometryIndexSha256 is a
  root geometry root. A cap pointer for another geometry than root current (publication waiting) is normal.
- Pairs referenced by protected cap indexes, by ALL jobs/*.json capPairs (dot files included, exactly like the
  publisher's Path.glob('*.json')) and EVERY pair manifest younger than GRACE (a crash before the job write leaves an
  unreferenced manifest whose reused content-addressed cap/alias may be old). Every protected pair manifest must
  exist and be canonical with the referenced hash; protected cap index references must agree with the pair (origin,
  atlas, renderer, capIdentity, geometry tile when the root index is retained). A missing or malformed referenced
  pair, cap payload or index is a hard failure, never a deletion permit.
- Caps of protected pairs (regular, 1 B..1 MiB; never decompressed).
- cave-data/objects aliases are removable hardlink names; root objects/ remain root GC's business. An alias of a
  protected pair's geometry with link count <= 1 is protected (it may be the only copy known here).
- Anything with max(mtime, ctime) >= now - GRACE (equality protected).

Provenance: cap pointer without cap history, cap indexes without history, a pointer that is neither history current
nor previous (previous None allows the pre-first-pointer crash), missing root journal or any malformed input fails
closed. No history and no pointer and no cap index: state 'no-cap-history', zero candidates. now <= bootstrapAt +
GRACE: state 'bootstrap-grace', zero candidates. Pointer, history, root pointer and root journal bytes and identity
are re-checked before returning.

Budgets: <= MAX_SCANNED directory entries (root indexes, jobs, cave-data indexes/pairs/caps/objects, unknown names
included), <= MAX_METADATA_BYTES reserved from lstat size BEFORE each read (plus 128 B + name per entry), <=
MAX_METADATA_FILES metadata reads. Exceeding any budget raises: the scan is complete or there is no plan.
Candidates are listed up to MAX_CANDIDATES (candidatesTruncated) only after the complete scan; totals are complete.
logicalBytes sums st_size of all candidates; reclaimableBytes is None (hardlinks make physical savings unknown).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time

import cave_pair as cp
import cave_pair_history as history
import cave_pair_index as ci
import hires_bundle as hb
import hires_history as hh
import hires_retention as hr


GRACE_SECONDS = history.GRACE_SECONDS
MAX_SCANNED = 400_000
# 983 regions / 62,912 tiles with eight retained generations need ~145 MiB
# including directory accounting. The isolated scale probe peaks below 550 MiB
# RSS. Keep a finite read budget within the supervisor's 2 GiB memory limit.
MAX_METADATA_BYTES = 256 * 1024 * 1024
MAX_METADATA_FILES = 150_000
MAX_CANDIDATES = 1_000
MAX_CAP_INDEX_BYTES = 16 * 1024 * 1024
MAX_PAIR_BYTES = 4096
MAX_JOB_BYTES = 2 * 1024 * 1024
MAX_JOB_PAIRS = 64
MAX_CAP_BYTES = 1024 * 1024
POINTER_BYTES = 4096
POINTER_FORMAT = 'UIE_CAVE_CAP_POINTER1'
CAP_INDEX_FIELDS = {'format', 'geometryIndexSha256', 'rendererIdentity', 'atlasSha256', 'capIdentity', 'pairs'}
KINDS = {'indexes': '.json', 'pairs': '.json', 'caps': '.uic.gz', 'objects': '.uih'}


class _Budget:
    """Per-call counters; no module state."""

    def __init__(self):
        self.scanned = self.bytes = self.files = 0

    def charge(self, amount):
        self.bytes += amount
        if self.bytes > MAX_METADATA_BYTES:
            raise hb.BundleError('cave retention metadata byte capacity exceeded')

    def entry(self, name):
        self.scanned += 1
        if self.scanned > MAX_SCANNED:
            raise hb.BundleError('cave retention scan entry capacity exceeded')
        self.charge(128 + len(name.encode('utf-8', 'surrogatepass')))

    def file(self, size):
        self.files += 1
        if self.files > MAX_METADATA_FILES:
            raise hb.BundleError('cave retention metadata file capacity exceeded')
        self.charge(size)


def _sha(value):
    return isinstance(value, str) and hb.HEX64.fullmatch(value) is not None


def _canonical(doc):
    return json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()


def _json(raw, what):
    try:
        return hb._strict_json(raw, what)
    except RecursionError:
        raise hb.BundleError(f'{what} nesting too deep') from None


def _young(info, cutoff):
    return max(info.st_mtime, info.st_ctime) >= cutoff


def _hex_name(suffix):
    def name_of(name):
        sha = name[:-len(suffix)] if name.endswith(suffix) else None
        return sha if _sha(sha) else None
    return name_of


def _job_name(name):
    # Same population as the publisher's Path.glob('*.json'): unlike a shell it includes dot files (and '.json'
    # itself), and it matches case-insensitively where the platform path case is insensitive (normcase on Windows).
    return name if os.path.normcase(name).endswith('.json') else None


def _scan(folder, budget, name_of):
    """{key: lstat-like info} of matching regular files and the count of other entries; symlinks fail."""
    hb._real_dir(folder)
    named, unknown = {}, 0
    with os.scandir(folder) as iterator:
        for entry in iterator:
            budget.entry(entry.name)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise hb.BundleError(f'symlink in cave store: {folder.name}/{entry.name}')
            key = name_of(entry.name) if stat.S_ISREG(info.st_mode) else None
            if key is None:
                unknown += 1
            else:
                named[key] = info
    return named, unknown


def _read(path, limit, budget):
    """Bounded read whose size is reserved from lstat before reading; symlinks/non-regular fail."""
    before = hb._regular(path)
    if not 1 <= before.st_size <= limit:
        raise hb.BundleError(f'{path.name} outside size limit')
    budget.file(before.st_size)
    raw, after = hb._read(path, limit)
    if hr._identity(after) != hr._identity(before):
        raise hb.BundleError(f'{path.name} changed during cave retention scan')
    return raw, after


def _optional(path, limit, budget):
    try:
        return _read(path, limit, budget)
    except FileNotFoundError:
        return None, None


def _unchanged(path, raw, info, limit, budget):
    final, final_info = _optional(path, limit, budget)
    if final != raw or (info is not None and hr._identity(final_info) != hr._identity(info)):
        raise hb.BundleError(f'{path.name} changed during cave retention scan')


def _cap_pointer(raw):
    doc = _json(raw, 'cave cap pointer')
    if (not isinstance(doc, dict) or set(doc) != {'format', 'indexSha256'} or doc['format'] != POINTER_FORMAT
            or not _sha(doc['indexSha256']) or _canonical(doc) != raw):
        raise hb.BundleError('invalid cave cap pointer')
    return doc['indexSha256']


def _cap_index(raw, sha):
    if hashlib.sha256(raw).hexdigest() != sha:
        raise hb.BundleError(f'cave cap index hash mismatch: {sha}')
    doc = _json(raw, 'cave cap index')
    if (not isinstance(doc, dict) or set(doc) != CAP_INDEX_FIELDS or doc['format'] != ci.FORMAT
            or not all(_sha(doc[k]) for k in ('geometryIndexSha256', 'rendererIdentity', 'atlasSha256', 'capIdentity'))
            or not isinstance(doc['pairs'], dict) or len(doc['pairs']) > hb.MAX_INDEX_ENTRIES):
        raise hb.BundleError(f'invalid cave cap index: {sha}')
    for key, pair in doc['pairs'].items():
        if not isinstance(key, str) or not ci.KEY.fullmatch(key) or not _sha(pair):
            raise hb.BundleError(f'invalid cave cap index reference: {sha}')
    if _canonical(doc) != raw:
        raise hb.BundleError(f'cave cap index is not canonical: {sha}')
    return doc


def _pair(raw, sha):
    if hashlib.sha256(raw).hexdigest() != sha:
        raise hb.BundleError(f'cave pair manifest hash mismatch: {sha}')
    doc = _json(raw, 'cave pair')
    try:
        canonical = cp.canonical(doc)                   # validates the schema first
    except ValueError as error:
        raise hb.BundleError(f'invalid cave pair {sha}: {error}') from None
    if canonical != raw:
        raise hb.BundleError(f'cave pair manifest is not canonical: {sha}')
    return doc


def _job_pairs(raw, name):
    job = _json(raw, 'cave job')
    pairs = job.get('capPairs', {}) if isinstance(job, dict) else None
    if not isinstance(pairs, dict) or len(pairs) > MAX_JOB_PAIRS:
        raise hb.BundleError(f'invalid cave job pair inventory: {name}')
    for key, sha in pairs.items():
        if not isinstance(key, str) or not ci.KEY.fullmatch(key) or not _sha(sha):
            raise hb.BundleError(f'invalid cave job pair reference: {name}')
    return pairs


def plan_retention(out, *, now=None):
    out = hb._owned(out)
    if now is None:
        now = time.time()
    if not history._timestamp(now):
        raise ValueError('now must be a finite nonnegative number')
    cutoff = now - GRACE_SECONDS
    budget = _Budget()
    root = out / 'cave-data'
    hb._real_dir(root)

    # 1. Provenance inputs (all small, bounded, lstat-regular).
    root_pointer_raw, root_pointer_info = _read(out / 'current.json', POINTER_BYTES, budget)
    journal_raw, journal_info = _read(out / hh.NAME, hh.MAX_JOURNAL_BYTES, budget)
    root_current = hr._pointer(root_pointer_raw)
    journal = hr._journal(journal_raw)
    cap_pointer_raw, cap_pointer_info = _optional(root / 'current.json', POINTER_BYTES, budget)
    history_raw, history_info = _optional(root / history.NAME, history.MAX_BYTES, budget)
    cap_pointer = _cap_pointer(cap_pointer_raw) if cap_pointer_raw is not None else None
    cap_history = history.parse(history_raw) if history_raw is not None else None

    # 2. Directory inventories (stat only; unknown names counted, never candidates).
    root_indexes, unknown_root = _scan(out / 'indexes', budget, _hex_name('.json'))
    jobs, unknown_jobs = _scan(out / 'jobs', budget, _job_name)
    entries, unknown = {}, {'rootIndexes': unknown_root, 'jobs': unknown_jobs}
    for kind, suffix in KINDS.items():
        if kind == 'indexes' and not os.path.lexists(root / kind):
            entries[kind], unknown[kind] = {}, 0          # created by the first cap publication
            continue
        entries[kind], unknown[kind] = _scan(root / kind, budget, _hex_name(suffix))

    if cap_history is None:
        if cap_pointer is not None:
            raise hb.BundleError('cave cap pointer without cap history')
        if entries['indexes']:
            raise hb.BundleError('cave cap indexes without cap history')
    elif cap_pointer not in (cap_history['currentIndexSha256'], cap_history['previousIndexSha256']):
        raise hb.BundleError('actual cave cap pointer is not in cap history')

    # 3. Root geometry roots, fully validated with the root helper (bytes, hash, schema, canonical JSON).
    root_roots = {root_current, journal['currentIndexSha256'], journal['previousIndexSha256']} - {None}
    root_roots |= {sha for sha, stamp in journal['lastPublished'].items() if stamp >= cutoff}
    root_roots |= {sha for sha, info in root_indexes.items() if _young(info, cutoff)}
    root_docs = {}
    for sha in sorted(root_roots):
        doc, context = hr._index(out, sha, budget.file)
        if context is None or context['caves'] is not True:
            raise hb.BundleError(f'retained root index is not cave geometry: {sha}')
        root_docs[sha] = doc

    # 4. Every cap index is read and validated before any candidate exists.
    cap_docs = {}
    for sha in sorted(entries['indexes']):
        raw, _ = _read(root / 'indexes' / f'{sha}.json', MAX_CAP_INDEX_BYTES, budget)
        cap_docs[sha] = _cap_index(raw, sha)
    cap_roots = {sha for sha, info in entries['indexes'].items() if _young(info, cutoff)}
    cap_roots |= {sha for sha, doc in cap_docs.items() if doc['geometryIndexSha256'] in root_roots}
    if cap_history is not None:
        cap_roots |= {cap_pointer, cap_history['currentIndexSha256'], cap_history['previousIndexSha256']} - {None}
        for sha, record in cap_history['records'].items():
            if record['publishedAt'] >= cutoff or record['geometryIndexSha256'] in root_roots:
                cap_roots.add(sha)
            if sha in cap_docs and cap_docs[sha]['geometryIndexSha256'] != record['geometryIndexSha256']:
                raise hb.BundleError(f'cap history geometry differs from cap index: {sha}')
    missing = cap_roots - set(cap_docs)
    if missing:
        raise hb.BundleError(f'protected cave cap index missing: {min(missing)}')
    for sha in cap_roots:
        doc, geometry = cap_docs[sha], root_docs.get(cap_docs[sha]['geometryIndexSha256'])
        if geometry is not None and (doc['atlasSha256'] != geometry['atlasSha256']
                                     or doc['rendererIdentity'] != geometry['rendererIdentity']
                                     or not set(doc['pairs']) <= set(geometry['tiles'])):
            raise hb.BundleError(f'protected cave cap index differs from its geometry: {sha}')

    # 5. In-flight references: every job's capPairs (covers the geometry-before-cap gap and waiting publications).
    job_pairs = set()
    for name in sorted(jobs):
        raw, _ = _read(out / 'jobs' / name, MAX_JOB_BYTES, budget)
        job_pairs.update(_job_pairs(raw, name).values())

    # 6. Protected pairs: read once each; a missing/malformed one is a hard failure. Young manifests join the closure
    # BEFORE validation: a producer interrupted before its job write leaves a referenced-by-nobody manifest whose
    # content-addressed cap (or alias) may be old and reused, so file-age grace alone would not keep its payloads.
    young_pairs = {sha for sha, info in entries['pairs'].items() if _young(info, cutoff)}
    protected_pairs = set(job_pairs) | young_pairs
    for sha in cap_roots:
        protected_pairs.update(cap_docs[sha]['pairs'].values())
    pair_docs = {}
    for sha in sorted(protected_pairs):
        if sha not in entries['pairs']:
            raise hb.BundleError(f'protected cave pair missing: {sha}')
        raw, _ = _read(root / 'pairs' / f'{sha}.json', MAX_PAIR_BYTES, budget)
        pair_docs[sha] = _pair(raw, sha)
    for sha in cap_roots:
        doc, geometry = cap_docs[sha], root_docs.get(cap_docs[sha]['geometryIndexSha256'])
        for key, pair_sha in doc['pairs'].items():
            pair = pair_docs[pair_sha]
            x, z = (int(value) * 64 for value in key.split(','))
            if (pair['origin'] != [x, z] or pair['atlasSha256'] != doc['atlasSha256']
                    or pair['rendererIdentity'] != doc['rendererIdentity'] or pair['capIdentity'] != doc['capIdentity']
                    or (geometry is not None and pair['geometrySha256'] != geometry['tiles'].get(key))):
                raise hb.BundleError(f'cave cap index {sha} reference differs from pair {pair_sha}')
    protected_caps = set()
    protected_aliases = set()
    for sha, pair in pair_docs.items():
        info = entries['caps'].get(pair['capSha256'])
        if info is None:
            raise hb.BundleError(f'protected cave cap payload missing: {pair["capSha256"]}')
        if not 1 <= info.st_size <= MAX_CAP_BYTES:
            raise hb.BundleError(f'invalid protected cave cap size: {pair["capSha256"]}')
        protected_caps.add(pair['capSha256'])
        geometry_sha = pair['geometrySha256']
        if geometry_sha in entries['objects'] and geometry_sha not in protected_aliases:
            alias = hb._regular(root / 'objects' / f'{geometry_sha}.uih')   # lstat: scandir has no nlink on Windows
            if alias.st_size != pair['geometryBytes']:
                raise hb.BundleError(f'cave geometry alias size differs from pair: {geometry_sha}')
            if alias.st_nlink <= 1:
                protected_aliases.add(geometry_sha)

    # 7. Candidates only now, after the complete protection closure.
    protected = {'indexes': cap_roots, 'pairs': protected_pairs, 'caps': protected_caps, 'objects': protected_aliases}
    totals = {kind: 0 for kind in KINDS}
    logical_bytes = 0
    listed = []
    for kind in KINDS:                                    # executor order: indexes, pairs, caps, aliases
        for sha, info in sorted(entries[kind].items()):
            if sha in protected[kind] or _young(info, cutoff):
                continue
            totals[kind] += 1
            logical_bytes += info.st_size
            if len(listed) < MAX_CANDIDATES:
                listed.append((kind, sha, info))
    if cap_history is None:
        state = 'no-cap-history'
    elif cap_history['bootstrapAt'] >= cutoff:
        state = 'bootstrap-grace'
    else:
        state = 'planned'
    candidates = []
    if state == 'planned':
        for kind, sha, info in listed:
            name = f'{sha}{KINDS[kind]}'
            current = hb._regular(root / kind / name)
            if current.st_size != info.st_size or current.st_mtime_ns != info.st_mtime_ns:
                raise hb.BundleError(f'candidate changed during cave retention scan: {kind}/{name}')
            candidates.append({'path': f'cave-data/{kind}/{name}', 'identity': hr._identity(current),
                               'links': current.st_nlink})

    # 8. Cooperative change detection (the caller's lock is the actual guarantee).
    _unchanged(out / 'current.json', root_pointer_raw, root_pointer_info, POINTER_BYTES, budget)
    _unchanged(out / hh.NAME, journal_raw, journal_info, hh.MAX_JOURNAL_BYTES, budget)
    _unchanged(root / 'current.json', cap_pointer_raw, cap_pointer_info, POINTER_BYTES, budget)
    _unchanged(root / history.NAME, history_raw, history_info, history.MAX_BYTES, budget)
    total = sum(totals.values())
    planned = state == 'planned'
    return {'dryRun': True, 'state': state,
            'rootCurrentIndexSha256': root_current, 'capPointerIndexSha256': cap_pointer,
            'bootstrapAt': cap_history['bootstrapAt'] if cap_history else None,
            'protected': {'rootIndexes': len(root_roots), 'capIndexes': len(cap_roots), 'pairs': len(protected_pairs),
                          'jobPairs': len(job_pairs), 'youngPairs': len(young_pairs), 'caps': len(protected_caps),
                          'aliases': len(protected_aliases)},
            'jobs': len(jobs), 'scanned': budget.scanned, 'metadataBytes': budget.bytes, 'metadataFiles': budget.files,
            'budgets': {'scanned': MAX_SCANNED, 'metadataBytes': MAX_METADATA_BYTES,
                        'metadataFiles': MAX_METADATA_FILES, 'candidates': MAX_CANDIDATES},
            'unknown': unknown,
            'candidateTotals': totals if planned else {kind: 0 for kind in KINDS},
            'candidateCount': total if planned else 0,
            'suppressedCandidates': 0 if planned else total,
            'candidatesTruncated': planned and total > len(candidates),
            'candidatesOmitted': total - len(candidates) if planned else 0,
            'logicalBytes': logical_bytes if planned else 0,
            'reclaimableBytes': None,
            'candidates': candidates}
