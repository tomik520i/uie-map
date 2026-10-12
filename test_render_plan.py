"""Tests for render_plan.py.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_render_plan.py" -v
"""
import hashlib
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import render_plan as rp  # noqa: E402


def h(text):
    return hashlib.sha256(str(text).encode()).hexdigest()


PIPE = h("pipeline-1")
PIPE2 = h("pipeline-2")
DIM = "minecraft:overworld"


def world(*coords, salt="a"):
    return {f"{x},{z}": h(f"{salt}:{x},{z}") for x, z in coords}


def tiles(plan):
    return [(j["tileX"], j["tileZ"]) for j in plan["jobs"]]


def removed(plan):
    return [(t["tileX"], t["tileZ"]) for t in plan["removedTiles"]]


def incremental(current, previous):
    return rp.plan_render(current, previous, PIPE, PIPE, DIM)


def block(cx0, cz0, cx1, cz1):
    """All chunks in an inclusive rectangle."""
    return [(x, z) for x in range(cx0, cx1 + 1) for z in range(cz0, cz1 + 1)]


# ---------------------------------------------------------------- independent oracle (no // or window helpers)

def oracle_tile(c):
    """Tile index of a chunk coordinate, found by search instead of floor division."""
    t = -100
    while not (t * 6 <= c < t * 6 + 6):
        t += 1
    return t


def oracle_view(state, tx, tz):
    cells = []
    for z in range(tz * 6 - 1, tz * 6 + 7):
        for x in range(tx * 6 - 1, tx * 6 + 7):
            cells.append(state.get(f"{x},{z}"))
    return tuple(cells)


def oracle_core_tiles(state):
    out = set()
    for key in state:
        x, z = (int(v) for v in key.split(","))
        out.add((oracle_tile(x), oracle_tile(z)))
    return out


class InitialAndUnchangedTests(unittest.TestCase):
    def test_initial_plan(self):
        current = world((0, 0), (5, 5), (6, 0), (-1, -1))
        plan = rp.plan_render(current, None, PIPE, None, DIM)
        self.assertTrue(plan["full"])
        self.assertEqual(plan["format"], "UIE_RENDER_PLAN1")
        self.assertEqual(tiles(plan), [(-1, -1), (0, 0), (1, 0)])
        self.assertEqual(plan["tileCount"], 3)
        job = plan["jobs"][1]
        self.assertEqual((job["originX"], job["originZ"], job["width"], job["depth"]), (-16, -16, 128, 128))
        self.assertEqual((job["coreOriginX"], job["coreOriginZ"], job["coreWidth"], job["coreDepth"]), (0, 0, 96, 96))
        self.assertEqual(job["coreChunks"], [[0, 0], [5, 5]])
        self.assertEqual(job["inputChunks"], [
            {"x": -1, "z": -1, "sha256": current["-1,-1"]}, {"x": 0, "z": 0, "sha256": current["0,0"]},
            {"x": 6, "z": 0, "sha256": current["6,0"]}, {"x": 5, "z": 5, "sha256": current["5,5"]}])
        self.assertEqual(len(job["missingChunks"]), 60)
        self.assertIn([6, 6], job["missingChunks"])
        self.assertIn([-1, 6], job["missingChunks"])
        self.assertEqual(job["dimension"], DIM)
        self.assertEqual(plan["removedTiles"], [])

    def test_unchanged_plans_nothing(self):
        current = world(*block(-3, -3, 8, 8))
        plan = incremental(current, dict(current))
        self.assertFalse(plan["full"])
        self.assertEqual(plan["jobs"], [])
        self.assertEqual(plan["removedTiles"], [])
        self.assertEqual(plan["tileCount"], 9)


