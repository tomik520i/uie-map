"""Private full-cell cave caps from the SAME Volume used for geometry.

Only full cube/fallback models and liquids get a full-cell cap. Partial models,
plants and technical/invisible models do not get invented cube caps. This first
format is a coarse cut mask, not block IDs, exact model silhouettes or collisions.
Partial-model omissions and diagnostic fallback cells are counted explicitly.
No filesystem writes; the caller must bind these bytes to the matching geometry.
"""
from collections import Counter
from cave_cap_codec import CELLS, SIDE, HEIGHT, encode


def build_cap_tile(volume, catalog):
    core=volume.core
    if core.width!=SIDE or core.depth!=SIDE or core.x%SIDE or core.z%SIDE:
        raise ValueError('Caps require a 64-aligned 64x64 core')
    mask=0
    for cz in range(4):
        for cx in range(4):
            if (core.x//16+cx,core.z//16+cz) in volume.present:
                mask|=1<<(cz*4+cx)
    cells=bytearray(CELLS)
    memo={};counts=Counter();partial=Counter();fallback=0
    for z in range(SIDE):
        for x in range(SIDE):
            if not mask & (1<<((z//16)*4+x//16)):
                continue
            col=volume.column_index(core.x+x,core.z+z)
            if col<0:raise ValueError('Present chunk has no column')
            base=col*HEIGHT;target=(z*SIDE+x)*HEIGHT
            for y in range(HEIGHT):
                block_id=volume.ids[base+y]
                if not block_id:continue
                meta=volume.metas[base+y]&15;key=(block_id,meta)
                if key not in memo:
                    name=volume.registry.get(block_id)
                    if name is None:raise ValueError('Block missing from registry')
                    model=catalog.model(name,meta)
                    if model.kind in ('cube','fallback'):code=1
                    elif model.kind=='liquid':code=3 if 'lava' in name.lower() else 2
                    else:code=0
                    omitted=model.kind not in ('cube','fallback','liquid','none','cross','vine','lily','ladder')
                    memo[key]=(code,model.kind if omitted else None,model.tier=='fallback')
                code,omitted,is_fallback=memo[key]
                cells[target+y]=code;counts[code]+=1
                if omitted:partial[omitted]+=1
                if is_fallback:fallback+=1
    return encode(core.x,core.z,cells,present_mask=mask), {
        'presentChunks':mask.bit_count(), 'classifiedNonAir':dict(counts),
        'partialModelsWithoutCap':dict(partial), 'fallbackCubeCells':fallback,
        'preciseCollisionData':False,
    }
