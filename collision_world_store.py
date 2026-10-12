"""Incremental publication of a large collision store without re-reading every UIB1 body on each publication.

Library only: no CLI, no lock acquisition, no scheduler, no deletion. The CALLER holds out/.export.lock (the lock
collision_export, collision_pair_stage and collision_gc share) for the whole publish_incremental call.

    cache = PairProofCache()                      # one per long-running process; never persisted
    budget = Budget(max_reads=..., max_bytes=...)  # per call
    publish_incremental(store, expected_index=<sha | None>, updates={'tx,tz': pairSha}, removals={'tx,tz'},
                        cache=cache, budget=budget)

PairProofCache: a pair is proven only by a FULL collision_bundle._verify_pair in THIS process (pair record, UIB1
identity/header, UIH identity/header). The proof stores the lstat identity (st_dev, st_ino, st_size, st_mtime_ns,
st_ctime_ns) of the pair file and both objects, measured before and after that verification. Later the pair is
accepted without reading only while all three identities are unchanged; anything else drops the proof and verifies
again. Symlinks, non-regular files and symlinked store folders are refused. Nothing is loaded from disk: a persisted
index is never trusted, and a restarted process warms up again within each call's Budget. Like
hires_bundle.VerificationCache, this does not protect against a writer bypassing the export lock, filesystem
corruption or privileged timestamp manipulation. Proofs are kept only on POSIX (ctime semantics); elsewhere every
call verifies everything.

Budget: every read is reserved at its real (lstat) size before it happens: this module's own read of the pair record
(1 read), then collision_bundle._verify_pair's second read of the record plus one read per object. A reservation that
does not fit raises WarmupPending; reads already made in that attempt stay counted, nothing is published, proofs gained
so far stay, so repeated calls progress. A call always completes at least one pair, so a pair larger than the budget
cannot block warm-up. That first pair can exceed the budget by at most 2 * MAX_PAIR_BYTES (16 KiB) + 2 *
MAX_OBJECT_BYTES (64 MiB): larger objects are refused (StoreError) before collision_bundle reads them.

Capacity: the cache is pruned to the merged index BEFORE verification (a replaced pair frees its slot); an index larger
than the cache capacity is refused instead of warming up forever.

publish_incremental:
  1. store and its folders are real directories; current.json (strict UIE_COLLISION_POINTER1) must name exactly
     expected_index (None = no pointer yet); the previous index is read with its SHA-256, strict canonical
     UIE_COLLISION_INDEX1 schema and collision_bundle limits;
  2. merged = previous - removals + updates; count (<= 90 000) and canonical size (<= 8 MiB) are checked BEFORE any
     verification;
  3. every merged pair is proven (cache or full verification within the budget);
  4. the pointer is compared with expected_index again and every proof is re-checked by lstat (also before an
     "unchanged" result); the immutable index is written and its directory fsynced; proofs re-checked; pointer
     compared; with the GC marker, collision_gc.record_publication runs (its failure refuses the publication) and the
     pointer is compared once more; only then current.json is replaced atomically and the store directory fsynced.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time

import collision_bundle as cb

POINTER_LIMIT = 4096


class WarmupPending(Exception):
    """Nothing was published. Retry in the SAME process with the same cache; proofs gained so far are kept."""


class StoreError(ValueError):
    """Refused: invalid store state, changed pointer (CAS), invalid input, or an object that fails verification."""


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(path: Path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise StoreError(f'missing {path.parent.name}/{path.name}') from None
    if not stat.S_ISREG(info.st_mode):
        raise StoreError(f'{path.parent.name}/{path.name} is not a regular non-symlink file')
    return info


def _real_dir(path: Path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise StoreError(f'missing directory {path.name}') from None
    if not stat.S_ISDIR(info.st_mode):
        raise StoreError(f'{path.name} is not a real directory')


def _read(path: Path, limit: int) -> bytes:
    """Bytes of a regular non-symlink file of 1..limit bytes that did not change while it was read."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        raise StoreError(f'missing {path.parent.name}/{path.name}') from None
    except OSError as error:
        raise StoreError(f'{path.name} unreadable: {error.strerror}') from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= limit:
            raise StoreError(f'{path.name} is not a regular file of 1..{limit} bytes')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if len(raw) != before.st_size or _identity(before) != _identity(after):
        raise StoreError(f'{path.name} changed while it was read')
    return raw