class ChangeTests(unittest.TestCase):
    def setUp(self):
        # Tiles (0,0), (1,0), (0,1), (1,1) fully generated.
        self.previous = world(*block(0, 0, 11, 11))

    def changed(self, *coords):
        current = dict(self.previous)
        for x, z in coords:
            current[f"{x},{z}"] = h(f"changed:{x},{z}")
        return current

    def test_change_inside_tile(self):
        self.assertEqual(tiles(incremental(self.changed((2, 3)), self.previous)), [(0, 0)])

    def test_change_on_tile_edges(self):
        self.assertEqual(tiles(incremental(self.changed((5, 2)), self.previous)), [(0, 0), (1, 0)])
        self.assertEqual(tiles(incremental(self.changed((6, 2)), self.previous)), [(0, 0), (1, 0)])
        self.assertEqual(tiles(incremental(self.changed((2, 6)), self.previous)), [(0, 0), (0, 1)])

    def test_change_on_tile_corner(self):
        self.assertEqual(tiles(incremental(self.changed((5, 5)), self.previous)), [(0, 0), (1, 0), (0, 1), (1, 1)])
        self.assertEqual(tiles(incremental(self.changed((6, 6)), self.previous)), [(0, 0), (1, 0), (0, 1), (1, 1)])

    def test_chunk_starting_a_new_tile_also_dirties_its_neighbour(self):
        # Chunk (12,0) creates tile (2,0) and is halo of the existing tile (1,0).
        previous = world(*block(0, 0, 11, 5))
        current = dict(previous)
        current["12,0"] = h("new")
        plan = incremental(current, previous)
        self.assertEqual(tiles(plan), [(1, 0), (2, 0)])
        new_tile = plan["jobs"][1]
        self.assertEqual(new_tile["coreChunks"], [[12, 0]])

    def test_addition(self):
        previous = world(*block(0, 0, 5, 5))
        current = dict(previous, **world((6, 0)))
        self.assertEqual(tiles(incremental(current, previous)), [(0, 0), (1, 0)])
        far = dict(previous, **world((20, 0)))
        self.assertEqual(tiles(incremental(far, previous)), [(3, 0)])

    def test_removal_of_halo_and_last_chunk(self):
        previous = world(*block(0, 0, 5, 5), (6, 0), (7, 0))
        current = dict(previous)
        del current["6,0"]  # halo of (0,0) and core of (1,0), which still has (7,0)
        plan = incremental(current, previous)
        self.assertEqual(tiles(plan), [(0, 0), (1, 0)])
        self.assertIn([6, 0], plan["jobs"][0]["missingChunks"])
        self.assertEqual(plan["removedTiles"], [])
        del current["7,0"]  # tile (1,0) loses its last core chunk
        plan = incremental(current, previous)
        self.assertEqual(tiles(plan), [(0, 0)])
        self.assertEqual(removed(plan), [(1, 0)])
        plan = incremental({}, previous)
        self.assertEqual(plan["jobs"], [])
        self.assertEqual(removed(plan), [(0, 0), (1, 0)])
        self.assertEqual(plan["tileCount"], 0)

    def test_negative_coordinates(self):
        current = world((-1, -1), (-6, 0), (-7, 0), (-12, -13))
        plan = rp.plan_render(current, None, PIPE, None, DIM)
        self.assertEqual(tiles(plan), [(-2, -3), (-1, -1), (-2, 0), (-1, 0)])
        by_tile = {(j["tileX"], j["tileZ"]): j for j in plan["jobs"]}
        self.assertEqual((by_tile[(-1, -1)]["originX"], by_tile[(-1, -1)]["originZ"]), (-112, -112))
        self.assertEqual(by_tile[(-1, 0)]["coreChunks"], [[-6, 0]])
        self.assertEqual(by_tile[(-2, 0)]["coreChunks"], [[-7, 0]])
        self.assertEqual(by_tile[(-2, -3)]["coreOriginX"], -192)
        self.assertEqual(by_tile[(-2, -3)]["coreOriginZ"], -288)
        # (-7,0) is halo of tile (-1,0) and (-6,0) is halo of tile (-2,0).
        self.assertIn({"x": -7, "z": 0, "sha256": current["-7,0"]}, by_tile[(-1, 0)]["inputChunks"])
        self.assertIn({"x": -6, "z": 0, "sha256": current["-6,0"]}, by_tile[(-2, 0)]["inputChunks"])
        # (-7,0) is core of (-2,0), halo of (-1,0) and the corner halo of (-1,-1) (window X -7..0, Z -7..0).
        changed = dict(current, **{"-7,0": h("x")})
        self.assertEqual(tiles(incremental(changed, current)), [(-1, -1), (-2, 0), (-1, 0)])

    def test_pipeline_change_is_full(self):
        previous = world(*block(0, 0, 11, 5))
        full = rp.plan_render(previous, previous, PIPE2, PIPE, DIM)
        self.assertTrue(full["full"])
        self.assertEqual(tiles(full), [(0, 0), (1, 0)])
        self.assertTrue(rp.plan_render(previous, previous, PIPE, None, DIM)["full"])
        first = rp.plan_render(previous, None, PIPE, None, DIM)
        self.assertNotEqual(first["jobs"][0]["fingerprint"], full["jobs"][0]["fingerprint"])
        gone = rp.plan_render(world((0, 0)), previous, PIPE2, PIPE, DIM)
        self.assertEqual(removed(gone), [(1, 0)])


