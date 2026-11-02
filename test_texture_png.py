import struct, unittest, zlib
import png_io
import texture_png as png


def image(depth=8,color=3,rows=b'\0\0',width=1,height=1,palette=b'\xff\0\0',chunks=()):
    raw=png_io.SIGNATURE+png_io._chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,depth,color,0,0,0))
    if palette is not None:raw+=png_io._chunk(b'PLTE',palette)
    for kind,data in chunks:raw+=png_io._chunk(kind,data)
    return raw+png_io._chunk(b'IDAT',zlib.compress(rows))+png_io._chunk(b'IEND',b'')


class PngTests(unittest.TestCase):
    def test_all_supported_depth_color_combinations(self):
        for color,depths in {0:(1,2,4,8,16),2:(8,16),3:(1,2,4,8),4:(8,16),6:(8,16)}.items():
            channels={0:1,2:3,3:1,4:2,6:4}[color]
            for depth in depths:
                with self.subTest(color=color,depth=depth):
                    raw=image(depth,color,b'\0'+b'\0'*((channels*depth+7)//8),palette=b'\xff\0\0' if color==3 else None)
                    self.assertEqual(png.verify(raw)['bitDepth'],depth)

    def test_all_palette_filters_reconstruct_indices(self):
        palette=b'\0\0\0\xff\xff\xff'
        # Two rows, each containing indices 1,1. Encoded second row varies by filter.
        for mode,body in [(0,b'\1\1'),(1,b'\1\0'),(2,b'\0\0'),(3,b'\1\0'),(4,b'\0\0')]:
            raw=image(rows=b'\0\1\1'+bytes([mode])+body,width=2,height=2,palette=palette)
            self.assertEqual(png.verify(raw)['width'],2)
        with self.assertRaisesRegex(ValueError,'missing palette'):
            png.verify(image(rows=b'\0\1\1\2\1\0',width=2,height=2,palette=palette))

    def test_packed_indices_and_ignored_padding(self):
        for depth in (1,2,4):
            self.assertEqual(png.verify(image(depth=depth,rows=bytes([0,(1<<(8-depth))-1])))['width'],1)
            with self.assertRaisesRegex(ValueError,'missing palette'):
                png.verify(image(depth=depth,rows=bytes([0,1<<(8-depth)])))

    def test_palette_and_transparency_validation(self):
        invalid=[image(palette=None),image(palette=b'x'),image(depth=1,palette=b'\0'*9),
                 image(color=0,palette=b'\0'*3),image(chunks=[(b'tRNS',b'\0\0')]),
                 image(chunks=[(b'tRNS',b'\0'),(b'tRNS',b'\0')]),
                 image(color=6,palette=None,rows=b'\0'*5,chunks=[(b'tRNS',b'\0')]),
                 image(color=0,palette=None,chunks=[(b'tRNS',b'\1\0')])]
        for raw in invalid:
            with self.subTest(raw=raw),self.assertRaises(ValueError):png.verify(raw)
        png.verify(image(chunks=[(b'tRNS',b'\x80')]))
        png.verify(image(color=0,palette=None,chunks=[(b'tRNS',b'\0\xff')]))

    def test_limits_crc_trailing_inflation_and_filters(self):
        valid=image()
        for raw in (valid[:-1],valid+b'x',image(rows=b'\0'*10000),image(rows=b'\5\0'),image(rows=b'\0')):
            with self.assertRaises(ValueError):png.verify(raw)
        bad=bytearray(valid);bad[-5]^=1
        with self.assertRaisesRegex(ValueError,'CRC'):png.verify(bytes(bad))
        with self.assertRaisesRegex(ValueError,'pixel limit'):png.verify(valid,max_pixels=0)

    def test_split_noncontiguous_and_trailing_zlib(self):
        raw=image();start=raw.index(b'IDAT')-4;header=raw[:start];data=zlib.compress(b'\0\0');end=png_io._chunk(b'IEND',b'')
        good=header+png_io._chunk(b'IDAT',data[:3])+png_io._chunk(b'IDAT',data[3:])+end
        png.verify(good)
        bad=header+png_io._chunk(b'IDAT',data[:3])+png_io._chunk(b'tEXt',b'x')+png_io._chunk(b'IDAT',data[3:])+end
        with self.assertRaisesRegex(ValueError,'order'):png.verify(bad)
        with self.assertRaises(ValueError):png.verify(header+png_io._chunk(b'IDAT',data+b'x')+end)

    def test_unknown_critical_is_unsupported_not_corrupt(self):
        with self.assertRaises(png.UnsupportedPng):png.verify(image(chunks=[(b'ABCD',b'')]))
        with self.assertRaisesRegex(ValueError,'chunk type'):png.verify(image(chunks=[(b'ABcD',b'')]))


if __name__=='__main__':unittest.main()
