"""Collision store garbage collection EXECUTOR - private library, no CLI, not wired into any timer.

    run_gc(out, *, previous_index=None, now=None, ...)       takes out/.export.lock itself (busy: nothing happens)
    gc_locked(out, *, previous_index=None, now=None, ...)    the CALLER must already hold out/.export.lock

`out` is a collision_export output directory: it must contain the ownership marker `.uie-collision-owned`
(created by whoever owns the directory; this module never creates it) and the store `out/store`. The GC ledger
`out/gc-state.json` lives OUTSIDE the store.

Why a ledger: collision_retention protects files by their mtime, but an index that was current for a long time
has an OLD mtime and becomes unreferenced the moment the next index is published. The ledger records when each
candidate was FIRST SEEN unreferenced; a file is deleted only after it stayed a candidate continuously for at
least `min_unreferenced_seconds` (>= 1800) of GC observations. A file that is referenced again (or whose identity
changed) leaves the ledger and starts over. A missing ledger starts empty (first observation: nothing deleted); an
unreadable or malformed ledger refuses the run.

Publication journal (fixes B -> A -> B between two GC runs, where A's ledger age and old mtime would otherwise
let A be deleted without a grace since it was last current): GC only runs for outputs that ALSO contain the
opt-in marker `.uie-collision-gc-enabled` and a valid `out/gc-publications.json` (missing or damaged: refused).
enable_gc(out) is the deliberate one-time initialization (journal first, protecting the current index, then the
marker); nothing enables it automatically. For enabled outputs collision_bundle.publish_index calls
record_publication(out, old, next, now) BEFORE replacing current.json, under the same caller-held export lock;
the outgoing and incoming index get lastProtectedAt = max(now, journal updatedAt). A journal write failure
refuses the publication (the old pointer stays). Every index protected within min_unreferenced_seconds keeps its
whole closure in GC (a missing or damaged one refuses the run). Journal entries are removed only when expired
AND their index file is gone; more than 200 000 entries refuse the publication instead of dropping young ones.

Lock contract: the exporter's exclusive out/.export.lock serializes publish_index (+ its journal record) and GC.

Each run (under the export lock):
  1. validates out (absolute, normalized, real directory, owned + enabled markers are regular files, store is a
     real directory) and loads the publication journal;
  2. plan = collision_retention.plan_retention(store, previous_index=..., verify_objects=True) - current, explicit
     previous and every young index keep their verified closure; any damage refuses the run;
  3. updates and writes the ledger atomically BEFORE any deletion;
  4. deletes eligible candidates in the order indexes -> pairs -> objects, at most max_delete_files and
     max_delete_bytes per run. Before the pair and the object phase the remaining index / pair files are read again
     and anything still referenced by a remaining file is kept, and the index / pair directories must not contain
     files unknown to the plan (another writer). Before EVERY unlink: the pointer hash equals the plan's, the
     relative path matches the known pattern, the parent is a real directory and the file is a regular file with
     exactly the planned identity (size, mtime, inode, device). Any mismatch stops the run (fail closed).
     Only files are removed: no directory removal, no recursion, nothing outside out/store.
  5. writes the ledger again without the deleted entries.
A crash at any point leaves every remaining index referring only to existing pairs and every remaining pair
referring only to existing objects; old unreferenced URLs may return 404 after the grace, which is expected.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import time

import collision_bundle as cb
import collision_retention as cr

STATE_FORMAT = 'UIE_COLLISION_GC_STATE1'
RESULT_FORMAT = 'UIE_COLLISION_GC_RESULT1'
JOURNAL_FORMAT = 'UIE_COLLISION_GC_PUBLICATIONS1'
OWNED_MARKER = '.uie-collision-owned'
ENABLED_MARKER = cb.GC_ENABLED_MARKER
JOURNAL_NAME = 'gc-publications.json'
MAX_JOURNAL_ENTRIES = 200_000
MAX_JOURNAL_BYTES = 32 * 1024 * 1024
LOCK_NAME = '.export.lock'
STATE_NAME = 'gc-state.json'
MIN_UNREFERENCED_SECONDS = 1800
DEFAULT_MAX_DELETE_FILES = 100
MAX_DELETE_FILES_LIMIT = 1000
DEFAULT_MAX_DELETE_BYTES = 64 * 1024 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_ENTRIES = cr.MAX_FILES
CANDIDATE_PATH = re.compile(r'(indexes|pairs)/[0-9a-f]{64}\.json|objects/[0-9a-f]{64}\.(uib|uih)')
PHASES = ('indexes', 'pairs', 'objects')


class GcRefused(Exception):
    """Nothing is deleted."""


class _Stop(Exception):
    """Fail closed in the middle of a run: no further deletion."""


# ------------------------------------------------------------------ ownership and lock

def check_owned_out(out):
    """(out, store) of an owned collision output directory, or GcRefused."""
    out = Path(out)
    if not out.is_absolute():
        raise GcRefused('out must be an absolute path')
    for path, kind in ((out, 'dir'), (out / OWNED_MARKER, 'file'), (out / 'store', 'dir')):
        try:
            info = os.lstat(path)
        except OSError:
            raise GcRefused(f'{path.name} missing: not an owned collision output') from None
        ok = stat.S_ISDIR(info.st_mode) if kind == 'dir' else stat.S_ISREG(info.st_mode)
        if stat.S_ISLNK(info.st_mode) or not ok:
            raise GcRefused(f'{path.name} must be a real {kind}, not a symlink')
    if out.resolve() != out:
        raise GcRefused('out contains symlinks or is not normalized (pass the resolved path)')
    return out, out / 'store'


def default_export_lock(out: Path):
    """The exporter's exclusive lock (out/.export.lock, flock LOCK_EX|LOCK_NB): handle, or None when busy.
    Without fcntl there is no exclusive lock, so GC refuses instead of running unlocked."""
    try:
        import fcntl
    except ImportError:
        raise GcRefused('no exclusive file lock on this platform; GC refuses to run unlocked') from None
    handle = open(Path(out) / LOCK_NAME, 'a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# ------------------------------------------------------------------ ledger

def _identity(item) -> tuple:
    return (item['size'], item['mtimeNs'], item['ino'], item['dev'])


def load_ledger(path: Path, store: Path) -> dict:
    """{relative path: entry}; {} when missing. Anything unreadable or malformed refuses the run."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise GcRefused('gc-state.json is not a regular file')
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_LEDGER_BYTES + 1)
    if len(raw) > MAX_LEDGER_BYTES:
        raise GcRefused('gc-state.json is too large')
    try:
        doc = json.loads(raw)
    except ValueError:
        raise GcRefused('gc-state.json is not JSON') from None
    if (not isinstance(doc, dict) or doc.get('format') != STATE_FORMAT or doc.get('store') != str(store)
            or not isinstance(doc.get('firstSeenUnreferenced'), dict)
            or len(doc['firstSeenUnreferenced']) > MAX_LEDGER_ENTRIES):
        raise GcRefused('gc-state.json is not a ledger of this store')
    entries = {}
    for rel, entry in doc['firstSeenUnreferenced'].items():
        if (not isinstance(rel, str) or not CANDIDATE_PATH.fullmatch(rel) or not isinstance(entry, dict)
                or not isinstance(entry.get('firstSeen'), (int, float)) or not math.isfinite(entry['firstSeen'])
                or not all(type(entry.get(k)) is int for k in ('size', 'mtimeNs', 'ino', 'dev'))):
            raise GcRefused(f'gc-state.json has an invalid entry {rel!r}')
        entries[rel] = entry
    return entries


