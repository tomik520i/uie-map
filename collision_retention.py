"""Read-only retention PLANNER for a collision store (collision_bundle layout). It never deletes, renames or
writes anything: it only reports which immutable files an eventual, separately reviewed executor could remove.

    plan_retention(store, *, previous_index=None, now=time.time(), grace_seconds=1800, verify_objects=False)
        -> plan dict, or raises RetentionRefused (the WHOLE plan is refused; nothing is ever inferred from a
           damaged graph)
    select_active_pairs(pairs, pinned_keys, recent_keys, max_tiles=32) -> selection dict (pure; no I/O)

Store layout (collision_bundle): current.json (pointer), indexes/<sha>.json, pairs/<sha>.json,
objects/<sha>.uib|.uih. Anything else (e.g. '.object-*' / '.pointer-*' temporaries, foreign files, directories)
is UNKNOWN: counted, never a candidate.

Kept (the "retained closure"):
  - the current index (from current.json) and an explicit previous index, with every pair they reference and
    every object those pairs reference;
  - every index whose mtime is younger than the grace period (or in the future: clock skew protects), with its
    closure;
  - every pair younger than the grace period (an export may be about to index it), with its objects;
  - every object younger than the grace period.
Every retained index and pair is verified (bounded reads via collision_bundle._read_object, SHA-256 of the exact
bytes, schema, canonical form, key/position, referenced objects present as regular files). Objects are checked
for existence and size; verify_objects=True additionally runs collision_bundle's full object verifiers.
Any symlink among the inspected entries, a missing or damaged retained file, too many files or too much metadata
refuses the whole plan. The pointer bytes are hashed before and after the scan; a change refuses the plan.

Candidates carry {path, size, mtimeNs, ino, dev} so an executor can re-validate each file (and re-run the plan
under its own lock) immediately before acting.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

import collision_bundle as cb

PLAN_FORMAT = 'UIE_COLLISION_RETENTION_PLAN1'
MIN_GRACE_SECONDS = 1800
MAX_FILES = 200_000
MAX_METADATA_BYTES = 256 * 1024 * 1024      # indexes (<= 8 MiB each) + pairs (<= 16 KiB each) read per plan
MAX_POINTER_BYTES = 4096
MAX_UNKNOWN_EXAMPLES = 50
FOLDERS = {'indexes': re.compile(r'([0-9a-f]{64})\.json'), 'pairs': re.compile(r'([0-9a-f]{64})\.json'),
           'objects': re.compile(r'([0-9a-f]{64})\.(uib|uih)')}
PAIR_FIELDS = {'format', 'x', 'z', 'atlasSha256', 'geometrySha256', 'blocksSha256'}


class RetentionRefused(Exception):
    """The plan is refused as a whole."""


# ------------------------------------------------------------------ scanning

def _check_store(store) -> Path:
    store = Path(store)
    if not store.is_absolute():
        raise RetentionRefused('the store path must be absolute')
    try:
        info = os.lstat(store)
    except OSError as error:
        raise RetentionRefused(f'store unavailable: {error}') from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RetentionRefused('the store must be a real directory, not a symlink')
    if store.resolve() != store:
        raise RetentionRefused('the store path contains symlinks or is not normalized (pass the resolved path)')
    return store


def _read_pointer(store: Path):
    """(sha256 of the exact pointer bytes, indexSha256)."""
    path = store / 'current.json'
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise RetentionRefused('current.json is not a regular file')
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_POINTER_BYTES + 1)
    except FileNotFoundError:
        raise RetentionRefused('no current.json: nothing is known to be current') from None
    if len(raw) > MAX_POINTER_BYTES:
        raise RetentionRefused('current.json is too large')
    try:
        doc = json.loads(raw)
    except ValueError:
        raise RetentionRefused('current.json is not JSON') from None
    if (not isinstance(doc, dict) or set(doc) != {'format', 'indexSha256'} or doc['format'] != cb.POINTER_FORMAT
            or not isinstance(doc['indexSha256'], str) or not cb.HEX64.fullmatch(doc['indexSha256'])):
        raise RetentionRefused('current.json is not a valid pointer')
    return hashlib.sha256(raw).hexdigest(), doc['indexSha256']


def _scan(store: Path):
    """{folder: {name: stat}}, unknown entries list. Never follows symlinks; refuses any symlink."""
    files = {folder: {} for folder in FOLDERS}
    unknown, total = [], 0
    with os.scandir(store) as entries:
        top = sorted(entries, key=lambda e: e.name)
    for entry in top:
        if entry.is_symlink():
            raise RetentionRefused(f'symlink in the store: {entry.name}')
        if entry.name == 'current.json' and entry.is_file(follow_symlinks=False):
            continue
        if entry.name in FOLDERS and entry.is_dir(follow_symlinks=False):
            with os.scandir(entry.path) as children:
                for child in children:
                    total += 1
                    if total > MAX_FILES:
                        raise RetentionRefused(f'more than {MAX_FILES} files in the store')
                    if child.is_symlink():
                        raise RetentionRefused(f'symlink in the store: {entry.name}/{child.name}')
                    if child.is_file(follow_symlinks=False) and FOLDERS[entry.name].fullmatch(child.name):
                        info = os.lstat(child.path)      # full identity (DirEntry.stat lacks st_ino on Windows)
                        if not stat.S_ISREG(info.st_mode):
                            raise RetentionRefused(f'{entry.name}/{child.name} changed type during the scan')
                        files[entry.name][child.name] = info
                    else:
                        unknown.append(f'{entry.name}/{child.name}')
            continue
        unknown.append(entry.name)              # foreign file or directory: never inspected further, never removed
    return files, sorted(unknown)


# ------------------------------------------------------------------ graph

def _young(info, cutoff_ns: int) -> bool:
    return info.st_mtime_ns >= cutoff_ns           # future mtimes are young too


class _Reader:
    def __init__(self, store: Path):
        self.store, self.bytes = store, 0

    def read(self, folder, name, limit):
        try:
            raw = cb._read_object(self.store, folder, name, limit)
        except (OSError, ValueError) as error:
            raise RetentionRefused(f'retained {folder}/{name} unreadable: {error}') from None
        self.bytes += len(raw)
        if self.bytes > MAX_METADATA_BYTES:
            raise RetentionRefused(f'more than {MAX_METADATA_BYTES} bytes of retained metadata')
        return raw


def _index_pairs(reader: _Reader, sha: str) -> dict:
    raw = reader.read('indexes', f'{sha}.json', cb.MAX_INDEX_BYTES)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise RetentionRefused(f'index {sha} does not match its hash')
    try:
        doc = json.loads(raw)
    except ValueError:
        raise RetentionRefused(f'index {sha} is not JSON') from None
    if (not isinstance(doc, dict) or set(doc) != {'format', 'tileSize', 'pairs'} or doc['format'] != cb.INDEX_FORMAT
            or doc['tileSize'] != cb.TILE or not isinstance(doc['pairs'], dict)
            or len(doc['pairs']) > cb.MAX_INDEX_ENTRIES or cb.canonical(doc) != raw):
        raise RetentionRefused(f'index {sha} is not a valid {cb.INDEX_FORMAT}')
    for key, pair in doc['pairs'].items():
        if not isinstance(key, str) or not cb.TILE_KEY.fullmatch(key) or not isinstance(pair, str) \
                or not cb.HEX64.fullmatch(pair):
            raise RetentionRefused(f'index {sha} has an invalid entry {key!r}')
    return doc['pairs']


def _pair_objects(reader: _Reader, sha: str, key=None) -> list:
    raw = reader.read('pairs', f'{sha}.json', cb.MAX_PAIR_BYTES)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise RetentionRefused(f'pair {sha} does not match its hash')
    try:
        pair = json.loads(raw)
    except ValueError:
        raise RetentionRefused(f'pair {sha} is not JSON') from None
    if (not isinstance(pair, dict) or set(pair) != PAIR_FIELDS or pair['format'] != cb.PAIR_FORMAT
            or cb.canonical(pair) != raw or type(pair['x']) is not int or type(pair['z']) is not int
            or pair['x'] % cb.TILE or pair['z'] % cb.TILE
            or not isinstance(pair['atlasSha256'], str) or not cb.HEX64.fullmatch(pair['atlasSha256'])
            or not isinstance(pair['blocksSha256'], str) or not cb.HEX64.fullmatch(pair['blocksSha256'])
            or not (pair['geometrySha256'] is None
                    or (isinstance(pair['geometrySha256'], str) and cb.HEX64.fullmatch(pair['geometrySha256'])))):
        raise RetentionRefused(f'pair {sha} is not a valid canonical {cb.PAIR_FORMAT}')
    if key is not None:
        tx, tz = (int(v) for v in key.split(','))
        if (pair['x'], pair['z']) != (tx * cb.TILE, tz * cb.TILE):
            raise RetentionRefused(f'pair {sha} covers {pair["x"]},{pair["z"]}, not tile {key}')
    objects = [(f'{pair["blocksSha256"]}.uib', pair['blocksSha256'], 'blocks')]
    if pair['geometrySha256'] is not None:
        objects.append((f'{pair["geometrySha256"]}.uih', pair['geometrySha256'], 'geometry'))
    return [(name, sha_, kind, pair['x'], pair['z']) for name, sha_, kind in objects]


# ------------------------------------------------------------------ plan

def plan_retention(store, *, previous_index=None, now=None, grace_seconds=MIN_GRACE_SECONDS,
                   verify_objects=False) -> dict:
    if not isinstance(grace_seconds, (int, float)) or not math.isfinite(grace_seconds) or grace_seconds < MIN_GRACE_SECONDS:
        raise ValueError(f'grace_seconds must be at least {MIN_GRACE_SECONDS}')
    if previous_index is not None and (not isinstance(previous_index, str) or not cb.HEX64.fullmatch(previous_index)):
        raise ValueError('previous_index must be a 64 hex index hash')
    now = time.time() if now is None else now
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('now must be finite')
    store = _check_store(store)
    pointer_before, current = _read_pointer(store)
    files, unknown = _scan(store)
    cutoff_ns = int((now - grace_seconds) * 1e9)
    reader = _Reader(store)

    # Roots: current, explicit previous, every young index.
    roots = {current: 'current'}
    if previous_index is not None:
        roots.setdefault(previous_index, 'previous')
    for name, info in files['indexes'].items():
        if _young(info, cutoff_ns):
            roots.setdefault(name[:-5], 'young')
    for sha, why in sorted(roots.items()):
        if f'{sha}.json' not in files['indexes']:
            raise RetentionRefused(f'{why} index {sha} is missing')

    keep_pairs, keep_objects = {}, {}
    pair_keys = {}
    for sha in sorted(roots):
        for key, pair in sorted(_index_pairs(reader, sha).items()):
            pair_keys.setdefault(pair, set()).add(key)
            keep_pairs[pair] = f'index {sha}'
    for name, info in files['pairs'].items():
        if _young(info, cutoff_ns):
            keep_pairs.setdefault(name[:-5], 'young pair')
    for pair in sorted(keep_pairs):
        if f'{pair}.json' not in files['pairs']:
            raise RetentionRefused(f'retained pair {pair} ({keep_pairs[pair]}) is missing')
        keys = sorted(pair_keys.get(pair, ())) or [None]
        for key in keys:
            for name, sha, kind, x, z in _pair_objects(reader, pair, key):
                info = files['objects'].get(name)
                if info is None or info.st_size == 0 or info.st_size > cb.MAX_OBJECT_BYTES:
                    raise RetentionRefused(f'object {name} of retained pair {pair} is missing or empty')
                if verify_objects:
                    try:
                        (cb._verify_blocks if kind == 'blocks' else cb._verify_geometry)(store, sha, x, z)
                    except (OSError, ValueError) as error:
                        raise RetentionRefused(f'object {name} of retained pair {pair} failed verification: {error}') from None
                keep_objects[name] = f'pair {pair}'
    for name, info in files['objects'].items():
        if _young(info, cutoff_ns):
            keep_objects.setdefault(name, 'young object')

    keep = ([f'indexes/{sha}.json' for sha in roots] + [f'pairs/{p}.json' for p in keep_pairs]
            + [f'objects/{o}' for o in keep_objects])
    candidates = []
    for folder, kept in (('indexes', {f'{s}.json' for s in roots}), ('pairs', {f'{p}.json' for p in keep_pairs}),
                         ('objects', set(keep_objects))):
        for name, info in files[folder].items():
            if name not in kept:
                candidates.append({'path': f'{folder}/{name}', 'size': info.st_size, 'mtimeNs': info.st_mtime_ns,
                                   'ino': info.st_ino, 'dev': info.st_dev})
    candidates.sort(key=lambda c: c['path'])

    pointer_after, current_after = _read_pointer(store)
    if pointer_after != pointer_before or current_after != current:
        raise RetentionRefused('current.json changed during planning')
    return {'format': PLAN_FORMAT, 'store': str(store), 'createdAt': now, 'graceSeconds': grace_seconds,
            'pointerSha256': pointer_before, 'currentIndex': current, 'previousIndex': previous_index,
            'retained': {'indexes': len(roots), 'pairs': len(keep_pairs), 'objects': len(keep_objects)},
            'keep': sorted(keep), 'candidates': candidates, 'candidateBytes': sum(c['size'] for c in candidates),
            'unknown': {'count': len(unknown), 'examples': unknown[:MAX_UNKNOWN_EXAMPLES]},
            'filesInspected': sum(len(v) for v in files.values()), 'metadataBytesRead': reader.bytes,
            'objectsVerified': bool(verify_objects)}


# ------------------------------------------------------------------ active selection (pure)

def select_active_pairs(pairs: dict, pinned_keys, recent_keys, max_tiles: int = 32) -> dict:
    """Which current pairs an exporter keeps in its next index: pinned keys first (in the given order), then
    recent keys (most recent first, as given), capped at max_tiles; everything else is dropped. Pure: it reads and
    changes nothing (no pointer, no files).

    Pins must fit: more distinct pins than max_tiles raises ValueError. A pinned or recent key without a pair is
    NOT invented: it is listed in missingPinned / missingRecent and stays unknown."""
    if not isinstance(max_tiles, int) or max_tiles < 1:
        raise ValueError('max_tiles must be a positive integer')
    if not isinstance(pairs, dict):
        raise ValueError('pairs must be a dict of "tx,tz" -> pair hash')
    for key, sha in pairs.items():
        if not isinstance(key, str) or not cb.TILE_KEY.fullmatch(key) or not isinstance(sha, str) \
                or not cb.HEX64.fullmatch(sha):
            raise ValueError(f'invalid pair entry {key!r}')
    pinned = list(dict.fromkeys(pinned_keys))
    recent = [k for k in dict.fromkeys(recent_keys) if k not in pinned]
    for key in pinned + recent:
        if not isinstance(key, str) or not cb.TILE_KEY.fullmatch(key):
            raise ValueError(f'invalid tile key {key!r}')
    if len(pinned) > max_tiles:
        raise ValueError(f'{len(pinned)} pinned tiles exceed max_tiles {max_tiles}')
    kept = [k for k in pinned if k in pairs]
    for key in recent:
        if len(kept) >= max_tiles:
            break
        if key in pairs:
            kept.append(key)
    kept_set = set(kept)
    return {'pairs': {k: pairs[k] for k in kept}, 'kept': kept,
            'dropped': sorted(k for k in pairs if k not in kept_set),
            'missingPinned': [k for k in pinned if k not in pairs],
            'missingRecent': [k for k in recent if k not in pairs]}
