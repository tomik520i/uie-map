"""Tests for batch_block_info.py with a small synthetic UIE2 batch.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_batch_block_info.py" -v
"""
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import batch_block_info as bbi  # noqa: E402
import capture_batch as cb  # noqa: E402
import capture_snapshot as cs  # noqa: E402

REGISTRY = {0: "minecraft:air", 1: "minecraft:stone", 7: "minecraft:bedrock", 12965: "hbm:tile.hadron_coil_gold",
            15983: "mod:top"}
# Input chunks x/z -2..1, core chunks -1..0. Core chunk (0,0) and halo chunk (-2,-2) are absent.
AREA, CORE = cs.Area(-32, -32, 64, 64), cs.Area(-16, -16, 32, 32)
PRESENT = {(cx, cz) for cx in range(-2, 2) for cz in range(-2, 2)} - {(0, 0), (-2, -2)}
CORE_PRESENT = [(-1, -1), (0, -1), (-1, 0)]  # sorted by (z, x)
HALO_ID = 15983


def expected(x, y, z):
    """Core content; halo chunks carry HALO_ID so any leak into the export is visible."""
    if y == 0:
        return 7, 0
    if 1 <= y <= 4:
        return 12965, x & 15
    if y == 5:
        return 1, z & 15
    return 0, 0


def halo(x, y, z):
    return (HALO_ID, 3) if y < 3 else (0, 0)


def build_snapshot():
    columns = []
    for z in range(AREA.z, AREA.z + AREA.depth):
        for x in range(AREA.x, AREA.x + AREA.width):
            chunk = (x >> 4, z >> 4)
            if chunk not in PRESENT:
                columns.append(bytes(cs.COLUMN_BYTES))
                continue
            in_core = CORE.x <= x < CORE.x + CORE.width and CORE.z <= z < CORE.z + CORE.depth
            fn = expected if in_core else halo
            columns.append(bytes([(x * 3 + z) & 0xFF])
                           + b"".join(struct.pack(">HB", *fn(x, y, z)) for y in range(256)))
    snapshot = cb.BatchSnapshot(AREA, CORE, dict(REGISTRY), b"".join(columns), frozenset(PRESENT))
    return cb.encode_batch(snapshot)


SNAPSHOT = build_snapshot()
SHA = hashlib.sha256(SNAPSHOT).hexdigest()


def decode(doc):
    voxels = []
    for count, block_id, meta in doc["runs"]:
        voxels.extend([(block_id, meta)] * count)
    return voxels


class TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.snapshot = self.root / "input" / "snapshot.bin"
        self.snapshot.parent.mkdir()
        self.snapshot.write_bytes(SNAPSHOT)
        self.data = self.root / "data"
        self.data.mkdir()
        self.manifest = self.data / "manifest.json"

    def write_manifest(self, **overrides):
        manifest = {"format": 1, "title": "UIE", "atlas": "atlas.png", "snapshotFormat": "UIE2",
                    "snapshotSha256": SHA, "sample": True,
                    "chunks": [{"file": f"chunk_{cx}_{cz}.json", "x": cx, "z": cz} for cx, cz in CORE_PRESENT]}
        manifest.update(overrides)
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        return self.manifest.read_bytes()

    def block_files(self):
        root = self.data / "blocks"
        return sorted(p.relative_to(self.data).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


class ExportTests(TestCase):
    def test_only_present_core_chunks_with_exact_voxels(self):
        info = bbi.export_batch_block_info(self.snapshot, self.data)
        self.assertEqual(info["format"], "UIE_BLOCKS1")
        self.assertEqual(info["snapshotSha256"], SHA)
        self.assertEqual([(c["x"], c["z"]) for c in info["chunks"]], CORE_PRESENT)
        self.assertEqual(self.block_files(), sorted(f"blocks/{SHA}/chunk_{cx}_{cz}.json" for cx, cz in CORE_PRESENT))
        for chunk in info["chunks"]:
            doc = json.loads((self.data / chunk["file"]).read_text("utf-8"))
            cx, cz = chunk["x"], chunk["z"]
            self.assertEqual((doc["format"], doc["x"], doc["z"], doc["width"], doc["depth"], doc["height"]),
                             ("UIE_BLOCKS1", cx * 16, cz * 16, 16, 16, 256))
            self.assertEqual(doc["registry"], {"0": "minecraft:air", "1": "minecraft:stone", "7": "minecraft:bedrock",
                                               "12965": "hbm:tile.hadron_coil_gold"}, "per-chunk registry, no halo ids")
            voxels = decode(doc)
            self.assertEqual(len(voxels), 16 * 16 * 256)
            for dz in range(16):
                for dx in range(16):
                    x, z = cx * 16 + dx, cz * 16 + dz
                    base = (dz * 16 + dx) * 256
                    for y in (0, 1, 4, 5, 6, 255):
                        self.assertEqual(voxels[base + y], expected(x, y, z), f"{x},{y},{z}")
        self.assertTrue(all(HALO_ID not in [v[0] for v in decode(json.loads((self.data / c["file"]).read_text()))]
                            for c in info["chunks"]))

    def test_deterministic_and_idempotent(self):
        first = bbi.export_batch_block_info(self.snapshot, self.data)
        contents = {f: (self.data / f).read_bytes() for f in self.block_files()}
        second = bbi.export_batch_block_info(self.snapshot, self.data)
        self.assertEqual(first, second)
        self.assertEqual({f: (self.data / f).read_bytes() for f in self.block_files()}, contents)

    def test_tampered_immutable_file_is_rejected(self):
        info = bbi.export_batch_block_info(self.snapshot, self.data)
        target = self.data / info["chunks"][1]["file"]
        target.write_bytes(target.read_bytes().replace(b'"runs"', b'"runz"'))
        with self.assertRaisesRegex(bbi.BatchBlockInfoError, "immutable"):
            bbi.export_batch_block_info(self.snapshot, self.data)

    def test_uie1_input_is_rejected(self):
        uie1 = self.root / "uie1.bin"
        uie1.write_bytes(cs.encode_snapshot(cs.Area(0, 0, 16, 16), {0: "minecraft:air"}, bytes(16 * 16 * cs.COLUMN_BYTES)))
        with self.assertRaises(cs.SnapshotError):
            bbi.export_batch_block_info(uie1, self.data)
        self.assertEqual(self.block_files(), [])

    def test_output_may_not_contain_the_snapshot(self):
        inside = self.data / "blocks" / SHA / "snapshot.bin"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(SNAPSHOT)
        with self.assertRaisesRegex(bbi.BatchBlockInfoError, "contain the snapshot"):
            bbi.export_batch_block_info(inside, self.data)

    def test_snapshot_change_during_export_is_detected(self):
        real = bbi._write_immutable

        def tamper(path, payload):
            real(path, payload)
            with self.snapshot.open("ab") as fh:
                fh.write(b"\x00")

        with mock.patch.object(bbi, "_write_immutable", tamper), self.assertRaisesRegex(bbi.BatchBlockInfoError, "changed"):
            bbi.export_batch_block_info(self.snapshot, self.data)


class AttachTests(TestCase):
    def test_attach_adds_block_info_and_keeps_other_fields(self):
        self.write_manifest()
        info = bbi.attach_batch_block_info(self.snapshot, self.manifest)
        manifest = json.loads(self.manifest.read_text("utf-8"))
        self.assertEqual(manifest["blockInfo"], info)
        self.assertEqual((manifest["title"], manifest["atlas"], manifest["snapshotSha256"]), ("UIE", "atlas.png", SHA))
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["blocks", "manifest.json"], "no temp files")
        self.assertEqual(bbi.attach_batch_block_info(self.snapshot, self.manifest), info, "idempotent")

    def test_rejections_leave_manifest_untouched_and_write_nothing(self):
        cases = {
            "sha mismatch": {"snapshotSha256": hashlib.sha256(b"other").hexdigest()},
            "uie1 manifest": {"snapshotFormat": "UIE1"},
            "format": {"format": 2},
            "missing core chunk included": {"chunks": [{"file": f"chunk_{cx}_{cz}.json", "x": cx, "z": cz}
                                                       for cx, cz in CORE_PRESENT + [(0, 0)]]},
            "halo chunk included": {"chunks": [{"file": f"chunk_{cx}_{cz}.json", "x": cx, "z": cz}
                                               for cx, cz in CORE_PRESENT + [(-2, -1)]]},
            "core chunk missing": {"chunks": [{"file": f"chunk_{cx}_{cz}.json", "x": cx, "z": cz}
                                              for cx, cz in CORE_PRESENT[:2]]},
            "duplicate coords": {"chunks": [{"file": f"chunk_{cx}_{cz}.json", "x": cx, "z": cz} for cx, cz in CORE_PRESENT]
                                 + [{"file": "copy.json", "x": -1, "z": -1}]},
            "duplicate file": {"chunks": [{"file": "same.json", "x": cx, "z": cz} for cx, cz in CORE_PRESENT]},
            "bool coords": {"chunks": [{"file": "a.json", "x": True, "z": -1}]},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                before = self.write_manifest(**overrides)
                with self.assertRaises(bbi.BatchBlockInfoError):
                    bbi.attach_batch_block_info(self.snapshot, self.manifest)
                self.assertEqual(self.manifest.read_bytes(), before)
                self.assertEqual(self.block_files(), [], "checks happen before any block file is written")

    def test_existing_different_block_info_is_not_overwritten(self):
        before = self.write_manifest(blockInfo={"format": "UIE_BLOCKS1", "chunks": []})
        with self.assertRaisesRegex(bbi.BatchBlockInfoError, "different blockInfo"):
            bbi.attach_batch_block_info(self.snapshot, self.manifest)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(self.block_files(), [], "rejected before any block file is written")

    def test_tamper_or_input_change_leaves_manifest_untouched(self):
        before = self.write_manifest()
        bbi.export_batch_block_info(self.snapshot, self.data)
        tampered = self.data / "blocks" / SHA / "chunk_0_-1.json"
        tampered.write_bytes(b"{}\n")
        with self.assertRaisesRegex(bbi.BatchBlockInfoError, "immutable"):
            bbi.attach_batch_block_info(self.snapshot, self.manifest)
        self.assertEqual(self.manifest.read_bytes(), before)
        tampered.unlink()
        real = bbi._write_immutable

        def change_manifest(path, payload):
            real(path, payload)
            with self.manifest.open("ab") as fh:
                fh.write(b" ")

        with mock.patch.object(bbi, "_write_immutable", change_manifest):
            with self.assertRaisesRegex(bbi.BatchBlockInfoError, "changed"):
                bbi.attach_batch_block_info(self.snapshot, self.manifest)
        self.assertNotIn(b"blockInfo", self.manifest.read_bytes())


if __name__ == "__main__":
    unittest.main()