def write_ledger(path: Path, store: Path, entries: dict, now: float, pointer: str):
    if len(entries) > MAX_LEDGER_ENTRIES:
        raise GcRefused('too many ledger entries')
    raw = json.dumps({'format': STATE_FORMAT, 'store': str(store), 'updatedAt': now, 'pointerSha256': pointer,
                      'firstSeenUnreferenced': dict(sorted(entries.items()))}, separators=(',', ':')).encode()
    if len(raw) > MAX_LEDGER_BYTES:
        raise GcRefused('ledger would exceed its size limit')
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with open(tmp, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)          # our own temporary next to the ledger, never a store file
        except FileNotFoundError:
            pass
        raise


# ------------------------------------------------------------------ publication journal

def _regular(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise GcRefused(f'{path.name} is not a regular file')
    return True


def load_journal(out: Path, store: Path) -> dict:
    """{'updatedAt': t, 'indexes': {indexSha: lastProtectedAt}}. Missing, unreadable or malformed refuses: an
    enabled store must have a deliberately initialized journal (enable_gc)."""
    path = out / JOURNAL_NAME
    if not _regular(path):
        raise GcRefused('GC is enabled but gc-publications.json is missing (initialize it with enable_gc)')
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_JOURNAL_BYTES + 1)
    if len(raw) > MAX_JOURNAL_BYTES:
        raise GcRefused('gc-publications.json is too large')
    try:
        doc = json.loads(raw)
    except ValueError:
        raise GcRefused('gc-publications.json is not JSON') from None
    finite = lambda v: isinstance(v, (int, float)) and math.isfinite(v)
    if (not isinstance(doc, dict) or doc.get('format') != JOURNAL_FORMAT or doc.get('store') != str(store)
            or not finite(doc.get('updatedAt')) or not isinstance(doc.get('indexes'), dict)
            or len(doc['indexes']) > MAX_JOURNAL_ENTRIES
            or not all(isinstance(k, str) and cb.HEX64.fullmatch(k) and finite(v) for k, v in doc['indexes'].items())):
        raise GcRefused('gc-publications.json is not a publication journal of this store')
    return {'updatedAt': doc['updatedAt'], 'indexes': dict(doc['indexes'])}


