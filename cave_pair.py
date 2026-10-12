"""Content-addressed cave geometry/cap pair. Private; no public pointer updates.

Caps and geometry must be generated from one Volume. This module verifies framing,
coordinates and hashes, not that two separately captured inputs were simultaneous.
The renderer is responsible for calling it before releasing that shared Volume.
"""
import gzip
import hashlib
import json
import re
import struct
from pathlib import Path
from cave_cap_codec import HEADER as CAP_HEADER, PAYLOAD, SIDE, HEIGHT

FORMAT='UIE_CAVE_PAIR1'
SHA=re.compile(r'[0-9a-f]{64}')
FIELDS={'format','origin','geometrySha256','capSha256','atlasSha256','rendererIdentity',
        'capIdentity','geometryBytes','capBytes','presentMask'}
MAX_GEOMETRY_BYTES=64*1024*1024
MAX_GEOMETRY_RAW=256*1024*1024

def cap_identity():
    h=hashlib.sha256(b'UIE_CAVE_PAIR1\0')
    for name in ('cave_cap_codec.py','cave_cap_export.py'):
        h.update(Path(__file__).with_name(name).read_bytes())
    return h.hexdigest()

def validate(doc):
    if not isinstance(doc,dict) or set(doc)!=FIELDS or doc['format']!=FORMAT:
        raise ValueError('Invalid cave pair fields')
    origin=doc['origin']
    if not isinstance(origin,list) or len(origin)!=2 or any(type(v) is not int or v%64 or v< -30_000_000 or v+64>30_000_000 for v in origin):
        raise ValueError('Invalid cave pair origin')
    for name in ('geometrySha256','capSha256','atlasSha256','rendererIdentity','capIdentity'):
        if not isinstance(doc[name],str) or not SHA.fullmatch(doc[name]):raise ValueError('Invalid cave pair hash')
    if type(doc['geometryBytes']) is not int or not 1<=doc['geometryBytes']<=MAX_GEOMETRY_BYTES:
        raise ValueError('Invalid cave geometry length')
    if type(doc['capBytes']) is not int or doc['capBytes']!=CAP_HEADER.size+PAYLOAD:
        raise ValueError('Invalid cave cap length')
    if type(doc['presentMask']) is not int or not 1<=doc['presentMask']<=65535:
        raise ValueError('Cave pair needs known chunks')
    return doc

def _origins(geometry,caps):
    if not isinstance(geometry,bytes) or not 1<=len(geometry)<=MAX_GEOMETRY_BYTES:
        raise ValueError('Invalid cave geometry bytes')
    if not isinstance(caps,bytes) or len(caps)!=CAP_HEADER.size+PAYLOAD:
        raise ValueError('Invalid cave cap bytes')
    # Bound decompression BEFORE allocation of arbitrary untrusted expanded output.
    import zlib
    reader=zlib.decompressobj(16+zlib.MAX_WBITS)
    try:raw=reader.decompress(geometry,MAX_GEOMETRY_RAW+1)
    except zlib.error as error:raise ValueError('Invalid cave geometry gzip') from error
    if len(raw)>MAX_GEOMETRY_RAW or not reader.eof or reader.unused_data or reader.unconsumed_tail:
        raise ValueError('Invalid or oversized cave geometry gzip')
    if len(raw)<24:raise ValueError('Truncated cave geometry')
    magic,version,flags,x,z,n0,n1=struct.unpack_from('<4sHHiiII',raw)
    if magic!=b'UIH1' or version!=1 or flags!=0 or not n0+n1 or len(raw)!=24+(n0+n1)*52:
        raise ValueError('Invalid cave geometry framing')
    cm,cv,cx,cz,width,depth,height,mask=CAP_HEADER.unpack_from(caps)
    if (cm,cv,width,depth,height)!=(b'UIC1',1,SIDE,SIDE,HEIGHT) or caps[5:8]!=b'\0\0\0':
        raise ValueError('Invalid cave cap header')
    if (x,z)!=(cx,cz):raise ValueError('Geometry and cap origins differ')
    return [x,z],mask

def create(geometry,caps,*,atlas_sha,renderer_identity):
    origin,mask=_origins(geometry,caps)
    doc={'format':FORMAT,'origin':origin,'geometrySha256':hashlib.sha256(geometry).hexdigest(),
         'capSha256':hashlib.sha256(caps).hexdigest(),'atlasSha256':atlas_sha,
         'rendererIdentity':renderer_identity,'capIdentity':cap_identity(),
         'geometryBytes':len(geometry),'capBytes':len(caps),'presentMask':mask}
    return validate(doc)

def verify(doc,geometry,caps):
    validate(doc)
    if len(geometry)!=doc['geometryBytes'] or len(caps)!=doc['capBytes']:
        raise ValueError('Cave pair lengths differ')
    if hashlib.sha256(geometry).hexdigest()!=doc['geometrySha256'] or hashlib.sha256(caps).hexdigest()!=doc['capSha256']:
        raise ValueError('Cave pair hashes differ')
    origin,mask=_origins(geometry,caps)
    if origin!=doc['origin'] or mask!=doc['presentMask']:raise ValueError('Cave pair metadata differs')
    return doc

def canonical(doc):
    return json.dumps(validate(doc),sort_keys=True,separators=(',',':')).encode()
