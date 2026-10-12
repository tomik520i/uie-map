"""Tests for carry-forward of whole unchanged area tiles in publish_generation.

Real prepared plans, synthetic GeometryCapture-shaped results, the real verifier, real area records and the
candidate adapter's real staging (as in test_area_publication). No queue, no Minecraft, no LIVE.
Stdlib only; runs on Windows and Linux."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import area_records
import capture_batch_file as cbf
import capture_snapshot as cs
import publish_attempt as pa
import publish_generation as pg
from pipeline_identity import create_pipeline_identity
from prepare_warm_plan import prepare_warm_plan
from publish_generation import PublicationError, publish_generation
from test_area_records import envelope, render
from test_prepare_warm_plan import build_world

sha = lambda raw: hashlib.sha256(raw).hexdigest()
EARLY = ("2026-09-30T10:00:00Z", "2026-09-30T10:00:05Z")
LATE = ("2026-10-01T08:00:00Z", "2026-10-01T08:00:07Z")
OVERLAPPING = ("2026-09-30T10:00:03Z", "2026-09-30T10:00:09Z")


def make_staged(directory, interval):
    """A complete UIE_STAGED_SOURCE1 sidecar (as test_publish_attempt.make_staged) with a chosen interval."""
    complete = json.loads((directory / "COMPLETE").read_bytes())
    snapshot = sha((directory / "snapshot.bin").read_bytes())
    sidecar = {"format": "UIE_STAGED_SOURCE1",
               "policy": {"acceptedConsistency": "observed-stable-files", "atomicWorldSnapshot": False,
                          "purpose": "best-effort map export explicitly accepted by the caller"},
               "warning": "best-effort map export; not a consistent world snapshot",
               "tile": {"x": 0, "z": 0},
               "stagedTile": {"consistency": "observed-stable-files", "atomicWorldSnapshot": False,
                              "sourceInterval": {"startUtc": interval[0], "endUtc": interval[1]}},
               "batch": {"snapshotSha256": snapshot,
                         "files": {name: sha((directory / name).read_bytes())
                                   for name in ("snapshot.bin", "snapshot.tiles.json", "provenance.json")}}}
    raw = json.dumps(sidecar, indent=2).encode()
    (directory / "staged-source.json").write_bytes(raw)
    complete["stagedSource"] = {"file": "staged-source.json", "sha256": sha(raw)}
    (directory / "COMPLETE").write_bytes(json.dumps(complete).encode())


def tree(root):
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class CarryForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        base = Path(cls._temp.name).resolve()
        level, region = build_world(base / "world")
        level2, region2 = build_world(base / "world2", missing={(-5, -5)})
        tile_a, tile_b = cbf.tile_areas(-1, -1), cbf.tile_areas(0, -1)
        # A core shifted by one chunk: overlaps tile A partly (5 of 6 columns).
        shifted = (cs.Area(-96, -112, 128, 128), cs.Area(-80, -96, 96, 96))
        batches = base / "batches"
        batches.mkdir()
        sources = {"A": (level, region, tile_a, EARLY), "B": (level, region, tile_b, EARLY),
                   "A2": (level2, region2, tile_a, LATE), "A2-overlapping": (level2, region2, tile_a, OVERLAPPING),
                   "A2-plain": (level2, region2, tile_a, None), "S": (level2, region2, shifted, LATE)}
        for name, (lvl, reg, (area, core), interval) in sources.items():
            cbf.create_batch(lvl, reg, area, core, batches / name)
            if interval is not None:
                make_staged(batches / name, interval)
        (base / "exporter.jar").write_bytes(b"exporter")
        identity = create_pipeline_identity({}, {"exporter": base / "exporter.jar"}, {"java": "8"})
        cls.key = identity["pipelineKey"]
        cls.templates = {}
        plans = {"A": ["A"], "B": ["B"], "AB": ["A", "B"], "A2": ["A2"], "A2-overlapping": ["A2-overlapping"],
                 "A2-plain": ["A2-plain"], "S": ["S"]}
        for plan, names in plans.items():
            cls.templates[plan] = base / f"template-{plan}"
            prepare_warm_plan([str(batches / n) for n in names], cls.templates[plan], world="uie", dimension="0",
                              pipeline_key=cls.key, min_free_bytes=0, pipeline_identity=identity)

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()
        self.count = 0

    def prepared(self, plan_name):
        """(staged specs/outputs, area record) exactly as the candidate adapter produces them."""
        self.count += 1
        job = f"{self.count:064x}"
        plan = self.dir / f"plan-{self.count}"
        shutil.copytree(self.templates[plan_name], plan)
        shutil.copyfile(plan / "COMPLETE", plan / "PLAN-COMPLETE.json")
        render(plan)
        plan_sha = sha((plan / "plan.json").read_bytes())
        raw = envelope(plan, plan_sha, self.key, job=job)
        record = area_records.build_area_record(plan, raw, verified_sha256=sha(raw), job_id=job, attempt=1)
        stage = self.dir / f"stage-{self.count}"
        stage.mkdir()
        staged = pa._stage(plan, stage, pa._plan_jobs(plan, json.loads(raw)["report"]))
        return staged, record

    def publish(self, staged, store, record=None, base=None, expected=None, **kwargs):
        kwargs.setdefault("compress_chunks", True)
        if record is not None:
            kwargs["area_record"] = record
        if base is not None:
            kwargs.setdefault("expected_revision", base)
            kwargs.setdefault("carry_forward", {"generation": base})
        return publish_generation(staged["specs"], store, world="uie", dimension="0", pipeline_key=self.key,
                                  expected_chunks=expected or staged["expected"], min_free_bytes=0, **kwargs)

    @staticmethod
    def inventory(store, generation):
        return json.loads((store / "generations" / generation / "COMPLETE.json").read_bytes())

    @staticmethod
    def manifest(store, generation):
        return (store / "generations" / generation / "manifest.json").read_bytes()

    @staticmethod
    def present(*records, drop=()):
        return sorted({tuple(c) for r in records for t in r["tiles"] if t["snapshotSha256"] not in drop
                       for c in t["present"]})

    def assert_refused(self, store, staged, record, base, pattern, expected=None, **kwargs):
        before = tree(store)
        with self.assertRaisesRegex(PublicationError, pattern):
            self.publish(staged, store, record, base=base, expected=expected, **kwargs)
        self.assertEqual(tree(store), before, "nothing written, pointer unchanged, lock released")

    # ------------------------------------------------------------------ accepted updates

    def test_new_tile_is_added_and_the_base_tile_is_carried_byte_exactly(self):
        staged_a, rec_a = self.prepared("A")
        staged_b, rec_b = self.prepared("B")
        store = self.dir / "store"
        g1 = self.publish(staged_a, store, rec_a)["generation"]
        expected = [list(c) for c in self.present(rec_a, rec_b)]
        g2 = self.publish(staged_b, store, rec_b, base=g1, expected=expected)["generation"]
        inv1, inv2 = self.inventory(store, g1), self.inventory(store, g2)
        self.assertEqual(inv2["areas"], [rec_a, rec_b])
        self.assertEqual(inv2["supersededTiles"], [])
        self.assertEqual(inv2["areaBase"], g1)
        self.assertEqual(inv2["snapshots"], sorted(inv1["snapshots"] + [t["snapshotSha256"] for t in rec_b["tiles"]]))
        for name, h in inv1["files"].items():
            if name.startswith(("chunks/", "blocks/", "provenance/")):
                self.assertEqual(inv2["files"][name], h, name)
        for policy in inv1["sourcePolicies"]:
            self.assertIn(policy, inv2["sourcePolicies"], "carried provenance kept verbatim")
        self.assertEqual({p["snapshotSha256"] for p in inv2["sourcePolicies"]}, set(inv2["snapshots"]))
        # The merged manifest is exactly what an untracked publication of both batch sets produces.
        direct = self.dir / "direct-both"
        both = {"specs": staged_a["specs"] + staged_b["specs"], "expected": expected}
        g_direct = self.publish(both, direct)["generation"]
        self.assertEqual(self.manifest(store, g2), self.manifest(direct, g_direct))
        current = json.loads((store / "current.json").read_bytes())
        self.assertEqual(current["generation"], g2)

    def test_replaced_tile_drops_its_deleted_chunk_and_keeps_the_other_tile(self):
        staged_ab, rec_ab = self.prepared("AB")
        staged_a2, rec_a2 = self.prepared("A2")
        store = self.dir / "store"
        g1 = self.publish(staged_ab, store, rec_ab)["generation"]
        old_a = next(t for t in rec_ab["tiles"] if [-5, -5] in t["present"])
        expected = [list(c) for c in self.present(rec_ab, rec_a2, drop={old_a["snapshotSha256"]})]
        self.assertNotIn([-5, -5], expected)
        g2 = self.publish(staged_a2, store, rec_a2, base=g1, expected=expected)["generation"]
        inv2 = self.inventory(store, g2)
        self.assertEqual(inv2["areas"], [rec_ab, rec_a2])
        self.assertEqual(inv2["supersededTiles"], [old_a["snapshotSha256"]])
        self.assertNotIn(old_a["snapshotSha256"], inv2["snapshots"])
        manifest = json.loads(self.manifest(store, g2))
        self.assertNotIn((-5, -5), {(c["x"], c["z"]) for c in manifest["chunks"]})
        self.assertFalse([n for n in inv2["files"] if n.startswith(("chunks/-5_-5-", "blocks/-5_-5-"))])
        # Same manifest as an untracked publication of the new A2 batch plus the carried B batch.
        b_spec = [s for s in staged_ab["specs"] if s["snapshotSha256"] != old_a["snapshotSha256"]]
        direct = self.dir / "direct"
        g_direct = self.publish({"specs": staged_a2["specs"] + b_spec, "expected": expected}, direct)["generation"]
        self.assertEqual(self.manifest(store, g2), self.manifest(direct, g_direct))
        # The replaced tile's provenance is not carried.
        self.assertNotIn(old_a["snapshotSha256"], {p["snapshotSha256"] for p in inv2["sourcePolicies"]})

        # Chain: a later base is itself a valid carry-forward base (its supersededTiles are honoured).
        staged_b2, rec_b2 = self.prepared("B")
        with self.assertRaisesRegex(PublicationError, "repeats an active base snapshot"):
            self.publish(staged_b2, store, rec_b2, base=g2, expected=expected)

    def test_area_whose_tiles_are_all_replaced_is_dropped(self):
        staged_a, rec_a = self.prepared("A")
        staged_b, rec_b = self.prepared("B")
        staged_a2, rec_a2 = self.prepared("A2")
        store = self.dir / "store"
        g1 = self.publish(staged_a, store, rec_a)["generation"]
        g2 = self.publish(staged_b, store, rec_b, base=g1,
                          expected=[list(c) for c in self.present(rec_a, rec_b)])["generation"]
        g3 = self.publish(staged_a2, store, rec_a2, base=g2,
                          expected=[list(c) for c in self.present(rec_b, rec_a2)])["generation"]
        inv3 = self.inventory(store, g3)
        self.assertEqual(inv3["areas"], [rec_b, rec_a2])
        self.assertEqual(inv3["supersededTiles"], [])
        self.assertEqual(inv3["areaBase"], g2)

    # ------------------------------------------------------------------ refusals

    def test_refusals_keep_the_base_and_write_nothing(self):
        staged_ab, rec_ab = self.prepared("AB")
        store = self.dir / "store"
        g1 = self.publish(staged_ab, store, rec_ab)["generation"]
        cases = {"A2-overlapping": "not observed after", "A2-plain": "observed source intervals",
                 "S": "partially overlaps"}
        for plan, pattern in cases.items():
            with self.subTest(plan):
                staged, record = self.prepared(plan)
                self.assert_refused(store, staged, record, g1, pattern,
                                    expected=[list(c) for c in self.present(record)])
        staged_a2, rec_a2 = self.prepared("A2")
        expected = [list(c) for c in self.present(rec_a2)]
        with self.subTest("carry needs a record"):
            self.assert_refused(store, staged_a2, None, g1, "needs an area record", expected=expected)
        with self.subTest("carry base must be the expected revision"):
            self.assert_refused(store, staged_a2, rec_a2, g1, "must be the expected revision", expected=expected,
                                carry_forward={"generation": "f" * 64})
        with self.subTest("stale base"):
            self.assert_refused(store, staged_a2, rec_a2, "e" * 64, "Stale publication base", expected=expected)
        with self.subTest("malformed spec"):
            self.assert_refused(store, staged_a2, rec_a2, g1, "Invalid carry-forward spec", expected=expected,
                                carry_forward={"generation": g1, "force": True})
        with self.subTest("expected chunks must include the carried tiles"):
            self.assert_refused(store, staged_a2, rec_a2, g1, "all expected chunks", expected=expected)

    def test_untracked_base_is_refused(self):
        staged_a, _ = self.prepared("A")
        staged_b, rec_b = self.prepared("B")
        store = self.dir / "store"
        g1 = self.publish(staged_a, store)["generation"]
        self.assert_refused(store, staged_b, rec_b, g1, "not area-tracked",
                            expected=[list(c) for c in self.present(rec_b)])

    def test_damaged_carried_blob_is_refused(self):
        staged_a, rec_a = self.prepared("A")
        staged_b, rec_b = self.prepared("B")
        store = self.dir / "store"
        g1 = self.publish(staged_a, store, rec_a)["generation"]
        blob = next(p for p in sorted((store / "generations" / g1 / "chunks").iterdir()))
        blob.write_bytes(blob.read_bytes()[:-1] + bytes([blob.read_bytes()[-1] ^ 1]))
        self.assert_refused(store, staged_b, rec_b, g1, "Carried base file is damaged",
                            expected=[list(c) for c in self.present(rec_a, rec_b)])

    def test_base_identity_mismatches_require_a_full_rebuild(self):
        staged_a, rec_a = self.prepared("A")
        _, rec_b = self.prepared("B")
        store = self.dir / "store"
        g1 = self.publish(staged_a, store, rec_a)["generation"]
        for field, value in (("atlasSha256", "1" * 64), ("layoutIdentitySha256", "2" * 64),
                             ("pipelineKey", "3" * 64), ("bakedLighting", False)):
            with self.subTest(field):
                other = copy.deepcopy(rec_b)
                other[field] = value
                pipeline = value if field == "pipelineKey" else self.key
                with self.assertRaisesRegex(PublicationError, "full rebuild"):
                    pg._carry_plan(store.resolve(), g1, pipeline, other)
        with self.subTest("untracked caller pipeline"):
            with self.assertRaisesRegex(PublicationError, "another pipeline"):
                pg._carry_plan(store.resolve(), g1, "4" * 64, rec_b)


if __name__ == "__main__":
    unittest.main()