def _strict_json(raw: bytes, what: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StoreError(f'duplicate key in {what}')
            result[key] = value
        return result

    def constant(_):
        raise StoreError(f'non-finite value in {what}')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)
    except StoreError:
        raise
    except (UnicodeError, ValueError) as error:
        raise StoreError(f'{what} is not valid JSON: {error}') from None


def _check_key(key):
    m = cb.TILE_KEY.fullmatch(key) if isinstance(key, str) else None
    if not m or abs(int(m.group(1))) > cb.MAX_TILE_COORD or abs(int(m.group(2))) > cb.MAX_TILE_COORD:
        raise StoreError(f'invalid tile key {key!r}')
    return key


def _check_sha(value, what):
    if not isinstance(value, str) or not cb.HEX64.fullmatch(value):
        raise StoreError(f'invalid {what} {value!r}')
    return value


class Budget:
    """Reads and bytes one publish_incremental call may spend on verification."""

    def __init__(self, max_reads: int, max_bytes: int):
        if type(max_reads) is not int or type(max_bytes) is not int or max_reads < 1 or max_bytes < 1:
            raise ValueError('budget limits must be positive integers')
        self.max_reads, self.max_bytes = max_reads, max_bytes
        self.reads = self.bytes = self.verified = 0

    def take(self, reads: int, nbytes: int):
        over = self.reads + reads > self.max_reads or self.bytes + nbytes > self.max_bytes
        if over and self.verified:                 # progress guarantee: the first pair of a call always proceeds
            raise WarmupPending(f'verification budget exhausted after {self.verified} pairs '
                                f'({self.reads} reads, {self.bytes} bytes)')
        self.reads += reads
        self.bytes += nbytes