class FingerprintAndDeterminismTests(unittest.TestCase):
    def fingerprints(self, current, dimension=DIM, pipeline=PIPE):
        return {(j["tileX"], j["tileZ"]): j["fingerprint"]
                for j in rp.plan_render(current, None, pipeline, None, dimension)["jobs"]}

    def test_insertion_order_does_not_matter(self):
        coords = block(-7, -7, 7, 7)
        forward = world(*coords)
        backward = {k: forward[k] for k in reversed(list(forward))}
        a = rp.plan_render(forward, None, PIPE, None, DIM)
        b = rp.plan_render(backward, None, PIPE, None, DIM)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        c = incremental(dict(forward, **{"3,3": h("x")}), backward)
        d = incremental({**{"3,3": h("x")}, **{k: v for k, v in backward.items() if k != "3,3"}}, forward)
        self.assertEqual(json.dumps(c, sort_keys=True), json.dumps(d, sort_keys=True))

    def test_fingerprint_identity(self):
        base = world(*block(0, 0, 5, 5))
        fp = self.fingerprints(base)[(0, 0)]
        self.assertRegex(fp, r"^[0-9a-f]{64}$")
        self.assertEqual(self.fingerprints(dict(base))[(0, 0)], fp)
        self.assertNotEqual(self.fingerprints(base, dimension="minecraft:the_nether")[(0, 0)], fp)
        self.assertNotEqual(self.fingerprints(base, pipeline=PIPE2)[(0, 0)], fp)
        with_halo = dict(base, **world((6, 3)))
        self.assertNotEqual(self.fingerprints(with_halo)[(0, 0)], fp, "a halo chunk appearing changes identity")
        changed_halo = dict(with_halo, **{"6,3": h("other")})
        self.assertNotEqual(self.fingerprints(changed_halo)[(0, 0)], self.fingerprints(with_halo)[(0, 0)])
        far = dict(base, **world((7, 3)))  # outside the window of tile (0,0)
        self.assertEqual(self.fingerprints(far)[(0, 0)], fp)

    def test_window_is_at_most_128_blocks(self):
        current = world(*block(-13, -13, 13, 13), (-100, 50))
        for job in rp.plan_render(current, None, PIPE, None, DIM)["jobs"]:
            self.assertEqual((job["width"], job["depth"]), (128, 128))
            cells = [(c["x"], c["z"]) for c in job["inputChunks"]] + [tuple(c) for c in job["missingChunks"]]
            self.assertEqual(len(cells), 64)
            self.assertEqual(len(set(cells)), 64)
            for x, z in cells:
                self.assertTrue(job["originX"] <= x * 16 and x * 16 + 16 <= job["originX"] + 128)
                self.assertTrue(job["originZ"] <= z * 16 and z * 16 + 16 <= job["originZ"] + 128)
            for x, z in job["coreChunks"]:
                self.assertTrue(job["coreOriginX"] <= x * 16 < job["coreOriginX"] + 96)
                self.assertTrue(job["coreOriginZ"] <= z * 16 < job["coreOriginZ"] + 96)
            self.assertEqual(job["originX"], job["coreOriginX"] - 16)
            self.assertEqual(job["originZ"], job["coreOriginZ"] - 16)

    def test_brute_force_dirty_dependencies(self):
        rng = random.Random(1729)
        coords = block(-9, -9, 9, 9)
        hashes = [h(i) for i in range(3)]
        for _ in range(150):
            previous = {f"{x},{z}": rng.choice(hashes) for x, z in coords if rng.random() < 0.4}
            current = dict(previous)
            for _ in range(rng.randint(0, 4)):
                x, z = rng.choice(coords)
                key = f"{x},{z}"
                action = rng.random()
                if action < 0.33:
                    current.pop(key, None)
                else:
                    current[key] = rng.choice(hashes)
            plan = incremental(current, previous)
            now_tiles, before_tiles = oracle_core_tiles(current), oracle_core_tiles(previous)
            expected = {t for t in now_tiles if oracle_view(current, *t) != oracle_view(previous, *t)}
            self.assertEqual(set(tiles(plan)), expected)
            self.assertEqual(set(removed(plan)), before_tiles - now_tiles)
            self.assertEqual(plan["tileCount"], len(now_tiles))
            # Planned tiles have a new fingerprint; all other current tiles keep theirs.
            fp_now = {(j["tileX"], j["tileZ"]): j["fingerprint"]
                      for j in rp.plan_render(current, None, PIPE, None, DIM)["jobs"]}
            fp_before = {(j["tileX"], j["tileZ"]): j["fingerprint"]
                         for j in rp.plan_render(previous, None, PIPE, None, DIM)["jobs"]}
            for tile in now_tiles:
                self.assertEqual(fp_now[tile] != fp_before.get(tile), tile in expected)
            for job in plan["jobs"]:
                self.assertEqual(job["fingerprint"], fp_now[(job["tileX"], job["tileZ"])])


