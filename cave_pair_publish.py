"""Publish the companion index after a cave geometry publication.

Caller holds out/.build.lock exclusively. Uses producer-verified immutable pair
objects; does not decompress the entire world every tick. Manifests are hashed,
payload existence/size is checked, and HTTP verifies payload hashes on access.
No deletion and no surface-pointer changes. Missing/stale pairs leave the old
cap pointer untouched (the browser rejects its mismatched geometry generation).

Fast path (no job or pair reads): when cave-data/current.json already names a canonical, hash-verified cap index
for exactly this geometry index, atlas, renderer and cap identity that covers every geometry tile, the publication
is unchanged. Geometry indexes are immutable and a cap index is only published complete, so later job changes for
the same geometry cannot make it wrong; payloads stay verified by the producer and the HTTP layer as before.
A missing cap pointer falls through to the full scan; an invalid or symlinked pointer/index fails closed.

Pairs whose geometry or capIdentity differ from the current geometry / cap identity are counted as stale (waiting,
never published, identity never guessed). missing = tiles without any usable pair reference; remaining = missing +
stale; the cap index is published only when remaining is 0.
"""
import hashlib,json,time
import hires_bundle as hb
import cave_pair as cp
import cave_pair_index as ci
import cave_pair_store as cs
import cave_pair_history as history
import hires_history as root_history

POINTER_FORMAT='UIE_CAVE_CAP_POINTER1'
INDEX_FIELDS={'format','geometryIndexSha256','rendererIdentity','atlasSha256','capIdentity','pairs'}
MAX_CAP_INDEX_BYTES=16*1024*1024

def _canonical(doc):
    return json.dumps(doc,sort_keys=True,separators=(',',':')).encode()

def _published_document(root):
    """Read and validate the actual cap pointer and its immutable index."""
    try:raw,_=hb._read(root/'current.json',4096)          # lstat-regular: a symlinked pointer is refused
    except FileNotFoundError:return None
    pointer=hb._strict_json(raw,'cave cap pointer')
    if (not isinstance(pointer,dict) or set(pointer)!={'format','indexSha256'} or pointer['format']!=POINTER_FORMAT
            or not isinstance(pointer['indexSha256'],str) or not hb.HEX64.fullmatch(pointer['indexSha256'])
            or _canonical(pointer)!=raw):
        raise ValueError('Invalid cave cap pointer')
    sha=pointer['indexSha256']
    try:
        hb._real_dir(root/'indexes')                          # a symlinked indexes folder is refused
        raw,_=hb._read(root/'indexes'/f'{sha}.json',MAX_CAP_INDEX_BYTES)
    except FileNotFoundError:raise ValueError('Cave cap pointer names a missing index') from None
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Cave cap index hash mismatch')
    doc=hb._strict_json(raw,'cave cap index')
    if (not isinstance(doc,dict) or set(doc)!=INDEX_FIELDS or doc['format']!=ci.FORMAT
            or not all(isinstance(doc[k],str) and hb.HEX64.fullmatch(doc[k])
                       for k in ('geometryIndexSha256','rendererIdentity','atlasSha256','capIdentity'))
            or not isinstance(doc['pairs'],dict) or len(doc['pairs'])>100000):
        raise ValueError('Invalid cave cap index')
    for key,pair in doc['pairs'].items():
        if not isinstance(key,str) or not ci.KEY.fullmatch(key) or not isinstance(pair,str) or not hb.HEX64.fullmatch(pair):
            raise ValueError('Invalid cave cap index reference')
    if _canonical(doc)!=raw:raise ValueError('Cave cap index is not canonical')
    return sha,doc

def _published_unchanged(root,geometry_sha,geometry,cap_identity):
    published=_published_document(root)
    if published is None:return None
    sha,doc=published
    if (doc['geometryIndexSha256']!=geometry_sha or doc['rendererIdentity']!=geometry.get('rendererIdentity')
            or doc['atlasSha256']!=geometry.get('atlasSha256') or doc['capIdentity']!=cap_identity
            or set(doc['pairs'])!=set(geometry['tiles'])):
        return None                                           # not this exact complete publication: full scan
    return sha,len(doc['pairs'])

