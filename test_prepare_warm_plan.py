"""Tests for prepare_warm_plan. Stdlib only; fixtures are real UIE2 batches built with capture_batch_file
from a tiny synthetic world (the capture_batch_file test helpers), then copied per test."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import capture_batch_file as cbf
import prepare_warm_plan as pwp
import reader
from prepare_warm_plan import WarmPlanError, prepare_warm_plan
from test_capture_batch_file import REGISTRY, chunk_nbt
from test_capture_snapshot import level_dat_bytes, region_bytes

sha = lambda raw: hashlib.sha256(raw).hexdigest()
KEY = "a" * 64
DATA = ("snapshot.bin", "snapshot.tiles.json", "provenance.json")


def build_world(root: Path, missing=()):
    """Chunks x -7..6, z -7..0 (both tile (-1,-1) and tile (0,-1) windows), no GT ores."""
    region = root / "region"
    region.mkdir(parents=True)
    (root / "level.dat").write_bytes(level_dat_bytes(REGISTRY))
    by_region = {}
    for cz in range(-7, 1):
        for cx in range(-7, 7):
            if (cx, cz) in missing:
                continue
            rx, rz, lx, lz = reader.chunk_to_region(cx, cz)
            by_region.setdefault((rx, rz), {})[(lx, lz)] = chunk_nbt(cx, cz)
    for (rx, rz), records in by_region.items():
        (region / f"r.{rx}.{rz}.mca").write_bytes(region_bytes(records))
    return root / "level.dat", region


class WarmPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.fixtures = Path(cls._temp.name).resolve()
        level, region = build_world(cls.fixtures / "world")
        (cls.fixtures / "batches").mkdir()
        for name, tile in (("A", (-1, -1)), ("B", (0, -1))):
            cbf.create_batch(level, region, *cbf.tile_areas(*tile), cls.fixtures / "batches" / name)
        # Same tile as A from a world with one core chunk missing: different content, overlapping core.
        level2, region2 = build_world(cls.fixtures / "world2", missing={(-5, -5)})
        cbf.create_batch(level2, region2, *cbf.tile_areas(-1, -1), cls.fixtures / "batches" / "C")

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()  # real path: linked ancestors are refused
        self.output = self.root / "plans" / "plan1"

    def batch(self, name, as_name=None):
        target = self.root / "src" / (as_name or name)
        shutil.copytree(self.fixtures / "batches" / name, target)
        return target

    def plan(self, batches, output=None, **kwargs):
        kwargs.setdefault("min_free_bytes", 0)
        kwargs.setdefault("pipeline_key", KEY)
        return prepare_warm_plan([str(b) for b in batches], output or self.output, world="uie", dimension="0", **kwargs)

    def resign(self, directory: Path):
        complete = json.loads((directory / "COMPLETE").read_bytes())
        complete["files"] = {name: sha((directory / name).read_bytes()) for name in DATA}
        complete["snapshotSha256"] = complete["files"]["snapshot.bin"]
        (directory / "COMPLETE").write_bytes(json.dumps(complete).encode())

    def make_staged(self, directory: Path, change=None):
        sidecar = {"format": "UIE_STAGED_SOURCE1",
                   "policy": {"acceptedConsistency": "observed-stable-files", "atomicWorldSnapshot": False,
                              "purpose": "best-effort map export explicitly accepted by the caller"},
                   "warning": "best-effort map export; not a consistent world snapshot",
                   "stagedTile": {"consistency": "observed-stable-files", "atomicWorldSnapshot": False,
                                  "sourceInterval": {"startUtc": "2026-09-30T10:00:00Z", "endUtc": "2026-09-30T10:00:05Z"}},
                   "batch": {"snapshotSha256": sha((directory / "snapshot.bin").read_bytes())}}
        if change:
            change(sidecar)
        raw = json.dumps(sidecar).encode()
        (directory / "staged-source.json").write_bytes(raw)
        complete = json.loads((directory / "COMPLETE").read_bytes())
        complete["stagedSource"] = {"file": "staged-source.json", "sha256": sha(raw)}
        (directory / "COMPLETE").write_bytes(json.dumps(complete).encode())

    def tree(self, root: Path):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def assert_not_published(self, output=None):
        output = output or self.output
        self.assertFalse(os.path.lexists(output))
        if output.parent.exists():
            self.assertEqual(list(output.parent.glob(f".{output.name}.warm-*")), [])

    # ------------------------------------------------------------------ success

    def test_two_nonoverlapping_batches_become_a_warm_plan(self):
        a, b = self.batch("A"), self.batch("B")
        before = self.tree(self.root / "src")
        record = self.plan([b, a])
        self.assertEqual(self.tree(self.root / "src"), before, "sources are only read")
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["COMPLETE", "inputs", "jobs.json", "plan.json"])
        self.assertFalse((self.output / "results").exists(), "WarmBatch reserves results itself")
        plan = json.loads((self.output / "plan.json").read_bytes())
        self.assertEqual(plan["format"], "UIE_WARM_BATCH1")
        self.assertEqual(len(plan["jobs"]), 2)
        for job in plan["jobs"]:
            self.assertRegex(job["id"], r"^[a-zA-Z0-9_-]{1,80}$")
            self.assertEqual(job["input"], f"inputs/{job['id']}/snapshot.bin")
            self.assertEqual(sha((self.output / job["input"]).read_bytes()), job["snapshotSha256"])
            self.assertTrue((self.output / "inputs" / job["id"] / "snapshot.tiles.json").is_file())
        jobs = json.loads((self.output / "jobs.json").read_bytes())
        self.assertEqual(jobs, record)
        self.assertEqual((jobs["format"], jobs["pipelineKey"], jobs["pipelineKeySource"]),
                         ("UIE_WARM_JOBS1", KEY, "caller-assertion"))
        self.assertEqual([j["core"]["x"] for j in jobs["jobs"]], [-96, 0], "deterministic order by core")
        for job, source in zip(jobs["jobs"], (a, b)):
            self.assertEqual(job["source"]["path"], str(source))
            self.assertEqual(job["source"]["completeSha256"], sha((source / "COMPLETE").read_bytes()))
            self.assertEqual(len(job["coreChunks"]), 36)
            self.assertEqual(job["presentCoreChunks"], job["coreChunks"])
            self.assertEqual(job["absentCoreChunks"], [])
            self.assertIs(job["sourcePolicy"]["stagedSource"], False)
            self.assertIn("offline", job["sourcePolicy"]["note"])
            for name in DATA:
                self.assertEqual((self.output / "inputs" / job["id"] / name).read_bytes(), (source / name).read_bytes())
        complete = json.loads((self.output / "COMPLETE").read_bytes())
        self.assertEqual(complete["format"], "UIE_WARM_PLAN_COMPLETE1")
        published = {p.relative_to(self.output).as_posix() for p in self.output.rglob("*") if p.is_file()} - {"COMPLETE"}
        self.assertEqual(set(complete["files"]), published)
        for relative, digest in complete["files"].items():
            self.assertEqual(sha((self.output / relative).read_bytes()), digest, relative)
        self.assert_not_published_stage()

    def assert_not_published_stage(self):
        self.assertEqual(list(self.output.parent.glob(f".{self.output.name}.warm-*")), [])

    def test_job_ids_are_deterministic_and_bound_to_the_plan_identity(self):
        a, b = self.batch("A"), self.batch("B")
        self.plan([a, b])
        self.plan([b, a], output=self.root / "plans" / "plan2")
        first = (self.output / "plan.json").read_bytes()
        self.assertEqual((self.root / "plans" / "plan2" / "plan.json").read_bytes(), first)
        self.plan([a], output=self.root / "plans" / "plan3", pipeline_key="b" * 64)
        other = json.loads((self.root / "plans" / "plan3" / "plan.json").read_bytes())["jobs"][0]["id"]
        self.assertNotIn(other, {j["id"] for j in json.loads(first)["jobs"]})

    # ------------------------------------------------------------------ refusals

    def test_duplicate_and_overlapping_batches_are_refused(self):
        a = self.batch("A")
        with self.assertRaisesRegex(WarmPlanError, "listed twice"):
            self.plan([a, a])
        with self.assertRaisesRegex(WarmPlanError, "same batch content"):
            self.plan([a, self.batch("A", "A-copy")])
        with self.assertRaisesRegex(WarmPlanError, "overlapping core chunks"):
            self.plan([a, self.batch("C")])
        self.assert_not_published()

    def test_marker_file_and_binding_tampering_is_refused(self):
        def flip_snapshot(d):
            raw = bytearray((d / "snapshot.bin").read_bytes())
            raw[-1] ^= 1
            (d / "snapshot.bin").write_bytes(bytes(raw))

        def wrong_marker_hash(d):
            complete = json.loads((d / "COMPLETE").read_bytes())
            complete["files"]["snapshot.tiles.json"] = "0" * 64
            (d / "COMPLETE").write_bytes(json.dumps(complete).encode())

        def unbound_tiles(d):
            tiles = json.loads((d / "snapshot.tiles.json").read_bytes())
            tiles["snapshotSha256"] = "1" * 64
            (d / "snapshot.tiles.json").write_bytes(json.dumps(tiles).encode())
            self.resign(d)

        def unbound_mask(d):
            provenance = json.loads((d / "provenance.json").read_bytes())
            provenance["absentChunks"].append(provenance["presentChunks"].pop())
            (d / "provenance.json").write_bytes(json.dumps(provenance).encode())
            self.resign(d)

        def not_uie2(d):
            (d / "snapshot.bin").write_bytes(b"UIE2 but truncated")
            self.resign(d)

        cases = {"snapshot bytes": flip_snapshot, "marker hash": wrong_marker_hash, "tiles binding": unbound_tiles,
                 "presence mask": unbound_mask, "invalid snapshot": not_uie2,
                 "extra file": lambda d: (d / "notes.txt").write_text("x"),
                 "missing provenance": lambda d: (d / "provenance.json").unlink(),
                 "marker format": lambda d: (d / "COMPLETE").write_bytes(b'{"format":"OTHER"}')}
        for label, tamper in cases.items():
            with self.subTest(label):
                a = self.batch("A", f"A-{label.replace(' ', '-')}")
                tamper(a)
                with self.assertRaises(WarmPlanError):
                    self.plan([a, self.batch("B", f"B-{label.replace(' ', '-')}")])
                self.assert_not_published()

    def test_staged_source_policy_is_carried_and_never_upgraded(self):
        b = self.batch("B")
        plain = self.plan([b])["jobs"][0]
        staged = self.batch("B", "B-staged")
        self.make_staged(staged)
        job = self.plan([staged], output=self.root / "plans" / "staged")["jobs"][0]
        policy = job["sourcePolicy"]
        self.assertEqual((policy["stagedSource"], policy["acceptedConsistency"], policy["atomicWorldSnapshot"]),
                         (True, "observed-stable-files", False))
        self.assertIn("not a consistent world snapshot", policy["warning"])
        self.assertEqual(policy["sourceInterval"]["startUtc"], "2026-09-30T10:00:00Z")
        self.assertEqual(job["source"]["stagedSourceSha256"], sha((staged / "staged-source.json").read_bytes()))
        self.assertTrue((self.root / "plans" / "staged" / "inputs" / job["id"] / "staged-source.json").is_file())
        self.assertNotEqual(job["id"], plain["id"], "the policy sidecar is part of the job identity")
        bad = {
            "upgraded consistency": lambda s: s["policy"].update(acceptedConsistency="offline-consistent"),
            "atomic claimed": lambda s: s["policy"].update(atomicWorldSnapshot=True),
            "tile atomic": lambda s: s["stagedTile"].update(atomicWorldSnapshot=True),
            "other snapshot": lambda s: s["batch"].update(snapshotSha256="2" * 64),
            "no warning": lambda s: s.pop("warning"),
        }
        for label, change in bad.items():
            with self.subTest(label):
                d = self.batch("B", f"B-{label.replace(' ', '-')}")
                self.make_staged(d, change)
                with self.assertRaisesRegex(WarmPlanError, "staged-source policy"):
                    self.plan([d], output=self.root / "plans" / "bad")
                self.assert_not_published(self.root / "plans" / "bad")
        hashed = self.batch("B", "B-hash")
        self.make_staged(hashed)
        (hashed / "staged-source.json").write_bytes((hashed / "staged-source.json").read_bytes() + b" ")
        with self.assertRaisesRegex(WarmPlanError, "COMPLETE.stagedSource"):
            self.plan([hashed], output=self.root / "plans" / "bad")
        unmarked = self.batch("B", "B-unmarked")
        (unmarked / "staged-source.json").write_text("{}")
        with self.assertRaisesRegex(WarmPlanError, "exactly"):
            self.plan([unmarked], output=self.root / "plans" / "bad")

    def test_source_changed_during_preparation_is_detected(self):
        a, b = self.batch("A"), self.batch("B")
        copy = pwp._copy
        changed = False

        def change_after_first_copy(source, target, expected, limit, reserve):
            nonlocal changed
            copy(source, target, expected, limit, reserve)
            if not changed:
                changed = True
                (a / "provenance.json").write_bytes((a / "provenance.json").read_bytes() + b" ")
        with mock.patch.object(pwp, "_copy", change_after_first_copy):
            with self.assertRaisesRegex(WarmPlanError, "changed|grew"):
                self.plan([a, b])
        self.assert_not_published()

    def test_links_and_overlap_are_refused(self):
        a = self.batch("A")
        for output in (a / "plan", self.root / "src"):
            with self.subTest(output=output), self.assertRaisesRegex(WarmPlanError, "overlaps"):
                self.plan([a], output=output)
        self.assertFalse((a / "plan").exists())
        alias = self.root / "alias"
        try:
            os.symlink(self.root, alias, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        with self.assertRaisesRegex(WarmPlanError, "symlink or reparse point"):
            self.plan([alias / "src" / "A"])
        with self.assertRaisesRegex(WarmPlanError, "symlink or reparse point"):
            self.plan([a], output=alias / "plans" / "x")
        self.assertFalse((self.root / "plans" / "x").exists())

    def test_budget_and_reserve_leave_nothing(self):
        a = self.batch("A")
        with self.assertRaisesRegex(WarmPlanError, "budget"):
            self.plan([a], max_write_bytes=1000)
        self.assert_not_published()
        with mock.patch.object(pwp.shutil, "disk_usage", return_value=SimpleNamespace(free=10)):
            with self.assertRaisesRegex(WarmPlanError, "reserve"):
                self.plan([a], min_free_bytes=100)
        self.assert_not_published()

    def test_existing_output_and_foreign_files_are_never_touched(self):
        a = self.batch("A")
        self.output.mkdir(parents=True)
        (self.output / "foreign.txt").write_text("keep")
        with self.assertRaisesRegex(WarmPlanError, "already exists"):
            self.plan([a])
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["foreign.txt"])
        other = self.root / "plans" / "other"
        foreign = other.parent / ".other.warm-foreign"
        foreign.mkdir()
        (other.parent / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(WarmPlanError, "budget"):
            self.plan([a], output=other, max_write_bytes=10)
        self.assertTrue(foreign.is_dir())
        self.assertEqual((other.parent / "keep.txt").read_text(), "keep")
        self.assertFalse(os.path.lexists(other))
        self.assertEqual(list(other.parent.glob(".other.warm-*")), [foreign])

    def test_invalid_arguments(self):
        a = self.batch("A")
        cases = [dict(batches=[]), dict(batches=[str(a)] * 257), dict(world=" "),
                 dict(pipeline_key="A" * 64), dict(pipeline_key=None), dict(max_write_bytes=True),
                 dict(min_free_bytes=-1)]
        for case in cases:
            kwargs = {"world": "uie", "dimension": "0", "pipeline_key": KEY, "min_free_bytes": 0, **case}
            batches = kwargs.pop("batches", [str(a)])
            with self.subTest(case=list(case)), self.assertRaises(WarmPlanError):
                prepare_warm_plan(batches, self.output, **kwargs)
        self.assert_not_published()
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", pwp.job_id("uie", "0", KEY, pwp.inspect_batch(a))))


if __name__ == "__main__":
    unittest.main()
