"""Canonical cap index tied to one already published immutable geometry index.

No writes. Pair manifests are content-addressed; payload validation remains the
producer's responsibility and the browser verifies cap bytes again when loading.
"""
import hashlib,json,re
from cave_pair import validate,canonical
from hires_context import validate_context
from hires_source import validate_source

FORMAT='UIE_CAVE_CAP_INDEX1'
HEX=re.compile(r'[0-9a-f]{64}')
KEY=re.compile(r'(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})')

def no_duplicates(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON key')
        result[key]=value
    return result

def build(geometry_raw,pair_documents,*,cap_identity):
    if not isinstance(geometry_raw,bytes) or len(geometry_raw)>16*1024*1024:raise ValueError('Invalid geometry index length')
    geometry=json.loads(geometry_raw,object_pairs_hook=no_duplicates)
    if (not isinstance(geometry,dict) or geometry.get('format')!='UIE_HIRES_IMMUTABLE_INDEX3'
        or geometry.get('tileSize')!=64 or not isinstance(geometry.get('tiles'),dict)
        or len(geometry['tiles'])>100000):raise ValueError('Invalid immutable geometry index')
    if set(geometry)!={'format','tileSize','rendererIdentity','atlasSha256','tiles','context','source'}:
        raise ValueError('Invalid geometry index fields')
    for key,value in geometry['tiles'].items():
        match=KEY.fullmatch(key) if isinstance(key,str) else None
        if not match or any(int(v)*64< -30000000 or int(v)*64+64>30000000 for v in match.groups()) or not isinstance(value,str) or not HEX.fullmatch(value):
            raise ValueError('Invalid geometry tile')
    context=validate_context(geometry.get('context'))
    if not context['caves']:raise ValueError('Cap index requires cave geometry')
    validate_source(geometry.get('source'),geometry['tiles'])
    for value in [geometry.get('atlasSha256'),geometry.get('rendererIdentity'),cap_identity]:
        if not isinstance(value,str) or not HEX.fullmatch(value):raise ValueError('Invalid cap index identity')
    if not isinstance(pair_documents,dict) or len(pair_documents)>100000:raise ValueError('Invalid cap pairs')
    refs={}
    for key,pair in pair_documents.items():
        match=KEY.fullmatch(key) if isinstance(key,str) else None
        if not match:raise ValueError('Invalid cap tile key')
        origin=[int(v)*64 for v in match.groups()]
        validate(pair)
        if (pair['origin']!=origin or pair['geometrySha256']!=geometry['tiles'].get(key)
            or pair['atlasSha256']!=geometry['atlasSha256'] or pair['rendererIdentity']!=geometry['rendererIdentity']
            or pair['capIdentity']!=cap_identity):raise ValueError('Cap pair differs from geometry generation')
        refs[key]=hashlib.sha256(canonical(pair)).hexdigest()
    doc={'format':FORMAT,'geometryIndexSha256':hashlib.sha256(geometry_raw).hexdigest(),
         'rendererIdentity':geometry['rendererIdentity'],'atlasSha256':geometry['atlasSha256'],
         'capIdentity':cap_identity,'pairs':refs}
    raw=json.dumps(doc,sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest(),raw
