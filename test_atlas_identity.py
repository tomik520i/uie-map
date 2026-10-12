"""Tests for atlas_identity.compare_atlases. Stdlib only; PNG fixtures are built in memory."""
import copy
import hashlib
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import atlas_identity as ai

KEY = "a" * 64
OTHER_KEY = "b" * 64


def png(width, height, value=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([value]) * (width * 4) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PNG = png(32, 32)


def sprite(name, x=0, y=0, w=16, h=16, rotated=False, uv=(0.01, 0.01, 0.49, 0.49), getter=None, matches=True,
           state="placed"):
    return {"iconName": name, "state": state, "x": x, "y": y, "width": w, "height": h, "rotated": rotated,
            "uv": list(uv), "getterUv": list(uv if getter is None else getter), "getterUvMatches": matches}


def layout(atlas=PNG, width=32, height=32):
    return {"format": "UIE_ATLAS_LAYOUT1", "width": width, "height": height, "reusable": True, "reuseBlockers": [],
            "missing": sprite("missingno"),
            "registered": {"stone": sprite("stone", 16, 0), "alias": sprite("stone", 0, 16)},
            "uploaded": {"missingno": sprite("missingno"), "stone": sprite("stone", 16, 0)},
            "atlasSha256": hashlib.sha256(atlas).hexdigest()}


class AtlasIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.count = 0

    def files(self, atlas=PNG, doc=None, text=None):
        self.count += 1
        atlas_path = self.root / f"atlas{self.count}.png"
        layout_path = self.root / f"layout{self.count}.json"
        atlas_path.write_bytes(atlas)
        if text is None:
            text = json.dumps(layout(atlas) if doc is None else doc)
        layout_path.write_text(text, encoding="utf-8")
        return atlas_path, layout_path

    def compare(self, first, second, key_a=KEY, key_b=KEY):
        return ai.compare_atlases(*first, *second, pipeline_key_a=key_a, pipeline_key_b=key_b)

    def assert_invalid(self, doc=None, text=None, atlas=PNG):
        pair = self.files(atlas, doc, text)
        with self.assertRaises(ValueError):
            self.compare(pair, self.files())
        with self.assertRaises(ValueError):
            self.compare(self.files(), pair)

    def mutated(self, change):
        doc = layout()
        change(doc)
        return doc

    # Compatible results

    def test_identical_atlas_and_layout(self):
        reordered = dict(reversed(list(layout().items())))
        result = self.compare(self.files(), self.files(text=json.dumps(reordered, indent=2)))
        self.assertTrue(result["compatible"])
        self.assertTrue(result["atlasBytesEqual"])
        self.assertRegex(result["layoutIdentitySha256"], r"^[0-9a-f]{64}$")
        expected = layout()
        del expected["atlasSha256"]
        self.assertEqual(result["layoutIdentitySha256"], ai.layout_identity(expected))

    def test_different_bytes_with_equal_layout_is_only_a_candidate(self):
        other = png(32, 32, 7)
        same = self.compare(self.files(), self.files())
        result = self.compare(self.files(), self.files(other))
        self.assertTrue(result["compatible"])
        self.assertFalse(result["atlasBytesEqual"])
        self.assertIn("NOT established", result["reason"])
        self.assertIn("candidate", result["reason"])
        # The identity excludes atlasSha256, so it is the same as for identical bytes.
        self.assertEqual(result["layoutIdentitySha256"], same["layoutIdentitySha256"])

    # Valid but incompatible

    def test_layout_differences_are_incompatible(self):
        moved = self.mutated(lambda d: d["registered"].__setitem__("alias", sprite("stone", 16, 16)))
        uv_bits = self.mutated(lambda d: d["uploaded"].__setitem__(
            "stone", sprite("stone", 16, 0, uv=(0.01, 0.01, 0.49, 0.4900000095367431))))
        renamed = self.mutated(lambda d: d["registered"].__setitem__("alias2", d["registered"].pop("alias")))
        for doc in (moved, uv_bits, renamed):
            with self.subTest(doc=doc):
                result = self.compare(self.files(), self.files(doc=doc))
                self.assertFalse(result["compatible"])
                self.assertEqual(result["reason"], "layouts differ")
                self.assertIsNone(result["layoutIdentitySha256"])

    def test_pipeline_keys(self):
        result = self.compare(self.files(), self.files(), key_b=OTHER_KEY)
        self.assertFalse(result["compatible"])
        self.assertIn("pipeline", result["reason"])
        self.assertIsNone(result["layoutIdentitySha256"])
        for bad in ("A" * 64, "a" * 63, "g" * 64, "", None, KEY.encode(), " " + "a" * 63):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.compare(self.files(), self.files(), key_a=bad)

    def test_unstitched_registered_sprite_blocks_reuse(self):
        def unstitched(d):
            d["registered"]["late"] = sprite("late", 0, 0, 0, 0, uv=(0.0, 0.0, 0.0, 0.0), state="unstitched")
            d["reusable"] = False
            d["reuseBlockers"] = ["registered/late: unstitched"]
        doc = self.mutated(unstitched)
        result = self.compare(self.files(doc=doc), self.files(doc=doc))
        self.assertFalse(result["compatible"])
        self.assertIn("not reusable", result["reason"])
        self.assertIsNone(result["layoutIdentitySha256"])
        # Inconsistent or misplaced unstitched entries are invalid.
        claimed = copy.deepcopy(doc)
        claimed["reusable"], claimed["reuseBlockers"] = True, []
        self.assert_invalid(claimed)
        in_uploaded = copy.deepcopy(doc)
        in_uploaded["uploaded"]["late"] = in_uploaded["registered"].pop("late")
        in_uploaded["reuseBlockers"] = ["uploaded/late: unstitched"]
        self.assert_invalid(in_uploaded)
        sized = copy.deepcopy(doc)
        sized["registered"]["late"]["width"] = 16
        self.assert_invalid(sized)

    def test_getter_mismatch_is_valid_but_not_reusable(self):
        def mismatch(d):
            odd = sprite("stone", 16, 0, getter=(0.01, 0.01, 0.5, 0.49), matches=False)
            d["uploaded"]["stone"] = odd
            d["registered"]["stone"] = copy.deepcopy(odd)
            d["reusable"] = False
            d["reuseBlockers"] = ["registered/stone: getter UV differs from stored UV",
                                  "uploaded/stone: getter UV differs from stored UV"]
        doc = self.mutated(mismatch)
        result = self.compare(self.files(), self.files(doc=doc))
        self.assertFalse(result["compatible"])
        self.assertIn("layout B is not reusable", result["reason"])
        nulled = copy.deepcopy(doc)
        nulled["uploaded"]["stone"]["getterUv"][2] = None
        nulled["registered"]["stone"]["getterUv"][2] = None
        self.assertFalse(self.compare(self.files(doc=nulled), self.files())["compatible"])
        # Contradictions: flag says equal but values differ; blockers out of order; -0.0 is not 0.0.
        lying = copy.deepcopy(doc)
        lying["uploaded"]["stone"]["getterUvMatches"] = True
        self.assert_invalid(lying)
        unordered = copy.deepcopy(doc)
        unordered["reuseBlockers"].reverse()
        self.assert_invalid(unordered)
        signed = self.mutated(lambda d: d["uploaded"].__setitem__(
            "stone", sprite("stone", 16, 0, uv=(0.0, 0.0, 0.49, 0.49), getter=(-0.0, 0.0, 0.49, 0.49))))
        self.assert_invalid(signed)

    # Invalid input

    def test_png_binding(self):
        self.assert_invalid(self.mutated(lambda d: d.__setitem__("atlasSha256", hashlib.sha256(b"x").hexdigest())))
        self.assert_invalid(self.mutated(lambda d: d.__setitem__("atlasSha256", d["atlasSha256"].upper())))
        small = png(32, 16)
        self.assert_invalid(layout(small), atlas=small)  # IHDR 32x16, layout 32x32
        self.assert_invalid(layout(b"not a png"), atlas=b"not a png")
        broken = bytearray(PNG)
        broken[29] ^= 1  # IHDR CRC
        self.assert_invalid(layout(bytes(broken)), atlas=bytes(broken))

    def test_field_types_and_unknown_fields(self):
        changes = {
            "string width": lambda d: d.__setitem__("width", "32"),
            "boolean width": lambda d: d.__setitem__("width", True),
            "float width": lambda d: d.__setitem__("width", 32.0),
            "format": lambda d: d.__setitem__("format", "UIE_ATLAS_LAYOUT2"),
            "reusable int": lambda d: d.__setitem__("reusable", 1),
            "blockers string": lambda d: d.__setitem__("reuseBlockers", "none"),
            "extra top field": lambda d: d.__setitem__("sprites", {}),
            "missing top field": lambda d: d.pop("registered"),
            "extra sprite field": lambda d: d["uploaded"]["stone"].__setitem__("frame", 0),
            "missing sprite field": lambda d: d["missing"].pop("rotated"),
            "float x": lambda d: d["uploaded"]["stone"].__setitem__("x", 16.0),
            "rotated int": lambda d: d["missing"].__setitem__("rotated", 0),
            "empty name": lambda d: d["missing"].__setitem__("iconName", ""),
            "integer uv": lambda d: d["missing"].__setitem__("uv", [0, 0.01, 0.49, 0.49]),
            "short uv": lambda d: d["missing"].__setitem__("uv", [0.01, 0.01, 0.49]),
            "unknown state": lambda d: d["missing"].__setitem__("state", "animated"),
            "unstitched missing": lambda d: d.__setitem__("missing", sprite("missingno", 0, 0, 0, 0, state="unstitched")),
            "empty uploaded": lambda d: d.__setitem__("uploaded", {}),
            "empty key": lambda d: d["registered"].__setitem__("", sprite("x")),
            "sprite list": lambda d: d["uploaded"].__setitem__("stone", []),
        }
        for label, change in changes.items():
            with self.subTest(label):
                self.assert_invalid(self.mutated(change))

    def test_json_strictness(self):
        text = json.dumps(layout())
        cases = {
            "duplicate key": text[:-1] + ', "width": 32}',
            "NaN": text.replace("0.49", "NaN", 1),
            "overflowing float": text.replace("0.49", "1e999", 1),
            "array": "[]",
            "truncated": text[:-1],
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assert_invalid(text=value)
        pair = self.files()
        pair[1].write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
        with self.assertRaises(ValueError):
            self.compare(pair, self.files())

    def test_uv_and_extent(self):
        invalid = {
            # Whole descriptors, so uv and getterUv agree and only the range/order check can refuse them.
            "uv above one": lambda d: d.__setitem__("missing", sprite("missingno", uv=(0.01, 0.01, 1.5, 0.49))),
            "reversed uv": lambda d: d.__setitem__("missing", sprite("missingno", uv=(0.49, 0.01, 0.01, 0.49))),
            "outside x": lambda d: d["uploaded"].__setitem__("stone", sprite("stone", 17, 0)),
            "negative y": lambda d: d["uploaded"].__setitem__("stone", sprite("stone", 0, -1)),
            "unrotated 32x16 at x=16": lambda d: d["uploaded"].__setitem__("wide", sprite("wide", 16, 0, 32, 16)),
            "rotated 32x16 at y=16": lambda d: d["uploaded"].__setitem__("wide", sprite("wide", 0, 16, 32, 16, True)),
        }
        for label, change in invalid.items():
            with self.subTest(label):
                self.assert_invalid(self.mutated(change))
        fits = self.mutated(lambda d: d["uploaded"].__setitem__("wide", sprite("wide", 16, 0, 32, 16, True)))
        self.assertTrue(self.compare(self.files(doc=fits), self.files(doc=fits))["compatible"])

    def test_files_must_be_regular_bounded_and_not_symlinks(self):
        atlas, layout_path = self.files()
        link = self.root / "link.json"
        try:
            os.symlink(layout_path, link)
        except (OSError, NotImplementedError):
            link = None
        if link is not None:
            with self.assertRaises(ValueError):
                self.compare((atlas, link), self.files())
        with self.assertRaises(ValueError):
            self.compare((atlas, self.root), self.files())
        with self.assertRaises(ValueError):
            self.compare((atlas, self.root / "absent.json"), self.files())
        with mock.patch.object(ai, "MAX_LAYOUT_BYTES", 100), self.assertRaises(ValueError):
            self.compare(self.files(), self.files())
        with mock.patch.object(ai, "MAX_ATLAS_BYTES", len(PNG) - 1), self.assertRaises(ValueError):
            self.compare(self.files(), self.files())


if __name__ == "__main__":
    unittest.main()
