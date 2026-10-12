"""PRIVATE SYNTHETIC benchmark of the immutable-hires retention planner and GC dry run. NOT production.

    python3 benchmark_hires_retention.py --dir /ABSOLUTE/NEW/uie-gc-synthetic-<label>

Builds a clearly labeled synthetic store in a NEW directory (refuses an existing one and production paths), then
measures, each in a fresh child process (so peak RSS is not the fixture build's):
  scenario "3-roots": current + previous + one recent journal generation (all V3, 56 782 tiles each)
  scenario "5-roots": the same store plus two more recent generations in the journal (metadata headroom)
using the REAL hires_retention.plan_retention and hires_gc.collect(execute=False). Nothing is ever deleted; the
directory is left in place for inspection (remove it yourself).

Object files hold a few bytes of text named by their SHA-256: the planner checks names, sizes and references, not
UIH contents. No render correctness is claimed. "now" is a synthetic time 24 h in the future (stored in the
manifest), so every file is older than the 30 min grace; journal timestamps are set relative to it. The system clock
is not changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import hires_bundle as hb
import hires_history as hh
import hires_retention as retention

TILES = 56_782
ORPHANS = 200
CHANGED_PER_GENERATION = 64
MANIFEST = 'SYNTHETIC-MANIFEST.json'
LABEL = 'SYNTHETIC GC BENCHMARK FIXTURE - not production, no render data'
FORBIDDEN = ('/home/minecraft/uie-map-preview', '/home/minecraft/uie-map-render')
NAME_PREFIX = 'uie-gc-synthetic-'


def canonical(doc) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def tile_key(i: int) -> str:
    return f'{i % 240 - 120},{i // 240 - 120}'          # 240 x 237 grid around 0,0, canonical keys


class Fixture:
    def __init__(self, out: Path):
        self.out = out
        self.bytes = 0
        self.files = 0

    def put(self, folder: str, name: str, raw: bytes):
        path = self.out / folder / name if folder else self.out / name
        with open(path, 'xb') as stream:                  # never overwrite
            stream.write(raw)
        self.bytes += len(raw)
        self.files += 1

    def asset(self, folder: str, suffix: str, raw: bytes) -> str:
        digest = sha(raw)
        self.put(folder, f'{digest}{suffix}', raw)
        return digest


def _check_target(path: Path) -> Path:
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise SystemExit('--dir must be an absolute normalized path')
    if any(str(path) == p or str(path).startswith(p + os.sep) for p in FORBIDDEN):
        raise SystemExit('--dir lies under a production path')
    if not path.name.startswith(NAME_PREFIX):
        raise SystemExit(f'--dir name must start with {NAME_PREFIX!r} (explicit synthetic label)')
    if os.path.lexists(path):
        raise SystemExit('--dir already exists; a NEW directory is required')
    if not path.parent.is_dir():
        raise SystemExit('the parent of --dir must exist')
    return path


def build(out: Path) -> dict:
    out.mkdir()                                            # fails if it appeared meanwhile
    out = out.resolve()
    fx = Fixture(out)
    fx.put('', hb.OWNED_MARKER, (LABEL + '\n').encode())
    fx.put('', 'README-SYNTHETIC.txt', (LABEL + '\nCreated by benchmark_hires_retention.py; safe to delete.\n').encode())
    for folder in ('indexes', 'objects', 'atlases', 'layouts'):
        (out / folder).mkdir()
    now = int(time.time()) + 24 * 3600                     # synthetic planner time, all files older than the grace
    atlas = fx.asset('atlases', '.png', hb.PNG_SIGNATURE + b'synthetic atlas\n')
    layout = fx.asset('layouts', '.json', canonical({'format': 'UIE_ATLAS_LAYOUT1', 'registered': {},
                                                         'synthetic': LABEL}))
    identity = sha(b'synthetic renderer identity')
    keys = [tile_key(i) for i in range(TILES)]

    def generation_tiles(generation: int, base: dict) -> dict:
        tiles = dict(base)
        start = generation * CHANGED_PER_GENERATION
        for i in range(start, start + CHANGED_PER_GENERATION):
            tiles[keys[i]] = fx.asset('objects', '.uih', f'synthetic tile {keys[i]} generation {generation}\n'.encode())
        return tiles

    current_tiles = {key: fx.asset('objects', '.uih', f'synthetic tile {key} generation 0\n'.encode()) for key in keys}
    # Legacy index matching the real hires_snapshot parser (UIE_HIRES_INDEX1, sha12 references).
    legacy = {'format': 'UIE_HIRES_INDEX1', 'tileSize': 64, 'renderer': 'synthetic', 'identity': identity,
              'caves': False, 'atlas': 'atlas.png', 'atlasSha256': atlas, 'jobs': 983, 'chunks': 0,
              'tiles': {key: obj[:12] for key, obj in current_tiles.items()}}
    legacy_raw = json.dumps(legacy, separators=(',', ':')).encode()
    fx.put('', 'index.json', legacy_raw)
    legacy_sha = sha(legacy_raw)
    # Exact canonical backfill progress for that legacy index (complete).
    progress = {'format': 'UIE_HIRES_BACKFILL_PROGRESS1', 'legacyIndexSha256': legacy_sha,
                'rendererIdentity': identity, 'atlasSha256': atlas, 'tileCount': TILES, 'atlasLinked': True,
                'objects': current_tiles}
    fx.put('', 'backfill-progress.json', canonical(progress))

    def index(tiles: dict, generation: int) -> str:
        doc = {'format': hb.INDEX_FORMAT_V3, 'tileSize': 64, 'rendererIdentity': identity, 'atlasSha256': atlas,
               'tiles': tiles,
               'context': {'world': 'uie-synthetic', 'dimension': '0', 'renderer': 'synthetic', 'caves': False,
                           'layoutSha256': layout},
               'source': {'legacyIndexSha256': legacy_sha if generation == 0 else sha(f'legacy {generation}'.encode()),
                          'producerFinishedAt': now - 60 * (generation + 1), 'planComplete': True,
                          'plannedJobs': 983, 'notScheduled': 0, 'failedJobs': 0, 'failedTiles': 0,
                          'staleTiles': []}}
        return fx.asset('indexes', '.json', canonical(doc))

    generations = [index(current_tiles, 0)]
    for generation in range(1, 5):                         # 1 previous, 2 recent, 3-4 used by the 5-roots scenario
        generations.append(index(generation_tiles(generation, current_tiles), generation))
    for i in range(ORPHANS):
        fx.asset('objects', '.uih', f'synthetic orphan {i} (never referenced)\n'.encode())
    fx.put('', 'current.json', canonical({'format': hb.POINTER_FORMAT, 'indexSha256': generations[0]}))
    manifest = {'label': LABEL, 'syntheticNow': now, 'tiles': TILES, 'orphans': ORPHANS,
                'changedTilesPerGeneration': CHANGED_PER_GENERATION, 'generations': generations,
                'legacyIndexSha256': legacy_sha, 'logicalBytes': fx.bytes, 'files': fx.files}
    write_journal(out, manifest, 3)
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1))
    return manifest


def write_journal(out: Path, manifest: dict, roots: int):
    """Journal naming `roots` generations as recent (all >= now - 30 min): current, previous, then others."""
    now = manifest['syntheticNow']
    generations = manifest['generations']
    doc = {'format': hh.FORMAT, 'currentIndexSha256': generations[0], 'previousIndexSha256': generations[1],
           'lastPublished': {generations[g]: now - 60 * (g + 1) for g in range(roots)}}
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    tmp = out / f'.{hh.NAME}.synthetic'
    tmp.write_bytes(raw)
    os.replace(tmp, out / hh.NAME)


def _rss_kib():
    try:
        with open('/proc/self/status') as status:
            for line in status:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def _peak_kib():
    if not sys.platform.startswith('linux'):
        return None
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if value > 0 else None


def measure(out: Path) -> dict:
    """Child process: one locked plan_retention, then one collect(execute=False). Never executes."""
    import fcntl
    import hires_gc as gc
    manifest = json.loads((out / MANIFEST).read_text())
    now = manifest['syntheticNow']
    result = {'baselineRssKiB': _rss_kib()}
    fd = os.open(out / '.build.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)     # the planner's documented precondition
        started = time.perf_counter()
        try:
            plan = retention.plan_retention(out, now=now)
            result['plan'] = {'seconds': round(time.perf_counter() - started, 3),
                              **{k: plan[k] for k in ('scanned', 'metadataBytes', 'retainedIndexes',
                                                      'candidateCount', 'candidatesOmitted', 'candidateTotals',
                                                      'preparationProgressValid', 'unknown')}}
        except (OSError, ValueError) as error:
            result['plan'] = {'seconds': round(time.perf_counter() - started, 3),
                              'error': f'{type(error).__name__}: {error}'}
        result['peakRssAfterPlanKiB'] = _peak_kib()
    finally:
        os.close(fd)                                       # collect takes the lock itself
    started = time.perf_counter()
    try:
        dry = gc.collect(out, execute=False, now=now)
        result['collect'] = {'seconds': round(time.perf_counter() - started, 3),
                             **{k: dry.get(k) for k in ('state', 'executed', 'selected', 'selectedBytes', 'removed',
                                                       'candidateCount', 'scanned', 'metadataBytes',
                                                       'retainedIndexes', 'preparationProgressValid')}}
        if dry.get('executed') is not False or dry.get('removed') != 0:
            raise SystemExit('unexpected execution in a dry run')
    except (OSError, ValueError) as error:
        result['collect'] = {'seconds': round(time.perf_counter() - started, 3),
                             'error': f'{type(error).__name__}: {error}'}
    result['peakRssKiB'] = _peak_kib()
    metadata = result['plan'].get('metadataBytes')
    if isinstance(metadata, int):
        result['metadataBudgetUsed'] = round(metadata / retention.MAX_METADATA_BYTES, 4)
        result['metadataHeadroomBytes'] = retention.MAX_METADATA_BYTES - metadata
    return result


def child(out: Path) -> dict:
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--measure', str(out)],
                          capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        return {'error': f'child exit {proc.returncode}', 'stderr': proc.stderr[-2000:]}
    return json.loads(proc.stdout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--dir', type=Path)
    parser.add_argument('--measure', type=Path, help=argparse.SUPPRESS)   # internal: child process
    args = parser.parse_args(argv)
    if os.name != 'posix':
        raise SystemExit('requires POSIX (flock); run on the isolated Ubuntu host')
    if args.measure is not None:
        print(json.dumps(measure(args.measure.resolve())))
        return 0
    if args.dir is None:
        parser.error('--dir is required')
    out = _check_target(args.dir)
    started = time.perf_counter()
    manifest = build(out)
    report = {'label': LABEL, 'directory': str(out.resolve()), 'buildSeconds': round(time.perf_counter() - started, 1),
              'fixture': {k: manifest[k] for k in ('tiles', 'orphans', 'changedTilesPerGeneration', 'files',
                                                   'logicalBytes', 'syntheticNow')},
              'limits': {'maxScanned': retention.MAX_SCANNED, 'maxMetadataBytes': retention.MAX_METADATA_BYTES,
                         'maxCandidates': retention.MAX_CANDIDATES},
              'scenarios': {}}
    report['scenarios']['3-roots'] = child(out.resolve())
    write_journal(out.resolve(), manifest, 5)            # same directory, two more recent generations
    report['scenarios']['5-roots'] = child(out.resolve())
    report['note'] = 'synthetic contents, no render correctness claim; collect ran with execute=False only'
    print(json.dumps(report, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
