"""Read-only benchmark of region-batched collision export for ONE region job (512 x 512 blocks, 64 tiles).

  python3 collision_region_probe.py --world W --catalog-dir C --hires-store H --scratch-root S --region jx,jz
                                    [--tile tx,tz ...] [--budget-mib 512] [--min-free-gib 24]
                                    [--world-id uie] [--dimension 0]
                                    [--pilot-store COLLISION_STORE] [--report NEW_FILE]

Publishes nothing: no pair, object, index, pointer, timer or store write. The only files it creates are private
region copies in its own temporary directory under --scratch-root (removed at the end) and, with --report, one new
report file. The JSON report goes to stdout; exit 0 only when the report says "ok": true.

Pipeline (same inputs and code path as collision_export.make_renderer / world_hires._render_rows):
  1. VISIBLE = the published immutable surface index: H/current.json (UIE_HIRES_POINTER1) -> H/indexes/<sha>.json,
     exact SHA-256, canonical JSON, ONLY UIE_HIRES_IMMUTABLE_INDEX3 with context world/dimension as given, caves
     false, tiles {key: full UIH SHA-256}, source.staleTiles. The legacy H/index.json is never read (the producer
     may already be ahead of the publisher). The renderer identity of the imported offline_mesher/offline_catalog/
     offline_volume (next to world_hires.py; canonical surface formula, see surface_identity) and the catalog atlas
     must equal the index, else the probe refuses before copying anything.
  2. registry SHA of level.dat, content fingerprint of the job input (region + 16 block halo; stored chunk
     payloads, collision_export.content_fingerprint) on the live world;
  3. ONE bounded copy of every region file of the job input (collision_export.copy_regions_bounded);
     the content fingerprint of the copies must equal the live one;
  4. per row of tiles, ONE 96 block high strip decoded strictly (world_hires.read_chunks with failed=set()); any
     unreadable chunk in a tile's input (halo included) fails that tile, it is never treated as air;
  5. per tile: Volume (world_hires._batch) -> mesh -> UIH1 geometry (world_hires.encode_tile) and UIB1 blocks
     (collision_tile.encode_raw) from the SAME Volume; the full geometry SHA-256 is compared with the visible tile;
     only sizes/hashes are kept, the Volume and mesh are dropped before the next tile;
  6. live content fingerprint and registry SHA again, visible pointer/index re-read: a different atlas, identity or
     context is fatal; a newer index only invalidates probed tiles whose reference or stale state changed.

Tile results: match | mismatch | unknown (no visible tile and no rendered geometry: missing chunks stay unknown)
| stale (visible revision is an old one) | changed (visible tile changed during the probe) | error.
"ok" requires: no fatal error, unchanged sources/registry, and every probed tile match or unknown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time

import collision_export as ce
import collision_tile as ct
import world_hires as wh

REPORT_FORMAT = 'UIE_COLLISION_REGION_PROBE1'
MIB = 1024 * 1024
GIB = 1024 * MIB
DEFAULT_BUDGET_BYTES = 512 * MIB
DEFAULT_MIN_FREE_BYTES = 24 * GIB
POINTER_LIMIT = 4096
INDEX_LIMIT = 16 * MIB                  # hires_bundle.MAX_INDEX_BYTES
INDEX_ENTRIES = 100_000                 # hires_bundle.MAX_INDEX_ENTRIES
POINTER_FORMAT = 'UIE_HIRES_POINTER1'
INDEX_FORMAT_V3 = 'UIE_HIRES_IMMUTABLE_INDEX3'
INDEX_KEYS = {'format', 'tileSize', 'rendererIdentity', 'atlasSha256', 'tiles', 'context', 'source'}
CONTEXT_KEYS = {'world', 'dimension', 'renderer', 'caves', 'layoutSha256'}            # hires_context.KEYS
SOURCE_KEYS = {'legacyIndexSha256', 'producerFinishedAt', 'planComplete', 'plannedJobs',
               'notScheduled', 'failedJobs', 'failedTiles', 'staleTiles'}            # hires_source.KEYS
OK_RESULTS = {'match', 'unknown'}


class ProbeError(Exception):
    """The whole probe is refused (fatal)."""


def parse_pair(text, what):
    m = ce.cb.TILE_KEY.fullmatch(text) if isinstance(text, str) else None
    if not m:
        raise ProbeError(f'invalid {what} {text!r} (expected x,z)')
    return int(m.group(1)), int(m.group(2))


def surface_identity(catalog_identity: str, code: bytes) -> str:
    """Renderer identity of a surface (caves=false) store, the formula verify_model_cutover.py checked against the
    deployed index: sha256(catalog.identity + sha256(MAGIC + mesher + catalog + volume sources)). Computed here
    because deployed world_hires.py copies may predate geometry_identity."""
    return hashlib.sha256(catalog_identity.encode() + hashlib.sha256(code).digest()).hexdigest()


def _read_bounded(path: Path, limit: int, what: str) -> bytes:
    """Bytes of a regular, non-symlink file of 1..limit bytes that did not change while it was read."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        raise ProbeError(f'{what} is missing') from None
    except OSError as error:
        raise ProbeError(f'{what} unreadable: {error.strerror}') from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= limit:
            raise ProbeError(f'{what} is not a regular file of 1..{limit} bytes')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if len(raw) != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ProbeError(f'{what} changed while it was read')
    return raw


