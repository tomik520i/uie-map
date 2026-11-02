"""Convert validated non-interlaced PNG to straight-alpha RGBA8 without dependencies.

Preserves encoded samples (no gamma/ICC transform); 16-bit samples use the high byte.
Transparency comparisons happen at original depth, before reduction to 8 bits.
"""
import struct
import zlib
import texture_png


def decode(raw, max_pixels=4*1024*1024):
    info=texture_png.verify(raw,max_pixels)
    width,height,depth,color=(info[k] for k in ('width','height','bitDepth','colorType'))
    channels={0:1,2:3,3:1,4:2,6:4}[color]
    stride=(width*channels*depth+7)//8; bpp=max(1,(channels*depth+7)//8)
    pos=8; idats=[]; palette=b''; transparency=b''
    while pos<len(raw):
        size=struct.unpack('>I',raw[pos:pos+4])[0];kind=raw[pos+4:pos+8];data=raw[pos+8:pos+8+size]
        if kind==b'IDAT':idats.append(data)
        elif kind==b'PLTE':palette=data
        elif kind==b'tRNS':transparency=data
        pos+=size+12
    # Exact expansion bound was checked against these immutable bytes above.
    filtered=zlib.decompress(b''.join(idats));out=bytearray(width*height*4);previous=bytearray(stride)
    transparent=struct.unpack('>'+'H'*channels,transparency) if transparency and color in (0,2) else None
    for y in range(height):
        offset=y*(stride+1);mode=filtered[offset];row=bytearray(filtered[offset+1:offset+1+stride])
        for i in range(stride):
            left=row[i-bpp] if i>=bpp else 0;above=previous[i];corner=previous[i-bpp] if i>=bpp else 0
            if mode==1:p=left
            elif mode==2:p=above
            elif mode==3:p=(left+above)//2
            elif mode==4:
                predict=left+above-corner;a=abs(predict-left);b=abs(predict-above);c=abs(predict-corner)
                p=left if a<=b and a<=c else above if b<=c else corner
            else:p=0
            row[i]=(row[i]+p)&255
        for x in range(width):
            if depth<8:
                samples=((row[x*depth//8]>>(8-depth-x*depth%8))&((1<<depth)-1),)
            elif depth==8:samples=tuple(row[x*channels:(x+1)*channels])
            else:samples=struct.unpack('>'+'H'*channels,row[x*channels*2:(x+1)*channels*2])
            if color==3:
                index=samples[0];rgb=palette[index*3:index*3+3]
                rgba=bytes(rgb)+bytes([transparency[index] if index<len(transparency) else 255])
            else:
                scaled=tuple(v>>8 if depth==16 else v*255//((1<<depth)-1) for v in samples)
                if color==0:rgba=bytes([scaled[0]]*3+[0 if samples==transparent else 255])
                elif color==2:rgba=bytes((*scaled,0 if samples==transparent else 255))
                elif color==4:rgba=bytes([scaled[0]]*3+[scaled[1]])
                else:rgba=bytes(scaled)
            index=(y*width+x)*4;out[index:index+4]=rgba
        previous=row
    return width,height,bytes(out)
