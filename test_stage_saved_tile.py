"""Tests for stage_saved_tile. Stdlib only; region files are opaque bytes (parsing belongs downstream)."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import stage_saved_tile as sst
from stage_saved_tile import StageError, plan_regions, stage_saved_tile

sha = lambda raw: hashlib.sha256(raw).hexdigest()


class StageSavedTileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()  # real path: linked ancestors are refused
        self.world = self.root / "world"
        self.region = self.world / "region"
        self.region.mkdir(parents=True)
        (self.world / "level.dat").write_bytes(b"\x1f\x8b level.dat bytes")
        # Tile (-1, 0) touches regions (-1,-1), (0,-1), (-1,0), (0,0); r.0.0.mca is absent.
        self.region_bytes = {"r.-1.-1.mca": b"corrupt but regular region bytes",
                             "r.0.-1.mca": os.urandom(5000), "r.-1.0.mca": b"\x00" * 8192}
        for name, raw in self.region_bytes.items():
            (self.region / name).write_bytes(raw)
        (self.region / "r.5.5.mca").write_bytes(b"outside the window, never copied")
        self.output = self.root / "staged" / "tile_-1_0"

    def stage(self, output=None, tile=(-1, 0), **kwargs):
        kwargs.setdefault("min_free_bytes", 0)
        return stage_saved_tile(self.world / "level.dat", self.region, output or self.output,
                                tile_x=tile[0], tile_z=tile[1], **kwargs)

    def world_bytes(self):
        return {p.relative_to(self.world).as_posix(): p.read_bytes() for p in self.world.rglob("*") if p.is_file()}

    def assert_not_staged(self, output=None):
        output = output or self.output
        self.assertFalse(os.path.lexists(output), "no output after a failure")
        self.assertEqual(list(output.parent.glob(f".{output.name}.stage-*")) if output.parent.exists() else [], [])

    def test_region_planning_with_negative_coordinates(self):
        area, core, regions = plan_regions(-1, 0)
        self.assertEqual((area.x, area.z, area.width, area.depth), (-112, -16, 128, 128))
        self.assertEqual((core.x, core.z, core.width, core.depth), (-96, 0, 96, 96))
        self.assertEqual(regions, [(-1, -1), (0, -1), (-1, 0), (0, 0)])
        self.assertEqual(plan_regions(1, 1)[2], [(0, 0)])
        self.assertEqual(plan_regions(1, 0)[2], [(0, -1), (0, 0)])
        self.assertEqual(plan_regions(-2, 1)[2], [(-1, 0)])
        for tile in [(x, z) for x in range(-12, 12) for z in range(-12, 12)]:
            self.assertLessEqual(len(plan_regions(*tile)[2]), 4)
        with self.assertRaises(StageError):
            plan_regions(True, 0)

    def test_stage_copies_exact_bytes_and_records_observed_stable_provenance(self):
        before = self.world_bytes()
        provenance = self.stage()
        self.assertEqual(self.world_bytes(), before, "sources are only read")
        self.assertEqual((self.output / "level.dat").read_bytes(), before["level.dat"])
        for name, raw in self.region_bytes.items():
            self.assertEqual((self.output / "region" / name).read_bytes(), raw, name)
        self.assertFalse((self.output / "region" / "r.0.0.mca").exists())
        self.assertFalse((self.output / "region" / "r.5.5.mca").exists(), "only the tile's regions")
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ["COMPLETE", "level.dat", "provenance.json", "region"])

        stored = json.loads((self.output / "provenance.json").read_bytes())
        self.assertEqual(stored, provenance)
        self.assertEqual(provenance["format"], "UIE_SAVED_TILE1")
        self.assertEqual(provenance["consistency"], "observed-stable-files")
        self.assertIs(provenance["atomicWorldSnapshot"], False)
        self.assertIn("not an atomic world snapshot", provenance["consistencyNote"])
        self.assertEqual(provenance["tile"], {"x": -1, "z": 0})
        self.assertEqual(provenance["input"], {"x": -112, "z": -16, "width": 128, "depth": 128})
        self.assertEqual(provenance["core"], {"x": -96, "z": 0, "width": 96, "depth": 96})
        interval = provenance["sourceInterval"]
        self.assertTrue(interval["startUtc"].endswith("Z") and interval["endUtc"].endswith("Z"))
        self.assertLessEqual(interval["startUtc"], interval["endUtc"])
        self.assertEqual(provenance["sources"]["levelDat"]["path"], str(self.world / "level.dat"))
        self.assertEqual(provenance["sources"]["levelDat"]["sha256"], sha(before["level.dat"]))
        self.assertEqual(provenance["sources"]["regionDir"], str(self.region))
        states = {(r["x"], r["z"]): r for r in provenance["regions"]}
        self.assertEqual(list(states), [(-1, -1), (0, -1), (-1, 0), (0, 0)])
        self.assertEqual(states[(0, 0)], {"x": 0, "z": 0, "state": "absent"})
        for (x, z), entry in states.items():
            if entry["state"] == "present":
                raw = self.region_bytes[f"r.{x}.{z}.mca"]
                self.assertEqual((entry["sha256"], entry["size"]), (sha(raw), len(raw)))
        self.assertEqual(provenance["files"], {"level.dat": sha(before["level.dat"]),
                                               **{f"region/{n}": sha(r) for n, r in self.region_bytes.items()}})
        complete = json.loads((self.output / "COMPLETE").read_bytes())
        self.assertEqual(complete, {"format": "UIE_SAVED_TILE1",
                                    "provenanceSha256": sha((self.output / "provenance.json").read_bytes())})
        self.assertEqual(list(self.output.parent.glob(".tile_-1_0.stage-*")), [])

    def test_tile_without_any_region_file_is_staged_with_absent_regions(self):
        provenance = self.stage(self.root / "staged" / "far", tile=(40, 40))
        self.assertEqual([r["state"] for r in provenance["regions"]], ["absent"])
        self.assertTrue((self.root / "staged" / "far" / "region").is_dir())
        self.assertEqual(list((self.root / "staged" / "far" / "region").iterdir()), [])
        self.assertEqual(set(provenance["files"]), {"level.dat"})

    def test_existing_output_is_never_reused(self):
        self.output.mkdir(parents=True)
        (self.output / "foreign.txt").write_text("keep")
        with self.assertRaisesRegex(StageError, "already exists"):
            self.stage()
        self.assertEqual((self.output / "foreign.txt").read_text(), "keep")
        self.assertEqual(list(self.output.iterdir()), [self.output / "foreign.txt"])

    def test_changed_source_is_detected_and_nothing_is_published(self):
        copy = sst._copy_file
        calls = 0

        def change_level_after_region(source, target, limit, label, reserve):
            nonlocal calls
            result = copy(source, target, limit, label, reserve)
            calls += 1
            if calls == 2:  # level.dat was already copied; now a "save" rewrites it
                (self.world / "level.dat").write_bytes(b"\x1f\x8b level.dat saved again")
            return result
        with mock.patch.object(sst, "_copy_file", change_level_after_region):
            with self.assertRaisesRegex(StageError, "level.dat changed"):
                self.stage()
        self.assert_not_staged()

    def test_changed_region_is_detected(self):
        copy = sst._copy_file

        def rewrite_first_region(source, target, limit, label, reserve):
            result = copy(source, target, limit, label, reserve)
            if label == "region/r.-1.-1.mca":
                (self.region / "r.-1.-1.mca").write_bytes(b"corrupt but regular region bytes, saved")
            return result
        with mock.patch.object(sst, "_copy_file", rewrite_first_region):
            with self.assertRaisesRegex(StageError, "r.-1.-1.mca changed"):
                self.stage()
        self.assert_not_staged()

    def test_absent_region_appearing_or_present_region_vanishing_is_detected(self):
        hash_copy = sst._hash_copy

        def create_absent_after_copies(path):
            # All regions were planned and r.0.0.mca recorded absent; a save creates it before the re-check.
            (self.region / "r.0.0.mca").write_bytes(b"new region")
            return hash_copy(path)
        with mock.patch.object(sst, "_hash_copy", create_absent_after_copies):
            with self.assertRaisesRegex(StageError, "appeared"):
                self.stage()
        self.assert_not_staged()
        (self.region / "r.0.0.mca").unlink()

        copy = sst._copy_file

        def delete_copied(source, target, limit, label, reserve):
            result = copy(source, target, limit, label, reserve)
            if label == "region/r.-1.0.mca":
                (self.region / "r.-1.0.mca").unlink()
            return result
        with mock.patch.object(sst, "_copy_file", delete_copied):
            with self.assertRaisesRegex(StageError, "missing"):
                self.stage()
        self.assert_not_staged()

    def test_write_budget_disk_reserve_and_size_limits(self):
        total = len(b"\x1f\x8b level.dat bytes") + sum(len(r) for r in self.region_bytes.values())
        with self.assertRaisesRegex(StageError, "budget"):
            self.stage(max_write_bytes=total - 1)
        self.assert_not_staged()
        with mock.patch.object(sst.shutil, "disk_usage", return_value=SimpleNamespace(free=10)):
            with self.assertRaisesRegex(StageError, "reserve"):
                self.stage(min_free_bytes=100)
        self.assert_not_staged()
        with mock.patch.object(sst, "MAX_REGION_BYTES", 100):
            with self.assertRaisesRegex(StageError, "larger than"):
                self.stage()
        self.assert_not_staged()
        with mock.patch.object(sst, "MAX_LEVEL_BYTES", 4):
            with self.assertRaisesRegex(StageError, "larger than"):
                self.stage()
        self.assert_not_staged()
        for kwargs in ({"min_free_bytes": -1}, {"max_write_bytes": True}, {"max_write_bytes": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(StageError):
                self.stage(**kwargs)

    def test_overlapping_output_is_refused(self):
        for output in (self.world / "staged", self.region / "staged", self.root):
            with self.subTest(output=output), self.assertRaisesRegex(StageError, "overlaps"):
                self.stage(output)
        self.assertFalse((self.world / "staged").exists())
        self.assertFalse((self.region / "staged").exists())

    def test_links_are_refused(self):
        outside = self.root / "outside.mca"
        outside.write_bytes(b"outside")
        try:
            (self.region / "r.-1.0.mca").unlink()
            os.symlink(outside, self.region / "r.-1.0.mca")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        with self.assertRaisesRegex(StageError, "links are refused"):
            self.stage()
        self.assert_not_staged()
        (self.region / "r.-1.0.mca").unlink()
        (self.region / "r.-1.0.mca").write_bytes(self.region_bytes["r.-1.0.mca"])
        alias = self.root / "alias"
        os.symlink(self.root, alias, target_is_directory=True)
        with self.assertRaisesRegex(StageError, "symlink or reparse point"):
            stage_saved_tile(alias / "world" / "level.dat", self.region, self.output, tile_x=-1, tile_z=0,
                             min_free_bytes=0)
        with self.assertRaisesRegex(StageError, "symlink or reparse point"):
            stage_saved_tile(self.world / "level.dat", alias / "world" / "region", self.output, tile_x=-1, tile_z=0,
                             min_free_bytes=0)
        with self.assertRaisesRegex(StageError, "symlink or reparse point"):
            self.stage(alias / "staged" / "tile")
        self.assertFalse((self.root / "staged" / "tile").exists(), "nothing created through a link")

    def test_failure_removes_only_its_own_files(self):
        parent = self.output.parent
        parent.mkdir(parents=True)
        foreign_stage = parent / ".tile_-1_0.stage-foreign"
        foreign_stage.mkdir()
        (parent / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(StageError, "budget"):
            self.stage(max_write_bytes=10)
        self.assertTrue(foreign_stage.is_dir(), "a foreign stage directory is never removed")
        self.assertEqual((parent / "keep.txt").read_text(), "keep")
        self.assertFalse(os.path.lexists(self.output))
        self.assertEqual(sorted(p.name for p in parent.iterdir()), [".tile_-1_0.stage-foreign", "keep.txt"])

    def test_missing_level_dat_is_refused(self):
        (self.world / "level.dat").unlink()
        with self.assertRaisesRegex(StageError, "missing"):
            self.stage()
        self.assertFalse(os.path.lexists(self.output))

    def test_metadata_change_during_final_read_is_refused(self):
        source = self.world / 'level.dat'
        original_open = sst._open_source

        class ChangedAfterRead:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def read(self, count):
                raw = self.stream.read(count)
                if not raw:
                    info = source.stat()
                    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
                return raw

        def opened(path, limit, label):
            stream, info = original_open(path, limit, label)
            return ChangedAfterRead(stream), info

        with mock.patch.object(sst, '_open_source', opened):
            with self.assertRaisesRegex(StageError, 'changed during verification'):
                sst._hash_source(source, sst.MAX_LEVEL_BYTES, 'level.dat')

    def test_same_bytes_with_changed_metadata_between_reads_are_refused(self):
        copy = sst._copy_file

        def rewrite(source, target, limit, label, reserve):
            result = copy(source, target, limit, label, reserve)
            if label == 'level.dat':
                info = source.stat()
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
            return result

        with mock.patch.object(sst, '_copy_file', rewrite):
            with self.assertRaisesRegex(StageError, 'changed before verification'):
                self.stage()
        self.assert_not_staged()


if __name__ == "__main__":
    unittest.main()
