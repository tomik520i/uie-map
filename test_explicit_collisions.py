import unittest,hashlib
from pathlib import Path
import test_explicit_hires as fixture
import collision_region_probe as probe,uiemap_collisions,world_hires as wh
from modpack_registry import read_registry

class ExplicitCollisionTests(unittest.TestCase):
    setUp=fixture.ExplicitHiresTests.setUp
    jar=fixture.ExplicitHiresTests.jar
    inspect=fixture.ExplicitHiresTests.inspect
    prepare=fixture.ExplicitHiresTests.prepare
    def test_toolkit_matches_surface_identity_and_exact_metadata(self):
        self.prepare();registry=read_registry(self.world)
        toolkit=probe.Toolkit(self.root/'catalog',registry=registry)
        catalog=toolkit._mesher.catalog
        code=wh.MAGIC+b''.join(Path(wh.__file__).with_name(n).read_bytes() for n in ('offline_mesher.py','offline_catalog.py','offline_volume.py','explicit_catalog.py'))
        self.assertEqual(toolkit.identity,wh.geometry_identity(catalog.identity,code))
        self.assertEqual(toolkit.atlas,hashlib.sha256((self.root/'catalog/atlas.png').read_bytes()).hexdigest())
        self.assertEqual(catalog.model('sample:stone',1).kind,'fallback')
        self.assertEqual(toolkit.fresh().identity,toolkit.identity)
    def test_missing_wrong_registry_and_ambiguous_catalog_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(probe.ProbeError,'authoritative'):probe.Toolkit(self.root/'catalog')
        with self.assertRaisesRegex(ValueError,'different world'):probe.Toolkit(self.root/'catalog',registry={0:'minecraft:air'})
        (self.root/'catalog/catalog.json').write_text('{}')
        with self.assertRaisesRegex(probe.ProbeError,'Ambiguous'):probe.Toolkit(self.root/'catalog',registry=read_registry(self.world))
    def test_dimension_toolkit_uses_root_registry_and_rechecks_on_retry(self):
        self.prepare();(self.world/'DIM42').mkdir()
        work=dict(catalog=str(self.root/'catalog'),worldPath=str(self.world),world='fixture',dimension='42')
        toolkit=uiemap_collisions.make_tools(work)
        self.assertEqual(toolkit.load_registry(self.world/'DIM42'),read_registry(self.world))
        self.assertEqual(toolkit.fresh().identity,toolkit.identity)
        from test_reader import level_dat
        (self.world/'level.dat').write_bytes(level_dat([('\x01minecraft:air',0)]))
        with self.assertRaisesRegex(ValueError,'different world'):toolkit.fresh()

if __name__=='__main__':unittest.main()
