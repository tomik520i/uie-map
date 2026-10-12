"""Tests for staged_batch.create_staged_batch. Stdlib only.

Wrapper paths use a small fake create_batch on a real staged tile (stage_saved_tile with opaque region
bytes). One test builds a real UIE2 batch from a tiny world made with the capture_batch_file test helpers."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import capture_batch as cb
import capture_snapshot as cs
import reader
import staged_batch as sb
import stage_saved_tile as sst
from staged_batch import StagedBatchError, create_staged_batch
from test_capture_batch_file import ABSENT, WINDOW, chunk_nbt, default_ores, default_tiles
from test_capture_snapshot import level_dat_bytes, region_bytes
from test_capture_batch_file import REGISTRY

sha = lambda raw: hashlib.sha256(raw).hexdigest()
TILE = (-1, -1)  # input -112..15 on both axes: regions (-1,-1), (0,-1), (-1,0), (0,0)


class StagedBatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()  # real path: linked ancestors are refused
        self.world = self.root / "world"
        (self.world / "region").mkdir(parents=True)
        self.output = self.root / "batches" / "tile_-1_-1"
        self.calls = 0

    # ------------------------------------------------------------------ fixtures

    def opaque_world(self):
        (self.world / "level.dat").write_bytes(b"\x1f\x8b opaque level.dat")
        for name in ("r.-1.-1.mca", "r.0.-1.mca", "r.-1.0.mca"):  # r.0.0.mca stays absent
            (self.world / "region" / name).write_bytes(f"opaque {name}".encode() * 50)

    def real_world(self):
        (self.world / "level.dat").write_bytes(level_dat_bytes(REGISTRY))
        by_region = {}
        ores, tiles = default_ores(), default_tiles()
        for cx, cz in (c for c in WINDOW if c not in ABSENT):
            rx, rz, lx, lz = reader.chunk_to_region(cx, cz)
            by_region.setdefault((rx, rz), {})[(lx, lz)] = chunk_nbt(cx, cz, ores.get((cx, cz), ()), tiles.get((cx, cz), ()))
        for (rx, rz), records in by_region.items():
            (self.world / "region" / f"r.{rx}.{rz}.mca").write_bytes(region_bytes(records))

    def stage(self, real=False):
        (self.real_world if real else self.opaque_world)()
        self.staged = self.root / "staged" / "tile"
        sst.stage_saved_tile(self.world / "level.dat", self.world / "region", self.staged,
                             tile_x=TILE[0], tile_z=TILE[1], min_free_bytes=0)
        return self.staged

    def fake_create_batch(self, level_sha=None, during=None):
        def fake(level_dat, region_dir, area, core, output_dir):
            self.calls += 1
            output = Path(output_dir)
            os.mkdir(output)
            level = Path(level_dat).read_bytes()
            regions = []
            for rx, rz in sorted(cs.plan_chunks(area)):
                path = Path(region_dir) / f"r.{rx}.{rz}.mca"
                if path.exists():
                    raw = path.read_bytes()
                    regions.append({"region": [rx, rz], "state": "present", "sha256": sha(raw), "size": len(raw)})
                else:
                    regions.append({"region": [rx, rz], "state": "absent"})
            files = {"snapshot.bin": b"fake UIE2 snapshot", "snapshot.tiles.json": b'{"tiles":[]}\n',
                     "provenance.json": json.dumps({"format": "UIE2_BATCH_PROVENANCE1", "regions": regions,
                                                    "levelDat": {"sha256": level_sha or sha(level), "size": len(level)}}
                                                   ).encode()}
            for name, raw in files.items():
                (output / name).write_bytes(raw)
            (output / "COMPLETE").write_bytes(json.dumps({
                "format": "UIE2_BATCH_COMPLETE1", "snapshotSha256": sha(files["snapshot.bin"]),
                "files": {name: sha(raw) for name, raw in files.items()}}).encode())
            if during:
                during()
            return {}
        return mock.patch.object(sb.cbf, "create_batch", fake)

    def convert(self, staged=None, output=None, tile=TILE, **kwargs):
        kwargs.setdefault("accept_consistency", "observed-stable-files")
        return create_staged_batch(staged or self.staged, output or self.output, tile_x=tile[0], tile_z=tile[1], **kwargs)

    def resign(self, change):
        provenance = json.loads((self.staged / "provenance.json").read_bytes())
        change(provenance)
        raw = json.dumps(provenance).encode()
        (self.staged / "provenance.json").write_bytes(raw)
        (self.staged / "COMPLETE").write_bytes(json.dumps({"format": "UIE_SAVED_TILE1", "provenanceSha256": sha(raw)}).encode())

    def staged_bytes(self):
        return {p.relative_to(self.staged).as_posix(): p.read_bytes() for p in self.staged.rglob("*") if p.is_file()}

    def assert_not_published(self):
        self.assertFalse(os.path.lexists(self.output))
        parent = self.output.parent
        self.assertEqual(list(parent.glob(f".{self.output.name}.staged-batch-*")) if parent.exists() else [], [])

    # ------------------------------------------------------------------ tests

    def test_policy_must_be_explicit_and_exact(self):
        self.stage()
        with self.fake_create_batch():
            with self.assertRaises(TypeError):
                create_staged_batch(self.staged, self.output, tile_x=-1, tile_z=-1)
            for bad in ("", "Observed-stable-files", "observed-stable-files ", "offline-consistent", None, True):
                with self.subTest(bad=bad), self.assertRaisesRegex(StagedBatchError, "accept_consistency"):
                    self.convert(accept_consistency=bad)
        self.assertEqual(self.calls, 0)
        self.assert_not_published()

    def test_success_binds_policy_sidecar_into_the_marker_last(self):
        self.stage()
        staged_before = self.staged_bytes()
        with self.fake_create_batch():
            record = self.convert()
        self.assertEqual(self.staged_bytes(), staged_before, "the staged tile is only read")
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ["COMPLETE", "provenance.json", "snapshot.bin", "snapshot.tiles.json", "staged-source.json"])
        sidecar_raw = (self.output / "staged-source.json").read_bytes()
        self.assertEqual(json.loads(sidecar_raw), record)
        marker = json.loads((self.output / "COMPLETE").read_bytes())
        self.assertEqual(marker["stagedSource"], {"file": "staged-source.json", "sha256": sha(sidecar_raw)})
        original = {k: v for k, v in marker.items() if k != "stagedSource"}
        self.assertEqual(original["format"], "UIE2_BATCH_COMPLETE1", "original UIE2 marker fields are kept")
        for name, digest in original["files"].items():
            self.assertEqual(sha((self.output / name).read_bytes()), digest, name)
        self.assertEqual(record["format"], "UIE_STAGED_SOURCE1")
        self.assertEqual(record["policy"]["acceptedConsistency"], "observed-stable-files")
        self.assertIs(record["policy"]["atomicWorldSnapshot"], False)
        self.assertIs(record["stagedTile"]["atomicWorldSnapshot"], False)
        self.assertIn("not a consistent world snapshot", record["warning"])
        staged_provenance = (self.staged / "provenance.json").read_bytes()
        self.assertEqual(record["stagedTile"]["provenanceSha256"], sha(staged_provenance))
        self.assertEqual(record["stagedTile"]["completeSha256"], sha((self.staged / "COMPLETE").read_bytes()))
        self.assertEqual(record["stagedTile"]["sourceInterval"], json.loads(staged_provenance)["sourceInterval"])
        self.assertEqual(record["tile"], {"x": -1, "z": -1})
        self.assertEqual([r["state"] for r in record["stagedTile"]["regions"]], ["present", "present", "present", "absent"])
        self.assertEqual(record["batch"]["snapshotSha256"], original["snapshotSha256"])
        self.assert_not_published_work_only()

    def assert_not_published_work_only(self):
        self.assertEqual(list(self.output.parent.glob(".tile_-1_-1.staged-batch-*")), [])

    def test_tampered_staged_tile_is_refused_before_any_build(self):
        cases = {
            "marker hash": lambda s: (s / "COMPLETE").write_bytes(
                json.dumps({"format": "UIE_SAVED_TILE1", "provenanceSha256": "0" * 64}).encode()),
            "provenance edited": lambda s: (s / "provenance.json").write_bytes((s / "provenance.json").read_bytes() + b" "),
            "level.dat bytes": lambda s: (s / "level.dat").write_bytes(b"\x1f\x8b opaque level.dat!"),
            "region bytes": lambda s: (s / "region" / "r.-1.-1.mca").write_bytes(b"changed"),
            "missing region": lambda s: (s / "region" / "r.0.-1.mca").unlink(),
            "extra region": lambda s: (s / "region" / "r.5.5.mca").write_bytes(b"extra"),
            "absent region present": lambda s: (s / "region" / "r.0.0.mca").write_bytes(b"appeared"),
            "extra top-level file": lambda s: (s / "notes.txt").write_text("extra"),
            "marker missing": lambda s: (s / "COMPLETE").unlink(),
            "consistency upgraded": lambda s: self.resign(lambda p: p.update(consistency="offline-consistent")),
            "atomic claimed": lambda s: self.resign(lambda p: p.update(atomicWorldSnapshot=True)),
            "files map": lambda s: self.resign(lambda p: p["files"].update({"level.dat": "1" * 64})),
            "unknown field": lambda s: self.resign(lambda p: p.update(extra=1)),
            "region state": lambda s: self.resign(lambda p: p["regions"][3].update(state="present")),
            "bool region coordinate": lambda s: self.resign(lambda p: p["regions"][3].update(x=False)),
            "float core coordinate": lambda s: self.resign(lambda p: p['core'].update(x=-96.0)),
        }
        for label, tamper in cases.items():
            with self.subTest(label):
                self.setUp()
                self.stage()
                tamper(self.staged)
                with self.fake_create_batch():
                    with self.assertRaises(StagedBatchError):
                        self.convert()
                self.assertEqual(self.calls, 0, "nothing is built from an unverified staged tile")
                self.assert_not_published()

    def test_wrong_tile_is_refused(self):
        self.stage()
        with self.fake_create_batch():
            for tile in ((0, -1), (-1, 0), (True, -1)):
                with self.subTest(tile=tile), self.assertRaises(StagedBatchError):
                    self.convert(tile=tile)
        self.assertEqual(self.calls, 0)
        self.assert_not_published()

    def test_staged_tile_changing_during_the_build_is_detected(self):
        self.stage()

        def change():
            (self.staged / "region" / "r.-1.0.mca").write_bytes(b"rewritten during the build")
        with self.fake_create_batch(during=change):
            with self.assertRaises(StagedBatchError):
                self.convert()
        self.assertEqual(self.calls, 1)
        self.assert_not_published()

    def test_batch_not_built_from_the_staged_bytes_is_refused(self):
        self.stage()
        with self.fake_create_batch(level_sha="2" * 64):
            with self.assertRaisesRegex(StagedBatchError, "not bound to the staged level.dat"):
                self.convert()
        self.assert_not_published()

    def test_existing_output_and_foreign_files_are_never_touched(self):
        self.stage()
        self.output.mkdir(parents=True)
        (self.output / "foreign.txt").write_text("keep")
        with self.fake_create_batch():
            with self.assertRaisesRegex(StagedBatchError, "already exists"):
                self.convert()
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["foreign.txt"])
        other = self.root / "batches" / "other"
        foreign_work = other.parent / ".other.staged-batch-foreign"
        foreign_work.mkdir()
        (other.parent / "keep.txt").write_text("keep")
        with self.fake_create_batch(level_sha="2" * 64):
            with self.assertRaises(StagedBatchError):
                self.convert(output=other)
        self.assertTrue(foreign_work.is_dir())
        self.assertEqual((other.parent / "keep.txt").read_text(), "keep")
        self.assertFalse(os.path.lexists(other))
        self.assertEqual(list(other.parent.glob(".other.staged-batch-*")), [foreign_work])

    def test_overlapping_output_and_links_are_refused(self):
        self.stage()
        with self.fake_create_batch():
            for output in (self.staged / "batch", self.staged.parent, self.world / "batch", self.world / "region" / "b"):
                with self.subTest(output=output), self.assertRaises(StagedBatchError):
                    self.convert(output=output)
        self.assertEqual(self.calls, 0)
        self.assertFalse((self.world / "batch").exists())
        alias = self.root / "alias"
        try:
            os.symlink(self.root, alias, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        with self.fake_create_batch():
            with self.assertRaisesRegex(StagedBatchError, "symlink or reparse point"):
                self.convert(staged=alias / "staged" / "tile")
            with self.assertRaisesRegex(StagedBatchError, "symlink or reparse point"):
                self.convert(output=alias / "batches" / "x")
        self.assertEqual(self.calls, 0)

    def test_real_tiny_staged_tile_becomes_a_verifiable_uie2_batch(self):
        self.stage(real=True)
        record = create_staged_batch(self.staged, self.output, tile_x=TILE[0], tile_z=TILE[1],
                                     accept_consistency="observed-stable-files")
        marker = json.loads((self.output / "COMPLETE").read_bytes())
        self.assertEqual(marker["format"], "UIE2_BATCH_COMPLETE1")
        for name, digest in marker["files"].items():
            self.assertEqual(sha((self.output / name).read_bytes()), digest, name)
        self.assertEqual(marker["stagedSource"]["sha256"], sha((self.output / "staged-source.json").read_bytes()))
        snapshot = (self.output / "snapshot.bin").read_bytes()
        self.assertEqual(sha(snapshot), marker["snapshotSha256"])
        batch = cb.parse_batch(snapshot)
        self.assertEqual((batch.core.x, batch.core.z), (-96, -96))
        provenance = json.loads((self.output / "provenance.json").read_bytes())
        self.assertEqual(provenance["levelDat"]["sha256"], sha((self.staged / "level.dat").read_bytes()))
        self.assertEqual(record["stagedTile"]["regions"][-1], {"x": 0, "z": 0, "state": "absent"})
        self.assert_not_published_work_only()


if __name__ == "__main__":
    unittest.main()
