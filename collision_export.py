"""Offline collision exporter for an explicit selection of hires tiles (bounded; never the whole world).

  python3 collision_export.py --world W --catalog-dir C --hires-store H --out O --tile 1,-2 [--tile 2,-2 ...]
                              [--max-tiles 4] [--budget-mib 512]

For each selected 64 x 64 tile, sequentially: copy the region files of the tile input (core + 16 block halo) to a
private scratch directory (stable copies, the live world is only read), decode strictly (an unreadable chunk
anywhere in the input fails the tile; it is never treated as air), build ONE Volume, mesh it and encode the UIB1
blocks from the same Volume. The pair is published only when its geometry hash equals the tile hash in the CURRENT
hires index (also re-read after the computation) and the catalog atlas equals the hires atlas. A tile the hires
index does not list is unknown here and skipped (no empty pair is invented). A tile the hires index marks stale
never matches. A previous pair is reused without rendering only when it matches the current hires geometry + atlas
AND its referenced objects verify (collision_bundle's identity/header checks); a damaged pair is reported, a missing
object is rewritten by re-rendering, a differing existing object is never overwritten (the tile fails).
Region copies are bounded (copy_regions_bounded): source sizes are reserved against the budget and the free space
floor before anything is copied, and growth during a copy is detected instead of copied.

Then the accepted pairs are merged into the previous collision index; previous entries that no longer match the
current hires index, or whose objects no longer verify, are not declared current (they stay stored). The merged index is published with
collision_bundle.publish_index, which verifies every referenced object and replaces current.json last, so any
failure keeps the previous index.

Layout of O (everything here is owned output and counts against the budget):
  store/          collision store (pairs/, objects/, indexes/, current.json) -> server UIE_COLLISION_STORE
  status.json     UIE_COLLISION_EXPORT_STATUS1 of the last run
  provenance.json UIE_COLLISION_PROVENANCE1 {tiles: {"tx,tz": {source, pair}}}: the source input revision that
                  produced each CURRENT pair; written atomically only after the index is current. The CLI reuses a
                  pair only with a matching entry (a hidden block change under an unchanged surface re-renders).
  .export.lock    exclusive non-blocking lock (Linux)
  .scratch-*      temporary region copies of one tile, removed after the tile

Limits: at most 128 tiles may be selected; at most --max-tiles (default 4, at most 32) are processed per run, the
rest is reported as deferred. Budget (default 512 MiB) covers all of O (store, metadata and scratch region copies)
and is checked before any work (with an estimate), before the region copies and before each publication (with the
exact new bytes), always keeping a metadata reserve (status, index, provenance and their atomic temporaries); free
space must stay >= 20 GiB. No Minecraft,
no deletion of existing data, no scheduler.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

import collision_bundle as cb
import collision_tile as ct
import world_hires as wh

STATUS_FORMAT = 'UIE_COLLISION_EXPORT_STATUS1'
MIB = 1024 * 1024
DEFAULT_MAX_TILES = 4
MAX_TILES_PER_RUN = 32
MAX_SELECTION = 128
DEFAULT_BUDGET_BYTES = 512 * MIB
MIN_FREE_BYTES = 20 * 1024 * MIB
ESTIMATE_TILE_BYTES = 2 * MIB          # pre-work estimate per tile (measured: ~0.45 MiB per real tile)
INDEX_ENTRY_BYTES = 85                 # worst canonical index entry (collision_bundle)
MAX_ERRORS = 32
MAX_MESSAGE = 300
SHA12 = re.compile(r'[0-9a-f]{12}')
HIRES_INDEX_LIMIT = 64 * MIB
# Source provenance (out/provenance.json): which source input revision produced which current pair.
PROVENANCE_FORMAT = 'UIE_COLLISION_PROVENANCE1'
SOURCE_FORMAT = 'UIE_COLLISION_SOURCE1'
PROVENANCE_ENTRY_BYTES = 200           # '"-468750,-468750":{"pair":"<64>","source":"<64>"},' is ~170 bytes
PROVENANCE_LIMIT = 32 * MIB
# Metadata headroom (status, index, provenance and their atomic temporaries) reserved inside the budget.
METADATA_BASE_BYTES = 64 * 1024


class ExportError(Exception):
    """The run is refused as a whole (nothing new is published)."""


class TileError(Exception):
    """One tile cannot be exported (it is reported; previous good data stays)."""


class CopyBudgetError(TileError):
    """The stable region copies of a tile would exceed the budget or the free space floor: the run stops cleanly."""


# ------------------------------------------------------------------ selection and paths

def parse_tile(text: str) -> tuple[int, int]:
    m = cb.TILE_KEY.fullmatch(text) if isinstance(text, str) else None
    if not m:
        raise ExportError(f'invalid tile {text!r} (expected tx,tz)')
    tx, tz = int(m.group(1)), int(m.group(2))
    if abs(tx) > cb.MAX_TILE_COORD or abs(tz) > cb.MAX_TILE_COORD:
        raise ExportError(f'tile {text} is outside the world')
    return tx, tz


def select_tiles(texts, max_tiles=DEFAULT_MAX_TILES):
    """(tiles processed this run, deferred tiles), both in the given order without duplicates."""
    if not texts:
        raise ExportError('an explicit --tile selection is required')
    if len(texts) > MAX_SELECTION:
        raise ExportError(f'at most {MAX_SELECTION} tiles may be selected')
    if not isinstance(max_tiles, int) or not 1 <= max_tiles <= MAX_TILES_PER_RUN:
        raise ExportError(f'--max-tiles must be 1..{MAX_TILES_PER_RUN}')
    tiles = list(dict.fromkeys(parse_tile(t) for t in texts))
    return tiles[:max_tiles], tiles[max_tiles:]


def validate_active_keys(active_keys, selection, deferred):
    """Canonical explicit active window, or None for the legacy keep-all merge. Validate before output writes."""
    if active_keys is None:
        return None
    if isinstance(active_keys, (str, bytes)):
        raise ExportError('active keys must be a nonempty collection of tile keys')
    try:
        keys = list(active_keys)
    except TypeError:
        raise ExportError('active keys must be a nonempty collection of tile keys') from None
    if not keys:
        raise ExportError('active window must contain 1..32 distinct tile keys')
    active = set()
    for key in keys:
        tx, tz = parse_tile(key)
        active.add(f'{tx},{tz}')
    if len(active) > 32:
        raise ExportError('active window must contain 1..32 distinct tile keys')
    requested = {f'{tx},{tz}' for tx, tz in list(selection) + list(deferred)}
    if not requested <= active:
        raise ExportError(f'selection/deferred tiles outside active window: {sorted(requested - active)}')
    return active


def check_paths(out: Path, **inputs: Path) -> Path:
    """The output must lie outside every input (world, hires store, catalog) and must not contain one."""
    out = Path(out).resolve()
    for name, path in inputs.items():
        p = Path(path).resolve()
        if out == p or out.is_relative_to(p) or p.is_relative_to(out):
            raise ExportError(f'--out must lie outside the {name} ({p})')
    return out


def dir_bytes(path: Path) -> int:
    total = 0
    for p in Path(path).rglob('*'):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _iso(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec='seconds')


def _message(error) -> str:
    text = error if isinstance(error, str) else f'{type(error).__name__}: {error}'
    return text[:MAX_MESSAGE]


# ------------------------------------------------------------------ indexes and pairs

def read_hires_index(hires_store: Path) -> dict:
    """{'atlasSha256', 'tiles': {key: sha12}, 'stale': set} of hires_store/index.json (world_hires output)."""
    path = Path(hires_store) / 'index.json'
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(HIRES_INDEX_LIMIT + 1)
        if len(raw) > HIRES_INDEX_LIMIT:
            raise ExportError('hires index is too large')
        doc = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ExportError(f'hires index unreadable: {_message(error)}') from None
    if (not isinstance(doc, dict) or doc.get('format') != wh.FORMAT_INDEX or doc.get('tileSize') != wh.TILE
            or not isinstance(doc.get('tiles'), dict)):
        raise ExportError('hires index is not a UIE_HIRES_INDEX1 with 64 block tiles')
    atlas = doc.get('atlasSha256')
    if not isinstance(atlas, str) or not cb.HEX64.fullmatch(atlas):
        raise ExportError('hires index has no valid atlasSha256')
    tiles = {}
    for key, sha in doc['tiles'].items():
        if not cb.TILE_KEY.fullmatch(key) or not isinstance(sha, str) or not SHA12.fullmatch(sha):
            raise ExportError(f'hires index has an invalid tile {key!r}')
        tiles[key] = sha
    stale = doc.get('stale', [])
    return {'atlasSha256': atlas, 'tiles': tiles, 'stale': set(stale) if isinstance(stale, list) else set()}


def read_collision_index(store: Path) -> dict:
    """Pairs {key: pairSha} of the published collision index; {} without current.json. A broken pointer or index
    refuses the run (it is never silently replaced)."""
    pointer_path = Path(store) / 'current.json'
    if not pointer_path.exists():
        return {}
    try:
        pointer = json.loads(pointer_path.read_bytes())
        sha = pointer['indexSha256']
        if pointer.get('format') != cb.POINTER_FORMAT or not cb.HEX64.fullmatch(sha):
            raise ValueError('invalid pointer')
        raw = cb._read_object(Path(store), 'indexes', f'{sha}.json', cb.MAX_INDEX_BYTES)
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('index content does not match its hash')
        doc = json.loads(raw)
        if doc.get('format') != cb.INDEX_FORMAT or doc.get('tileSize') != cb.TILE or not isinstance(doc.get('pairs'), dict):
            raise ValueError('not a UIE_COLLISION_INDEX1')
        return dict(doc['pairs'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ExportError(f'previous collision index unreadable: {_message(error)}') from None


def read_pair(store: Path, pair_sha) -> dict | None:
    """The pair record if it is present and matches its hash, else None (never raises)."""
    if not isinstance(pair_sha, str) or not cb.HEX64.fullmatch(pair_sha):
        return None
    try:
        raw = cb._read_object(Path(store), 'pairs', f'{pair_sha}.json', cb.MAX_PAIR_BYTES)
        if hashlib.sha256(raw).hexdigest() != pair_sha:
            return None
        pair = json.loads(raw)
        return pair if isinstance(pair, dict) and pair.get('format') == cb.PAIR_FORMAT else None
    except (OSError, ValueError):
        return None


def pair_matches(pair, key: str, hires: dict) -> bool:
    """A pair is current only for the visible hires revision: same tile geometry (sha12) and same atlas, and the
    hires tile is not a stale (previous, failed-to-render) revision."""
    if not pair or key in hires['stale']:
        return False
    tx, tz = (int(v) for v in key.split(','))
    geometry = pair.get('geometrySha256')
    return (pair.get('x') == tx * cb.TILE and pair.get('z') == tz * cb.TILE
            and pair.get('atlasSha256') == hires['atlasSha256'] and isinstance(geometry, str)
            and geometry[:12] == hires['tiles'].get(key))


def verify_pair_objects(store: Path, key: str, pair_sha: str):
    """None when the pair record AND its referenced objects are intact (identity, header, tile position), else the
    reason. Uses collision_bundle's verifier, the same check publish_index applies; never writes anything."""
    try:
        cb._verify_pair(Path(store), key, pair_sha)
        return None
    except (OSError, ValueError) as error:
        return _message(error)


