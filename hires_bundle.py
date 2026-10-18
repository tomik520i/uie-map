"""Private, bounded immutable hires store prototype; no CLI, GC, renderer or HTTP integration.

Caller holds the exclusive out/.build.lock across ingest_* and publish_index. `out` must be an
absolute normalized real directory with a regular `.uie-hires-bundle-owned` marker. Sources
must be stable regular files on the same filesystem and may only change by atomic replacement.
No copying fallback is performed when hardlinks are unavailable.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zlib

import world_hires as wh

OWNED_MARKER = '.uie-hires-bundle-owned'
INDEX_FORMAT = 'UIE_HIRES_IMMUTABLE_INDEX1'
INDEX_FORMAT_V2 = 'UIE_HIRES_IMMUTABLE_INDEX2'
INDEX_FORMAT_V3 = 'UIE_HIRES_IMMUTABLE_INDEX3'
POINTER_FORMAT = 'UIE_HIRES_POINTER1'
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_UIH_RAW = 256 * 1024 * 1024
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_INDEX_ENTRIES = 100_000
HEX64 = re.compile(r'[0-9a-f]{64}')
TILE_KEY = re.compile(r'(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})')
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class BundleError(ValueError):
    pass


class WarmupPending(BundleError):
    """Safe to retry in the SAME process with the same verification cache; no pointer committed."""


def _real_dir(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise BundleError(f'{path} is not a real directory')


def _owned(out):
    out = Path(out)
    if not out.is_absolute() or out.resolve() != out:
        raise BundleError('output must be absolute, normalized and without symlink ancestors')
    _real_dir(out)
    marker = out / OWNED_MARKER
    if not stat.S_ISREG(os.lstat(marker).st_mode):
        raise BundleError('ownership marker is not a regular file')
    return out


def _folder(out, name):
    path = out / name
    try:
        path.mkdir()
    except FileExistsError:
        pass
    _real_dir(path)
    return path


def _regular(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise BundleError(f'{path} is not a regular file')
    return info


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read(path, limit):
    info = _regular(path)
    if info.st_size < 1 or info.st_size > limit:
        raise BundleError(f'{path.name} exceeds the size limit or is empty')
    with open(path, 'rb') as stream:
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(data) != info.st_size or _identity(info) != _identity(after):
        raise BundleError(f'{path.name} changed during reading')
    return data, info


def _tile_coords(key):
    match = TILE_KEY.fullmatch(key) if isinstance(key, str) else None
    if not match:
        raise BundleError(f'invalid tile key {key!r}')
    tx, tz = int(match[1]), int(match[2])
    if abs(tx) > 30_000_000 // wh.TILE or abs(tz) > 30_000_000 // wh.TILE:
        raise BundleError('tile outside world border')
    return tx, tz


def _verify_tile(data, key):
    tx, tz = _tile_coords(key)
    inflater = zlib.decompressobj(wbits=31)
    head = bytearray()
    total = 0
    try:
        for offset in range(0, len(data), 65536):
            block = data[offset:offset + 65536]
            while block:
                raw = inflater.decompress(block, min(65536, MAX_UIH_RAW - total + 1))
                total += len(raw)
                if len(head) < wh.HEADER.size:
                    head.extend(raw[:wh.HEADER.size - len(head)])
                if total > MAX_UIH_RAW:
                    raise BundleError('UIH1 decompressed size exceeds limit')
                block = inflater.unconsumed_tail
        if not inflater.eof or inflater.unused_data:
            raise BundleError('UIH1 gzip is truncated or has trailing bytes')
    except zlib.error as error:
        raise BundleError(f'invalid UIH1 gzip: {error}') from None
    if len(head) != wh.HEADER.size:
        raise BundleError('UIH1 header is missing')
    magic, version, flags, x, z, opaque, translucent = wh.HEADER.unpack(head)
    if (magic, version, flags, x, z) != (wh.MAGIC, 1, 0, tx * wh.TILE, tz * wh.TILE):
        raise BundleError('UIH1 header does not match tile')
    if opaque + translucent == 0 or total != wh.HEADER.size + (opaque + translucent) * 52:
        raise BundleError('UIH1 payload length/count mismatch')


def _verify_png(data):
    if not data.startswith(PNG_SIGNATURE) or len(data) < 24:
        raise BundleError('atlas is not a PNG')


def _fsync_dir(path):
    if os.name != 'posix':
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard_created(target, identity):
    """Remove a content-addressed name this ingest just linked, only while it is still exactly that inode.

    A pre-existing object is never passed here; a name replaced since the link stays. Cleanup errors are swallowed so
    the caller re-raises its original error; a leftover name still fails closed (re-verified on ingest and publish)."""
    try:
        if _identity(os.lstat(target)) != identity:
            return
        os.unlink(target)
        _fsync_dir(target.parent)
    except OSError:
        pass


def _ingest(out, source, folder, suffix, verify, key=None):
    out = _owned(out)
    dest_dir = _folder(out, folder)
    source = Path(source)
    raw, source_info = _read(source, MAX_OBJECT_BYTES)
    verify(raw, key) if key is not None else verify(raw)
    sha = hashlib.sha256(raw).hexdigest()
    target = dest_dir / f'{sha}.{suffix}'
    if source_info.st_dev != os.stat(dest_dir).st_dev:
        raise BundleError('source and object store are on different filesystems')
    created = True
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        created = False
    except OSError as error:
        raise BundleError(f'hardlink unavailable: {error}') from error
    created_identity = _identity(os.lstat(target)) if created else None
    try:
        # Hash the captured link, not the mutable source name: a concurrent rename cannot substitute the content.
        linked, linked_info = _read(target, MAX_OBJECT_BYTES)
        if hashlib.sha256(linked).hexdigest() != sha or linked != raw:
            raise BundleError('immutable object differs from its content address')
        if _identity(_regular(source)) != _identity(source_info) or (created and linked_info.st_ino != source_info.st_ino):
            raise BundleError('source changed while it was linked')
    except BaseException:
        if created:
            _discard_created(target, created_identity)
        raise
    # Existing objects may be a different inode with the same content; a newly created link must share the inode.
    if os.name == 'posix':
        with open(target, 'rb') as stream:
            os.fsync(stream.fileno())
    _fsync_dir(dest_dir)
    return sha


def ingest_tile(out, source, key):
    """Hardlink a UIH1 tile from a stable regular source; return its full SHA-256."""
    _tile_coords(key)
    return _ingest(out, source, 'objects', 'uih', _verify_tile, key)


def ingest_atlas(out, source):
    """Hardlink a bounded PNG atlas; return its full SHA-256."""
    return _ingest(out, source, 'atlases', 'png', _verify_png)


def _verify_layout(raw):
    if len(raw)>MAX_INDEX_BYTES:raise BundleError('atlas layout exceeds size limit')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise BundleError('duplicate atlas layout key')
            result[key]=value
        return result
    def constant(_):raise BundleError('nonfinite atlas layout value')
    try:doc=json.loads(raw.decode('utf-8'),object_pairs_hook=unique,parse_constant=constant)
    except (ValueError,UnicodeError) as error:raise BundleError(f'invalid atlas layout: {error}') from None
    if not isinstance(doc,dict) or doc.get('format')!='UIE_ATLAS_LAYOUT1' or not isinstance(doc.get('registered'),dict):
        raise BundleError('invalid atlas layout schema')


def ingest_layout(out,source):
    """Capture the exact atlas layout, with a bounded schema check and content address."""
    if _regular(source).st_size>MAX_INDEX_BYTES:raise BundleError('atlas layout exceeds size limit')
    return _ingest(out,source,'layouts','json',_verify_layout)


def _atomic(path, raw):
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _immutable_index(path, raw):
    if path.exists():
        existing, _ = _read(path, MAX_INDEX_BYTES)
        if existing != raw:
            raise BundleError('immutable index differs')
        # A prior publication may have stopped after creating this name but before the directory sync.
        if os.name == 'posix':
            with open(path, 'rb') as stream:
                os.fsync(stream.fileno())
        _fsync_dir(path.parent)
        return
    fd, tmp = tempfile.mkstemp(prefix='.index.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            existing, _ = _read(path, MAX_INDEX_BYTES)
            if existing != raw:
                raise BundleError('immutable index differs')
        _fsync_dir(path.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)


class VerificationCache:
    """Process-local content proofs, never loaded from disk or accepted from a ledger.

    POSIX only: ctime detects in-place changes even when mtime is restored. Every cache miss
    performs full hash + UIH validation; path/key/hash and dev/inode/size/mtime/ctime must all
    match to reuse it. Caller holds the exclusive build lock. Not protection against a writer
    bypassing that lock, kernel/filesystem corruption, or privileged timestamp manipulation.
    Restart loses the cache and requires a fresh warmup. No geometry bytes are retained.
    """
    def __init__(self, max_entries=MAX_INDEX_ENTRIES):
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_INDEX_ENTRIES:
            raise ValueError('invalid verification cache capacity')
        self.max_entries = max_entries
        self._proofs = {}

    def retain(self, paths):
        self._proofs = {k: v for k, v in self._proofs.items() if k in paths}

    def verify(self, path, key, sha, allow_read=True, max_read_bytes=None):
        identity = lambda info: (*_identity(info), info.st_ctime_ns)
        token = (str(path), key, sha)
        before = identity(_regular(path))
        if os.name == 'posix' and self._proofs.get(token) == before:
            return False
        self._proofs.pop(token, None)
        if not allow_read:
            raise WarmupPending('verification warmup pending: per-call tile read budget exhausted')
        if max_read_bytes is not None and before[2] > max_read_bytes:
            raise WarmupPending('verification warmup pending: per-call tile byte budget exhausted')
        raw, info = _read(path, MAX_OBJECT_BYTES)
        if identity(info) != before or identity(_regular(path)) != before:
            raise BundleError('object changed during verification')
        if hashlib.sha256(raw).hexdigest() != sha:
            raise BundleError(f'object hash mismatch for {key}')
        _verify_tile(raw, key)
        if identity(_regular(path)) != before:
            raise BundleError('object changed during verification')
        if os.name == 'posix':
            if len(self._proofs) >= self.max_entries:
                raise BundleError('verification cache capacity exhausted')
            self._proofs[token] = before
        return len(raw)


_FORMAT_RANK = {INDEX_FORMAT: 1, INDEX_FORMAT_V2: 2, INDEX_FORMAT_V3: 3}


def _strict_json(raw, what):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BundleError(f'duplicate key in {what}')
            result[key] = value
        return result

    def constant(_):
        raise BundleError(f'non-finite value in {what}')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)
    except BundleError:
        raise
    except (UnicodeError, ValueError) as error:
        raise BundleError(f'invalid {what} JSON: {error}') from None


def _checked_context(context):
    from hires_context import validate_context      # lazy: no import cycle, no module-level dependency
    try:
        return validate_context(context)
    except BundleError:
        raise
    except ValueError as error:
        raise BundleError(f'invalid hires context: {error}') from None


def _checked_source(source, tiles):
    from hires_source import validate_source
    try:
        return validate_source(source, tiles)
    except BundleError:
        raise
    except ValueError as error:
        raise BundleError(f'invalid hires source metadata: {error}') from None


def _current_index(out):
    """(indexSha256, format, context) of the index current.json names; None only when current.json is absent.

    Reads the small pointer and that one index once: exact SHA-256 of its bytes, strict V1/V2/V3 schema and the
    publisher's canonical JSON. Anything else fails closed (BundleError); tile objects are not read."""
    try:
        raw, _ = _read(out/'current.json', 4096)
    except FileNotFoundError:
        return None
    pointer = _strict_json(raw, 'current pointer')
    if (not isinstance(pointer, dict) or set(pointer) != {'format', 'indexSha256'}
            or pointer['format'] != POINTER_FORMAT or not isinstance(pointer['indexSha256'], str)
            or not HEX64.fullmatch(pointer['indexSha256'])):
        raise BundleError('invalid current pointer')
    sha = pointer['indexSha256']
    try:
        _real_dir(out/'indexes')
        raw, _ = _read(out/'indexes'/f'{sha}.json', MAX_INDEX_BYTES)
    except FileNotFoundError:
        raise BundleError('current index is missing') from None
    if hashlib.sha256(raw).hexdigest() != sha:
        raise BundleError('current index hash mismatch')
    doc = _strict_json(raw, 'current index')
    rank = _FORMAT_RANK.get(doc.get('format')) if isinstance(doc, dict) and isinstance(doc.get('format'),str) else None
    if rank is None:
        raise BundleError('current index has an unknown format')
    fields = {'format', 'tileSize', 'rendererIdentity', 'atlasSha256', 'tiles'}
    fields |= {'context'} if rank >= 2 else set()
    fields |= {'source'} if rank == 3 else set()
    if (set(doc) != fields or type(doc['tileSize']) is not int or doc['tileSize'] != wh.TILE
            or not isinstance(doc['rendererIdentity'], str) or not HEX64.fullmatch(doc['rendererIdentity'])
            or not isinstance(doc['atlasSha256'], str) or not HEX64.fullmatch(doc['atlasSha256'])
            or not isinstance(doc['tiles'], dict) or len(doc['tiles']) > MAX_INDEX_ENTRIES):
        raise BundleError('invalid current index schema')
    for key, object_sha in doc['tiles'].items():
        _tile_coords(key)
        if not isinstance(object_sha, str) or not HEX64.fullmatch(object_sha):
            raise BundleError('invalid current index reference')
    context = _checked_context(doc['context']) if rank >= 2 else None
    if rank == 3:
        _checked_source(doc['source'], doc['tiles'])
    if raw != json.dumps(doc, sort_keys=True, separators=(',', ':')).encode():
        raise BundleError('current index is not canonical JSON')
    return sha, doc['format'], context