def _strict_json(raw: bytes, what: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProbeError(f'duplicate key in {what}')
            result[key] = value
        return result

    def constant(_):
        raise ProbeError(f'non-finite value in {what}')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)
    except ProbeError:
        raise
    except (UnicodeError, ValueError) as error:
        raise ProbeError(f'{what} is not valid JSON: {error}') from None


def _hex64(value):
    return isinstance(value, str) and ce.cb.HEX64.fullmatch(value) is not None


def read_visible(hires_store: Path, *, world: str = 'uie', dimension: str = '0') -> dict:
    """The PUBLISHED surface index: current.json -> indexes/<sha>.json, only a canonical immutable V3 index of the
    given world/dimension with caves=false. Never falls back to the legacy index.json. Any other state is refused.

    Returns {'indexSha256', 'atlasSha256', 'identity', 'tiles': {key: full UIH SHA-256}, 'stale': set,
    'planComplete'}."""
    store = Path(hires_store)
    pointer = _strict_json(_read_bounded(store / 'current.json', POINTER_LIMIT, 'current.json'), 'current.json')
    if (not isinstance(pointer, dict) or set(pointer) != {'format', 'indexSha256'}
            or pointer['format'] != POINTER_FORMAT or not _hex64(pointer['indexSha256'])):
        raise ProbeError('current.json is not a UIE_HIRES_POINTER1')
    sha = pointer['indexSha256']
    raw = _read_bounded(store / 'indexes' / f'{sha}.json', INDEX_LIMIT, 'current index')
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ProbeError('current index content does not match its hash')
    doc = _strict_json(raw, 'current index')
    if not isinstance(doc, dict) or doc.get('format') != INDEX_FORMAT_V3:
        raise ProbeError(f'current index is not {INDEX_FORMAT_V3}')
    if (set(doc) != INDEX_KEYS or type(doc['tileSize']) is not int or doc['tileSize'] != wh.TILE
            or not _hex64(doc['rendererIdentity']) or not _hex64(doc['atlasSha256'])
            or not isinstance(doc['tiles'], dict) or len(doc['tiles']) > INDEX_ENTRIES):
        raise ProbeError('current index schema invalid')
    for key, ref in doc['tiles'].items():
        if not ce.cb.TILE_KEY.fullmatch(key) or not _hex64(ref):
            raise ProbeError(f'current index has an invalid tile {key!r}')
    context = doc['context']
    if (not isinstance(context, dict) or set(context) != CONTEXT_KEYS or not _hex64(context['layoutSha256'])
            or not isinstance(context['renderer'], str)):
        raise ProbeError('current index context invalid')
    if context['world'] != world or context['dimension'] != dimension or context['caves'] is not False:
        raise ProbeError(f'current index is not the surface of world {world!r} dimension {dimension!r}')
    source = doc['source']
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS or type(source['planComplete']) is not bool:
        raise ProbeError('current index source metadata invalid')
    stale = source['staleTiles']
    if (not isinstance(stale, list) or stale != sorted(set(stale))
            or any(not isinstance(k, str) or k not in doc['tiles'] for k in stale)):
        raise ProbeError('current index stale tiles invalid')
    if raw != json.dumps(doc, sort_keys=True, separators=(',', ':')).encode():
        raise ProbeError('current index is not canonical JSON')
    return {'indexSha256': sha, 'atlasSha256': doc['atlasSha256'], 'identity': doc['rendererIdentity'],
            'tiles': dict(doc['tiles']), 'stale': set(stale), 'planComplete': source['planComplete']}