def write_journal(out: Path, store: Path, journal: dict):
    if len(journal['indexes']) > MAX_JOURNAL_ENTRIES:
        raise GcRefused(f'publication journal would exceed {MAX_JOURNAL_ENTRIES} entries')
    raw = json.dumps({'format': JOURNAL_FORMAT, 'store': str(store), 'updatedAt': journal['updatedAt'],
                      'indexes': dict(sorted(journal['indexes'].items()))}, separators=(',', ':')).encode()
    if len(raw) > MAX_JOURNAL_BYTES:
        raise GcRefused('publication journal would exceed its size limit')
    path = out / JOURNAL_NAME
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with open(tmp, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        cb._fsync_directory(out)  # journal rename must be durable before current.json can advance
    except BaseException:
        try:
            os.unlink(tmp)          # our own temporary next to the journal, never a store file
        except FileNotFoundError:
            pass
        raise


def _check_enabled(out: Path):
    if not _regular(out / ENABLED_MARKER):
        raise GcRefused(f'GC is not enabled for this output ({ENABLED_MARKER} missing)')


def record_publication(out, old_index, next_index, now) -> dict:
    """Called by collision_bundle.publish_index (GC-enabled outputs only) BEFORE current.json is replaced, under the
    caller's exclusive export lock: protects the outgoing and the incoming index as of `now`. The time never goes
    backwards (max with the journal's updatedAt). Any failure raises; the publication is then refused."""
    out, store = check_owned_out(out)
    _check_enabled(out)
    journal = load_journal(out, store)
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise GcRefused('now must be finite')
    at = max(now, journal['updatedAt'])
    for sha in (old_index, next_index):
        if sha is not None:
            if not isinstance(sha, str) or not cb.HEX64.fullmatch(sha):
                raise GcRefused(f'invalid index hash {sha!r}')
            journal['indexes'][sha] = max(journal['indexes'].get(sha, at), at)
    journal['updatedAt'] = at
    write_journal(out, store, journal)
    return journal


def enable_gc(out, now=None) -> dict:
    """Deliberate one-time opt-in (never called automatically): initializes the journal - protecting the current
    index as of now - and then creates the enabled marker. Idempotent for an already valid journal."""
    out, store = check_owned_out(out)
    now = time.time() if now is None else now
    if _regular(out / JOURNAL_NAME):
        journal = load_journal(out, store)
    else:
        journal = {'updatedAt': now, 'indexes': {}}
        current = cb._current_index(store)
        if current is not None:
            journal['indexes'][current] = now
        write_journal(out, store, journal)
    if not _regular(out / ENABLED_MARKER):
        with open(out / ENABLED_MARKER, 'x', encoding='utf-8') as stream:
            stream.write('collision GC enabled deliberately; publications are journaled\n')
    return journal


def _journal_closure(store: Path, protected) -> set:
    """Relative paths of recently published indexes and their whole closure (pairs and objects). A protected index
    or any part of its closure that is missing or damaged refuses the run."""
    reader = cr._Reader(store)
    paths = set()
    try:
        for sha in sorted(protected):
            paths.add(f'indexes/{sha}.json')
            for key, pair in cr._index_pairs(reader, sha).items():
                paths.add(f'pairs/{pair}.json')
                for name, object_sha, kind, x, z in cr._pair_objects(reader, pair, key):
                    try:
                        (cb._verify_blocks if kind == 'blocks' else cb._verify_geometry)(store, object_sha, x, z)
                    except (OSError, ValueError) as error:
                        raise GcRefused(f'object {name} of recently published index {sha} failed verification: '
                                        f'{error}') from None
                    paths.add(f'objects/{name}')
    except cr.RetentionRefused as error:
        raise GcRefused(f'recently published index unusable: {error}') from None
    return paths


# ------------------------------------------------------------------ remaining references

def _listing(store: Path, folder: str) -> set:
    path = store / folder
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return set()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _Stop(f'{folder} is not a real directory')
    return {name for name in os.listdir(path) if cr.FOLDERS[folder].fullmatch(name)}


def _still_referenced(store: Path, phase: str, known: dict) -> set:
    """Names a remaining file still refers to: pairs referenced by any remaining index (pair phase), objects
    referenced by any remaining pair (object phase). Unknown new files or unreadable ones stop the run."""
    reader = cr._Reader(store)
    for folder in ('indexes', 'pairs'):
        new = _listing(store, folder) - known[folder]
        if new:
            raise _Stop(f'{folder} gained files unknown to the plan (another writer): {sorted(new)[:3]}')
    try:
        if phase == 'pairs':
            refs = set()
            for name in sorted(_listing(store, 'indexes')):
                refs.update(f'{p}.json' for p in cr._index_pairs(reader, name[:-5]).values())
            return refs
        refs = set()
        for name in sorted(_listing(store, 'pairs')):
            refs.update(obj for obj, *_ in cr._pair_objects(reader, name[:-5]))
        return refs
    except cr.RetentionRefused as error:
        raise _Stop(f'remaining {phase} references unreadable: {error}') from None


# ------------------------------------------------------------------ run

def gc_locked(out, *, previous_index=None, now=None, min_unreferenced_seconds=MIN_UNREFERENCED_SECONDS,
              max_delete_files=DEFAULT_MAX_DELETE_FILES, max_delete_bytes=DEFAULT_MAX_DELETE_BYTES,
              unlink=os.unlink) -> dict:
    """One GC run. CALLER CONTRACT: out/.export.lock is held exclusively for the whole call (run_gc does it)."""
    if (not isinstance(min_unreferenced_seconds, (int, float)) or not math.isfinite(min_unreferenced_seconds)
            or min_unreferenced_seconds < MIN_UNREFERENCED_SECONDS):
        raise ValueError(f'min_unreferenced_seconds must be at least {MIN_UNREFERENCED_SECONDS}')
    if not isinstance(max_delete_files, int) or not 1 <= max_delete_files <= MAX_DELETE_FILES_LIMIT:
        raise ValueError(f'max_delete_files must be 1..{MAX_DELETE_FILES_LIMIT}')
    if not isinstance(max_delete_bytes, int) or max_delete_bytes < 0:
        raise ValueError('max_delete_bytes must be a non-negative integer')
    now = time.time() if now is None else now
    result = {'format': RESULT_FORMAT, 'state': None, 'reason': None, 'at': now, 'pointerSha256': None,
              'candidates': 0, 'newlyObserved': 0, 'eligible': 0, 'deleted': [], 'deletedBytes': 0,
              'skipped': {'fileBudget': 0, 'byteBudget': 0, 'referencedByRemaining': 0}, 'ledgerEntries': None,
              'ledgerWritten': False, 'journalProtected': None, 'journalEntries': None}
    try:
        out, store = check_owned_out(out)
        if os.path.lexists(out / '.uie-collision-world-gc'):
            raise GcRefused('world GC profile requires collision_world_gc, not legacy GC')
        _check_enabled(out)
        journal = load_journal(out, store)
        ledger_path = out / STATE_NAME
        ledger = load_ledger(ledger_path, store)
        try:
            plan = cr.plan_retention(store, previous_index=previous_index, now=now, verify_objects=True)
        except cr.RetentionRefused as error:
            raise GcRefused(f'retention plan refused: {error}') from None
        pointer = plan['pointerSha256']
        result['pointerSha256'] = pointer
        # Indexes published (as outgoing or incoming) within the protection period keep their whole closure, even
        # when their file mtime is old (B -> A -> B between two GC runs). Future timestamps count as recent.
        protected = {sha for sha, at in journal['indexes'].items() if now - at < min_unreferenced_seconds}
        shielded = _journal_closure(store, protected)
        result['journalProtected'] = len(protected)
        candidates = {c['path']: c for c in plan['candidates'] if c['path'] not in shielded}
        entries = {}
        for rel, c in candidates.items():
            old = ledger.get(rel)
            if old is not None and _identity(old) == _identity(c):
                first = min(old['firstSeen'], now)         # a future firstSeen (clock skew) never ages a file
            else:
                first = now                                # first observation, or a different file at that path
                result['newlyObserved'] += 1
            entries[rel] = {'firstSeen': first, 'size': c['size'], 'mtimeNs': c['mtimeNs'], 'ino': c['ino'],
                            'dev': c['dev']}
        result['candidates'] = len(candidates)
        write_ledger(ledger_path, store, entries, now, pointer)          # observations persist before deleting
        result['ledgerWritten'] = True
        eligible = sorted((rel for rel, e in entries.items() if now - e['firstSeen'] >= min_unreferenced_seconds),
                          key=lambda rel: (PHASES.index(rel.split('/')[0]), rel))
        result['eligible'] = len(eligible)
        known = {folder: {p.split('/')[1] for p in plan['keep'] + [c['path'] for c in plan['candidates']]
                          if p.startswith(folder + '/')} for folder in PHASES}
        try:
            for phase in PHASES:
                todo = [rel for rel in eligible if rel.startswith(phase + '/')]
                if not todo:
                    continue
                protected = _still_referenced(store, phase, known) if phase != 'indexes' else set()
                for rel in todo:
                    name = rel.split('/')[1]
                    c = candidates[rel]
                    if len(result['deleted']) >= max_delete_files:
                        result['skipped']['fileBudget'] += 1
                        continue
                    if result['deletedBytes'] + c['size'] > max_delete_bytes:
                        result['skipped']['byteBudget'] += 1
                        continue
                    if name in protected:
                        result['skipped']['referencedByRemaining'] += 1
                        continue
                    _check_before_unlink(store, rel, c, pointer)
                    try:
                        unlink(store / phase / name)
                    except OSError as error:
                        raise _Stop(f'could not delete {rel}: {error}') from None
                    result['deleted'].append(rel)
                    result['deletedBytes'] += c['size']
                    entries.pop(rel, None)
                    known[phase].discard(name)
            result['state'] = 'done'
        except _Stop as error:
            result.update(state='stopped', reason=str(error)[:300])
        try:
            write_ledger(ledger_path, store, entries, now, pointer)
        except (OSError, GcRefused) as error:
            result['ledgerWritten'] = False
            result['reason'] = (result['reason'] or '') + f' ledger not rewritten: {error}'[:300]
        result['ledgerEntries'] = len(entries)
        # Journal entries are dropped only when their protection expired AND their index file is gone.
        expired = [sha for sha, at in journal['indexes'].items()
                   if now - at >= min_unreferenced_seconds and not (store / 'indexes' / f'{sha}.json').exists()]
        if expired:
            for sha in expired:
                del journal['indexes'][sha]
            try:
                write_journal(out, store, journal)
            except (OSError, GcRefused) as error:
                result['reason'] = (result['reason'] or '') + f' journal not pruned: {error}'[:300]
        result['journalEntries'] = len(journal['indexes'])
    except GcRefused as error:
        result.update(state='refused', reason=str(error)[:300])
    return result


def _check_before_unlink(store: Path, rel: str, candidate: dict, pointer: str):
    try:
        current, _index = cr._read_pointer(store)
    except cr.RetentionRefused as error:
        raise _Stop(f'pointer unreadable before deleting {rel}: {error}') from None
    if current != pointer:
        raise _Stop('current.json changed after planning')
    if not CANDIDATE_PATH.fullmatch(rel):
        raise _Stop(f'unexpected path {rel!r}')
    folder, name = rel.split('/')
    try:
        parent = os.lstat(store / folder)
    except OSError as error:
        raise _Stop(f'{folder} unavailable: {error}') from None
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise _Stop(f'{folder} is not a real directory')
    try:
        info = os.lstat(store / folder / name)
    except FileNotFoundError:
        raise _Stop(f'{rel} disappeared after planning') from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _Stop(f'{rel} is not a regular file')
    if (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev) != _identity(candidate):
        raise _Stop(f'{rel} changed after planning')


def run_gc(out, *, lock=default_export_lock, **options) -> dict:
    """gc_locked under the exporter's exclusive lock; busy -> state 'busy' and nothing read or written."""
    try:
        out_path, _store = check_owned_out(out)
        handle = lock(out_path)
    except GcRefused as error:
        return {'format': RESULT_FORMAT, 'state': 'refused', 'reason': str(error)[:300]}
    if handle is None:
        return {'format': RESULT_FORMAT, 'state': 'busy', 'reason': 'the exporter holds out/.export.lock'}
    try:
        return gc_locked(out_path, **options)
    finally:
        handle.close()
