import hashlib,json,tempfile,unittest,zlib,struct,subprocess,sys
from pathlib import Path
from test_atlas_identity import PNG,layout,png
from test_offline_renderer import ASSETS
import catalog_package as package
import png_io


class PackageTests(unittest.TestCase):
    def fixture(self,root):
        source=root/'source';source.mkdir()
        (source/'assets.json').write_text(json.dumps(ASSETS))
        (source/'atlas-layout.json').write_text(json.dumps(layout()))
        (source/'atlas.png').write_bytes(PNG)
        for name in ('grass.png','foliage.png'):(source/name).write_bytes(png(256,256))
        return source

    def test_package_identity_hashes_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=self.fixture(root);out=root/'out'
            before={p.name:p.read_bytes() for p in source.iterdir()}
            result=package.build(source,out)
            for name,digest in result['files'].items():self.assertEqual(hashlib.sha256((out/name).read_bytes()).hexdigest(),digest)
            self.assertEqual(before,{p.name:p.read_bytes() for p in source.iterdir()})
            with self.assertRaisesRegex(ValueError,'already exists'):package.build(source,out)
            self.assertEqual(json.loads((out/'package.json').read_text()),result)

    def test_invalid_binding_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=self.fixture(root);out=root/'out'
            (source/'atlas.png').write_bytes(png(32,32,7))
            with self.assertRaises(ValueError):package.build(source,out)
            self.assertEqual(list(root.iterdir()),[source])

    def test_cli_preview_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=self.fixture(root);out=root/'out'
            run=subprocess.run([sys.executable,'-B',str(Path(package.__file__).with_name('uiemap.py')),
                                'catalog','--source',str(source),'--out',str(out)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertTrue(json.loads(run.stdout)['readOnly']);self.assertFalse(out.exists())

    def test_bad_crc_truncation_and_excess_expansion(self):
        bad=bytearray(PNG);bad[-5]^=1
        oversized=(PNG[:33]+png_io._chunk(b'IDAT',zlib.compress(b'\0'*5000))+png_io._chunk(b'IEND',b''))
        for raw in (bytes(bad),PNG[:-1],PNG+b'x',oversized):
            with self.subTest(size=len(raw)),self.assertRaises(ValueError):package.verify_png(raw)

    def test_split_idat_and_filters(self):
        data=zlib.compress(b'\0'+b'\1'*128)
        header=png(32,1)[:33]
        raw=header+png_io._chunk(b'IDAT',data[:3])+png_io._chunk(b'IDAT',data[3:])+png_io._chunk(b'IEND',b'')
        self.assertEqual(package.verify_png(raw),dict(width=32,height=1))
        bad=header+png_io._chunk(b'IDAT',zlib.compress(b'\5'+b'\0'*128))+png_io._chunk(b'IEND',b'')
        with self.assertRaisesRegex(ValueError,'filter'):package.verify_png(bad)


if __name__=='__main__':unittest.main()