def _record_history(out,root,sha,geometry_sha):
    """History is durable before any pointer change; caller still holds build lock.

    Conservatively preserve all geometry records in the bounded root history,
    including previous/current. No directory-wide payload reads and no deletion.
    A missing root journal is allowed for initial private stores; malformed is not.
    """
    published=_published_document(root)
    pointer_sha=published[0] if published else None
    pointer_geometry=published[1]['geometryIndexSha256'] if published else None
    protected=set(root_history._read_journal(out))|{geometry_sha}
    prepared=history.prepare(history.load(root),pointer_sha=pointer_sha,
        pointer_geometry_sha=pointer_geometry,index_sha=sha,geometry_sha=geometry_sha,
        now=time.time(),protected_geometry=protected)
    if prepared.changed:hb._atomic(root/history.NAME,prepared.raw)

def publish_from_jobs(out,geometry_sha,*,cap_identity=None):
    out=hb._owned(out)
    cap_identity=cp.cap_identity() if cap_identity is None else cap_identity
    if not isinstance(cap_identity,str) or not hb.HEX64.fullmatch(cap_identity):raise ValueError('Invalid cap identity')
    current=hb._current_index(out)
    if not current or current[0]!=geometry_sha:raise ValueError('Cave geometry pointer changed')
    if not current[2] or not current[2]['caves']:raise ValueError('Cave publisher refuses surface geometry')
    geometry_raw,_=hb._read(out/'indexes'/f'{geometry_sha}.json',hb.MAX_INDEX_BYTES)
    geometry=hb._strict_json(geometry_raw,'cave geometry')
    root=out/'cave-data'
    hb._real_dir(root)
    unchanged=_published_unchanged(root,hashlib.sha256(geometry_raw).hexdigest(),geometry,cap_identity)
    if unchanged is not None:
        sha,pairs=unchanged
        _record_history(out,root,sha,geometry_sha)
        if hb._current_index(out)!=current:raise ValueError('Cave geometry changed during cap check')
        return {'state':'published','published':True,'indexSha256':sha,'pairs':pairs,'unchanged':True,'fastPath':True}
    for folder in [out/'jobs',out/'objects',root/'pairs',root/'caps']:hb._real_dir(folder)
    refs={}
    paths=list((out/'jobs').glob('*.json'))
    if len(paths)>100000:raise ValueError('Too many cave job records')
    for path in paths:
        raw,_=hb._read(path,2*1024*1024)
        job=hb._strict_json(raw,'cave job')
        pairs=job.get('capPairs',{}) if isinstance(job,dict) else None
        if not isinstance(pairs,dict) or len(pairs)>64:raise ValueError('Invalid cave job pair inventory')
        for key,sha in pairs.items():
            if key not in geometry['tiles']:continue
            if not isinstance(sha,str) or not hb.HEX64.fullmatch(sha):raise ValueError('Invalid cave pair reference')
            if key in refs and refs[key]!=sha:raise ValueError('Conflicting cave pair references')
            refs[key]=sha
    documents={}
    stale=0
    for key,sha in refs.items():
        raw,_=hb._read(root/'pairs'/f'{sha}.json',4096)
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Cave pair manifest hash mismatch')
        doc=hb._strict_json(raw,'cave pair');cp.validate(doc)
        if cp.canonical(doc)!=raw:raise ValueError('Cave pair manifest is not canonical')
        # Another geometry revision or cap rules of another producer code: waiting, never published.
        if doc['geometrySha256']!=geometry['tiles'][key] or doc['capIdentity']!=cap_identity:stale+=1;continue
        if hb._regular(out/'objects'/f"{doc['geometrySha256']}.uih").st_size!=doc['geometryBytes']:
            raise ValueError('Cave geometry payload size differs')
        size=hb._regular(root/'caps'/f"{doc['capSha256']}.uic.gz").st_size
        if size<1 or size>1024*1024:raise ValueError('Invalid compressed cave cap size')
        documents[key]=doc
    missing=len(geometry['tiles'])-len(documents)-stale
    if missing or stale:
        return {'state':'waiting-for-pairs','published':False,'missing':missing,'stale':stale,'remaining':missing+stale}
    sha,raw=ci.build(geometry_raw,documents,cap_identity=cap_identity)
    cs._directory(root/'indexes')
    cs._content(root/'indexes'/f'{sha}.json',raw)
    _record_history(out,root,sha,geometry_sha)
    if hb._current_index(out)!=current:raise ValueError('Cave geometry changed before cap commit')
    pointer=_canonical({'format':POINTER_FORMAT,'indexSha256':sha})
    old=None
    try:old,_=hb._read(root/'current.json',4096)
    except FileNotFoundError:pass
    if old!=pointer:hb._atomic(root/'current.json',pointer)
    return {'state':'published','published':True,'indexSha256':sha,'pairs':len(documents),'unchanged':old==pointer}
