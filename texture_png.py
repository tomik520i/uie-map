"""Bounded non-interlaced PNG validation for mod resources, including indexed PNGs.

Validates image structure, CRCs, decompression, filters and palette indices.
Does not convert pixels, play animation, or validate unrelated ancillary metadata.
"""
import struct
import zlib
import atlas_identity


class UnsupportedPng(ValueError):
    pass


def verify(raw, max_pixels=4*1024*1024):
    width, height = atlas_identity._png_size(raw, 'texture')
    if width * height > max_pixels: raise ValueError('Texture exceeds pixel limit')
    depth, color, compression, filtering, interlace = raw[24:29]
    legal = {0:(1,2,4,8,16), 2:(8,16), 3:(1,2,4,8), 4:(8,16), 6:(8,16)}
    if color not in legal or depth not in legal[color] or compression or filtering or interlace not in (0,1):
        raise ValueError('Invalid PNG header modes')
    if interlace: raise UnsupportedPng('Interlaced PNG validation is not implemented')
    channels = {0:1,2:3,3:1,4:2,6:4}[color]
    stride = (width * channels * depth + 7)//8
    expected = (stride+1)*height
    pos=8; palette=None; transparency=False; idats=[]; closed=False; ended=False
    while pos < len(raw):
        if pos+12>len(raw): raise ValueError('Truncated PNG chunk')
        size=struct.unpack('>I',raw[pos:pos+4])[0]; kind=raw[pos+4:pos+8]; end=pos+size+12
        if end>len(raw): raise ValueError('Truncated PNG data')
        if any(not (65<=b<=90 or 97<=b<=122) for b in kind) or kind[2]&32:
            raise ValueError('Invalid PNG chunk type')
        data=raw[pos+8:end-4]
        if zlib.crc32(kind+data)&0xffffffff != struct.unpack('>I',raw[end-4:end])[0]:
            raise ValueError('PNG CRC mismatch')
        if kind==b'IHDR':
            if pos!=8: raise ValueError('Repeated IHDR')
        elif kind==b'PLTE':
            if palette is not None or idats or transparency or color in (0,4) or not size or size%3 or size>768:
                raise ValueError('Invalid PNG palette')
            palette=size//3
            if color==3 and palette>2**depth: raise ValueError('Palette exceeds bit depth')
        elif kind==b'tRNS':
            if transparency or idats: raise ValueError('Invalid PNG transparency order')
            transparency=True
            if color==3:
                if palette is None or not 1<=size<=palette: raise ValueError('Invalid indexed transparency')
            elif color in (0,2):
                if size!=channels*2 or any(v>=2**depth for v in struct.unpack('>'+'H'*channels,data)):
                    raise ValueError('Invalid PNG transparent sample')
            else: raise ValueError('Transparency chunk forbidden for alpha PNG')
        elif kind==b'IDAT':
            if closed or color==3 and palette is None: raise ValueError('Invalid PNG image data order')
            idats.append(data)
        elif kind==b'IEND':
            if size or end!=len(raw): raise ValueError('Invalid PNG ending (including trailing bytes)')
            ended=True; break
        elif not kind[0]&32:
            raise UnsupportedPng('Unsupported critical PNG chunk')
        if idats and kind!=b'IDAT': closed=True
        pos=end
    if not ended or not idats: raise ValueError('Incomplete PNG stream')
    inflater=zlib.decompressobj()
    try: decoded=inflater.decompress(b''.join(idats),expected+1)
    except zlib.error as error: raise ValueError('Invalid PNG zlib stream') from error
    if len(decoded)!=expected or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise ValueError('PNG expansion/stream length mismatch')
    if any(decoded[y*(stride+1)]>4 for y in range(height)): raise ValueError('Invalid PNG filter')
    if color==3:
        # Filter byte distance is one for all packed palette depths. Reconstruct rows
        # to reject nonexistent palette entries, not just a well-formed zlib stream.
        previous=bytearray(stride)
        for y in range(height):
            offset=y*(stride+1); mode=decoded[offset]; row=bytearray(decoded[offset+1:offset+1+stride])
            for i in range(stride):
                left=row[i-1] if i else 0; above=previous[i]; corner=previous[i-1] if i else 0
                if mode==1: predictor=left
                elif mode==2: predictor=above
                elif mode==3: predictor=(left+above)//2
                elif mode==4:
                    p=left+above-corner; a=abs(p-left); b=abs(p-above); c=abs(p-corner)
                    predictor=left if a<=b and a<=c else above if b<=c else corner
                else: predictor=0
                row[i]=(row[i]+predictor)&255
            mask=(1<<depth)-1
            for x in range(width):
                value=(row[x*depth//8]>>(8-depth-(x*depth%8)))&mask
                if value>=palette: raise ValueError('PNG pixel references missing palette entry')
            previous=row
    return dict(width=width,height=height,bitDepth=depth,colorType=color)
