"""Tests for planar_lod.simplify_axis_aligned. Stdlib only; meshes are built from explicit unit faces."""
import copy
import math
import unittest

from planar_lod import simplify_axis_aligned
from surface_lod import linear_to_srgb

CYCLIC = {0: (1, 2), 1: (2, 0), 2: (0, 1)}
WHITE = lambda u0, v0, u1, v1: (1.0, 1.0, 1.0)
OPAQUE = lambda u0, v0, u1, v1: True


def run(mesh, sampler=WHITE, **kwargs):
    """Geometry tests state opacity explicitly: without a predicate nothing is merged."""
    kwargs.setdefault("is_opaque", OPAQUE)
    return simplify_axis_aligned(mesh, sampler, **kwargs)


def face(axis, sign, plane, u, v, uv=0.0, color=1.0):
    """One unit square as two triangles (4 vertices) with the winding of orientation sign*axis."""
    b, c = CYCLIC[axis]
    positions, uvs = [], []
    for du, dv in ((0, 0), (1, 0), (1, 1), (0, 1)):
        point = [0, 0, 0]
        point[axis], point[b], point[c] = plane, u + du, v + dv
        positions += point
        uvs += [uv + 0.01 * du, uv + 0.01 * dv]
    order = [0, 1, 2, 0, 2, 3] if sign > 0 else [0, 2, 1, 0, 3, 2]
    return positions, uvs, [color] * 12, order


def mesh_of(*faces, extra=()):
    """faces from face(); extra = raw (positions, uvs, colors, indices) triangles appended as-is."""
    mesh = {"positions": [], "uvs": [], "colors": [], "indices": []}
    for positions, uvs, colors, order in [*faces, *extra]:
        base = len(mesh["positions"]) // 3
        mesh["positions"] += positions
        mesh["uvs"] += uvs
        mesh["colors"] += colors
        mesh["indices"] += [base + i for i in order]
    return mesh


def normal(positions, tri):
    a, b, c = (positions[3 * i:3 * i + 3] for i in tri)
    e1 = [b[k] - a[k] for k in range(3)]
    e2 = [c[k] - a[k] for k in range(3)]
    return (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])


def coverage(coarse, test):
    """Oriented unit cells covered by the coarse quads; also checks planarity and winding of every quad."""
    cells = set()
    p, idx = coarse["positions"], coarse["indices"]
    test.assertEqual(len(idx) % 6, 0)
    for q in range(0, len(idx), 6):
        points = {tuple(p[3 * i:3 * i + 3]) for i in idx[q:q + 6]}
        axes = [a for a in range(3) if len({pt[a] for pt in points}) == 1]
        test.assertEqual(len(axes), 1, "every quad lies in one axis-aligned plane")
        axis = axes[0]
        n1, n2 = normal(p, idx[q:q + 3]), normal(p, idx[q + 3:q + 6])
        for n in (n1, n2):
            test.assertTrue(all(n[k] == 0 for k in range(3) if k != axis) and n[axis] != 0)
        test.assertEqual(math.copysign(1, n1[axis]), math.copysign(1, n2[axis]), "both triangles share the winding")
        sign = 1 if n1[axis] > 0 else -1
        b, c = CYCLIC[axis]
        plane = next(iter(points))[axis]
        us, vs = [pt[b] for pt in points], [pt[c] for pt in points]
        for u in range(min(us), max(us)):
            for v in range(min(vs), max(vs)):
                cell = (axis, sign, plane, u, v)
                test.assertNotIn(cell, cells, "coarse quads never overlap")
                cells.add(cell)
    return cells