class PairProofCache:
    def __init__(self, max_entries: int = cb.MAX_INDEX_ENTRIES):
        if type(max_entries) is not int or not 1 <= max_entries <= cb.MAX_INDEX_ENTRIES:
            raise ValueError('invalid cache capacity')
        self.max_entries = max_entries
        self._proofs = {}

    def __len__(self):
        return len(self._proofs)

    @staticmethod
    def _paths(store: Path, pair_sha, blocks, geometry):
        return (store / 'pairs' / f'{pair_sha}.json', store / 'objects' / f'{blocks}.uib',
                store / 'objects' / f'{geometry}.uih' if geometry is not None else None)

    @staticmethod
    def _identities(paths):
        return tuple(_identity(_regular(p)) if p is not None else None for p in paths)

    def proven(self, store: Path, key: str, pair_sha: str) -> bool:
        """True when a proof exists and all three files still have the proven identity (no read). A stale proof is
        dropped."""
        token = (str(store), key, pair_sha)
        proof = self._proofs.get(token)
        if proof is None:
            return False
        blocks, geometry, identities = proof
        try:
            if self._identities(self._paths(store, pair_sha, blocks, geometry)) == identities:
                return True
        except StoreError:
            pass
        del self._proofs[token]
        return False

    def verify(self, store: Path, key: str, pair_sha: str, budget: Budget) -> int:
        """Prove one pair (cache hit or full verification within the budget). Returns bytes read."""
        store = Path(store)
        _check_key(key)
        _check_sha(pair_sha, 'pair hash')
        if os.name == 'posix' and self.proven(store, key, pair_sha):
            return 0
        pair_path = store / 'pairs' / f'{pair_sha}.json'
        pair_info = _regular(pair_path)
        if pair_info.st_size > cb.MAX_PAIR_BYTES:
            raise StoreError(f'pairs/{pair_sha}.json is too large')
        budget.take(1, pair_info.st_size)                   # this module's own read of the pair record
        record = _strict_json(_read(pair_path, cb.MAX_PAIR_BYTES), f'pairs/{pair_sha}.json')
        if not isinstance(record, dict):
            raise StoreError(f'pairs/{pair_sha}.json is not an object')
        blocks = _check_sha(record.get('blocksSha256'), 'blocks hash')
        geometry = record.get('geometrySha256')
        if geometry is not None:
            _check_sha(geometry, 'geometry hash')
        paths = self._paths(store, pair_sha, blocks, geometry)
        before = self._identities(paths)
        if before[0] != _identity(pair_info):
            raise StoreError(f'pairs/{pair_sha}.json changed while it was read')
        objects = [identity for identity in before[1:] if identity is not None]
        if any(identity[2] > cb.MAX_OBJECT_BYTES for identity in objects):
            raise StoreError(f'pair {key} references an object larger than {cb.MAX_OBJECT_BYTES} bytes')
        object_bytes = sum(identity[2] for identity in objects)
        # collision_bundle._verify_pair reads the pair record again, then each object once.
        budget.take(1 + len(objects), pair_info.st_size + object_bytes)
        try:
            checked = cb._verify_pair(store, key, pair_sha)      # existing full validation, unchanged rules
        except (OSError, ValueError) as error:
            raise StoreError(f'pair {key} failed verification: {error}') from None
        if checked != key:
            raise StoreError(f'pair {pair_sha} verified for {checked}, not {key}')
        if self._identities(paths) != before:
            raise StoreError(f'pair {key} changed during verification')
        budget.verified += 1
        if os.name == 'posix':
            token = (str(store), key, pair_sha)
            if token not in self._proofs and len(self._proofs) >= self.max_entries:
                raise StoreError('proof cache capacity exhausted')
            self._proofs[token] = (blocks, geometry, before)
        return 2 * pair_info.st_size + object_bytes

    def retain(self, store: Path, entries: dict):
        """Keep only proofs of `entries` {key: pairSha} in `store` (bounds memory to the current index)."""
        wanted = {(str(Path(store)), key, sha) for key, sha in entries.items()}
        self._proofs = {k: v for k, v in self._proofs.items() if k in wanted}


def _read_pointer(store: Path):
    """indexSha256 named by current.json, None when it does not exist; anything malformed refuses."""
    path = store / 'current.json'
    if not os.path.lexists(path):
        return None
    doc = _strict_json(_read(path, POINTER_LIMIT), 'current.json')
    if (not isinstance(doc, dict) or set(doc) != {'format', 'indexSha256'} or doc['format'] != cb.POINTER_FORMAT):
        raise StoreError('current.json is not a UIE_COLLISION_POINTER1')
    return _check_sha(doc['indexSha256'], 'current index hash')


def _read_index(store: Path, sha: str) -> dict:
    """Pairs of the immutable index `sha`: exact hash, strict canonical UIE_COLLISION_INDEX1, collision limits."""
    _real_dir(store / 'indexes')
    raw = _read(store / 'indexes' / f'{sha}.json', cb.MAX_INDEX_BYTES)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise StoreError('previous index content does not match its hash')
    doc = _strict_json(raw, 'previous index')
    if (not isinstance(doc, dict) or set(doc) != {'format', 'tileSize', 'pairs'} or doc['format'] != cb.INDEX_FORMAT
            or type(doc['tileSize']) is not int or doc['tileSize'] != cb.TILE or not isinstance(doc['pairs'], dict)
            or len(doc['pairs']) > cb.MAX_INDEX_ENTRIES):
        raise StoreError('previous index is not a valid UIE_COLLISION_INDEX1')
    for key, pair_sha in doc['pairs'].items():
        _check_key(key)
        _check_sha(pair_sha, 'previous pair hash')
    if cb.canonical(doc) != raw:
        raise StoreError('previous index is not canonical')
    return dict(doc['pairs'])


