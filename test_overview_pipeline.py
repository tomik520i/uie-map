"""Overview stages B+C and publication on the fixture world: tiles, pyramid, index and the CAS pointer."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import overview_surface as ovs
import overview_tiles as ovt
import png_io
import publish_overview as po
from test_prepare_warm_plan import build_world


class OverviewPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._temp.name).resolve()
        build_world(cls.base / "world")
        cls.surface = cls.base / "surface"
        assert ovs.main(["--world", str(cls.base / "world"), "--out", str(cls.surface), "--workers", "1"]) == 0
        registry = json.loads((cls.surface / "registry.json").read_text())
        names = sorted(set(registry["blocks"].values()))
        cls.colors = cls.base / "colors.json"
        cls.colors.write_text(json.dumps({"source": "test", "byMeta": {}, "byName": {names[0]: [10, 20, 30, 1]}}))
        cls.tiles = cls.base / "tiles"
        cls.index = ovt.build(cls.surface, cls.colors, cls.tiles, workers=1)

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def test_index_pyramid_and_tile_contents(self):
        index = self.index
        self.assertEqual(index["format"], "UIE_OVERVIEW1")
        top = index["tiles"][str(index["levels"] - 1)]
        self.assertTrue(1 <= len(top) <= 4 and all(t in ([-1, -1], [0, -1], [-1, 0], [0, 0]) for t in top),
                        "pyramid ends in the 2x2 block around the origin")
        self.assertEqual(index["levels"], 1, "fixture already lies around the origin")
        for level, tiles in index["tiles"].items():
            for tx, tz in tiles:
                w, h, c, px = png_io.decode((self.tiles / f"L{level}" / f"{tx}_{tz}.png").read_bytes())
                self.assertEqual((w, h, c), (512, 512, 4))
                self.assertTrue(any(px[3::4]))
                self.assertEqual(png_io.decode((self.tiles / f"L{level}" / f"{tx}_{tz}.h.png").read_bytes())[2], 1)
        # Fixture: chunks x -7..6, z -7..0 -> regions (-1|0, -1|0); block columns outside chunks transparent.
        self.assertEqual(index["tiles"]["0"], [[-1, -1], [-1, 0], [0, -1], [0, 0]])
        _, _, _, px = png_io.decode((self.tiles / "L0" / "0_-1.png").read_bytes())
        inside = ((512 - 7 * 16 + 5) * 512 + 5) * 4      # chunk (0,-7): local chunk row 25 of region (0,-1)
        outside = ((10) * 512 + 300) * 4                  # z far north of the fixture
        self.assertEqual(px[outside + 3], 0)
        self.assertEqual(px[inside + 3], 255)
        self.assertTrue(index["fallbackColumns"], "blocks without a measured colour are reported")

    def test_publication_is_atomic_cas_and_refuses_tampering(self):
        store = self.base / "store"
        pointer = po.publish(self.tiles, store, None)
        self.assertEqual(json.loads((store / "current.json").read_text())["build"], pointer["build"])
        self.assertTrue((store / "builds" / pointer["build"] / "L0" / "0_-1.h.png").exists())
        with self.assertRaisesRegex(po.OverviewPublishError, "stale"):
            po.publish(self.tiles, store, None)
        again = po.publish(self.tiles, store, pointer["build"])      # same build: idempotent
        self.assertEqual(again["build"], pointer["build"])
        bad = self.base / "bad-tiles"
        shutil.copytree(self.tiles, bad)
        (bad / "L0" / "0_-1.h.png").unlink()
        with self.assertRaisesRegex(po.OverviewPublishError, "differ"):
            po.publish(bad, store, pointer["build"])
        (bad / "L0" / "extra.txt").write_text("x")
        with self.assertRaisesRegex(po.OverviewPublishError, "unexpected"):
            po.publish(bad, store, pointer["build"])
        self.assertEqual(json.loads((store / "current.json").read_text())["build"], pointer["build"])
        self.assertEqual([p.name for p in store.iterdir() if p.name.startswith(".")], [], "no staging left")


if __name__ == "__main__":
    unittest.main()
