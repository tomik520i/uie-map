import unittest
from unittest.mock import Mock
from pathlib import Path
import collision_region_probe as probe
import collision_world_region as region

class ScopeTests(unittest.TestCase):
    def test_negative_tile_has_full_halo(self):
        area=probe.input_area(-1,-1,[(-1,-1)])
        expected=probe.wh.tile_area(-1,-1)[0]
        self.assertEqual((area.x,area.z,area.width,area.depth),
                         (expected.x,expected.z,expected.width,expected.depth))
        self.assertEqual(area.width,96)
        with self.assertRaises(probe.ProbeError):probe.input_area(0,0,[(-1,-1)])

    def test_publication_rechecks_same_tile_footprint_and_rejects_change(self):
        tools=Mock();tools.fingerprint.return_value='changed'
        report=dict(format=probe.REPORT_FORMAT,ok=True,scratchCleanup=True,region=[1,-1],
                    inputScope='tile-halo-v1',tiles={'9,-2':{}},source={'after':'old'})
        with self.assertRaisesRegex(ValueError,'source changed'):
            region.accepted_updates(report,world=Path('world'),hires_store=Path('hires'),tools=tools)
        area=tools.fingerprint.call_args.args[1];expected=probe.wh.tile_area(9,-2)[0]
        self.assertEqual((area.x,area.z,area.width,area.depth),(expected.x,expected.z,expected.width,expected.depth))
        tools.read_index.assert_not_called()

    def test_scope_cannot_hide_additional_tiles(self):
        report=dict(format=probe.REPORT_FORMAT,ok=True,scratchCleanup=True,region=[0,0],
                    inputScope='tile-halo-v1',tiles={'0,0':{},'1,0':{}})
        with self.assertRaisesRegex(ValueError,'exactly one'):
            region.accepted_updates(report,world=Path('world'),hires_store=Path('hires'),tools=Mock())

if __name__=='__main__':unittest.main()