def peak_rss_kib():
    try:
        import resource
    except ImportError:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss      # KiB on Linux


class Toolkit:
    """The real code path (exact modules next to world_hires.py). Tests substitute individual attributes.

    `modules` (capture_batch, offline_catalog, offline_mesher, offline_volume) and `code_dir` exist only for tests;
    normally the imported modules must live next to world_hires.py, whose bytes define the identity."""

    def __init__(self, catalog_dir: Path, *, modules=None, code_dir=None, read_index=read_visible):
        self._catalog_dir = Path(catalog_dir)
        if modules is None:
            import capture_batch
            import offline_catalog
            import offline_mesher
            import offline_volume
            modules = {'capture_batch': capture_batch, 'offline_catalog': offline_catalog,
                       'offline_mesher': offline_mesher, 'offline_volume': offline_volume}
        here = Path(code_dir or Path(wh.__file__).resolve().parent).resolve()
        for name in ('offline_catalog', 'offline_mesher', 'offline_volume'):
            if Path(modules[name].__file__).resolve().parent != here:
                raise ProbeError(f'{name} is not imported from {here}: renderer identity unknown')
        offline_catalog, offline_mesher = modules['offline_catalog'], modules['offline_mesher']
        catalog = offline_catalog.Catalog.load_compiled(Path(catalog_dir) / 'catalog.json')
        code = wh.MAGIC + b''.join((here / n).read_bytes()
                                   for n in ('offline_mesher.py', 'offline_catalog.py', 'offline_volume.py'))
        self.identity = surface_identity(catalog.identity, code)
        self.atlas = hashlib.sha256((Path(catalog_dir) / 'atlas.png').read_bytes()).hexdigest()
        self._mesher = offline_mesher.Mesher(catalog, offline_mesher.Colormap(Path(catalog_dir) / 'grass.png',
                                                                              Path(catalog_dir) / 'foliage.png'),
                                             caves=False)
        self._capture_batch, self._volume = modules['capture_batch'], modules['offline_volume'].Volume
        self.copy_regions = ce.copy_regions_bounded
        self.fingerprint = ce.content_fingerprint
        self.load_registry = ce.read_registry
        self.read_chunks = wh.read_chunks
        self.read_index = read_index
        self.encode_geometry = wh.encode_tile
        self.encode_blocks = ct.encode_raw

    def build_volume(self, t_area, t_core, window):
        return self._volume(wh._batch(self._capture_batch, t_area, t_core, window))

    def mesh(self, volume):
        return self._mesher.mesh_batch(volume)

    def fresh(self):
        """Independent worker state for a conservative retry; all normal identity/SHA gates still apply."""
        return Toolkit(self._catalog_dir, read_index=self.read_index)


def _rect_keys(rect):
    return {(cx, cz) for cx in range(rect.x >> 4, (rect.x + rect.width) >> 4)
            for cz in range(rect.z >> 4, (rect.z + rect.depth) >> 4)}