class PlanarLodTests(unittest.TestCase):
    # ------------------------------------------------------------------ geometry (explicitly opaque)

    def test_all_six_orientations_keep_their_winding(self):
        faces = [face(a, s, 3, 1, 2) for a in range(3) for s in (1, -1)]
        result = run(mesh_of(*faces))
        self.assertEqual(coverage(result["coarse"], self),
                         {(a, s, 3, 1, 2) for a in range(3) for s in (1, -1)})
        self.assertEqual(result["fallback"]["indices"], [])
        self.assertEqual(result["stats"]["coarseQuads"], 6)
        self.assertEqual(result["stats"]["recognizedSquares"], 6)

    def test_flat_grid_merges_into_one_quad(self):
        faces = [face(1, 1, 64, u, v) for u in range(4) for v in range(4)]
        result = run(mesh_of(*faces))
        self.assertEqual(result["stats"]["coarseQuads"], 1)
        self.assertEqual(result["stats"]["outputTriangles"], 2)
        self.assertEqual(coverage(result["coarse"], self), {(1, 1, 64, u, v) for u in range(4) for v in range(4)})
        self.assertEqual(result["coarse"]["uvs"], [0.0] * 8, "coarse mesh is untextured")
        self.assertEqual(result["coarse"]["colors"], [1.0] * 12, "white stays exactly white")

    def test_floating_island_stays_disconnected_without_invented_walls(self):
        floor = [face(1, 1, 0, u, v) for u in range(6) for v in range(6)]
        island_top = [face(1, 1, 10, u, v) for u in range(2, 4) for v in range(2, 4)]
        island_bottom = [face(1, -1, 9, u, v) for u in range(2, 4) for v in range(2, 4)]
        result = run(mesh_of(*floor, *island_top, *island_bottom))
        cells = coverage(result["coarse"], self)
        self.assertEqual({(a, s, p) for a, s, p, _, _ in cells}, {(1, 1, 0), (1, -1, 9), (1, 1, 10)},
                         "only the original planes; no vertical wall, no bridge between layers")
        self.assertEqual(cells, {(1, 1, 0, u, v) for u in range(6) for v in range(6)}
                         | {(1, 1, 10, u, v) for u in range(2, 4) for v in range(2, 4)}
                         | {(1, -1, 9, u, v) for u in range(2, 4) for v in range(2, 4)})
        self.assertEqual(set(result["coarse"]["positions"][1::3]), {0, 9, 10}, "no vertex moves off its plane")

    def test_holes_are_never_filled(self):
        faces = [face(1, 1, 5, u, v) for u in range(3) for v in range(3) if (u, v) != (1, 1)]
        result = run(mesh_of(*faces))
        cells = coverage(result["coarse"], self)
        self.assertEqual(cells, {(1, 1, 5, u, v) for u in range(3) for v in range(3) if (u, v) != (1, 1)})
        self.assertGreaterEqual(result["stats"]["coarseQuads"], 2)

    def test_colour_orientation_and_plane_boundaries_are_kept(self):
        dark = lambda u0, v0, u1, v1: (0.1, 0.1, 0.1) if u0 >= 0.5 else (1.0, 1.0, 1.0)
        faces = [face(1, 1, 0, 0, 0), face(1, 1, 0, 1, 0, uv=0.6),   # same plane, different colour
                 face(1, -1, 0, 0, 1), face(1, 1, 0, 0, 1),           # same cell, opposite orientation
                 face(1, 1, 1, 1, 1)]                                 # adjacent cell on another plane
        result = run(mesh_of(*faces), dark)
        self.assertEqual(coverage(result["coarse"], self), {(1, 1, 0, 0, 0), (1, 1, 0, 1, 0), (1, -1, 0, 0, 1),
                                                            (1, 1, 0, 0, 1), (1, 1, 1, 1, 1)})
        # Only the two white +y cells at plane 0, (0,0) and (0,1), share plane, orientation and colour.
        self.assertEqual(result["stats"]["coarseQuads"], 4)
        tinted = run(mesh_of(face(1, 1, 0, 0, 0), face(1, 1, 0, 1, 0, color=0.5)))
        self.assertEqual(tinted["stats"]["coarseQuads"], 2, "vertex tint changes the level")

    def test_sloped_non_unit_and_transparent_geometry_is_preserved_exactly(self):
        sloped = ([0, 0, 0, 1, 1, 0, 1, 1, 1, 0, 0, 1], [0.1, 0.1, 0.2, 0.1, 0.2, 0.2, 0.1, 0.2],
                  [0.3] * 12, [0, 1, 2, 0, 2, 3])
        big = face(1, 1, 0, 0, 0)
        big = ([v * 2 for v in big[0]], big[1], big[2], big[3])           # a 2x2 square as two triangles
        half = face(1, 1, 0, 5, 5)
        half = ([v + 0.5 if i % 3 == 1 else v for i, v in enumerate(half[0])], half[1], half[2], half[3])
        transparent = {"positions": [0, 0, 0, 1, 0, 0, 0, 0, 1], "uvs": [0, 0, 1, 0, 0, 1], "colors": [1] * 9,
                       "indices": [0, 1, 2]}
        mesh = mesh_of(face(1, 1, 3, 0, 0), extra=[sloped, big, half])
        mesh["transparentParts"] = [copy.deepcopy(transparent)]
        result = run(mesh)
        self.assertEqual(result["stats"]["coarseQuads"], 1)
        fallback = result["fallback"]
        self.assertEqual(fallback["transparentParts"], [transparent], "transparent parts unchanged")
        self.assertEqual(result["stats"]["fallbackTriangles"], 6)
        self.assertEqual(fallback["positions"], mesh["positions"][12:], "fallback values are exact")
        self.assertEqual(fallback["uvs"], mesh["uvs"][8:])
        self.assertEqual(fallback["colors"], mesh["colors"][12:])
        glass = mesh_of(face(1, 1, 0, 0, 0))
        glass["transparent"] = True
        kept = run(glass)
        self.assertEqual(kept["stats"]["coarseQuads"], 0)
        self.assertIs(kept["fallback"]["transparent"], True)
        self.assertEqual(kept["fallback"]["indices"], [0, 1, 2, 0, 2, 3])

    def test_duplicates_and_non_tiling_pairs_stay_in_the_fallback(self):
        result = run(mesh_of(face(1, 1, 0, 0, 0), face(1, 1, 0, 0, 0, uv=0.6), face(1, 1, 0, 1, 0)))
        self.assertEqual(result["stats"]["duplicateSquares"], 2)
        self.assertEqual(result["stats"]["fallbackTriangles"], 4)
        self.assertEqual(coverage(result["coarse"], self), {(1, 1, 0, 1, 0)}, "no merge across a duplicate cell")
        p, u, c, _ = face(1, 1, 0, 0, 0)
        for order in ([0, 1, 2, 0, 1, 3], [0, 1, 2, 0, 3, 2]):
            with self.subTest(order=order):
                r = run(mesh_of((p, u, c, order)))
                self.assertEqual(r["stats"]["coarseQuads"], 0)
                self.assertEqual(r["stats"]["fallbackTriangles"], 2)
        p, u, c, order = face(1, 1, 0, 0, 0)
        r = run(mesh_of((p, [x + 1.5 for x in u], c, order)))
        self.assertEqual(r["stats"]["coarseQuads"], 0, "UVs outside the atlas are not merged")
        self.assertEqual(r["stats"]["notAcceptedSquares"], 1)

    # ------------------------------------------------------------------ opacity

    def test_without_opacity_knowledge_everything_is_kept_and_nothing_is_sampled(self):
        calls = []
        mesh = mesh_of(*[face(1, 1, 0, u, v) for u in range(3) for v in range(3)])
        result = simplify_axis_aligned(mesh, lambda *b: calls.append(b) or (1.0, 1.0, 1.0))
        self.assertEqual(calls, [], "the colour sampler is not called without an explicit opaque decision")
        self.assertEqual(result["stats"]["coarseQuads"], 0)
        self.assertEqual(result["stats"]["notAcceptedSquares"], 9)
        for key in ("positions", "uvs", "colors", "indices"):
            self.assertEqual(result["fallback"][key], mesh[key], key)

    def test_cutout_faces_stay_exact_and_split_the_opaque_merge(self):
        leaf = lambda u0, v0, u1, v1: u0 < 0.5           # faces with uv >= 0.5 are cut-out (e.g. leaves)
        sampled = []
        faces = [face(1, 1, 0, 0, 0), face(1, 1, 0, 1, 0, uv=0.6, color=0.8), face(1, 1, 0, 2, 0)]
        mesh = mesh_of(*faces)
        result = run(mesh, lambda *b: sampled.append(b) or (1.0, 1.0, 1.0), is_opaque=leaf)
        self.assertEqual(coverage(result["coarse"], self), {(1, 1, 0, 0, 0), (1, 1, 0, 2, 0)},
                         "no bridge across the cut-out cell")
        self.assertEqual(result["stats"]["coarseQuads"], 2)
        self.assertEqual(len(sampled), 2, "only accepted opaque faces are sampled")
        self.assertTrue(all(b[0] < 0.5 for b in sampled))
        fallback = result["fallback"]
        self.assertEqual(fallback["positions"], mesh["positions"][12:24], "cut-out face kept exactly")
        self.assertEqual(fallback["uvs"], mesh["uvs"][8:16])
        self.assertEqual(fallback["colors"], mesh["colors"][12:24])
        self.assertEqual(fallback["indices"], [0, 1, 2, 0, 2, 3])
        self.assertEqual(result["stats"]["notAcceptedSquares"], 1)

    def test_invalid_opacity_predicates_are_refused(self):
        good = mesh_of(face(1, 1, 0, 0, 0))
        for answer in (1, 0, "yes", None, 1.0):
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, "is_opaque must return a bool"):
                run(good, is_opaque=lambda *b, answer=answer: answer)
        for predicate in (True, "opaque", 1):
            with self.subTest(predicate=predicate), self.assertRaisesRegex(ValueError, "is_opaque must be None"):
                run(good, is_opaque=predicate)

    # ------------------------------------------------------------------ colour levels

    def test_black_and_white_are_exact_and_levels_are_nearest_srgb(self):
        black = run(mesh_of(face(1, 1, 0, 0, 0)), lambda *b: (0.0, 0.0, 0.0))
        self.assertEqual(black["coarse"]["colors"], [0.0] * 12, "true black is not lifted")
        white = run(mesh_of(face(1, 1, 0, 0, 0)), lambda *b: (1.0, 1.0, 1.0))
        self.assertEqual(white["coarse"]["colors"], [1.0] * 12)
        for steps in (2, 3, 8, 256):
            bound = 1 / (2 * (steps - 1)) + 1e-12
            for i in range(0, 101):
                t = i / 100
                with self.subTest(steps=steps, linear=t):
                    out = run(mesh_of(face(1, 1, 0, 0, 0)), lambda *b, t=t: (t, t, t), color_steps=steps)
                    emitted = out["coarse"]["colors"][0]
                    self.assertLessEqual(abs(emitted - linear_to_srgb(t)), bound)
                    self.assertAlmostEqual(emitted * (steps - 1), round(emitted * (steps - 1)), places=9)
        near = run(mesh_of(face(1, 1, 0, 0, 0, color=0.99), face(1, 1, 0, 1, 0, color=1.0)))
        self.assertEqual(near["stats"]["coarseQuads"], 1, "near-white faces share the white level and merge")

    # ------------------------------------------------------------------ validation

    def test_bad_data_and_options_are_refused(self):
        good = mesh_of(face(1, 1, 0, 0, 0))
        for steps in (0, 1, 257, True, 2.0, None):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                run(good, color_steps=steps)
        with self.assertRaises(ValueError):
            run(good, "not callable")
        for bad in ((1.2, 0, 0), (0, 0), (math.nan, 0, 0), "rgb", (-0.1, 0, 0)):
            with self.subTest(callback=bad), self.assertRaises(ValueError):
                run(good, lambda *bounds, bad=bad: bad)
        broken = [
            {**good, "indices": [0, 1, 9, 0, 2, 3]},
            {**good, "uvs": good["uvs"][:-1]},
            {**good, "colors": [2.0] * 12},
            {**good, "positions": [math.inf] + good["positions"][1:]},
            {**good, "transparent": "yes"},
            {**good, "transparentParts": {}},
            {**good, "transparentParts": [{**good, "transparentParts": []}]},
            "not a mesh",
        ]
        for mesh in broken:
            with self.subTest(mesh=str(mesh)[:40]), self.assertRaises(ValueError):
                run(mesh)


if __name__ == "__main__":
    unittest.main()
