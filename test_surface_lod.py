"""Tests for surface_lod.py with synthetic exporter-shaped meshes.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_surface_lod.py" -v
"""
import copy
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import surface_lod as sl  # noqa: E402


def to_srgb(c):  # reference formula written out independently of the module
    return 12.92 * c if c <= 0.0031308 else 1.055 * math.pow(c, 1 / 2.4) - 0.055


def to_linear(c):
    return c / 12.92 if c <= 0.04045 else math.pow((c + 0.055) / 1.055, 2.4)


def top_quad(x0, z0, x1, z1, y, tint=(1.0, 1.0, 1.0), uv=(0.1, 0.2, 0.3, 0.4), y_of=None):
    """Upward face in Minecraft RenderBlocks.renderFaceYPos vertex order, indices 0,1,2 / 0,2,3 (exporter)."""
    h = y_of or (lambda px, pz: y)
    u0, v0, u1, v1 = uv
    corners = [(x1, z1, u1, v1), (x1, z0, u1, v0), (x0, z0, u0, v0), (x0, z1, u0, v1)]
    return {"positions": [c for px, pz, _, _ in corners for c in (px, h(px, pz), pz)],
            "uvs": [c for _, _, u, v in corners for c in (u, v)],
            "colors": list(tint) * 4,
            "indices": [0, 1, 2, 0, 2, 3]}


def bottom_quad(x0, z0, x1, z1, y):
    part = top_quad(x0, z0, x1, z1, y)
    part["indices"] = [0, 2, 1, 0, 3, 2]  # reversed winding: faces down
    return part


def side_quad(x0, x1, z, y0, y1):
    return {"positions": [x0, y0, z, x1, y0, z, x1, y1, z, x0, y1, z], "uvs": [0.0] * 8,
            "colors": [1.0] * 12, "indices": [0, 1, 2, 0, 2, 3]}


def grey(bounds):
    return (0.5, 0.5, 0.5)


def always(x, y, z):
    return True


def raster(parts, x=0, z=0, width=4, depth=4, sample_color=grey, accept_hit=always):
    return sl.rasterize(parts, x, z, width, depth, sample_color, accept_hit)


def heights(field):
    return [None if s is None else s["height"] for s in field["samples"]]


