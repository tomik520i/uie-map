"""One region transaction for a future world runner. Caller holds out/.export.lock.

Staging never makes a failed/changed region visible. Publication retries warm the
process cache; source and surface are checked again before each publication attempt.
No service, scheduling or deletion. Hardlinks count at full logical size in budget.
"""
from pathlib import Path
import hashlib
import os
import shutil
import stat

import collision_bundle as cb
import collision_export as ce
import collision_pair_stage as stage
import collision_region_probe as probe

MIB = 1024**2


def logical_bytes(root):
    """Strict owned-output accounting: unreadable entries and links refuse, never undercount."""
    total = 0
    def failed(error):
        raise error
    stage._directory(Path(root))
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        for name in dirs + files:
            info = (Path(directory) / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError('unexpected nonregular output entry')
    return total


def stage_region(jx, jz, *, out, world, hires_store, tools, tiles=None,
                 budget_bytes=30*1024**3, min_free_bytes=24*1024**3,
                 disk_free=lambda p: shutil.disk_usage(p).free):
    out, world, hires_store = map(Path, (out, world, hires_store))
    stage._directory(out)
    ce.check_paths(out, world=world, hires=hires_store)
    for name in ('store', 'scratch'):
        (out / name).mkdir(exist_ok=True)
        stage._directory(out / name)
    used = logical_bytes(out)
    # Reserve scratch, one worst pair, and index/status/ledger atomic replacements.
    metadata = 64*MIB
    area, _ = probe.wh.job_area(jx, jz)
    sizes = []
    for rx, rz in probe.wh._regions_of(area):
        path = world/'region'/f'r.{rx}.{rz}.mca'
        try: info=path.stat()
        except FileNotFoundError: continue
        if not stat.S_ISREG(info.st_mode):raise ValueError('source region is not a regular file')
        sizes.append(info.st_size)
    scratch = max(512*MIB, sum(sizes)*11//10+MIB)
    if scratch>4*1024**3:raise ValueError('region-too-large: scratch exceeds 4 GiB ceiling')
    if used + metadata + scratch + 72*MIB > budget_bytes:
        raise ValueError('store budget cannot reserve region staging')
    def on_match(key, volume, mesh):
        nonlocal used
        if used + metadata + scratch + 72*MIB > budget_bytes:
            raise ValueError('store budget exhausted during region')
        pair = stage.stage_pair(out/'store', hires_store, volume, mesh,
                                min_free_bytes=min_free_bytes, disk_free=disk_free)
        # Conservative even when the objects already existed; resets on next region.
        used += pair['geometryBytes'] + pair['blockGzipBytes'] + cb.MAX_PAIR_BYTES
        return pair
    return probe.probe_region(jx, jz, world=world, hires_store=hires_store,
                             scratch_root=out/'scratch', tools=tools, tiles=tiles,
                             on_match=on_match, budget_bytes=scratch,
                             min_free_bytes=min_free_bytes, disk_free=disk_free)


def accepted_updates(report, *, world, hires_store, tools):
    """Recheck a provisional report, e.g. after bounded cache warm-up. Never infer air."""
    if report.get('format') != probe.REPORT_FORMAT or report.get('ok') is not True:
        raise ValueError('region did not finish successfully')
    if report.get('scratchCleanup') is not True:
        raise ValueError('region scratch cleanup incomplete')
    jx, jz = report['region']
    area, _ = probe.wh.job_area(jx, jz)
    source = report['source']
    if (tools.fingerprint(Path(world)/'region', area) != source['after']
            or ce.registry_sha256(tools.load_registry(world)) != source['registryAfter']):
        raise ValueError('region source changed before publication')
    current = tools.read_index(hires_store)
    if (current['identity'] != report['visible']['identity']
            or current['atlasSha256'] != report['visible']['atlasSha256']):
        raise ValueError('surface identity changed before publication')
    updates = {}
    for key, entry in report['tiles'].items():
        if key in current['stale'] or current['tiles'].get(key) != entry['visibleSha256']:
            raise ValueError('surface tile changed before publication: '+key)
        if entry['result'] == 'unknown':
            continue
        if entry['result'] != 'match':
            raise ValueError('region contains an unaccepted tile')
        pair = entry['staged']
        tx,tz=ce.parse_tile(key)
        fields=('format','x','z','atlasSha256','geometrySha256','blocksSha256')
        if (pair.get('format')!=cb.PAIR_FORMAT or (pair.get('x'),pair.get('z'))!=(tx*64,tz*64)
                or hashlib.sha256(cb.canonical({k:pair[k] for k in fields})).hexdigest()!=pair['pairSha256']):
            raise ValueError('staged pair metadata does not match its hash')
        if (pair['geometrySha256'] != entry['geometrySha256']
                or pair['blocksSha256'] != entry['blockSha256']
                or pair['atlasSha256'] != current['atlasSha256']):
            raise ValueError('staged pair differs from checked region')
        updates[key] = pair['pairSha256']
    return updates