def encode_payload(volume, mesh_result) -> dict:
    """The objects publish_pair would write, computed without writing (for the hash check and the budget)."""
    core = volume.core
    blocks = ct.encode_raw(volume, core.x, core.z)
    docs = [doc for _, doc in sorted(mesh_result['chunks'].items())]
    geometry = wh.encode_tile(core.x, core.z, docs)
    objects = {f'{ct.identity(blocks)}.uib': len(ct.compressed(blocks))}
    if geometry:
        objects[f'{hashlib.sha256(geometry).hexdigest()}.uih'] = len(geometry)
    return {'geometrySha12': hashlib.sha256(geometry).hexdigest()[:12] if geometry else None, 'objects': objects}


def metadata_reserve(entries: int) -> int:
    """Budget headroom for the files written after the payload check: status.json, the index (+ its temporary) and
    provenance.json (+ its temporary), for an index of `entries` entries."""
    return METADATA_BASE_BYTES + 2 * (INDEX_ENTRY_BYTES + PROVENANCE_ENTRY_BYTES) * max(0, entries)


def read_provenance(out: Path):
    """({key: {'source', 'pair'}}, ignored reason | None). Missing, unreadable or malformed state is never trusted:
    it yields no (or fewer) entries, which only forces re-rendering."""
    path = Path(out) / 'provenance.json'
    if not path.exists():
        return {}, None
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(PROVENANCE_LIMIT + 1)
        if len(raw) > PROVENANCE_LIMIT:
            raise ValueError('too large')
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get('format') != PROVENANCE_FORMAT or not isinstance(doc.get('tiles'), dict):
            raise ValueError(f'not a {PROVENANCE_FORMAT}')
        if len(doc['tiles']) > cb.MAX_INDEX_ENTRIES:
            raise ValueError('too many entries')
    except (OSError, ValueError) as error:
        return {}, f'provenance ignored: {_message(error)}'
    entries, invalid = {}, 0
    for key, entry in doc['tiles'].items():
        if (isinstance(key, str) and cb.TILE_KEY.fullmatch(key) and isinstance(entry, dict) and set(entry) == {'source', 'pair'}
                and all(isinstance(entry[k], str) and cb.HEX64.fullmatch(entry[k]) for k in ('source', 'pair'))):
            entries[key] = {'source': entry['source'], 'pair': entry['pair']}
        else:
            invalid += 1
    return entries, (f'{invalid} malformed provenance entries ignored' if invalid else None)


