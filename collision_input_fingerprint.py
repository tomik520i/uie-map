"""Fingerprint exactly the decoded block/biome inputs consumed by capture_batch.

Only small tile retries use this digest. Entity/tick/light fields are not consumed
by the offline renderer. Registry and geometry identities remain separate gates.
Concurrent writes during reading, malformed chunks and missing biomes still fail.
"""
import hashlib
import json
import os
from pathlib import Path
import reader
import world_hires as wh


def chunk_digest(chunk):
    if not isinstance(chunk.biomes,bytes) or len(chunk.biomes)!=256:
        raise ValueError('invalid collision input biomes')
    h=hashlib.sha256(b'UIE_RENDER_CHUNK_INPUT1')
    h.update(json.dumps([chunk.x,chunk.z]).encode());h.update(chunk.biomes)
    for y,section in sorted(chunk.sections.items()):
        h.update(bytes([y]))
        for name in ('blocks','add','blocks16','data'):
            value=getattr(section,name)
            h.update(name.encode())
            if value is None:h.update(b'none')
            else:h.update(len(value).to_bytes(4,'big'));h.update(value)
    return h.digest()


def fingerprint(region_dir,area):
    h=hashlib.sha256(b'UIE_COLLISION_RENDER_INPUT1')
    for rx,rz in wh._regions_of(area):
        path=Path(region_dir)/f'r.{rx}.{rz}.mca'
        try:stream=path.open('rb')
        except FileNotFoundError:
            h.update(json.dumps([rx,rz,'missing']).encode());continue
        with stream:
            before=os.fstat(stream.fileno());header=stream.read(8192)
            if len(header)!=8192:raise ValueError('incomplete collision region header')
            with reader.RegionFile(stream,rx,rz) as region:
                for cz in range(max(area.z//16,rz*32),min((area.z+area.depth)//16,rz*32+32)):
                    for cx in range(max(area.x//16,rx*32),min((area.x+area.width)//16,rx*32+32)):
                        root=region.read_chunk_nbt(cx&31,cz&31)
                        h.update(json.dumps([cx,cz,root is not None]).encode())
                        if root is not None:
                            chunk=reader.decode_chunk(root)
                            if (chunk.x,chunk.z)!=(cx,cz):raise ValueError('collision chunk coordinates differ')
                            h.update(chunk_digest(chunk))
            after=path.stat();stream.seek(0)
            if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or stream.read(8192)!=header):
                raise ValueError('region changed while reading renderer inputs')
    return h.hexdigest()
