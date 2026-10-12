"""Unit tests of the offline renderer with a synthetic catalog and synthetic volumes. Stdlib only, Windows/Linux."""
import json
import tempfile
import unittest
from array import array
from pathlib import Path

import png_io
from offline_catalog import Catalog
from offline_mesher import Colormap, Mesher
from offline_volume import HEIGHT, Volume
from capture_snapshot import Area

UV = {"stone": [0.0, 0.0, 0.1, 0.1], "dirt": [0.1, 0.0, 0.2, 0.1], "grass_top": [0.2, 0.0, 0.3, 0.1],
      "grass_side": [0.3, 0.0, 0.4, 0.1], "grass_side_overlay": [0.4, 0.0, 0.5, 0.1], "leaves_oak": [0.5, 0.0, 0.6, 0.1],
      "water_still": [0.6, 0.0, 0.7, 0.1], "water_flow": [0.7, 0.0, 0.8, 0.1], "tallgrass": [0.8, 0.0, 0.9, 0.1],
      "planks_oak": [0.9, 0.0, 1.0, 0.1], "missingno": [0.0, 0.9, 0.1, 1.0]}


def face(name):
    return {"name": name, "uv": UV[name]}


def block(rt, opaque, cls, faces, color=0xFFFFFF, metas=1):
    return {"renderType": rt, "opaque": opaque, "class": cls,
            "variants": [{"meta": m, "color": color, "faces": [face(f) if f else None for f in faces]} for m in range(metas)]}


ASSETS = {"blocks": {
    "minecraft:air": block(-1, False, "net.minecraft.block.BlockAir", [None] * 6),
    "minecraft:stone": block(0, True, "net.minecraft.block.BlockStone", ["stone"] * 6),
    "minecraft:dirt": block(0, True, "net.minecraft.block.BlockDirt", ["dirt"] * 6),
    "minecraft:grass": block(0, True, "net.minecraft.block.BlockGrass", ["dirt", "grass_top"] + ["grass_side"] * 4, color=0x7FB238),
    "minecraft:leaves": block(0, True, "net.minecraft.block.BlockOldLeaf", ["leaves_oak"] * 6, color=0x48B518),
    "minecraft:water": block(4, False, "net.minecraft.block.BlockStaticLiquid", ["water_still"] * 2 + ["water_flow"] * 4, metas=8),
    "minecraft:flowing_water": block(4, False, "net.minecraft.block.BlockDynamicLiquid", ["water_still"] * 2 + ["water_flow"] * 4, metas=8),
    "minecraft:tallgrass": block(1, False, "net.minecraft.block.BlockTallGrass", ["tallgrass"] * 6, color=0x7FB238, metas=3),
    "minecraft:fence": block(11, False, "net.minecraft.block.BlockFence", ["planks_oak"] * 6),
    "mod:mystery": block(999, False, "mod.Mystery", ["stone"] * 6),
    "minecraft:oak_stairs": block(10, False, "net.minecraft.block.BlockStairs", ["planks_oak"] * 6, metas=8),
}, "failures": []}
LAYOUT = {"format": "UIE_ATLAS_LAYOUT1", "missing": {"iconName": "missingno", "uv": UV["missingno"]},
          "registered": {k: {"iconName": k, "state": "placed", "uv": v} for k, v in UV.items() if k != "missingno"}}
REGISTRY = {0: "minecraft:air", 1: "minecraft:stone", 2: "minecraft:grass", 3: "minecraft:dirt", 18: "minecraft:leaves",
            9: "minecraft:water", 8: "minecraft:flowing_water", 31: "minecraft:tallgrass", 85: "minecraft:fence",
            700: "mod:mystery", 53: "minecraft:oak_stairs"}


def volume(blocks, x0=-16, z0=-16, size=48, core=(0, 0, 16, 16)):
    """A synthetic Volume: window [x0, x0+size) x [z0, z0+size), all chunks present, given {(x,y,z): (id, meta)}."""
    v = Volume.__new__(Volume)
    v.x0, v.z0, v.width, v.depth = x0, z0, size, size
    v.core = Area(*core)
    v.registry = dict(REGISTRY)
    n = size * size
    v.present = frozenset(((x0 + i) >> 4, (z0 + j) >> 4) for i in range(0, size, 16) for j in range(0, size, 16))
    v._present_cols = bytearray([1]) * n
    v.biomes = bytearray([1]) * n          # plains
    v.ids = array("H", bytes(2 * n * HEIGHT))
    v.metas = bytearray(n * HEIGHT)
    for (x, y, z), (block_id, meta) in blocks.items():
        i = ((z - z0) * size + (x - x0)) * HEIGHT + y
        v.ids[i] = block_id
        v.metas[i] = meta
    return v


class OfflineRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        d = Path(cls._temp.name)
        (d / "assets.json").write_text(json.dumps(ASSETS))
        (d / "layout.json").write_text(json.dumps(LAYOUT))
        px = bytes([90, 180, 60]) * (256 * 256)
        for name in ("grass.png", "foliage.png"):
            (d / name).write_bytes(png_io.encode(256, 256, 3, px))
        cls.dir = d
        cls.catalog = Catalog.load(d / "assets.json", d / "layout.json")
        Catalog.compile(d / "assets.json", d / "layout.json", d / "catalog.json")
        cls.colormap = Colormap(d / "grass.png", d / "foliage.png")

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def mesh(self, blocks, caves=True, leaves="fancy", **kw):
        vol = volume(blocks, **kw)
        return vol, Mesher(self.catalog, self.colormap, caves=caves, leaves=leaves).mesh_batch(vol)

    @staticmethod
    def quads(doc):
        out = []
        for part in [doc, *doc.get("transparentParts", [])]:
            p = part["positions"]
            for q in range(0, len(part["indices"]), 6):
                vs = sorted(set(part["indices"][q:q + 6]))
                out.append(tuple(sorted((p[3 * v], p[3 * v + 1], p[3 * v + 2]) for v in vs)))
        return out

    def test_compiled_catalog_has_identical_models_and_unused_sprites(self):
        compiled = Catalog.load_compiled(self.dir / "catalog.json")
        self.assertEqual(compiled.identity, self.catalog.identity)
        self.assertIn("grass_side_overlay", compiled.sprites, "sprites no face uses are kept")
        for name in ASSETS["blocks"]:
            for meta in range(4):
                a, b = self.catalog.model(name, meta), compiled.model(name, meta)
                self.assertEqual((a.kind, a.tier, a.opacity, a.faces, a.tint, a.shape), (b.kind, b.tier, b.opacity, b.faces, b.tint, b.shape))

    def test_occlusion_between_solid_cubes_and_world_bottom(self):
        _, r = self.mesh({(1, 5, 1): (1, 0), (2, 5, 1): (1, 0), (3, 0, 3): (1, 0)})
        quads = self.quads(r["chunks"][(0, 0)])
        self.assertEqual(len(quads), 10 + 5, "shared face hidden; y=0 bottom never drawn")
        self.assertNotIn(tuple(sorted([(2, 5, 1), (2, 6, 1), (2, 5, 2), (2, 6, 2)])), quads)

    def test_leaves_never_hide_neighbours_and_fancy_keeps_leaf_joins(self):
        blocks = {(1, 5, 1): (1, 0), (2, 5, 1): (18, 0), (3, 5, 1): (18, 0)}
        _, fancy = self.mesh(blocks)
        _, fast = self.mesh(blocks, leaves="fast")
        stone_face = tuple(sorted([(2, 5, 1), (2, 6, 1), (2, 5, 2), (2, 6, 2)]))
        self.assertGreaterEqual(self.quads(fancy["chunks"][(0, 0)]).count(stone_face), 1, "stone face behind leaves is drawn")
        self.assertEqual(len(self.quads(fancy["chunks"][(0, 0)])) - len(self.quads(fast["chunks"][(0, 0)])), 2,
                         "fancy leaves keep the two faces between the leaf blocks")

    def test_grass_gets_tinted_top_and_side_overlays(self):
        _, r = self.mesh({(4, 5, 4): (2, 0)})
        doc = r["chunks"][(0, 0)]
        self.assertEqual(len(self.quads(doc)), 1 + 1 + 4 + 4, "top, bottom (air below), four sides and four overlays")
        uvs = doc["uvs"]
        overlay = [UV["grass_side_overlay"][0], UV["grass_side_overlay"][3]]
        self.assertIn(overlay[0], uvs)

    def test_liquid_heights_and_still_flowing_join(self):
        _, r = self.mesh({(5, 5, 5): (9, 0), (6, 5, 5): (8, 3)})
        doc = r["chunks"][(0, 0)]
        tp = doc["transparentParts"][0]
        ys = sorted(set(round(tp["positions"][i], 3) for i in range(1, len(tp["positions"]), 3)))
        # Vanilla RenderBlocks.getLiquidHeight corners: source weighs 10x, air counts as empty.
        self.assertEqual(ys, [5.0, round(5 + 1 - (4 / 9 + 3) / 4, 3), round(5 + 1 - (11 / 9 + 3) / 14, 3),
                              round(5 + 1 - (11 / 9 + 4 / 9 + 2) / 14, 3)])
        p = tp["positions"]
        shared = {round(p[i + 1], 6) for i in range(0, len(p), 3) if round(p[i], 6) == 6 and p[i + 1] > 5.01}
        self.assertEqual(len(shared), 1, "both blocks meet at the same height on their shared edge")
        joined = tuple(sorted([(6, 5, 5), (6, 6, 5), (6, 5, 6), (6, 6, 6)]))
        self.assertNotIn(joined, self.quads(doc), "no face between still and flowing water")

    def test_fence_connects_to_fence_and_solid(self):
        _, alone = self.mesh({(8, 5, 8): (85, 0)})
        _, joined = self.mesh({(8, 5, 8): (85, 0), (9, 5, 8): (85, 0), (8, 5, 7): (1, 0)})
        self.assertEqual(len(self.quads(alone["chunks"][(0, 0)])), 6, "post only")
        self.assertGreater(len(self.quads(joined["chunks"][(0, 0)])), 6 + 6 + 2 * 2 * 6, "rails to the fence and the stone")

    def test_stairs_straight_outer_and_inner_corners(self):
        """1.7.10 BlockStairs: east-facing (meta 0) stairs at (5,5,5); high part is the back quarter x 0.5..1."""
        def boxes(neighbours, meta=0):
            blocks = {(5, 5, 5): (53, meta), **{pos: (53, m) for pos, m in neighbours.items()}}
            vol = volume(blocks)
            mesher = Mesher(self.catalog, self.colormap)
            mesher.mesh_batch(vol)
            return sorted(mesher._stairs_boxes(self.catalog.model("minecraft:oak_stairs", meta), vol, 5, 5, 5))
        slab = ((0, 0, 0), (1, 0.5, 1))
        self.assertEqual(boxes({}), sorted([slab, ((0.5, 0.5, 0), (1, 1, 1))]), "straight")
        self.assertEqual(boxes({(6, 5, 5): 3}), sorted([slab, ((0.5, 0.5, 0), (1, 1, 0.5))]),
                         "outer corner: north-facing stair behind keeps the north half")
        self.assertEqual(boxes({(6, 5, 5): 2}), sorted([slab, ((0.5, 0.5, 0.5), (1, 1, 1))]))
        self.assertEqual(boxes({(4, 5, 5): 3}), sorted([slab, ((0.5, 0.5, 0), (1, 1, 1)), ((0, 0.5, 0), (0.5, 1, 0.5))]),
                         "inner corner: extra front quarter on the north side")
        self.assertEqual(boxes({(6, 5, 5): 3, (5, 5, 6): 0}), sorted([slab, ((0.5, 0.5, 0), (1, 1, 1))]),
                         "no corner when the same stair continues beside it")
        self.assertEqual(boxes({(6, 5, 5): 7}), sorted([slab, ((0.5, 0.5, 0), (1, 1, 1))]),
                         "upside-down neighbour does not turn a normal stair")
        self.assertEqual(boxes({}, meta=4), sorted([((0, 0.5, 0), (1, 1, 1)), ((0.5, 0, 0), (1, 0.5, 1))]), "upside down")

    def test_negative_coordinates_and_chunk_ownership(self):
        _, r = self.mesh({(-3, 5, -2): (1, 0)}, core=(-16, -16, 16, 16))
        quads = self.quads(r["chunks"][(-1, -1)])
        self.assertEqual(len(quads), 6)
        xs = {p[0] for q in quads for p in q}
        self.assertEqual(xs, {-3, -2})

    def test_unknown_block_is_a_reported_fallback(self):
        _, r = self.mesh({(3, 7, 3): (700, 0)})
        stats = r["stats"]
        self.assertEqual(stats["fallbackBlocks"][("mod:mystery", 0)], 1)
        self.assertEqual(stats["fallbackExamples"][("mod:mystery", 0)][:3], (3, 7, 3))
        colors = r["chunks"][(0, 0)]["colors"]
        self.assertTrue(any(c > 0.9 for c in colors[0::3]) and all(c < 0.4 for c in colors[1::3]), "magenta marker")

    def test_surface_mode_drops_enclosed_caves_but_keeps_open_faces(self):
        blocks = {(x, y, z): (1, 0) for x in range(0, 16) for z in range(0, 16) for y in range(1, 11)}
        for (x, y, z) in [(5, 4, 5), (6, 4, 5)]:            # sealed cave
            del blocks[(x, y, z)]
        _, caves = self.mesh(blocks, caves=True)
        _, surface = self.mesh(blocks, caves=False)
        self.assertGreater(len(self.quads(caves["chunks"][(0, 0)])), len(self.quads(surface["chunks"][(0, 0)])))
        self.assertEqual(surface["stats"]["culledCaveFaces"], 10, "the cave's 10 inner faces are dropped")


if __name__ == "__main__":
    unittest.main()