def _journal_state(out, history):
    """(currentIndexSha256, previousIndexSha256) of the schema-valid publication journal, None when it is absent.

    hires_history._read_journal does the strict schema check (corrupt or unreadable -> BundleError); the small
    journal is then read once more for its two root fields and must still carry the same records."""
    records = history._read_journal(out)
    try:
        raw, _ = _read(out/history.NAME, history.MAX_JOURNAL_BYTES)
    except FileNotFoundError:
        if records:
            raise BundleError('publication history disappeared during read') from None
        return None
    doc = history._json(raw)
    if not isinstance(doc, dict) or doc.get('lastPublished') != records:
        raise BundleError('publication history changed during read')
    return doc['currentIndexSha256'], doc['previousIndexSha256']


def _check_monotonic(current, index_format, context):
    """A store never loses metadata (V3 -> V2/V1, V2 -> V1) and never changes world/dimension. Upgrades pass."""
    if current is None:
        return
    _, current_format, current_context = current
    if _FORMAT_RANK[index_format] < _FORMAT_RANK[current_format]:
        raise BundleError(f'cannot downgrade the store from {current_format} to {index_format}')
    if current_context is not None and any(current_context[k] != context[k] for k in ('world', 'dimension')):
        raise BundleError('cannot change world/dimension of a store')


