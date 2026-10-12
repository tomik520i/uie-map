"""UIC1 prototype: two bits per voxel for cave cut caps, not block IDs or collision shapes.

Raw payload (transport may gzip once):24-byte little-endian header plus262144bytes.
Classes0=empty,1=solid cut,2=water,3=lava. z-major,x,y, four Y cells per byte.
Header ends with a 16-bit chunk-presence mask (z-major, four chunks per row).
Missing chunks are unknown, not air; callers must provide the mask explicitly.
"""
import struct
HEADER=struct.Struct('<4sB3xiiHHHH')
SIDE=64
HEIGHT=256
CELLS=SIDE*SIDE*HEIGHT
PAYLOAD=CELLS//4

def encode(origin_x,origin_z,classes,*,present_mask):
    for value in (origin_x,origin_z):
        if type(value) is not int or value%SIDE or value < -30_000_000 or value+SIDE>30_000_000:
            raise ValueError('Invalid tile origin')
    if type(present_mask) is not int or not 0<=present_mask<=65535:
        raise ValueError('Invalid chunk-presence mask')
    if len(classes)!=CELLS:raise ValueError('Expected64x64x256classes')
    packed=bytearray(PAYLOAD)
    for i,code in enumerate(classes):
        if type(code) is not int or not 0<=code<=3:raise ValueError('Invalid cap class')
        packed[i//4]|=code<<((i%4)*2)
    return HEADER.pack(b'UIC1',1,origin_x,origin_z,SIDE,SIDE,HEIGHT,present_mask)+packed
