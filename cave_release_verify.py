"""Read-only whole cave release audit. Caller holds .build.lock throughout.

Validates stored objects, not current Minecraft-world freshness. Never writes.
"""
from pathlib import Path
import hashlib
import json
import zlib
import cave_pair as cp
import cave_pair_index as ci
import hires_bundle as hb


def canonical(doc):
    return json.dumps(doc,sort_keys=True,separators=(',',':')).encode()


def read_json(path,limit):
    raw=hb._read(path,limit)[0]
    doc=hb._strict_json(raw,'release audit')
    if canonical(doc)!=raw:raise ValueError('Noncanonical release JSON')
    return raw,doc


def read_pointer(root,fmt):
    raw,doc=read_json(root/'current.json',4096)
    if (not isinstance(doc,dict) or set(doc)!={'format','indexSha256'} or doc['format']!=fmt
        or not isinstance(doc['indexSha256'],str) or not hb.HEX64.fullmatch(doc['indexSha256'])):
        raise ValueError('Invalid release pointer')
    return raw,doc['indexSha256']


def read_hash(path,sha,limit):
    raw=hb._read(path,limit)[0]
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Release object hash mismatch')
    return raw


def decode_caps(raw):
    limit=cp.CAP_HEADER.size+cp.PAYLOAD
    reader=zlib.decompressobj(16+zlib.MAX_WBITS)
    try:data=reader.decompress(raw,limit+1)
    except zlib.error as e:raise ValueError('Invalid cap gzip') from e
    if len(data)!=limit or not reader.eof or reader.unused_data or reader.unconsumed_tail:
        raise ValueError('Incomplete, trailing or oversized cap gzip')
    return data


def verify_release(out,*,expected_identity,require_complete=True):
    if not isinstance(expected_identity,str) or not hb.HEX64.fullmatch(expected_identity):raise ValueError('Invalid expected identity')
    if type(require_complete)is not bool:raise ValueError('require_complete must be bool')
    out=hb._owned(Path(out));caps_root=out/'cave-data'
    for folder in ('indexes','objects','atlases','layouts','cave-data','cave-data/indexes','cave-data/pairs','cave-data/caps'):
        hb._real_dir(out/folder)
    gp,gs=read_pointer(out,hb.POINTER_FORMAT);cpointer,cs=read_pointer(caps_root,'UIE_CAVE_CAP_POINTER1')
    gr=read_hash(out/'indexes'/f'{gs}.json',gs,hb.MAX_INDEX_BYTES)
    cr=read_hash(caps_root/'indexes'/f'{cs}.json',cs,hb.MAX_INDEX_BYTES)
    g=hb._strict_json(gr,'geometry index');c=hb._strict_json(cr,'cap index')
    if canonical(g)!=gr or canonical(c)!=cr:raise ValueError('Noncanonical index')
    if not isinstance(c,dict) or set(c)!={'format','geometryIndexSha256','rendererIdentity','atlasSha256','capIdentity','pairs'}:
        raise ValueError('Invalid cap index schema')
    if c['format']!=ci.FORMAT or c['geometryIndexSha256']!=gs or c['rendererIdentity']!=expected_identity:
        raise ValueError('Cap index binding differs')
    if not isinstance(c['pairs'],dict) or len(c['pairs'])>100000:raise ValueError('Invalid pair map')
    # Existing index validator checks the entire geometry schema, context and source.
    ci.build(gr,{},cap_identity=c['capIdentity'])
    if g['rendererIdentity']!=expected_identity or c['capIdentity']!=cp.cap_identity():raise ValueError('Renderer/cap identity differs')
    source=g['source']
    if require_complete and (not source['planComplete'] or any(source[k] for k in ('notScheduled','failedJobs','failedTiles','staleTiles'))):
        raise ValueError('Cave release is incomplete or stale')
    if set(c['pairs'])!=set(g['tiles']):raise ValueError('Pair coverage differs from geometry')
    atlas=read_hash(out/'atlases'/(g['atlasSha256']+'.png'),g['atlasSha256'],hb.MAX_OBJECT_BYTES);hb._verify_png(atlas)
    layout_sha=g['context']['layoutSha256']
    hb._verify_layout(read_hash(out/'layouts'/(layout_sha+'.json'),layout_sha,hb.MAX_INDEX_BYTES))
    docs={}
    for key,sha in c['pairs'].items():
        if not isinstance(sha,str) or not hb.HEX64.fullmatch(sha):raise ValueError('Invalid pair hash')
        raw=read_hash(caps_root/'pairs'/(sha+'.json'),sha,8192)
        doc=hb._strict_json(raw,'pair');cp.validate(doc)
        if cp.canonical(doc)!=raw:raise ValueError('Noncanonical pair')
        geom=hb._read(out/'objects'/(doc['geometrySha256']+'.uih'),cp.MAX_GEOMETRY_BYTES)[0]
        cap=decode_caps(hb._read(caps_root/'caps'/(doc['capSha256']+'.uic.gz'),cp.CAP_HEADER.size+cp.PAYLOAD+65536)[0])
        cp.verify(doc,geom,cap);docs[key]=doc
    rebuilt,rebuilt_raw=ci.build(gr,docs,cap_identity=c['capIdentity'])
    if rebuilt!=cs or rebuilt_raw!=cr:raise ValueError('Rebuilt cap index differs')
    if hb._read(out/'current.json',4096)[0]!=gp or hb._read(caps_root/'current.json',4096)[0]!=cpointer:
        raise ValueError('Release pointers changed')
    return dict(ok=True,geometryIndexSha256=gs,capIndexSha256=cs,verifiedPairs=len(docs),
                rendererIdentity=expected_identity,planComplete=source['planComplete'],
                pointersUnchanged=True,worldFreshnessChecked=False,filesDeleted=0)