class RasterizeTests(unittest.TestCase):
    def test_flat_top_face(self):
        field = raster([top_quad(0, 0, 4, 4, 65)])
        self.assertEqual((field["x"], field["z"], field["width"], field["depth"]), (0, 0, 4, 4))
        self.assertEqual(heights(field), [65.0] * 16, "diagonal centres hit via the shared edge")
        for s in field["samples"]:
            for ch in s["color"]:
                self.assertAlmostEqual(ch, to_srgb(0.5), places=9)

    def test_downward_and_vertical_faces_are_ignored(self):
        self.assertEqual(heights(raster([bottom_quad(0, 0, 4, 4, 100)])), [None] * 16)
        self.assertEqual(heights(raster([side_quad(0, 4, 2, 60, 90)])), [None] * 16)
        self.assertEqual(heights(raster([bottom_quad(0, 0, 4, 4, 100), top_quad(0, 0, 4, 4, 65)])), [65.0] * 16)
        degenerate = top_quad(0, 0, 4, 4, 70)
        degenerate["positions"][3:6] = degenerate["positions"][0:3]  # collapse one vertex
        field = raster([degenerate])
        self.assertTrue(all(h is None or h == 70.0 for h in heights(field)))

    def test_stepped_terrain(self):
        field = raster([top_quad(0, 0, 2, 4, 60), top_quad(2, 0, 4, 4, 64)])
        self.assertEqual(heights(field), [60.0, 60.0, 64.0, 64.0] * 4)

    def test_sloped_face_is_interpolated(self):
        field = raster([top_quad(0, 0, 4, 4, None, y_of=lambda px, pz: 60 + pz)])
        for j in range(4):
            for i in range(4):
                self.assertAlmostEqual(field["samples"][j * 4 + i]["height"], 60 + j + 0.5, places=9)

    def test_highest_hit_wins_in_any_order(self):
        low, high = top_quad(0, 0, 4, 4, 65), top_quad(1, 1, 3, 3, 70)
        expected = [65.0] * 4 + [65.0, 70.0, 70.0, 65.0] * 2 + [65.0] * 4
        self.assertEqual(heights(raster([low, high])), expected)
        self.assertEqual(heights(raster([high, low])), expected)

    def test_exact_ties_keep_the_first_hit(self):
        red, blue = top_quad(0, 0, 4, 4, 65, tint=(1.0, 0.0, 0.0)), top_quad(0, 0, 4, 4, 65, tint=(0.0, 0.0, 1.0))
        white = lambda bounds: (1.0, 1.0, 1.0)  # noqa: E731
        for parts, want in (([red, blue], [1.0, 0.0, 0.0]), ([blue, red], [0.0, 0.0, 1.0])):
            got = raster(parts, sample_color=white)["samples"][5]["color"]
            for g, w in zip(got, want):
                self.assertAlmostEqual(g, w, places=12)

    def test_rejected_cloud_reveals_ground(self):
        calls = []

        def no_clouds(px, y, pz):
            calls.append((px, y, pz))
            return y < 128

        field = raster([top_quad(0, 0, 4, 4, 65), top_quad(0, 0, 4, 4, 192)], accept_hit=no_clouds)
        self.assertEqual(heights(field), [65.0] * 16)
        self.assertIn((0.5, 192.0, 0.5), calls)
        self.assertIn((3.5, 65.0, 2.5), calls)
        self.assertTrue(all(px % 1 == 0.5 and pz % 1 == 0.5 for px, _, pz in calls), "column centres")
        only_clouds = raster([top_quad(0, 0, 4, 4, 192)], accept_hit=no_clouds)
        self.assertEqual(heights(only_clouds), [None] * 16)

    def test_absent_columns_stay_none(self):
        field = raster([top_quad(0, 0, 2, 2, 64)])
        self.assertEqual(heights(field), [64.0, 64.0, None, None, 64.0, 64.0, None, None] + [None] * 8)

    def test_negative_coordinates(self):
        field = raster([top_quad(-8, -8, -6, -7, 30), top_quad(-3, -2, -1, 0, 40)], x=-8, z=-8, width=8, depth=8)
        samples = field["samples"]
        self.assertEqual((samples[0]["height"], samples[1]["height"]), (30.0, 30.0))  # X -8,-7 at Z -8
        self.assertIsNone(samples[8])  # Z -7 row
        for wx, wz in [(-3, -2), (-2, -2), (-3, -1), (-2, -1)]:
            self.assertEqual(samples[(wz + 8) * 8 + (wx + 8)]["height"], 40.0)
        self.assertEqual(sum(s is not None for s in samples), 6)
        partial = raster([top_quad(-10, -10, 10, 10, 50)], x=-2, z=-2, width=4, depth=4)
        self.assertEqual(heights(partial), [50.0] * 16, "geometry larger than the field is clipped, not rejected")

    def test_tint_and_texture_colour_space(self):
        field = raster([top_quad(0, 0, 4, 4, 64, tint=(0.5, 0.5, 0.5))], sample_color=lambda b: (1.0, 1.0, 1.0))
        self.assertAlmostEqual(field["samples"][0]["color"][0], 0.5, places=9, msg="sRGB tint round trip")
        field = raster([top_quad(0, 0, 4, 4, 64, tint=(0.5, 1.0, 0.2))], sample_color=lambda b: (0.25, 0.5, 1.0))
        expected = [to_srgb(to_linear(0.5) * 0.25), to_srgb(0.5), to_srgb(to_linear(0.2))]
        for got, want in zip(field["samples"][6]["color"], expected):
            self.assertAlmostEqual(got, want, places=9)

    def test_tint_is_interpolated_in_linear_space(self):
        # Tint 1 at the x=4 edge vertices, 0 at x=0: at a column centre the linear tint equals the barycentric weight.
        part = top_quad(0, 0, 4, 4, 64)
        part["colors"] = [1.0] * 3 + [1.0] * 3 + [0.0] * 3 + [0.0] * 3  # corners (x1,z1) (x1,z0) (x0,z0) (x0,z1)
        field = raster([part], sample_color=lambda b: (1.0, 1.0, 1.0))
        for i in range(4):
            self.assertAlmostEqual(field["samples"][4 + i]["color"][1], to_srgb((i + 0.5) / 4), places=9)

    def test_sample_color_receives_uv_bounds_once_per_present_column(self):
        seen = []

        def record(bounds):
            seen.append(bounds)
            return (1.0, 1.0, 1.0)

        raster([top_quad(0, 0, 2, 4, 60, uv=(0.25, 0.5, 0.375, 0.625)), top_quad(0, 0, 4, 4, 10)], sample_color=record)
        self.assertEqual(len(seen), 16)
        self.assertEqual(seen.count((0.25, 0.5, 0.375, 0.625)), 8)
        self.assertEqual(seen.count((0.1, 0.2, 0.3, 0.4)), 8)
        seen.clear()
        raster([top_quad(0, 0, 4, 4, 192)], sample_color=record, accept_hit=lambda *a: False)
        self.assertEqual(seen, [], "never called without an accepted hit")

    def test_generator_parts_deterministic_and_unmodified(self):
        parts = [top_quad(0, 0, 4, 4, 65, tint=(0.3, 0.6, 0.9)), top_quad(1, 1, 3, 3, 66)]
        before = copy.deepcopy(parts)
        a = raster(iter(parts))
        b = raster(copy.deepcopy(parts))
        self.assertEqual(json.dumps(a), json.dumps(b))
        self.assertEqual(parts, before)
        mesh = sl.build_mesh(a, step=2)
        self.assertEqual(json.dumps(mesh), json.dumps(sl.build_mesh(copy.deepcopy(a), step=2)))
        self.assertEqual(json.dumps(a), json.dumps(b), "build_mesh does not modify the field")

    def test_malformed_input_is_rejected(self):
        good = top_quad(0, 0, 4, 4, 65)
        broken = {
            "not a dict": "part",
            "positions not /3": {**good, "positions": good["positions"][:-1]},
            "position NaN": {**good, "positions": [math.nan] + good["positions"][1:]},
            "position bool": {**good, "positions": [True] + good["positions"][1:]},
            "uv short": {**good, "uvs": good["uvs"][:-2]},
            "uv string": {**good, "uvs": ["0"] + good["uvs"][1:]},
            "colors short": {**good, "colors": good["colors"][:-3]},
            "color above 1": {**good, "colors": [1.5] + good["colors"][1:]},
            "index out of range": {**good, "indices": [0, 1, 4, 0, 2, 3]},
            "index negative": {**good, "indices": [0, 1, -1, 0, 2, 3]},
            "index float": {**good, "indices": [0, 1, 2.0, 0, 2, 3]},
            "indices not /3": {**good, "indices": [0, 1, 2, 0]},
            "missing array": {k: v for k, v in good.items() if k != "uvs"},
        }
        for label, part in broken.items():
            with self.subTest(label), self.assertRaises(ValueError):
                raster([good, part])
        for args in [(0.5, 0, 4, 4), (0, 0, 0, 4), (0, 0, 4, 129), (True, 0, 4, 4), (0, 0, 4.0, 4)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                sl.rasterize([good], *args, grey, always)
        for bad_color in [lambda b: (0.5, 0.5), lambda b: (1.2, 0, 0), lambda b: (math.nan, 0, 0), lambda b: None]:
            with self.assertRaises(ValueError):
                raster([good], sample_color=bad_color)
        with self.assertRaises(ValueError):
            raster([good], accept_hit=lambda *a: 1)


class BuildMeshTests(unittest.TestCase):
    def up_facing(self, mesh):
        p, idx = mesh["positions"], mesh["indices"]
        for t in range(0, len(idx), 3):
            a, b, c = (p[3 * idx[t + k]:3 * idx[t + k] + 3] for k in range(3))
            u = [b[k] - a[k] for k in range(3)]
            v = [c[k] - a[k] for k in range(3)]
            self.assertGreater(u[2] * v[0] - u[0] * v[2], 0, "triangle faces +Y")

    def test_flat_field_shares_vertices(self):
        field = raster([top_quad(0, 0, 8, 8, 65)], width=8, depth=8)
        mesh = sl.build_mesh(field, step=4)
        self.assertEqual(mesh["stats"], {"step": 4, "finePresent": 64, "fineMissing": 0, "cells": 4,
                                         "cellsEmitted": 4, "vertices": 9, "triangles": 8,
                                         "maxFineEdgeRise": 0.0, "maxCoarseCellRise": 0.0,
                                         "coarseEligible": False})
        points = [tuple(mesh["positions"][i:i + 3]) for i in range(0, len(mesh["positions"]), 3)]
        self.assertEqual(len(set(points)), 9, "border vertices exist once and are shared by index")
        self.assertEqual(sorted({(px, pz) for px, _, pz in points}), [(gx, gz) for gx in (0, 4, 8) for gz in (0, 4, 8)])
        self.assertTrue(all(py == 65.0 for _, py, _ in points))
        self.assertEqual(mesh["uvs"], [0.0] * 18)
        self.assertEqual(len(mesh["colors"]), 27)
        self.assertTrue(all(0.0 <= c <= 1.0 for c in mesh["colors"]))
        self.up_facing(mesh)

    def test_boundary_heights_average_adjacent_columns(self):
        field = raster([top_quad(0, 0, 4, 8, 60), top_quad(4, 0, 8, 8, 64)], width=8, depth=8)
        mesh = sl.build_mesh(field, step=4)
        by_xz = {(mesh["positions"][i], mesh["positions"][i + 2]): mesh["positions"][i + 1]
                 for i in range(0, len(mesh["positions"]), 3)}
        self.assertEqual(by_xz[(0.0, 0.0)], 60.0)
        self.assertEqual(by_xz[(4.0, 4.0)], 62.0, "columns x=3 (60) and x=4 (64) around the grid line")
        self.assertEqual(by_xz[(8.0, 8.0)], 64.0, "outer corner uses the only inside column")
        self.up_facing(mesh)

    def test_colours_average_in_linear_space(self):
        black_white = [top_quad(0, 0, 1, 2, 64, tint=(0.0, 0.0, 0.0)), top_quad(1, 0, 2, 2, 64)]
        field = raster(black_white, width=2, depth=2, sample_color=lambda b: (1.0, 1.0, 1.0))
        mesh = sl.build_mesh(field, step=1)
        centre = [i for i in range(0, len(mesh["positions"]), 3) if mesh["positions"][i] == 1.0 and mesh["positions"][i + 2] == 1.0]
        vi = centre[0] // 3
        self.assertAlmostEqual(mesh["colors"][3 * vi], to_srgb(0.5), places=9)

    def test_missing_fine_column_omits_only_its_cell(self):
        field = raster([top_quad(0, 0, 8, 8, 65)], width=8, depth=8)
        field["samples"][5 * 8 + 6] = None  # column (6,5) lies in cell (1,1)
        mesh = sl.build_mesh(field, step=4)
        self.assertEqual(mesh["stats"]["cellsEmitted"], 3)
        self.assertEqual(mesh["stats"]["fineMissing"], 1)
        self.assertEqual(mesh["stats"]["triangles"], 6)
        points = {(mesh["positions"][i], mesh["positions"][i + 2]) for i in range(0, len(mesh["positions"]), 3)}
        self.assertNotIn((8.0, 8.0), points, "a corner used only by the missing cell is not emitted")
        empty = raster([], width=8, depth=8)
        self.assertEqual(sl.build_mesh(empty, step=4)["indices"], [])

    def test_negative_origin_and_steps(self):
        field = raster([top_quad(-16, -16, 0, 0, 40)], x=-16, z=-16, width=16, depth=16)
        for step, cells in [(1, 256), (2, 64), (4, 16), (8, 4), (16, 1)]:
            mesh = sl.build_mesh(field, step=step)
            self.assertEqual((mesh["stats"]["cellsEmitted"], mesh["stats"]["triangles"]), (cells, 2 * cells))
            xs = mesh["positions"][0::3]
            self.assertEqual((min(xs), max(xs)), (-16.0, 0.0))
            self.up_facing(mesh)
        for step in (3, 32, 0, True, "4"):
            with self.subTest(step=step), self.assertRaises(ValueError):
                sl.build_mesh(field, step=step)
        shifted = raster([top_quad(-10, -16, 10, 0, 40)], x=-6, z=-16, width=16, depth=16)
        with self.assertRaises(ValueError):
            sl.build_mesh(shifted, step=4)  # origin -6 not divisible by 4
        with self.assertRaises(ValueError):
            sl.build_mesh({**field, "samples": field["samples"][:-1]})
        with self.assertRaises(ValueError):
            sl.build_mesh({**field, "samples": [{"height": 1.0}] + field["samples"][1:]})


class EligibilityGuardTests(unittest.TestCase):
    """Fail-closed publication guard: a height field joins floating surfaces to the ground below."""

    def ground_and_island(self, i0, j0, size=2, ground=64, top=100):
        return raster([top_quad(0, 0, 16, 16, ground), top_quad(i0, j0, i0 + size, j0 + size, top)],
                      width=16, depth=16)

    def test_island_between_coarse_vertices_is_ineligible(self):
        # Island columns 5..6 lie strictly inside coarse cell (1,1); every step-4 grid vertex sees only ground.
        field = self.ground_and_island(5, 5)
        mesh = sl.build_mesh(field, step=4, max_edge_rise=8)
        stats = mesh["stats"]
        self.assertEqual(stats["maxCoarseCellRise"], 0.0, "coarse corners are all ground: the island is invisible")
        self.assertEqual(stats["maxFineEdgeRise"], 36.0)
        self.assertFalse(stats["coarseEligible"])
        self.assertEqual(stats["triangles"], 32, "diagnostic mesh is still returned in full")
        self.assertTrue(all(h == 64.0 for h in mesh["positions"][1::3]))

    def test_island_on_a_coarse_vertex_is_ineligible(self):
        # Island columns 3..4 surround grid vertex (4,4): the coarse mesh stretches a false wall to 100.
        field = self.ground_and_island(3, 3)
        stats = sl.build_mesh(field, step=4, max_edge_rise=8)["stats"]
        self.assertEqual(stats["maxCoarseCellRise"], 36.0)
        self.assertEqual(stats["maxFineEdgeRise"], 36.0)
        self.assertFalse(stats["coarseEligible"])
        self.assertTrue(sl.build_mesh(field, step=4, max_edge_rise=36)["stats"]["coarseEligible"],
                        "the guard is a threshold, not topology detection: an explicit 36 accepts the wall")

    def test_smooth_ramp_is_eligible_at_its_exact_rise(self):
        # Height 60 + column index along X: fine rise 1; interior coarse cell corners (averages of the two
        # columns beside each grid line) differ by exactly 4, border cells by 3.5.
        ramp = [top_quad(i, 0, i + 1, 16, 60 + i) for i in range(16)]
        field = raster(ramp, width=16, depth=16)
        stats = sl.build_mesh(field, step=4, max_edge_rise=4)["stats"]
        self.assertEqual((stats["maxFineEdgeRise"], stats["maxCoarseCellRise"]), (1.0, 4.0))
        self.assertTrue(stats["coarseEligible"], "a rise equal to the threshold is allowed")
        self.assertFalse(sl.build_mesh(field, step=4, max_edge_rise=3.999)["stats"]["coarseEligible"])
        self.assertTrue(sl.build_mesh(field, step=1, max_edge_rise=1)["stats"]["coarseEligible"])

    def test_no_threshold_is_diagnostic_only(self):
        field = raster([top_quad(0, 0, 16, 16, 64)], width=16, depth=16)
        for call in (lambda: sl.build_mesh(field), lambda: sl.build_mesh(field, 4),
                     lambda: sl.build_mesh(field, step=4, max_edge_rise=None)):
            stats = call()["stats"]
            self.assertEqual((stats["maxFineEdgeRise"], stats["maxCoarseCellRise"]), (0.0, 0.0))
            self.assertFalse(stats["coarseEligible"])
        self.assertTrue(sl.build_mesh(field, max_edge_rise=0)["stats"]["coarseEligible"])

    def test_missing_fine_sample_is_ineligible(self):
        field = raster([top_quad(0, 0, 16, 16, 64)], width=16, depth=16)
        field["samples"][7 * 16 + 9] = None
        mesh = sl.build_mesh(field, step=4, max_edge_rise=1000)
        self.assertFalse(mesh["stats"]["coarseEligible"])
        self.assertEqual(mesh["stats"]["cellsEmitted"], 15)
        self.assertFalse(sl.build_mesh(raster([], width=16, depth=16), max_edge_rise=1000)["stats"]["coarseEligible"])

    def test_threshold_validation(self):
        field = raster([top_quad(0, 0, 16, 16, 64)], width=16, depth=16)
        for bad in (-1, -0.5, math.nan, math.inf, True, "4", [4]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sl.build_mesh(field, step=4, max_edge_rise=bad)
        with self.assertRaises(TypeError):
            sl.build_mesh(field, 4, 8)  # max_edge_rise is keyword-only
        self.assertTrue(sl.build_mesh(field, 4, max_edge_rise=2.5)["stats"]["coarseEligible"])


if __name__ == "__main__":
    unittest.main()