def publish_index(out, tiles, atlas_sha256, renderer_identity, *, verification_cache=None, max_tile_reads=None,
                  max_tile_bytes=None, context=None, source=None, record_history=False):
    """Verify every immutable reference, publish an immutable index, then atomically replace current.json.

    The caller holds out/.build.lock. Failure before pointer replacement leaves the old pointer unchanged.
    No alias, legacy index, GC or renderer state is touched.
    Optional process-local VerificationCache avoids re-reading unchanged verified tiles on POSIX.
    max_tile_reads bounds misses; exhaustion preserves both the old pointer and in-memory proofs
    for the next call. No persistent proof cache. Default is full verification as before.
    """
    out = _owned(out)
    if type(record_history) is not bool:raise ValueError('record_history must be boolean')
    from dimension_binding import check_context
    check_context(out,context)
    if source is not None:
        if context is None:raise ValueError('source metadata requires context')
        source=_checked_source(source,tiles)
    if context is not None:
        context=_checked_context(context)
        _real_dir(out/'layouts')
        layout,_=_read(out/'layouts'/f'{context["layoutSha256"]}.json',MAX_INDEX_BYTES)
        if hashlib.sha256(layout).hexdigest()!=context['layoutSha256']:raise BundleError('atlas layout hash mismatch')
        _verify_layout(layout)
    if not isinstance(tiles, dict) or len(tiles) > MAX_INDEX_ENTRIES:
        raise BundleError('too many tiles or invalid tiles mapping')
    if verification_cache is not None and type(verification_cache) is not VerificationCache:
        raise ValueError('verification_cache must be a process-local VerificationCache')
    if max_tile_reads is not None and (verification_cache is None or os.name != 'posix'
            or type(max_tile_reads) is not int or not 0 <= max_tile_reads <= MAX_INDEX_ENTRIES):
        raise ValueError('bounded warmup requires a POSIX verification cache and a valid tile read budget')
    if max_tile_bytes is not None and (verification_cache is None or os.name != 'posix'
            or type(max_tile_bytes) is not int or not 1 <= max_tile_bytes <= 2*1024**3):
        raise ValueError('bounded warmup requires a POSIX verification cache and a valid tile byte budget')
    for key, sha in tiles.items():
        _tile_coords(key)
        if not isinstance(sha, str) or not HEX64.fullmatch(sha):
            raise BundleError(f'invalid object hash for {key}')
    if verification_cache is not None:
        if len(tiles) > verification_cache.max_entries:
            raise BundleError('map exceeds verification cache capacity')
        verification_cache.retain({(str(out/'objects'/f'{sha}.uih'), key, sha) for key, sha in tiles.items()})
    if not isinstance(atlas_sha256, str) or not HEX64.fullmatch(atlas_sha256):
        raise BundleError('invalid atlas hash')
    if not isinstance(renderer_identity, str) or not HEX64.fullmatch(renderer_identity):
        raise BundleError('invalid renderer identity')
    # Before any object verification or write: the current generation (exact hash, strict schema) must not lose
    # metadata or change world/dimension. A store without current.json accepts any format (first V1 snapshot).
    index_format = INDEX_FORMAT_V3 if source is not None else INDEX_FORMAT_V2 if context is not None else INDEX_FORMAT
    current = _current_index(out)
    _check_monotonic(current, index_format, context)
    _real_dir(out/'objects')
    _real_dir(out/'atlases')
    if (out/'indexes').exists():
        _real_dir(out/'indexes')
    atlas, _ = _read(out/'atlases'/f'{atlas_sha256}.png', MAX_OBJECT_BYTES)
    if hashlib.sha256(atlas).hexdigest() != atlas_sha256:
        raise BundleError('atlas object hash mismatch')
    _verify_png(atlas)
    reads = 0
    read_bytes = 0
    for key, sha in tiles.items():
        _tile_coords(key)
        if not isinstance(sha, str) or not HEX64.fullmatch(sha):
            raise BundleError(f'invalid object hash for {key}')
        if verification_cache is not None:
            used = verification_cache.verify(out/'objects'/f'{sha}.uih', key, sha,
                                             max_tile_reads is None or reads < max_tile_reads,
                                             None if max_tile_bytes is None else max_tile_bytes-read_bytes)
            reads += bool(used)
            read_bytes += used
            continue
        raw, _ = _read(out/'objects'/f'{sha}.uih', MAX_OBJECT_BYTES)
        if hashlib.sha256(raw).hexdigest() != sha:
            raise BundleError(f'object hash mismatch for {key}')
        _verify_tile(raw, key)
    doc = {'format': index_format, 'tileSize': wh.TILE, 'rendererIdentity': renderer_identity,
            'atlasSha256': atlas_sha256, 'tiles': tiles}
    if context is not None:
        doc.update(context=context)
    if source is not None:
        doc.update(source=source)
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > MAX_INDEX_BYTES:
        raise BundleError('index too large')
    sha = hashlib.sha256(raw).hexdigest()
    indexes = _folder(out, 'indexes')
    _immutable_index(indexes/f'{sha}.json', raw)
    if current is not None and current[0] == sha:
        # Republishing the verified current index (all checks above still ran): current.json stays byte/inode
        # identical; a valid journal already naming it as current stays untouched, so its previous is kept.
        if record_history:
            import hires_history as history
            journal = _journal_state(out, history)          # corrupt journal: BundleError (fail closed)
            if journal is None or journal[0] != sha:
                # Absent journal, or a candidate recorded before a crash that never became current: repair it
                # conservatively, keeping the journal's other generation as previous.
                previous = next((s for s in (journal or ()) if s is not None and s != sha), None)
                history.record_publication(out, sha, previous)
        # A pointer renamed by an earlier call that stopped before its directory sync becomes durable now.
        _fsync_dir(out)
        return {'indexSha256': sha, 'entries': len(tiles), 'unchanged': True}
    if record_history:
        import hires_history as history
        # The pointer was validated above under the same caller-held lock; it is not read a second time.
        previous_sha=current[0] if current is not None else None
        # Crash after this write merely retains extra references; a failed journal never advances current.
        history.record_publication(out,sha,previous_sha)
    pointer = json.dumps({'format': POINTER_FORMAT, 'indexSha256': sha}, sort_keys=True, separators=(',', ':')).encode()
    _atomic(out/'current.json', pointer)
    return {'indexSha256': sha, 'entries': len(tiles), 'unchanged': False}