def write_provenance(out: Path, entries: dict):
    raw = json.dumps({'format': PROVENANCE_FORMAT, 'tiles': dict(sorted(entries.items()))},
                     sort_keys=True, separators=(',', ':')).encode()
    path = Path(out) / 'provenance.json'
    fd, tmp = tempfile.mkstemp(prefix='.provenance-', dir=out)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def new_bytes(store: Path, payload: dict) -> int:
    pair_json = 512   # canonical pair record is ~300 bytes
    return pair_json + sum(size for name, size in payload['objects'].items() if not (Path(store) / 'objects' / name).exists())


# ------------------------------------------------------------------ export

def _write_status(out: Path, status: dict):
    path = Path(out) / 'status.json'
    tmp = path.with_name(f'.status.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(status, indent=1), encoding='utf-8')
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def export_tiles(selection, deferred, *, out, hires_store, render, catalog_atlas_sha256,
                 budget_bytes=DEFAULT_BUDGET_BYTES, min_free_bytes=MIN_FREE_BYTES,
                 estimate_tile_bytes=ESTIMATE_TILE_BYTES, disk_free=lambda p: shutil.disk_usage(p).free,
                 now=time.time, publish_pair=cb.publish_pair, publish_index=cb.publish_index,
                 source_revision=None, active_keys=None) -> dict:
    """Export `selection` [(tx, tz)] sequentially; `render(tx, tz) -> (volume, mesh_result)` raises on failure.

    source_revision(tx, tz) -> 64 hex: revision of the tile's source inputs (the CLI always supplies it). With it, a
    previous pair is reused only when out/provenance.json records that exact revision for that exact pair (on top of
    the hires geometry/atlas and object checks); it is read before and after a render (a change refuses the tile),
    and the provenance is saved only after the index was published (or the unchanged index is already current).
    Without it (legacy/test callers) reuse is snapshot-only: hidden block changes under an unchanged surface are not
    detected, and provenance.json is neither trusted nor written.

    Returns (and writes to out/status.json) the run status. Raises only on unexpected crashes (after writing a
    failed status); per-tile problems and run refusals are reported in the status."""
    active_keys = validate_active_keys(active_keys, selection, deferred)
    out = Path(out)
    store = out / 'store'
    store.mkdir(parents=True, exist_ok=True)
    status = {'format': STATUS_FORMAT, 'state': 'running', 'startedAt': _iso(now()), 'finishedAt': None,
              'requested': len(selection) + len(deferred), 'completed': 0, 'published': 0, 'reused': 0,
              'skipped': 0, 'failed': 0, 'refused': 0, 'staleDropped': 0, 'corruptPairs': 0, 'corruptDropped': 0,
              'repaired': 0, 'budgetStopped': False, 'sourceRevisionChecked': source_revision is not None,
              'sourceRerendered': 0, 'provenance': {'loaded': 0, 'ignored': None, 'saved': False, 'entries': None},
              'indexPublished': False, 'indexSha256': None, 'previousIndexEntries': None, 'indexEntries': None,
              'activeLimit': len(active_keys) if active_keys is not None else None,
              'evicted': 0, 'missingActive': [],
              'bytesBefore': dir_bytes(out), 'bytesAfter': None, 'newBytes': 0, 'tiles': {}, 'errors': []}
    _write_status(out, status)

    def corrupt(key, reason):
        status['corruptPairs'] += 1
        if len(status['errors']) < MAX_ERRORS:
            status['errors'].append({'tile': key, 'result': 'corrupt', 'reason': _message(reason)})

    def note(key, result, reason=None):
        status['tiles'][key] = {'result': result, **({'reason': _message(reason)} if reason else {})}
        counter = {'published': 'published', 'reused': 'reused', 'skipped': 'skipped', 'failed': 'failed',
                   'refused': 'refused'}[result]
        status[counter] += 1
        if result in ('published', 'reused'):
            status['completed'] += 1
        if result in ('failed', 'refused') and len(status['errors']) < MAX_ERRORS:
            status['errors'].append({'tile': key, 'result': result, 'reason': _message(reason)})

    index_error = None
    try:
        hires = read_hires_index(hires_store)
        if catalog_atlas_sha256 != hires['atlasSha256']:
            raise ExportError('catalog atlas differs from the visible hires atlas (wrong catalog or stale hires)')
        previous = read_collision_index(store)
        status['previousIndexEntries'] = len(previous)
        reserve = metadata_reserve((len(previous) if active_keys is None else min(len(previous), len(active_keys)))
                                   + len(selection))
        if hasattr(render, 'reserve_bytes'):
            render.reserve_bytes = reserve      # the region copies of the CLI renderer keep the same headroom
        if status['bytesBefore'] + estimate_tile_bytes * len(selection) + reserve > budget_bytes:
            raise ExportError(f'budget: {status["bytesBefore"]} bytes used + estimate for {len(selection)} tiles '
                              f'+ {reserve} metadata reserve exceeds {budget_bytes}')
        if disk_free(out) - reserve < min_free_bytes:
            raise ExportError('free disk space is below the minimum')
        provenance, ignored = read_provenance(out) if source_revision else ({}, None)
        status['provenance'].update(loaded=len(provenance), ignored=ignored)

        def revision(tx, tz):
            value = source_revision(tx, tz)
            if not isinstance(value, str) or not cb.HEX64.fullmatch(value):
                raise TileError(f'invalid source revision {value!r}')
            return value

        accepted = {}
        proofs = {}                         # key -> (source revision, pair) established in this run
        pending = [f'{tx},{tz}' for tx, tz in selection]
        for tx, tz in selection:
            key = pending.pop(0)
            if disk_free(out) < min_free_bytes:
                note(key, 'skipped', 'free disk space below the minimum')
                for rest in pending:
                    note(rest, 'skipped', 'not started: free disk space below the minimum')
                break
            visible = hires['tiles'].get(key)
            if visible is None:
                note(key, 'skipped', 'not in the hires index: geometry unknown, no pair is invented')
                continue
            if key in hires['stale']:
                note(key, 'refused', 'hires tile is a stale (previous) revision')
                continue
            rev = None
            if source_revision:
                try:
                    rev = revision(tx, tz)
                except Exception as error:      # unreadable inputs or registry changed: no reuse, no render
                    note(key, 'failed', error)
                    continue
            prior = previous.get(key)
            damaged = None
            rerender = None
            if prior and pair_matches(read_pair(store, prior), key, hires):
                # Reuse needs ALL of: visible geometry + atlas match, recorded source revision for exactly this pair
                # (when sources are checked), and intact referenced objects.
                if rev is not None and provenance.get(key) != {'source': rev, 'pair': prior}:
                    rerender = 'source revision unknown or changed since the pair was published; re-rendered'
                    status['sourceRerendered'] += 1
                else:
                    damaged = verify_pair_objects(store, key, prior)
                    if damaged is None:
                        accepted[key] = prior
                        if rev is not None:
                            proofs[key] = (rev, prior)
                        note(key, 'reused')
                        continue
                    corrupt(key, damaged)   # re-render: a missing object is rewritten, a differing one is refused
            try:
                volume, mesh = render(tx, tz)
                payload = encode_payload(volume, mesh)
            except CopyBudgetError as error:
                status['budgetStopped'] = True
                note(key, 'skipped', error)
                for rest in pending:
                    note(rest, 'skipped', 'not started: budget')
                break
            except Exception as error:      # source read, decode, mesh or encode failure: this tile only
                note(key, 'failed', error)
                continue
            if payload['geometrySha12'] != visible:
                note(key, 'refused', f'rendered geometry {payload["geometrySha12"]} differs from hires {visible}')
                continue
            try:
                fresh = read_hires_index(hires_store)    # the visible revision may have changed meanwhile
            except ExportError as error:
                note(key, 'failed', error)
                continue
            if (fresh['tiles'].get(key) != visible or fresh['atlasSha256'] != hires['atlasSha256']
                    or key in fresh['stale']):
                note(key, 'refused', 'hires tile or atlas changed during the export')
                continue
            if rev is not None:
                try:
                    after = revision(tx, tz)
                except Exception as error:
                    note(key, 'failed', error)
                    continue
                if after != rev:
                    note(key, 'refused', 'source inputs changed during the export')
                    continue
            needed = new_bytes(store, payload)
            if dir_bytes(out) + needed + reserve > budget_bytes or disk_free(out) - needed - reserve < min_free_bytes:
                status['budgetStopped'] = True
                note(key, 'skipped', f'budget/free space: cannot reserve {needed} new bytes + {reserve} metadata bytes')
                for rest in pending:
                    note(rest, 'skipped', 'not started: budget')
                break
            try:
                pair = publish_pair(store, volume, mesh, hires['atlasSha256'])
            except Exception as error:
                note(key, 'failed', error)
                continue
            if not pair_matches(pair, key, fresh):
                note(key, 'refused', 'published pair does not match the hires tile')
                continue
            accepted[key] = pair['pairSha256']
            if rev is not None:
                proofs[key] = (rev, pair['pairSha256'])
            status['newBytes'] += needed
            if damaged:
                status['repaired'] += 1
                note(key, 'published', f'previous pair objects were damaged ({damaged}); republished')
            else:
                note(key, 'published', rerender)
        for tx, tz in deferred:
            note(f'{tx},{tz}', 'skipped', f'deferred: at most {len(selection)} tiles per run')

        # Merge: previous entries stay current only while they match the visible (non-stale) hires revision AND
        # their objects are intact. Without this check an unchanged merge would skip publish_index's verification.
        final = read_hires_index(hires_store)
        merged = {}
        for key, sha in previous.items():
            if active_keys is not None and key not in active_keys:
                continue                           # eviction candidates are not re-verified or carried forward
            if key not in accepted:
                if not pair_matches(read_pair(store, sha), key, final):
                    status['staleDropped'] += 1
                    continue
                damaged = verify_pair_objects(store, key, sha)
                if damaged is None:
                    merged[key] = sha
                else:
                    status['corruptDropped'] += 1
                    if key not in status['tiles']:      # a selected tile already reported its failure
                        corrupt(key, damaged)
        for key, sha in accepted.items():
            damaged = None
            if pair_matches(read_pair(store, sha), key, final):
                damaged = verify_pair_objects(store, key, sha)
                if damaged is None:
                    merged[key] = sha
                    continue
            if damaged is None:
                reason = 'hires changed (or became stale) before the index was published'
            else:
                reason = f'pair objects damaged before the index was published: {damaged}'
            status[status['tiles'][key]['result']] -= 1
            status['completed'] -= 1
            status['tiles'][key] = {'result': 'refused', 'reason': _message(reason)}
            status['refused'] += 1
            if len(status['errors']) < MAX_ERRORS:
                status['errors'].append({'tile': key, 'result': 'refused', 'reason': _message(reason)})
        status['indexEntries'] = len(merged)
        status['missingActive'] = sorted(active_keys - merged.keys()) if active_keys is not None else []
        if merged == previous:            # nothing to change: the previous index (if any) stays current
            if (store / 'current.json').exists():
                status['indexSha256'] = json.loads((store / 'current.json').read_bytes())['indexSha256']
        else:
            if (dir_bytes(out) + metadata_reserve(len(merged)) > budget_bytes
                    or disk_free(out) - metadata_reserve(len(merged)) < min_free_bytes):
                status['budgetStopped'] = True
                index_error = 'budget: the index would exceed the budget; previous index kept'
            else:
                try:
                    result = publish_index(store, merged)
                    status['indexPublished'] = True
                    status['indexSha256'] = result['indexSha256']
                except Exception as error:
                    index_error = f'index publication failed, previous index kept: {_message(error)}'
        if not index_error and active_keys is not None:
            status['evicted'] = len(previous.keys() - active_keys)
        provenance_error = None
        if source_revision and not index_error:
            # Only now is `merged` the current index: record proofs for exactly its pairs. Entries of pairs that are
            # no longer current are dropped, so the state is bounded by the index size.
            kept = {}
            for key, sha in merged.items():
                if key in proofs and proofs[key][1] == sha:
                    kept[key] = {'source': proofs[key][0], 'pair': sha}
                elif provenance.get(key, {}).get('pair') == sha:
                    kept[key] = provenance[key]
            status['provenance']['entries'] = len(kept)
            if kept != provenance or ignored:
                try:
                    if disk_free(out) - metadata_reserve(len(kept)) < min_free_bytes:
                        raise ExportError('free space cannot cover provenance metadata reserve')
                    write_provenance(out, kept)
                    status['provenance']['saved'] = True
                except Exception as error:      # safe: the next run re-renders instead of reusing
                    provenance_error = f'provenance not saved (next run re-renders): {_message(error)}'
        if provenance_error:
            status['errors'] = status['errors'][:MAX_ERRORS - 1] + [{'tile': None, 'result': 'failed', 'reason': provenance_error}]
        if index_error:
            status['errors'] = status['errors'][:MAX_ERRORS - 1] + [{'tile': None, 'result': 'failed', 'reason': index_error}]
            status['state'] = f'failed: {index_error}'[:MAX_MESSAGE]
        elif (status['completed'] == status['requested'] and not status['corruptDropped']
              and not status['missingActive'] and not provenance_error):
            status['state'] = 'done'      # repaired (rewritten missing objects, re-verified) tiles count as done
        else:
            status['state'] = 'partial'
    except ExportError as error:
        status['state'] = f'failed: {_message(str(error))}'
    except BaseException as error:
        status['state'] = f'failed: crashed: {_message(error)}'
        status['finishedAt'] = _iso(now())
        status['bytesAfter'] = dir_bytes(out)
        _write_status(out, status)
        raise
    status['finishedAt'] = _iso(now())
    status['bytesAfter'] = dir_bytes(out)
    _write_status(out, status)
    return status


# ------------------------------------------------------------------ real renderer (CLI)

COPY_BLOCK = 1024 * 1024
COPY_ATTEMPTS = 3


def copy_regions_bounded(region_dir: Path, scratch: Path, area, *, out: Path, budget_bytes: int,
                         min_free_bytes: int = MIN_FREE_BYTES, disk_free=lambda p: shutil.disk_usage(p).free,
                         attempts: int = COPY_ATTEMPTS, stat=os.stat, sleep=time.sleep) -> dict:
    """Stable private copies {(rx, rz): path} of the region files touching `area`, like world_hires.copy_regions
    (accepted only when size and mtime did not change around the copy), but bounded:

    - before ANY copy, the sum of the current source sizes must fit the budget (all of `out`, which contains
      `scratch`) and keep the free space floor; otherwise CopyBudgetError and nothing is copied;
    - each attempt copies at most the size observed at its start plus one byte (growth is detected, never copied
      unboundedly) into the SAME target, so a retry never coexists with an earlier partial copy;
    - before each attempt the reservation is checked again with the size seen now (a grown file is re-checked);
    - a file that keeps changing fails the tile (TileError)."""
    sources = [(rx, rz, Path(region_dir) / f'r.{rx}.{rz}.mca') for rx, rz in wh._regions_of(area)]
    sources = [(rx, rz, p) for rx, rz, p in sources if p.exists()]
    used = dir_bytes(out)                      # includes the store, the status and an empty scratch directory

    def reserve(copied: int, size: int):
        if used + copied + size > budget_bytes:
            raise CopyBudgetError(f'budget: region copies need {copied + size} bytes, {used} used, budget {budget_bytes}')
        if disk_free(out) - size < min_free_bytes:
            raise CopyBudgetError('free disk space would fall below the minimum while copying regions')

    total = sum(stat(p).st_size for _, _, p in sources)
    reserve(0, total)
    copies, copied = {}, 0
    for rx, rz, source in sources:
        target = Path(scratch) / source.name
        for attempt in range(attempts):
            before = stat(source)
            reserve(copied, before.st_size + 1)
            written = 0
            with open(source, 'rb') as src, open(target, 'wb') as dst:     # 'wb' truncates an earlier attempt
                while written <= before.st_size:
                    block = src.read(min(COPY_BLOCK, before.st_size + 1 - written))
                    if not block:
                        break
                    dst.write(block)
                    written += len(block)
            after = stat(source)
            if (written == before.st_size and (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
                    and target.stat().st_size == before.st_size):
                copies[(rx, rz)] = target
                copied += written
                break
            if attempt + 1 < attempts:
                sleep(0.2)
        else:
            target.unlink(missing_ok=True)
            raise TileError(f'{source.name} kept changing while it was copied')
    return copies


def read_registry(world: Path) -> dict:
    """The block id -> name registry of level.dat (only this part of level.dat defines how voxels are named)."""
    import reader
    return dict(reader.parse_fml_registry(reader.parse_level_dat((Path(world) / 'level.dat').read_bytes())).blocks)


def registry_sha256(registry: dict) -> str:
    return hashlib.sha256(json.dumps(sorted((int(k), str(v)) for k, v in registry.items()),
                                     separators=(',', ':')).encode()).hexdigest()


def content_fingerprint(region_dir: Path, area) -> str:
    """Hash actual stored payloads of input chunks, not second-resolution save timestamps.

    Reads are bounded by the Anvil sector allocation (at most 255 sectors per chunk). A concurrently changing
    region refuses this attempt. Unrelated chunk payloads are not hashed. Rendering still uses stable private copies.
    """
    digest = hashlib.sha256(b'UIE_COLLISION_CONTENT2')
    for rx, rz in wh._regions_of(area):
        path = Path(region_dir) / f'r.{rx}.{rz}.mca'
        try:
            stream = path.open('rb')
        except FileNotFoundError:
            digest.update(json.dumps([rx, rz, 'missing']).encode())
            continue
        with stream:
            before = os.fstat(stream.fileno())
            header = stream.read(8192)
            if len(header) != 8192:
                raise TileError(f'{path.name}: incomplete region header')
            for cz in range(max(area.z // 16, rz * 32), min((area.z + area.depth + 15) // 16, rz * 32 + 32)):
                for cx in range(max(area.x // 16, rx * 32), min((area.x + area.width + 15) // 16, rx * 32 + 32)):
                    slot = 4 * ((cx & 31) + (cz & 31) * 32)
                    loc = int.from_bytes(header[slot:slot + 4], 'big')
                    offset, sectors = loc >> 8, loc & 255
                    digest.update(json.dumps([cx, cz, bool(loc)]).encode())
                    if not loc:
                        continue
                    if offset < 2 or not sectors or (offset + sectors) * 4096 > before.st_size:
                        raise TileError(f'{path.name}: invalid allocation for {cx},{cz}')
                    stream.seek(offset * 4096)
                    prefix = stream.read(4)
                    size = int.from_bytes(prefix, 'big')
                    if len(prefix) != 4 or not 1 <= size <= sectors * 4096 - 4:
                        raise TileError(f'{path.name}: invalid payload length for {cx},{cz}')
                    payload = stream.read(size)
                    if len(payload) != size:
                        raise TileError(f'{path.name}: truncated chunk {cx},{cz}')
                    digest.update(prefix); digest.update(payload)
            after = path.stat()
            stream.seek(0)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or stream.read(8192) != header):
                raise TileError(f'{path.name}: changed during source digest')
    return digest.hexdigest()


def make_source_revision(world: Path, registry_sha: str, *, fingerprint=content_fingerprint, load_registry=read_registry):
    """source_revision(tx, tz) for the CLI: SHA-256 of the format, tile, stored input chunk payloads including halo,
    and the registry the renderer uses. Same-second saves with unchanged Anvil allocation are detected. When
    level.dat's registry no longer equals the renderer's (registry_sha), it raises: the render would be labelled with a
    registry it did not use. Player, time and other level.dat metadata are deliberately not part of the revision."""
    region_dir = Path(world) / 'region'

    def source_revision(tx, tz):
        current = registry_sha256(load_registry(world))
        if current != registry_sha:
            raise TileError('level.dat block registry changed since the renderer started; restart the export')
        t_area, _ = wh.tile_area(tx, tz)
        doc = [SOURCE_FORMAT, f'{tx},{tz}', fingerprint(region_dir, t_area), registry_sha]
        return hashlib.sha256(json.dumps(doc, separators=(',', ':')).encode()).hexdigest()

    return source_revision


def make_renderer(world: Path, catalog_dir: Path, scratch_root: Path, *, budget_bytes: int = DEFAULT_BUDGET_BYTES,
                  min_free_bytes: int = MIN_FREE_BYTES, disk_free=lambda p: shutil.disk_usage(p).free):
    """render(tx, tz) from stable private region copies of the tile input (core + halo), strict reads. The copies
    live in scratch_root (= the output directory) and are bounded by the same budget (minus render.reserve_bytes,
    set by export_tiles for the metadata) and free space floor. render.source_revision is the matching
    source_revision callback (same registry snapshot)."""
    import capture_batch
    from offline_catalog import Catalog
    from offline_mesher import Colormap, Mesher
    from offline_volume import Volume
    registry = read_registry(world)
    snapshot_sha = registry_sha256(registry)  # before _batch extends the live copy with placeholder names
    wh._W['registry'] = dict(registry)        # world_hires._batch reads (and extends) the registry from here
    mesher = Mesher(Catalog.load_compiled(catalog_dir / 'catalog.json'),
                    Colormap(catalog_dir / 'grass.png', catalog_dir / 'foliage.png'), caves=False)
    region_dir = world / 'region'

    def render(tx, tz):
        t_area, t_core = wh.tile_area(tx, tz)
        with tempfile.TemporaryDirectory(dir=scratch_root, prefix='.scratch-') as scratch:
            copies = copy_regions_bounded(region_dir, Path(scratch), t_area, out=scratch_root,
                                          budget_bytes=budget_bytes - render.reserve_bytes,
                                          min_free_bytes=min_free_bytes, disk_free=disk_free)
            failed = set()
            chunks = wh.read_chunks(copies, t_area, failed=failed)
            for copy in copies.values():
                copy.unlink(missing_ok=True)
        if failed:
            raise TileError(f'source read failed for {len(failed)} chunk(s) of the tile input (halo included), '
                            f'e.g. {min(failed)}')
        volume = Volume(wh._batch(capture_batch, t_area, t_core, chunks))
        return volume, mesher.mesh_batch(volume)

    render.reserve_bytes = METADATA_BASE_BYTES
    render.source_revision = make_source_revision(world, snapshot_sha)
    return render


def _lock(out: Path):
    handle = open(out / '.export.lock', 'a')
    try:
        import fcntl
    except ImportError:
        return handle                     # Windows (tests): no concurrent exporter there
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--world', required=True, type=Path)
    ap.add_argument('--catalog-dir', required=True, type=Path)
    ap.add_argument('--hires-store', required=True, type=Path)
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--tile', required=True, action='append', help='tx,tz of a 64 block tile (repeatable)')
    ap.add_argument('--active-tile', action='append', help='tx,tz retained in the active collision index (repeatable, 1..32)')
    ap.add_argument('--max-tiles', type=int, default=DEFAULT_MAX_TILES)
    ap.add_argument('--budget-mib', type=int, default=DEFAULT_BUDGET_BYTES // MIB)
    a = ap.parse_args(argv)
    try:
        selection, deferred = select_tiles(a.tile, a.max_tiles)
        active_keys = validate_active_keys(a.active_tile, selection, deferred)
        if not 1 <= a.budget_mib <= 64 * 1024:
            raise ExportError('--budget-mib must be 1..65536')
        out = check_paths(a.out, world=a.world, **{'hires store': a.hires_store}, catalog=a.catalog_dir)
    except ExportError as error:
        print(json.dumps({'state': f'failed: {error}'}))
        return 1
    if hasattr(os, 'nice'):
        os.nice(15)
    out.mkdir(parents=True, exist_ok=True)
    lock = _lock(out)
    if lock is None:
        print(json.dumps({'state': 'skipped: another export holds the lock'}))
        return 3
    try:
        atlas = hashlib.sha256((a.catalog_dir / 'atlas.png').read_bytes()).hexdigest()
        render = make_renderer(a.world.resolve(), a.catalog_dir.resolve(), out, budget_bytes=a.budget_mib * MIB)
        status = export_tiles(selection, deferred, out=out, hires_store=a.hires_store.resolve(), render=render,
                              catalog_atlas_sha256=atlas, budget_bytes=a.budget_mib * MIB,
                              source_revision=render.source_revision, active_keys=active_keys)   # CLI checks source revisions
    finally:
        lock.close()
    print(json.dumps(status, indent=1))
    return 0 if status['state'] == 'done' else 2 if status['state'] == 'partial' else 1


if __name__ == '__main__':
    sys.exit(main())
