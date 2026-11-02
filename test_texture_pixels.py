import struct,unittest
import texture_pixels
from test_texture_png import image


class PixelTests(unittest.TestCase):
    def decode(self,**kwargs):return texture_pixels.decode(image(**kwargs))[2]
    def test_palette_alpha_and_packed_row_padding(self):
        self.assertEqual(self.decode(depth=2,width=3,rows=b'\0\x18',palette=b'\xff\0\0\0\xff\0\0\0\xff',chunks=[(b'tRNS',b'\0\x80')]),
                         bytes([255,0,0,0,0,255,0,128,0,0,255,255]))
    def test_grey_low_depth_scales_and_transparent_sample(self):
        self.assertEqual(self.decode(depth=2,color=0,width=4,rows=b'\0\x1b',palette=None,chunks=[(b'tRNS',b'\0\2')]),
                         bytes([0,0,0,255,85,85,85,255,170,170,170,0,255,255,255,255]))
    def test_rgb_and_grey_alpha(self):
        self.assertEqual(self.decode(color=2,rows=b'\0\x11\x22\x33',palette=None),bytes([17,34,51,255]))
        self.assertEqual(self.decode(color=4,rows=b'\0\x11\x22',palette=None),bytes([17,17,17,34]))
    def test_16bit_transparency_compared_before_truncating(self):
        raw=b'\0'+struct.pack('>HH',0x1234,0x1235)
        self.assertEqual(self.decode(depth=16,color=0,width=2,rows=raw,palette=None,chunks=[(b'tRNS',b'\x12\x34')]),
                         bytes([18,18,18,0,18,18,18,255]))
        self.assertEqual(self.decode(depth=16,color=6,rows=b'\0'+struct.pack('>HHHH',0x1234,0xabcd,0xffff,0x8080),palette=None),bytes([18,171,255,128]))
    def test_all_filters_use_pixel_byte_distance(self):
        first=bytes([1,2,3,4,5,6,7,8]);second=bytes([9,10,11,12,13,14,15,16])
        for mode in range(5):
            encoded=[]
            for i,value in enumerate(second):
                a=second[i-4] if i>=4 else 0;b=first[i];c=first[i-4] if i>=4 else 0
                p=a+b-c;pa=abs(p-a);pb=abs(p-b);pc=abs(p-c)
                pred=[0,a,b,(a+b)//2,a if pa<=pb and pa<=pc else b if pb<=pc else c][mode]
                encoded.append((value-pred)&255)
            result=self.decode(color=6,width=2,height=2,rows=b'\0'+first+bytes([mode])+bytes(encoded),palette=None)
            self.assertEqual(result,first+second)
    def test_invalid_png_and_limits_are_checked_before_conversion(self):
        with self.assertRaises(ValueError):texture_pixels.decode(b'bad')
        with self.assertRaises(ValueError):texture_pixels.decode(image(),max_pixels=0)


if __name__=='__main__':unittest.main()