class ValidationTests(unittest.TestCase):
    def test_malformed_keys(self):
        for key in ["0,0 ", " 0,0", "00,1", "-0,1", "1,-0", "+1,2", "1", "a,b", "1,2,3", "1;2", "", "1.0,2", 1, None]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                rp.plan_render({key: h(1)}, None, PIPE, None, DIM)
        with self.assertRaises(ValueError):
            rp.plan_render(world((0, 0)), {"x": h(1)}, PIPE, PIPE, DIM)

    def test_int32_block_range(self):
        rp.plan_render({"134217720,0": h(1)}, None, PIPE, None, DIM)
        for key in ["134217727,0", "0,-134217728", "99999999999,0"]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                rp.plan_render({key: h(1)}, None, PIPE, None, DIM)

    def test_hashes_and_identity(self):
        for value in [h(1).upper(), h(1)[:-1], h(1) + "0", "g" * 64, None, 5]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                rp.plan_render({"0,0": value}, None, PIPE, None, DIM)
        for pipe in [PIPE.upper(), "", None, 1]:
            with self.subTest(pipe=pipe), self.assertRaises(ValueError):
                rp.plan_render(world((0, 0)), None, pipe, None, DIM)
        with self.assertRaises(ValueError):
            rp.plan_render(world((0, 0)), None, PIPE, "abc", DIM)
        for dim in ["", "   ", None, 0]:
            with self.subTest(dim=dim), self.assertRaises(ValueError):
                rp.plan_render(world((0, 0)), None, PIPE, None, dim)
        for bad in [[("0,0", h(1))], "0,0", None]:
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                rp.plan_render(bad, None, PIPE, None, DIM)
        with self.assertRaises(TypeError):
            rp.plan_render(world((0, 0)), [], PIPE, PIPE, DIM)

    def test_inputs_are_not_modified(self):
        current = world((0, 0), (7, 7))
        previous = world((0, 0))
        snapshot = (dict(current), dict(previous))
        incremental(current, previous)
        self.assertEqual((current, previous), snapshot)


if __name__ == "__main__":
    unittest.main()
