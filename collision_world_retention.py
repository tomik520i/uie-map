"""READ-ONLY retention planner and pure deletion selection for a whole-world collision store.

Nothing here deletes, renames or writes files, and nothing is wired into a runner or timer. A separate executor
(future task) would hold out/.export.lock, persist the ledger returned by select_deletions BEFORE unlinking, and
re-check every file identity and the pointer immediately before each unlink (collision_gc._check_before_unlink).
The legacy collision_retention / collision_gc modules are unchanged and keep serving bounded pilot stores.

    plan = plan_world_retention(store, now=..., journal={'updatedAt': t, 'indexes': {sha: t}},
                                cache=collision_world_store.PairProofCache(...), budget=Budget(...))
    decision = select_deletions(plan, ledger, now=..., limits=WorldRetentionLimits())

Roots (retained with their whole closure): the current index, every index whose mtime is younger than the grace
period (future mtimes count as young), and every index the publication journal protected less than
min_unreferenced_seconds ago (future protection times count as recent). Every YOUNG pair (any mtime younger than the
grace, indexed or not) and its objects, and every young object, are retained too.

Verification: each root index is read once (bounded, no symlinks), checked against its SHA-256, strict canonical
UIE_COLLISION_INDEX1 schema and collision limits. Every required (key, pairSha) of the roots and young pairs is
proven through PairProofCache.verify (full collision_bundle._verify_pair the first time in this process, then lstat
identities incl. ctime) within the caller's Budget: an exhausted budget raises WarmupPending (nothing decided; the
next call continues). A pair under the wrong key, missing or damaged objects refuse the whole plan. Identical
(key, pair) references are verified once; two revisions of the same key are two separate proofs.

Cache capacity: the planner never evicts proofs. If the unique required proofs exceed cache.max_entries the plan is
refused up front; if the cache is filled by OTHER proofs (e.g. the publisher's) so that a required proof cannot be
stored, the plan is refused as well. A dedicated retention cache sized for several historical revisions (a future
API, e.g. PairProofCache(max_entries=...) beyond 90 000 or a token-set retain) is the caller's decision; this module
does not change collision_world_store.

Old (non-root) indexes are NEVER parsed: they are candidates by name, even when their payload is malformed.
Phases are strictly ordered, decided at planning time:
  indexes  always allowed;
  pairs    only when no non-root index exists (otherwise an old index could still reference an unretained pair);
  objects  only when additionally no unretained pair exists (otherwise an old pair could still reference an object).
Candidates of a disallowed phase are reported, but select_deletions neither observes nor selects them (their grace
starts only once they are truly unreferenced).

Unknown names (temporaries, foreign files, directories) are counted, never candidates; any symlink refuses. The plan
is plain JSON (lists, dicts, ints, strings, bools).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

import collision_bundle as cb
import collision_world_store as cws

PLAN_FORMAT = 'UIE_COLLISION_WORLD_RETENTION_PLAN1'
DECISION_FORMAT = 'UIE_COLLISION_WORLD_RETENTION_DECISION1'
MIN_SECONDS = 1800
POINTER_LIMIT = 4096
FOLDERS = {'indexes': re.compile(r'([0-9a-f]{64})\.json'), 'pairs': re.compile(r'([0-9a-f]{64})\.json'),
           'objects': re.compile(r'([0-9a-f]{64})\.(uib|uih)')}
PHASES = ('indexes', 'pairs', 'objects')
CANDIDATE_PATH = re.compile(r'(indexes|pairs)/[0-9a-f]{64}\.json|objects/[0-9a-f]{64}\.(uib|uih)')
PAIR_FIELDS = {'format', 'x', 'z', 'atlasSha256', 'geometrySha256', 'blocksSha256'}
IDENTITY_FIELDS = ('size', 'mtimeNs', 'ctimeNs', 'ino', 'dev')

WarmupPending = cws.WarmupPending


class RetentionRefused(Exception):
    """The whole plan or selection is refused; nothing may be deleted on its basis."""


@dataclasses.dataclass(frozen=True)
class WorldRetentionLimits:
    max_files: int = 1_000_000                # entries scanned in indexes/, pairs/, objects/
    max_root_index_bytes: int = 512 << 20     # root index bytes read per plan (each index <= 8 MiB)
    max_pair_bytes: int = 64 << 20            # pair record bytes read per plan (retained closure)
    max_ledger_entries: int = 300_000
    max_ledger_bytes: int = 64 << 20          # canonical JSON of the ledger entries
    max_delete_files: int = 1000              # per selection
    max_delete_bytes: int = 1 << 30           # per selection

    def __post_init__(self):
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 1:
                raise ValueError(f'{field.name} must be a positive integer')


# ------------------------------------------------------------------ helpers

def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _identity(info) -> dict:
    return {'size': info.st_size, 'mtimeNs': info.st_mtime_ns, 'ctimeNs': info.st_ctime_ns,
            'ino': info.st_ino, 'dev': info.st_dev}


def _check_store(store) -> Path:
    store = Path(store)
    if not store.is_absolute() or Path(os.path.realpath(store)) != store:
        raise RetentionRefused('store must be an absolute path without symlinks')
    try:
        info = os.lstat(store)
    except OSError as error:
        raise RetentionRefused(f'store unavailable: {error}') from None
    if not stat.S_ISDIR(info.st_mode):
        raise RetentionRefused('store is not a real directory')
    return store


def _read(path: Path, limit: int, what: str) -> bytes:
    try:
        return cws._read(path, limit)
    except cws.StoreError as error:
        raise RetentionRefused(f'{what}: {error}') from None


def _pointer(store: Path):
    """(sha256 of the exact pointer bytes, current index)."""
    path = store / 'current.json'
    if not os.path.lexists(path):
        raise RetentionRefused('no current.json: nothing is known to be current')
    raw = _read(path, POINTER_LIMIT, 'current.json')
    try:
        doc = cws._strict_json(raw, 'current.json')
    except cws.StoreError as error:
        raise RetentionRefused(str(error)) from None
    if (not isinstance(doc, dict) or set(doc) != {'format', 'indexSha256'} or doc['format'] != cb.POINTER_FORMAT
            or not isinstance(doc['indexSha256'], str) or not cb.HEX64.fullmatch(doc['indexSha256'])):
        raise RetentionRefused('current.json is not a valid pointer')
    return hashlib.sha256(raw).hexdigest(), doc['indexSha256']


def _scan(store: Path, max_files: int):
    """{folder: {name: identity}}, unknown names. Every entry met (top level and inside the three folders) counts
    against max_files BEFORE anything is kept; nothing is materialized or sorted beyond the limit. Foreign
    directories are counted as one entry and never entered. Symlinks refuse; unknown names are never candidates."""
    files = {folder: {} for folder in FOLDERS}
    unknown, total = [], 0

    def count():
        nonlocal total
        total += 1
        if total > max_files:
            raise RetentionRefused(f'more than {max_files} files in the store')
    with os.scandir(store) as entries:
        for entry in entries:
            count()
            if entry.is_symlink():
                raise RetentionRefused(f'symlink in the store: {entry.name}')
            if entry.name == 'current.json' and entry.is_file(follow_symlinks=False):
                continue
            if entry.name in FOLDERS and entry.is_dir(follow_symlinks=False):
                with os.scandir(entry.path) as children:
                    for child in children:
                        count()
                        if child.is_symlink():
                            raise RetentionRefused(f'symlink in the store: {entry.name}/{child.name}')
                        if child.is_file(follow_symlinks=False) and FOLDERS[entry.name].fullmatch(child.name):
                            info = os.lstat(child.path)
                            if not stat.S_ISREG(info.st_mode):
                                raise RetentionRefused(f'{entry.name}/{child.name} changed type during the scan')
                            files[entry.name][child.name] = _identity(info)
                        else:
                            unknown.append(f'{entry.name}/{child.name}')
                continue
            unknown.append(entry.name)
    return files, sorted(unknown)


def _validate_journal(journal) -> dict:
    if (not isinstance(journal, dict) or set(journal) != {'updatedAt', 'indexes'} or not _finite(journal['updatedAt'])
            or not isinstance(journal['indexes'], dict)):
        raise RetentionRefused('journal must be {"updatedAt": finite, "indexes": {sha: finite}}')
    for sha, at in journal['indexes'].items():
        if not isinstance(sha, str) or not cb.HEX64.fullmatch(sha) or not _finite(at):
            raise RetentionRefused(f'invalid journal entry {sha!r}')
    return journal['indexes']


class _Meter:
    def __init__(self, limit, what):
        self.limit, self.what, self.bytes = limit, what, 0

    def add(self, n):
        self.bytes += n
        if self.bytes > self.limit:
            raise RetentionRefused(f'more than {self.limit} bytes of {self.what} read')


def _root_index(store: Path, sha: str, meter: _Meter) -> dict:
    """Pairs of root index `sha` (hash, strict canonical schema, limits)."""
    path = store / 'indexes' / f'{sha}.json'
    try:
        size = os.lstat(path).st_size
    except FileNotFoundError:
        raise RetentionRefused(f'root index {sha} is missing') from None
    meter.add(size)                                        # accounted before reading
    raw = _read(path, cb.MAX_INDEX_BYTES, f'root index {sha}')
    if hashlib.sha256(raw).hexdigest() != sha:
        raise RetentionRefused(f'root index {sha} does not match its hash')
    try:
        doc = cws._strict_json(raw, f'index {sha}')
    except cws.StoreError as error:
        raise RetentionRefused(str(error)) from None
    if (not isinstance(doc, dict) or set(doc) != {'format', 'tileSize', 'pairs'} or doc['format'] != cb.INDEX_FORMAT
            or type(doc['tileSize']) is not int or doc['tileSize'] != cb.TILE or not isinstance(doc['pairs'], dict)
            or len(doc['pairs']) > cb.MAX_INDEX_ENTRIES or cb.canonical(doc) != raw):
        raise RetentionRefused(f'root index {sha} is not a valid canonical {cb.INDEX_FORMAT}')
    for key, pair in doc['pairs'].items():
        try:
            cws._check_key(key)
        except cws.StoreError:
            raise RetentionRefused(f'root index {sha} has an invalid key {key!r}') from None
        if not isinstance(pair, str) or not cb.HEX64.fullmatch(pair):
            raise RetentionRefused(f'root index {sha} has an invalid pair for {key}')
    return doc['pairs']


def _pair_record(store: Path, sha: str, meter: _Meter) -> dict:
    path = store / 'pairs' / f'{sha}.json'
    try:
        size = os.lstat(path).st_size
    except FileNotFoundError:
        raise RetentionRefused(f'retained pair {sha} is missing') from None
    meter.add(size)
    raw = _read(path, cb.MAX_PAIR_BYTES, f'pair {sha}')
    if hashlib.sha256(raw).hexdigest() != sha:
        raise RetentionRefused(f'pair {sha} does not match its hash')
    try:
        pair = cws._strict_json(raw, f'pair {sha}')
    except cws.StoreError as error:
        raise RetentionRefused(str(error)) from None
    if (not isinstance(pair, dict) or set(pair) != PAIR_FIELDS or pair['format'] != cb.PAIR_FORMAT
            or cb.canonical(pair) != raw or type(pair['x']) is not int or type(pair['z']) is not int
            or pair['x'] % cb.TILE or pair['z'] % cb.TILE
            or not isinstance(pair['blocksSha256'], str) or not cb.HEX64.fullmatch(pair['blocksSha256'])
            or not (pair['geometrySha256'] is None
                    or (isinstance(pair['geometrySha256'], str) and cb.HEX64.fullmatch(pair['geometrySha256'])))):
        raise RetentionRefused(f'pair {sha} is not a valid canonical {cb.PAIR_FORMAT}')
    return pair


def _objects_of(pair: dict) -> list:
    names = [f'{pair["blocksSha256"]}.uib']
    if pair['geometrySha256'] is not None:
        names.append(f'{pair["geometrySha256"]}.uih')
    return names


# ------------------------------------------------------------------ plan

def plan_world_retention(store, *, now, journal, cache, budget, limits=None, grace_seconds=MIN_SECONDS,
                         min_unreferenced_seconds=MIN_SECONDS) -> dict:
    """Read-only plan. Raises WarmupPending (retry later, nothing decided) or RetentionRefused."""
    limits = WorldRetentionLimits() if limits is None else limits
    if not isinstance(limits, WorldRetentionLimits):
        raise ValueError('limits must be WorldRetentionLimits')
    if not _finite(now):
        raise ValueError('now must be finite')
    for name, value in (('grace_seconds', grace_seconds), ('min_unreferenced_seconds', min_unreferenced_seconds)):
        if not _finite(value) or value < MIN_SECONDS:
            raise ValueError(f'{name} must be at least {MIN_SECONDS}')
    protected_at = _validate_journal(journal)
    store = _check_store(store)
    pointer_before, current = _pointer(store)
    files, unknown = _scan(store, limits.max_files)
    cutoff_ns = int((now - grace_seconds) * 1e9)
    young = lambda identity: identity['mtimeNs'] >= cutoff_ns

    roots = {current: 'current'}
    for name, identity in files['indexes'].items():
        if young(identity):
            roots.setdefault(name[:-5], 'young')
    for sha, at in protected_at.items():
        if now - at < min_unreferenced_seconds:
            roots.setdefault(sha, 'journal')
    for sha, why in sorted(roots.items()):
        if f'{sha}.json' not in files['indexes']:
            raise RetentionRefused(f'{why} index {sha} is missing')

    index_meter = _Meter(limits.max_root_index_bytes, 'root indexes')
    required = set()                                       # unique (key, pairSha) tokens
    for sha in sorted(roots):
        required.update(_root_index(store, sha, index_meter).items())
    pair_meter = _Meter(limits.max_pair_bytes, 'pair records')
    records = {}
    for name, identity in sorted(files['pairs'].items()):
        if young(identity):                                # young pairs: protected with their objects, bound to
            sha = name[:-5]                                # the key their own record names
            records[sha] = pair = _pair_record(store, sha, pair_meter)
            required.add((f'{pair["x"] // cb.TILE},{pair["z"] // cb.TILE}', sha))
    if len(required) > cache.max_entries:
        raise RetentionRefused(f'{len(required)} retained pair proofs exceed the proof cache capacity '
                               f'{cache.max_entries}; use a larger dedicated retention cache')
    for key, sha in sorted(required):
        try:
            cache.verify(store, key, sha, budget)          # WarmupPending propagates: nothing decided
        except cws.StoreError as error:
            raise RetentionRefused(f'retained pair {key} {sha}: {error}') from None
    keep_pairs = {sha for _, sha in required}
    keep_objects = set()
    for sha in sorted(keep_pairs):
        pair = records.get(sha) or _pair_record(store, sha, pair_meter)
        for name in _objects_of(pair):
            if name not in files['objects']:
                raise RetentionRefused(f'object {name} of retained pair {sha} is missing')
            keep_objects.add(name)
    for name, identity in files['objects'].items():
        if young(identity):
            keep_objects.add(name)

    def candidate(folder, name):
        return {'path': f'{folder}/{name}', **files[folder][name]}
    index_candidates = sorted((candidate('indexes', n) for n in files['indexes'] if n[:-5] not in roots),
                              key=lambda c: (c['mtimeNs'], c['path']))     # oldest first; never parsed
    pair_candidates = sorted((candidate('pairs', n) for n in files['pairs'] if n[:-5] not in keep_pairs),
                             key=lambda c: c['path'])
    object_candidates = sorted((candidate('objects', n) for n in files['objects'] if n not in keep_objects),
                               key=lambda c: c['path'])
    pair_phase = not index_candidates
    object_phase = pair_phase and not pair_candidates

    # Final recheck AFTER all metadata reads: every retained proof still holds (a file changed while later pairs were
    # verified -> retry), and every root index / retained pair record is still the file the scan saw (replaced or
    # rewritten after it was read -> refuse). Nothing is trusted from an earlier call beyond the in-process proofs.
    if os.name == 'posix':
        for key, sha in sorted(required):
            if not cache.proven(store, key, sha):
                raise WarmupPending(f'retained pair {key} {sha} changed after verification')
    for folder, names in (('indexes', [f'{sha}.json' for sha in roots]), ('pairs', [f'{sha}.json' for sha in keep_pairs])):
        for name in sorted(names):
            try:
                now_identity = _identity(os.lstat(store / folder / name))
            except FileNotFoundError:
                raise RetentionRefused(f'{folder}/{name} disappeared during planning') from None
            if now_identity != files[folder].get(name):           # absent from the scan: appeared meanwhile
                raise RetentionRefused(f'{folder}/{name} changed during planning')
    pointer_after, current_after = _pointer(store)
    if pointer_after != pointer_before or current_after != current:
        raise RetentionRefused('current.json changed during planning')
    return {'format': PLAN_FORMAT, 'store': str(store), 'createdAt': now, 'graceSeconds': grace_seconds,
            'minUnreferencedSeconds': min_unreferenced_seconds, 'pointerSha256': pointer_before,
            'currentIndex': current, 'roots': {sha: roots[sha] for sha in sorted(roots)},
            'retained': {'pairs': len(keep_pairs), 'objects': len(keep_objects), 'proofs': len(required)},
            'candidates': {'indexes': index_candidates, 'pairs': pair_candidates, 'objects': object_candidates},
            'pairPhaseAllowed': pair_phase, 'objectPhaseAllowed': object_phase,
            'known': {folder: sorted(files[folder]) for folder in PHASES},
            'unknown': {'count': len(unknown), 'examples': unknown[:50]},
            'metadataBytes': {'rootIndexes': index_meter.bytes, 'pairs': pair_meter.bytes}}


# ------------------------------------------------------------------ pure selection

def _validate_entry(rel, entry):
    if (not isinstance(rel, str) or not CANDIDATE_PATH.fullmatch(rel) or not isinstance(entry, dict)
            or set(entry) != {'firstSeen', *IDENTITY_FIELDS} or not _finite(entry['firstSeen'])
            or not all(type(entry[k]) is int for k in IDENTITY_FIELDS)):
        raise RetentionRefused(f'invalid ledger entry {rel!r}')


def _ledger_bytes(entries) -> int:
    return len(json.dumps(dict(sorted(entries.items())), sort_keys=True, separators=(',', ':')).encode())


def select_deletions(plan, ledger, *, now, min_unreferenced_seconds=MIN_SECONDS, limits=None) -> dict:
    """Pure: no I/O. Observe the candidates of ALLOWED phases (firstSeen kept only for the same identity incl.
    ctime, never in the future), select those unreferenced for at least min_unreferenced_seconds within the delete
    budgets. Returns {'format', 'entries' (the new ledger), 'phase', 'selected', 'skipped', 'ledgerBytes'}."""
    limits = WorldRetentionLimits() if limits is None else limits
    if not isinstance(limits, WorldRetentionLimits):
        raise ValueError('limits must be WorldRetentionLimits')
    if not _finite(now):
        raise ValueError('now must be finite')
    if not _finite(min_unreferenced_seconds) or min_unreferenced_seconds < MIN_SECONDS:
        raise ValueError(f'min_unreferenced_seconds must be at least {MIN_SECONDS}')
    if (not isinstance(plan, dict) or plan.get('format') != PLAN_FORMAT
            or type(plan.get('pairPhaseAllowed')) is not bool or type(plan.get('objectPhaseAllowed')) is not bool
            or not isinstance(plan.get('candidates'), dict) or set(plan['candidates']) != set(PHASES)
            or not _finite(plan.get('createdAt'))):
        raise RetentionRefused('not a world retention plan')
    if now < plan['createdAt']:
        raise RetentionRefused('plan was created after `now`; recompute the plan')
    seen = set()
    for phase in PHASES:
        items = plan['candidates'][phase]
        if not isinstance(items, list):
            raise RetentionRefused(f'plan candidates of {phase} are not a list')
        for item in items:
            if (not isinstance(item, dict) or set(item) != {'path', *IDENTITY_FIELDS}
                    or not isinstance(item['path'], str) or not item['path'].startswith(phase + '/')
                    or not CANDIDATE_PATH.fullmatch(item['path'])
                    or not all(type(item[k]) is int for k in IDENTITY_FIELDS)
                    or min(item['size'], item['ino'], item['dev']) < 0):
                raise RetentionRefused(f'invalid plan candidate in {phase}')
            if item['path'] in seen:
                raise RetentionRefused(f'duplicate plan candidate {item["path"]}')
            seen.add(item['path'])
    # Phase flags must follow from the candidate lists exactly as the planner derives them.
    if (plan['pairPhaseAllowed'] != (not plan['candidates']['indexes'])
            or plan['objectPhaseAllowed'] != (plan['pairPhaseAllowed'] and not plan['candidates']['pairs'])):
        raise RetentionRefused('inconsistent plan phases')
    if not isinstance(ledger, dict) or len(ledger) > limits.max_ledger_entries:
        raise RetentionRefused('ledger is not a bounded mapping')
    for rel, entry in ledger.items():
        _validate_entry(rel, entry)
    if _ledger_bytes(ledger) > limits.max_ledger_bytes:
        raise RetentionRefused('ledger exceeds its byte limit')

    allowed = ['indexes'] + (['pairs'] if plan['pairPhaseAllowed'] else []) + \
              (['objects'] if plan['objectPhaseAllowed'] else [])
    entries, observed = {}, {}
    for phase in allowed:
        for item in plan['candidates'][phase]:
            rel = item['path']
            old = ledger.get(rel)
            same = old is not None and all(old[k] == item[k] for k in IDENTITY_FIELDS)
            first = min(old['firstSeen'], now) if same else now     # new or replaced file: grace starts now
            entries[rel] = {'firstSeen': first, **{k: item[k] for k in IDENTITY_FIELDS}}
            observed[rel] = item
    if len(entries) > limits.max_ledger_entries:
        raise RetentionRefused(f'more than {limits.max_ledger_entries} ledger entries')
    size = _ledger_bytes(entries)
    if size > limits.max_ledger_bytes:
        raise RetentionRefused('ledger would exceed its byte limit')

    # Only one allowed phase can have candidates (pairs need no index candidate, objects no pair candidate);
    # selection stays within the first allowed phase that yields something, in plan order (indexes: oldest first).
    phase, selected, skipped = None, [], {'young': 0, 'fileBudget': 0, 'byteBudget': 0}
    total = 0
    for current_phase in allowed:
        for item in plan['candidates'][current_phase]:
            entry = entries[item['path']]
            if now - entry['firstSeen'] < min_unreferenced_seconds:
                skipped['young'] += 1
                continue
            if len(selected) >= limits.max_delete_files:
                skipped['fileBudget'] += 1
                continue
            if total + item['size'] > limits.max_delete_bytes:
                skipped['byteBudget'] += 1
                continue
            selected.append(dict(item))
            total += item['size']
        if selected:
            phase = current_phase
            break
    return {'format': DECISION_FORMAT, 'entries': dict(sorted(entries.items())), 'phase': phase,
            'selected': selected, 'selectedBytes': total, 'skipped': skipped, 'ledgerBytes': size,
            'suppressedPhases': [p for p in PHASES if p not in allowed]}