def probe_region(jx, jz, *, world, hires_store, scratch_root, tools, tiles=None, pilot_store=None,
                 budget_bytes=DEFAULT_BUDGET_BYTES, min_free_bytes=DEFAULT_MIN_FREE_BYTES,
                 disk_free=lambda p: shutil.disk_usage(p).free, clock=time.perf_counter,
                 on_match=None) -> dict:
    """Run the probe; data problems return ok=false.

    Optional on_match(key, volume, mesh) may stage immutable objects, NEVER publish.
    Its returned JSON metadata is provisional until the WHOLE report is ok=true:
    source fingerprints, final surface references and scratch cleanup happen later.
    The CLI does not supply a callback and remains read-only.
    """
    started = clock()
    area, core = wh.job_area(jx, jz)
    report = {'format': REPORT_FORMAT, 'ok': False, 'region': [jx, jz], 'fatal': None, 'tiles': {},
              'counts': {}, 'copy': {'calls': 0, 'files': 0, 'bytes': 0}, 'decode': {'strips': 0},
              'seconds': {}, 'source': {}, 'visible': {}, 'peakRssKiB': None,
              'limits': {'budgetBytes': budget_bytes, 'minFreeBytes': min_free_bytes}}
    seconds = report['seconds']

    def timed(name, fn, *args, **kw):
        t = clock()
        try:
            return fn(*args, **kw)
        finally:
            seconds[name] = round(seconds.get(name, 0) + clock() - t, 4)

    all_keys = [(tx, tz) for tz in range(core.z // wh.TILE, (core.z + core.depth) // wh.TILE)
                for tx in range(core.x // wh.TILE, (core.x + core.width) // wh.TILE)]
    selected = all_keys if not tiles else list(dict.fromkeys(tiles))
    outside = [t for t in selected if t not in all_keys]
    scratch = None
    saved_registry = wh._W.get('registry', None)
    try:
        if outside:
            raise ProbeError(f'tiles outside region {jx},{jz}: {outside}')
        visible = tools.read_index(hires_store)
        report['visible'] = {'indexSha256': visible['indexSha256'], 'atlasSha256': visible['atlasSha256'],
                             'identity': visible['identity'], 'planComplete': visible.get('planComplete')}
        if tools.identity != visible['identity']:
            raise ProbeError('renderer identity of the probe code differs from the visible index')
        if tools.atlas != visible['atlasSha256']:
            raise ProbeError('catalog atlas differs from the visible index')
        region_dir = Path(world) / 'region'
        registry = tools.load_registry(world)
        registry_sha = ce.registry_sha256(registry)
        live_before = timed('fingerprintBefore', tools.fingerprint, region_dir, area)
        report['source'] = {'registrySha256': registry_sha, 'before': live_before}
        if disk_free(scratch_root) < min_free_bytes:
            raise ProbeError('free disk space below the minimum before copying')
        scratch = Path(tempfile.mkdtemp(prefix='collision-probe-', dir=scratch_root))
        report['copy']['calls'] += 1
        copies = timed('copy', tools.copy_regions, region_dir, scratch, area, out=scratch,
                       budget_bytes=budget_bytes, min_free_bytes=min_free_bytes, disk_free=disk_free)
        report['copy']['files'] = len(copies)
        report['copy']['bytes'] = sum(Path(p).stat().st_size for p in copies.values())
        copied = timed('fingerprintCopy', tools.fingerprint, scratch, area)
        report['source']['copy'] = copied
        if copied != live_before:
            raise ProbeError('copied region content differs from the live content (changed during the copy)')
        wh._W['registry'] = dict(registry)                  # world_hires._batch reads (and extends) it from here
        counts = report['counts']
        rows = sorted({tz for _, tz in selected})
        for tz in rows:
            strip = wh._Rect(area.x, tz * wh.TILE - wh.HALO, area.width, wh.TILE + 2 * wh.HALO)
            unreadable = set()
            chunks = timed('decode', tools.read_chunks, copies, strip, failed=unreadable)
            report['decode']['strips'] += 1
            for tx in [x for x, z in selected if z == tz]:
                key = f'{tx},{tz}'
                entry = report['tiles'][key] = _probe_tile(tx, tz, key, chunks, unreadable, visible, tools, timed,
                                                           pilot_store, on_match)
                counts[entry['result']] = counts.get(entry['result'], 0) + 1
            del chunks
        live_after = timed('fingerprintAfter', tools.fingerprint, region_dir, area)
        registry_after = ce.registry_sha256(tools.load_registry(world))
        report['source'].update(after=live_after, registryAfter=registry_after)
        if live_after != live_before or registry_after != registry_sha:
            raise ProbeError('world input or block registry changed during the probe')
        final = tools.read_index(hires_store)
        report['visible']['finalIndexSha256'] = final['indexSha256']
        if final['atlasSha256'] != visible['atlasSha256'] or final['identity'] != visible['identity']:
            raise ProbeError('visible atlas or renderer identity changed during the probe')
        # A newer publication elsewhere in the world does not invalidate the probed tiles; only their own references.
        for key, entry in report['tiles'].items():
            if final['tiles'].get(key) != visible['tiles'].get(key) or (key in final['stale']) != (key in visible['stale']):
                counts[entry['result']] -= 1
                entry.update(result='changed', reason='visible tile changed during the probe')
                counts['changed'] = counts.get('changed', 0) + 1
        report['ok'] = all(e['result'] in OK_RESULTS for e in report['tiles'].values())
    except ProbeError as error:
        report['fatal'] = str(error)[:500]
    except Exception as error:                              # corrupt input, budget, unexpected: never "success"
        report['fatal'] = f'{type(error).__name__}: {error}'[:500]
    finally:
        if saved_registry is None:
            wh._W.pop('registry', None)
        else:
            wh._W['registry'] = saved_registry
        if scratch is not None:
            report['scratchCleanup'] = _cleanup(scratch)
        report['seconds']['total'] = round(clock() - started, 4)
        report['peakRssKiB'] = peak_rss_kib()
    if scratch is not None and report.get('scratchCleanup') is not True:
        report['ok'] = False                                # leftover private copies are reported, not hidden
    return report


def _probe_tile(tx, tz, key, chunks, unreadable, visible, tools, timed, pilot_store, on_match=None):
    t_area, t_core = wh.tile_area(tx, tz)
    reference = visible['tiles'].get(key)               # full UIH SHA-256 of the published V3 index
    entry = {'visibleSha256': reference, 'visibleSha12': reference[:12] if reference else None,
             'coreChunks': 0, 'geometrySha256': None,
             'geometryBytes': 0, 'blockSha256': None, 'blockRawBytes': None, 'blockGzipBytes': None,
             'presentMask': 0}
    try:
        bad = _rect_keys(t_area) & unreadable
        if bad:
            raise ProbeError(f'unreadable chunk(s) in the tile input (halo included), e.g. {min(bad)}')
        window = {k: c for k, c in chunks.items() if k in _rect_keys(t_area)}
        core_present = _rect_keys(t_core) & set(window)
        entry['coreChunks'] = len(core_present)
        geometry = None
        if core_present:                        # like world_hires: no core chunk -> no tile (unknown, not air)
            volume = timed('volume', tools.build_volume, t_area, t_core, window)
            mesh = timed('mesh', tools.mesh, volume)
            docs = [doc for _, doc in sorted(mesh['chunks'].items())]
            geometry = timed('encodeGeometry', tools.encode_geometry, t_core.x, t_core.z, docs) if docs else None
            raw = timed('encodeBlocks', tools.encode_blocks, volume, t_core.x, t_core.z)
            entry['presentMask'] = ct.HEADER.unpack_from(raw)[3]
            entry.update(blockSha256=ct.identity(raw), blockRawBytes=len(raw),
                         blockGzipBytes=len(timed('gzip', ct.compressed, raw)))
            del raw
        if geometry:
            entry.update(geometrySha256=hashlib.sha256(geometry).hexdigest(), geometryBytes=len(geometry))
        if key in visible['stale']:
            entry['result'] = 'stale'
        elif reference is None and entry['geometrySha256'] is None:
            entry['result'] = 'unknown'
        else:
            entry['result'] = 'match' if entry['geometrySha256'] == reference else 'mismatch'
        if entry['result'] == 'match' and on_match is not None:
            entry['staged'] = timed('stage', on_match, key, volume, mesh)
        if pilot_store is not None:
            entry['pilot'] = _pilot(pilot_store, key, entry, visible['atlasSha256'])
    except Exception as error:
        entry.update(result='error', reason=f'{type(error).__name__}: {error}'[:300])
    return entry


def _pilot(store, key, entry, atlas):
    """Informational only: the pilot's block SHA for the same geometry and atlas (hidden blocks may differ)."""
    try:
        pair = ce.read_pair(Path(store), ce.read_collision_index(Path(store)).get(key))
    except Exception as error:
        return {'state': 'unreadable', 'reason': f'{type(error).__name__}: {error}'[:200]}
    if not pair:
        return {'state': 'no-pair'}
    if pair.get('geometrySha256') != entry['geometrySha256'] or pair.get('atlasSha256') != atlas:
        return {'state': 'geometry-differs'}
    return {'state': 'same-blocks' if pair.get('blocksSha256') == entry['blockSha256'] else 'different-blocks'}


def _cleanup(scratch: Path):
    """Remove only the region copies this probe made, then its own directory. True, or the reason."""
    try:
        leftover = []
        for p in scratch.iterdir():
            if p.is_file() and not p.is_symlink() and p.name.startswith('r.') and p.name.endswith('.mca'):
                p.unlink()
            else:
                leftover.append(p.name)
        if leftover:
            return f'not removed (unexpected entries): {sorted(leftover)[:10]}'
        scratch.rmdir()
        return True
    except OSError as error:
        return f'{type(error).__name__}: {error}'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--world', required=True, type=Path)
    ap.add_argument('--catalog-dir', required=True, type=Path)
    ap.add_argument('--hires-store', required=True, type=Path)
    ap.add_argument('--scratch-root', required=True, type=Path)
    ap.add_argument('--region', required=True)
    ap.add_argument('--tile', action='append')
    ap.add_argument('--budget-mib', type=int, default=DEFAULT_BUDGET_BYTES // MIB)
    ap.add_argument('--min-free-gib', type=int, default=DEFAULT_MIN_FREE_BYTES // GIB)
    ap.add_argument('--world-id', default='uie', help='context.world the published index must carry')
    ap.add_argument('--dimension', default='0', help='context.dimension the published index must carry')
    ap.add_argument('--pilot-store', type=Path)
    ap.add_argument('--report', type=Path, help='also write the report to this NEW file')
    a = ap.parse_args(argv)
    try:
        jx, jz = parse_pair(a.region, 'region')
        tiles = [parse_pair(t, 'tile') for t in a.tile] if a.tile else None
        if not 1 <= a.budget_mib <= 64 * 1024 or not 1 <= a.min_free_gib <= 1024:
            raise ProbeError('--budget-mib must be 1..65536 and --min-free-gib 1..1024')
        scratch_root = a.scratch_root.resolve()
        if not scratch_root.is_dir():
            raise ProbeError('--scratch-root must be an existing directory')
        ce.check_paths(scratch_root, world=a.world, **{'hires store': a.hires_store}, catalog=a.catalog_dir)
        if a.report is not None:
            ce.check_paths(a.report.resolve().parent, world=a.world, **{'hires store': a.hires_store})
            if a.report.exists():
                raise ProbeError('--report must name a new file')
        if hasattr(os, 'nice'):
            os.nice(15)
        tools = Toolkit(a.catalog_dir.resolve(),
                        read_index=lambda store: read_visible(store, world=a.world_id, dimension=a.dimension))
    except (ProbeError, ce.ExportError, OSError, ValueError) as error:
        print(json.dumps({'format': REPORT_FORMAT, 'ok': False, 'fatal': f'{type(error).__name__}: {error}'[:500]}))
        return 1
    report = probe_region(jx, jz, world=a.world.resolve(), hires_store=a.hires_store.resolve(),
                          scratch_root=scratch_root, tools=tools, tiles=tiles,
                          pilot_store=a.pilot_store.resolve() if a.pilot_store else None,
                          budget_bytes=a.budget_mib * MIB, min_free_bytes=a.min_free_gib * GIB)
    text = json.dumps(report, indent=1, sort_keys=True)
    if a.report is not None:
        with open(a.report, 'x', encoding='utf-8') as stream:      # never overwrites
            stream.write(text)
    print(text)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
