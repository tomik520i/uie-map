"""Static per-chunk block identification from the SAME offline UIE1 geometry snapshot.

Only block registry names, IDs and metadata are published. No NBT or inventories.
Returned blockInfo can be inserted into the geometry manifest after checking its chunk set.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
from capture_snapshot import Snapshot, parse_snapshot, SnapshotError


def chunk_documents(snapshot: Snapshot):
    a = snapshot.area
    for cz in range(a.z // 16, (a.z + a.depth - 1) // 16 + 1):
        for cx in range(a.x // 16, (a.x + a.width - 1) // 16 + 1):
            x0, z0 = max(a.x, cx * 16), max(a.z, cz * 16)
            width = min(a.x + a.width, (cx + 1) * 16) - x0
            depth = min(a.z + a.depth, (cz + 1) * 16) - z0
            runs, registry = [], {}
            for z in range(z0, z0 + depth):
                for x in range(x0, x0 + width):
                    _, column = snapshot.column(x, z)
                    for block_id, meta in column:
                        if block_id not in snapshot.registry or not snapshot.registry[block_id]:
                            raise SnapshotError(f'Missing registry ID {block_id}')
                        if not 0 <= meta <= 15:
                            raise SnapshotError(f'Metadata outside 0..15: {meta}')
                        registry[str(block_id)] = snapshot.registry[block_id]
                        if runs and runs[-1][1:] == [block_id, meta]:
                            runs[-1][0] += 1
                        else:
                            runs.append([1, block_id, meta])
            yield cx, cz, {'format': 'UIE_BLOCKS1', 'x': x0, 'z': z0,
                           'width': width, 'depth': depth, 'height': 256,
                           'registry': registry, 'runs': runs}


def export_block_info(snapshot_path: Path, data_dir: Path) -> dict:
    snapshot_path, data_dir = Path(snapshot_path).resolve(), Path(data_dir).resolve()
    raw = snapshot_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = parse_snapshot(raw)
    # Content-addressed paths keep an old browser manifest compatible during publication.
    target = data_dir / 'blocks' / digest
    if snapshot_path == target or target in snapshot_path.parents:
        raise SnapshotError('Output would contain input')
    target.mkdir(parents=True, exist_ok=True)
    chunks = []
    for cx, cz, doc in chunk_documents(snapshot):
        name = f'chunk_{cx}_{cz}.json'
        payload = (json.dumps(doc, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        path = target / name
        if path.exists():
            if path.read_bytes() != payload:
                raise SnapshotError(f'Unexpected content in immutable output {path}')
        else:
            # Exclusive creation; a partially interrupted immutable file must be diagnosed,
            # never silently accepted as a valid previous export.
            with path.open('xb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        chunks.append({'x': cx, 'z': cz, 'file': path.relative_to(data_dir).as_posix()})
    if snapshot_path.read_bytes() != raw:
        raise SnapshotError('Snapshot changed during publication')
    return {'format': 'UIE_BLOCKS1', 'snapshotSha256': digest, 'chunks': chunks}


def attach_block_info(snapshot_path: Path, manifest_path: Path) -> dict:
    """Attach to an existing geometry manifest made from the same snapshot.

    The caller must supply the actual geometry input; chunk-set equality alone
    cannot prove that two snapshots of the same area came from the same moment.
    """
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    snapshot = parse_snapshot(Path(snapshot_path).read_bytes())
    a = snapshot.area
    expected = {(cx,cz) for cz in range(a.z//16,(a.z+a.depth-1)//16+1)
                for cx in range(a.x//16,(a.x+a.width-1)//16+1)}
    actual = {(c['x'],c['z']) for c in manifest['chunks']}
    if manifest.get('format') != 1 or actual != expected:
        raise SnapshotError('Geometry manifest has a different chunk set or format')
    info = export_block_info(snapshot_path,manifest_path.parent)
    manifest['blockInfo'] = info
    temp = manifest_path.with_name(manifest_path.name+'.block-info.tmp')
    with temp.open('x',encoding='utf-8') as stream:
        json.dump(manifest,stream,ensure_ascii=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temp,manifest_path)
    return info


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument('--data-dir', type=Path, help='Write chunks and print blockInfo JSON')
    output.add_argument('--manifest', type=Path, help='Write chunks and atomically update this geometry manifest')
    args = parser.parse_args()
    print(json.dumps(attach_block_info(args.snapshot,args.manifest) if args.manifest else export_block_info(args.snapshot,args.data_dir), indent=2))
