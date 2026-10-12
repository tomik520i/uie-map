import tempfile,unittest,gzip,hashlib,os
from pathlib import Path
from unittest.mock import patch
import cave_pair_store as store
import test_cave_pair as fixtures

class StoreTests(unittest.TestCase):
    setUp=fixtures.PairTests.setUp
    def test_repeat_is_immutable_and_corruption_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();args=dict(atlas_sha='a'*64,renderer_identity='b'*64)
            sha,doc=store.store(root,self.geometry,self.caps,**args)
            manifest=root/'cave-data/pairs'/f'{sha}.json';stamp=manifest.stat().st_mtime_ns
            self.assertEqual(store.store(root,self.geometry,self.caps,**args)[0],sha)
            self.assertEqual(manifest.stat().st_mtime_ns,stamp)
            caps=root/'cave-data/caps'/f"{doc['capSha256']}.uic.gz"
            self.assertEqual(gzip.decompress(caps.read_bytes()),self.caps)
            self.assertLess(caps.stat().st_size,len(self.caps)//10)
            caps.write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError,'existing'):store.store(root,self.geometry,self.caps,**args)
            self.assertEqual(caps.read_bytes(),b'broken')
    def test_interrupted_cap_write_does_not_create_pair_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();real=store._content
            def fail_caps(path,raw):
                if path.name.endswith('.uic.gz'):raise OSError('disk full')
                return real(path,raw)
            with patch.object(store,'_content',side_effect=fail_caps):
                with self.assertRaises(OSError):store.store(root,self.geometry,self.caps,atlas_sha='a'*64,renderer_identity='b'*64)
            self.assertEqual(list((root/'cave-data/pairs').iterdir()),[])
            self.assertFalse((root/'current.json').exists())
    def test_alias_shares_geometry_inode_and_future_replace_preserves_object(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();(root/'tiles').mkdir()
            _,doc=store.store(root,self.geometry,self.caps,atlas_sha='a'*64,renderer_identity='b'*64)
            source=root/'cave-data/objects'/f"{doc['geometrySha256']}.uih"
            store.publish_geometry_alias(root,doc['origin'],doc['geometrySha256'])
            alias=root/'tiles/-1_2.uih';self.assertTrue(os.path.samefile(source,alias))
            tmp=root/'tiles/new.tmp';tmp.write_bytes(b'new');os.replace(tmp,alias)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),doc['geometrySha256'])
            self.assertEqual(alias.read_bytes(),b'new')