def _check_store(store) -> Path:
    store = Path(store)
    if not store.is_absolute() or Path(os.path.realpath(store)) != store:
        raise StoreError('store must be an absolute path without symlinks')
    _real_dir(store)
    for name in ('pairs', 'objects', 'indexes'):
        if os.path.lexists(store / name):
            _real_dir(store / name)
    return store


def _cas(store: Path, expected):
    current = _read_pointer(store)
    if current != expected:
        raise StoreError(f'current.json names {current}, expected {expected}: refused (concurrent publication)')


def publish_incremental(store, *, expected_index, updates, removals, cache: PairProofCache, budget: Budget,
                        now=None) -> dict:
    """See the module documentation. Raises WarmupPending (retry) or StoreError (refused); never partial."""
    store = _check_store(store)
    if expected_index is not None:
        _check_sha(expected_index, 'expected index hash')
    if not isinstance(updates, dict) or not isinstance(removals, (set, frozenset)):
        raise StoreError('updates must be a dict and removals a set')
    for key, pair_sha in updates.items():
        _check_key(key)
        _check_sha(pair_sha, 'update pair hash')
    for key in removals:
        _check_key(key)
    if set(updates) & set(removals):
        raise StoreError('a tile cannot be updated and removed in the same publication')
    _cas(store, expected_index)
    previous = _read_index(store, expected_index) if expected_index is not None else {}
    merged = {k: v for k, v in previous.items() if k not in removals}
    merged.update(updates)
    if len(merged) > cb.MAX_INDEX_ENTRIES:
        raise StoreError(f'index would exceed {cb.MAX_INDEX_ENTRIES} entries')
    raw = cb.canonical({'format': cb.INDEX_FORMAT, 'tileSize': cb.TILE, 'pairs': merged})
    if len(raw) > cb.MAX_INDEX_BYTES:
        raise StoreError('index would exceed 8 MiB')
    if len(merged) > cache.max_entries:          # would never finish warming up
        raise StoreError(f'proof cache capacity {cache.max_entries} is smaller than the index ({len(merged)})')
    cache.retain(store, merged)                  # before verification: replaced pairs free their slots
    bytes_read = 0
    for key in sorted(merged):                   # deterministic order: warm-up progresses across calls
        bytes_read += cache.verify(store, key, merged[key], budget)
    digest = hashlib.sha256(raw).hexdigest()
    result = {'indexSha256': digest, 'entries': len(merged), 'removed': len(previous.keys() & removals),
              'updated': len(updates), 'bytesRead': bytes_read, 'reads': budget.reads}
    _cas(store, expected_index)                  # nothing is written when the pointer moved during verification
    _recheck(cache, store, merged)               # an earlier pair may have changed while later ones were verified
    if expected_index is not None and merged == previous:
        return dict(result, indexSha256=expected_index, unchanged=True)
    cb._immutable(store / 'indexes' / f'{digest}.json', raw)
    cb._fsync_directory(store / 'indexes')       # the new index entry is durable before any pointer names it
    _recheck(cache, store, merged)
    _cas(store, expected_index)
    gc_enabled = cb._gc_enabled(store)
    if gc_enabled:
        import collision_gc   # lazy, like collision_bundle (collision_gc imports collision_bundle)
        try:
            collision_gc.record_publication(store.parent, expected_index, digest, time.time() if now is None else now)
        except Exception as error:
            raise StoreError(f'GC publication journal not updated, publication refused: {error}') from error
        _cas(store, expected_index)              # the pointer must still be the expected one after the journal
    cb._replace_atomic(store / 'current.json', cb.canonical({'format': cb.POINTER_FORMAT, 'indexSha256': digest}))
    cb._fsync_directory(store)
    return dict(result, unchanged=False)


def _recheck(cache, store, merged):
    """Every proof still matches its files (lstat only); otherwise retry later, never publish or report success."""
    if os.name != 'posix':
        return
    for key in sorted(merged):
        if not cache.proven(store, key, merged[key]):
            raise WarmupPending(f'pair {key} changed after verification')
