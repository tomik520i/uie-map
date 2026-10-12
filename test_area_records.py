"""Tests for area_records with real prepare_warm_plan outputs (plain, staged-source and one with an absent core
chunk), synthetic GeometryCapture-shaped results, the real verifier and real VERIFIED.json envelopes.
No queue, no Minecraft, no publication. Stdlib only; runs on Windows and Linux."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import capture_batch_file as cbf
import verify_rendered_plan as vrp
from area_records import AreaRecordError, build_area_record, validate_area_record
from pipeline_identity import create_pipeline_identity
from prepare_warm_plan import prepare_warm_plan
from test_atlas_identity import layout, png
from test_prepare_warm_plan import build_world
from test_publish_attempt import make_staged, same_length_mesh_change

sha = lambda raw: hashlib.sha256(raw).hexdigest()
JOB = "a" * 64
MISSING = object()


def sidecar_raw(directory, interval=MISSING, consistency="observed-stable-files", snapshot=None):
    """A UIE_STAGED_SOURCE1 sidecar like test_publish_attempt.make_staged, with a controllable interval."""
    snapshot = snapshot or sha((directory / "snapshot.bin").read_bytes())
    tile = {"consistency": "observed-stable-files", "atomicWorldSnapshot": False}
    if interval is not MISSING:
        tile["sourceInterval"] = interval
    doc = {"format": "UIE_STAGED_SOURCE1",
           "policy": {"acceptedConsistency": consistency, "atomicWorldSnapshot": False,
                      "purpose": "best-effort map export explicitly accepted by the caller"},
           "warning": "best-effort map export; not a consistent world snapshot", "tile": {"x": 0, "z": 0},
           "stagedTile": tile,
           "batch": {"snapshotSha256": snapshot,
                     "files": {name: sha((directory / name).read_bytes())
                               for name in ("snapshot.bin", "snapshot.tiles.json", "provenance.json")}}}
    return json.dumps(doc, indent=2).encode()


def stage_with(directory, interval):
    raw = sidecar_raw(directory, interval)
    (directory / "staged-source.json").write_bytes(raw)
    complete = json.loads((directory / "COMPLETE").read_bytes())
    complete["stagedSource"] = {"file": "staged-source.json", "sha256": sha(raw)}
    (directory / "COMPLETE").write_bytes(json.dumps(complete).encode())
ATLAS = png(32, 32)
LAYOUT = json.dumps(layout(ATLAS)).encode()


def render(plan, generated_at="2026-10-01T12:00:00Z", rendered=10):
    jobs = json.loads((plan / "jobs.json").read_bytes())["jobs"]
    for n, (job, info) in enumerate(zip(json.loads((plan / "plan.json").read_bytes())["jobs"], jobs)):
        geometry = plan / "results" / job["id"] / "geometry"
        geometry.mkdir(parents=True)
        (geometry / "atlas.png").write_bytes(ATLAS)
        (geometry / "atlas-layout.json").write_bytes(LAYOUT)
        chunks = []
        for x, z in info["presentCoreChunks"]:
            mesh = {"positions": [x * 16, 64, z * 16, x * 16 + 1, 64, z * 16, x * 16, 64, z * 16 + 1],
                    "uvs": [0, 0, 1, 0, 0, 1], "colors": [1] * 9, "indices": [0, 1, 2]}
            (geometry / f"chunk_{x}_{z}.json").write_bytes(json.dumps(mesh).encode())
            chunks.append({"file": f"chunk_{x}_{z}.json", "x": x, "z": z})
        core = info["core"]
        manifest = {"format": 1, "snapshotFormat": "UIE2", "snapshotSha256": job["snapshotSha256"],
                    "atlas": "atlas.png", "atlasLayout": "atlas-layout.json", "atlasLayoutSha256": sha(LAYOUT),
                    "bakedLighting": True, "chunks": chunks,
                    "bounds": {"min": [core["x"], 0, core["z"]], "max": [core["x"] + 96, 200 + n, core["z"] + 96]},
                    "coverage": {"renderedBlocks": rendered + n, "unsupportedBlocks": 0, "unsupportedNames": [],
                                 "emptyBlocks": 3}}
        if generated_at is not None:
            manifest["generatedAt"] = generated_at if n == 0 else "2026-10-01T12:05:00+00:00"
        raw = json.dumps(manifest).encode()
        (geometry / "manifest.json").write_bytes(raw)
        (geometry.parent / "receipt.json").write_bytes(json.dumps({
            "format": "UIE_WARM_RESULT1", "id": job["id"], "snapshotSha256": job["snapshotSha256"],
            "state": "captured", "manifestSha256": sha(raw), "elapsedSeconds": 0.5}).encode())


def envelope(plan, plan_sha, key, job=JOB, attempt=1, change=None):
    report = vrp.verify_rendered_plan(plan, expected_plan_sha256=plan_sha, marker_name="PLAN-COMPLETE.json")
    doc = {"format": "UIE_WARM_VERIFIED1", "jobId": job, "attempt": attempt, "planSha256": plan_sha,
           "pipelineKey": key, "report": report}
    if change:
        change(doc)
    return json.dumps(doc, indent=2, sort_keys=True).encode() + b"\n"


class AreaRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        base = Path(cls._temp.name).resolve()
        level, region = build_world(base / "world")
        level2, region2 = build_world(base / "world2", missing={(-5, -5)})
        batches = base / "batches"
        batches.mkdir()
        for name, (lvl, reg), tile in (("A", (level, region), (-1, -1)), ("B", (level, region), (0, -1)),
                                       ("A-absent", (level2, region2), (-1, -1))):
            cbf.create_batch(lvl, reg, *cbf.tile_areas(*tile), batches / name)
        for name in ("A", "B"):
            shutil.copytree(batches / name, batches / f"{name}-staged")
            make_staged(batches / f"{name}-staged")
        # Sidecars that record no interval (key absent, explicit null) and one with a malformed (naive) interval.
        for name, interval in (("A-nointerval", MISSING), ("B-nointerval", None),
                               ("A-badinterval", {"startUtc": "2026-09-30T10:00:00", "endUtc": "2026-09-30T10:00:05Z"})):
            shutil.copytree(batches / name.split("-")[0], batches / name)
            stage_with(batches / name, interval)
        cls.batches = batches
        (base / "exporter.jar").write_bytes(b"exporter")
        record = create_pipeline_identity({}, {"exporter": base / "exporter.jar"}, {"java": "8"})
        cls.key = record["pipelineKey"]
        cls.templates = {}
        for kind, names in (("plain", ("A", "B")), ("staged", ("A-staged", "B-staged")), ("absent", ("A-absent", "B")),
                            ("nointerval", ("A-nointerval", "B-nointerval")),
                            ("badinterval", ("A-badinterval", "B-staged"))):
            cls.templates[kind] = base / f"template-{kind}"
            prepare_warm_plan([str(batches / n) for n in names], cls.templates[kind], world="uie", dimension="0",
                              pipeline_key=cls.key, min_free_bytes=0, pipeline_identity=record)

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()

    def attempt(self, kind="plain", name="plan", **render_kwargs):
        plan = self.dir / name
        shutil.copytree(self.templates[kind], plan)
        shutil.copyfile(plan / "COMPLETE", plan / "PLAN-COMPLETE.json")
        render(plan, **render_kwargs)
        plan_sha = sha((plan / "plan.json").read_bytes())
        return plan, plan_sha, envelope(plan, plan_sha, self.key)

    def build(self, plan, raw, **kwargs):
        args = {"verified_sha256": sha(raw), "job_id": JOB, "attempt": 1, **kwargs}
        return build_area_record(plan, raw, **args)

    # ------------------------------------------------------------------ building

    def test_plain_record_is_deterministic_exact_and_a_defensive_copy(self):
        plan, plan_sha, raw = self.attempt()
        record = self.build(plan, raw)
        self.assertEqual(self.build(plan, raw), record, "deterministic")
        self.assertEqual((record["jobId"], record["attempt"], record["verifiedSha256"], record["planSha256"],
                          record["pipelineKey"]), (JOB, 1, sha(raw), plan_sha, self.key))
        self.assertEqual((record["atlasSha256"], record["bakedLighting"]), (sha(ATLAS), True))
        jobs = json.loads((plan / "plan.json").read_bytes())["jobs"]
        self.assertEqual(sorted(t["snapshotSha256"] for t in record["tiles"]), sorted(j["snapshotSha256"] for j in jobs))
        self.assertEqual(sorted(t["gridTile"] for t in record["tiles"]), [[-1, -1], [0, -1]])
        for tile in record["tiles"]:
            self.assertEqual((len(tile["coreChunks"]), len(tile["present"]), tile["absent"]), (36, 36, []))
            self.assertEqual((tile["sourceInterval"], tile["stagedSourceSha256"]), (None, None))
            self.assertEqual(tile["coverage"]["emptyBlocks"], 3)
        aggregate = record["aggregate"]
        self.assertEqual((aggregate["chunks"], aggregate["coverage"]["renderedBlocks"]), (72, 21))
        self.assertEqual(aggregate["bounds"], {"min": [-96, 0, -96], "max": [96, 201, 0]})
        self.assertEqual(aggregate["generatedAt"], "2026-10-01T12:05:00+00:00", "latest instant, original text")
        present = [c for t in record["tiles"] for c in t["present"]]
        expected = {"pipelineKey": self.key, "atlasSha256": sha(ATLAS), "bakedLighting": True,
                    "layoutIdentitySha256": record["layoutIdentitySha256"], "publishedChunks": present,
                    "snapshots": [t["snapshotSha256"] for t in record["tiles"]]}
        copy_ = validate_area_record(record, expected=expected)
        self.assertEqual(copy_, record)
        copy_["tiles"][0]["present"].pop()
        self.assertEqual(len(record["tiles"][0]["present"]), 36, "the returned record is independent")

    def test_staged_sidecar_interval_is_recorded_from_the_verified_sidecar(self):
        plan, _, raw = self.attempt("staged")
        record = self.build(plan, raw)
        for tile in record["tiles"]:
            sidecar = plan / "inputs" / tile["job"] / "staged-source.json"
            self.assertEqual(tile["stagedSourceSha256"], sha(sidecar.read_bytes()))
            self.assertEqual(tile["sourceInterval"], {"startUtc": "2026-09-30T10:00:00Z", "endUtc": "2026-09-30T10:00:05Z"})

    def test_absent_core_chunk_is_part_of_the_partition(self):
        plan, _, raw = self.attempt("absent")
        record = self.build(plan, raw)
        tile = next(t for t in record["tiles"] if t["gridTile"] == [-1, -1])
        self.assertEqual((tile["absent"], len(tile["present"]), len(tile["coreChunks"])), ([[-5, -5]], 35, 36))
        self.assertEqual(record["aggregate"]["chunks"], 71)

    def test_changed_mesh_or_mismatched_envelope_is_refused(self):
        plan, plan_sha, raw = self.attempt()
        cases = {
            "wrong hash": dict(verified_sha256="0" * 64),
            "other job": dict(job_id="b" * 64),
            "other attempt": dict(attempt=2),
        }
        for label, kwargs in cases.items():
            with self.subTest(label), self.assertRaises(AreaRecordError):
                self.build(plan, raw, **kwargs)
        for label, change in {
                "report differs": lambda d: d["report"]["coverage"].update(renderedBlocks=999),
                "other plan hash": lambda d: d.update(planSha256="1" * 64),
                "other pipeline": lambda d: d.update(pipelineKey="2" * 64),
                "extra field": lambda d: d.update(note="x")}.items():
            with self.subTest(label), self.assertRaises(AreaRecordError):
                self.build(plan, envelope(plan, plan_sha, self.key, change=change))
        same_length_mesh_change(plan / "results")
        with self.assertRaisesRegex(AreaRecordError, "differs"):
            self.build(plan, raw)

    def test_missing_capture_time_is_refused_not_invented(self):
        plan, _, raw = self.attempt(generated_at=None)
        with self.assertRaisesRegex(AreaRecordError, "generatedAt|incomplete"):
            self.build(plan, raw)

    # ------------------------------------------------------------------ validation

    def record(self, kind="plain"):
        plan, _, raw = self.attempt(kind, name=f"plan-{kind}")
        return self.build(plan, raw)

    def assert_refused(self, record, change, **kwargs):
        doc = copy.deepcopy(record)
        change(doc)
        with self.assertRaises(AreaRecordError):
            validate_area_record(doc, **kwargs)

    def test_overlapping_or_duplicate_tiles_are_refused(self):
        record = self.record()

        def duplicate(r, job, snapshot):
            tile = dict(copy.deepcopy(r["tiles"][0]), job=job, snapshotSha256=snapshot)
            r["tiles"].append(tile)
            r["tiles"].sort(key=lambda t: t["job"])
        for label, change in {
                "same core, other job": lambda r: duplicate(r, "zzz", "f" * 64),
                "same snapshot": lambda r: duplicate(r, "zzz", r["tiles"][0]["snapshotSha256"]),
                "same job twice": lambda r: duplicate(r, r["tiles"][0]["job"], "f" * 64)}.items():
            with self.subTest(label):
                self.assert_refused(record, change)

    def test_forged_or_inconsistent_metadata_is_refused(self):
        record = self.record()
        staged = self.record("staged")
        first = lambda r: r["tiles"][0]
        cases = {
            "coverage without aggregate": lambda r: first(r)["coverage"].update(renderedBlocks=1),
            "bounds without aggregate": lambda r: first(r)["bounds"]["max"].__setitem__(1, 255),
            "aggregate count": lambda r: r["aggregate"].update(chunks=71),
            "aggregate time": lambda r: r["aggregate"].update(generatedAt="2026-10-01T12:00:00Z"),
            "present chunk also absent": lambda r: first(r)["absent"].append(first(r)["present"][0]),
            "present moved to absent unsorted": lambda r: first(r)["absent"].insert(0, first(r)["present"].pop()),
            "core chunk outside the core": lambda r: first(r)["coreChunks"].append([50, 50]),
            "core not 16-aligned": lambda r: first(r)["core"].update(x=first(r)["core"]["x"] + 8),
            "core larger than UIE2 allows": lambda r: first(r)["core"].update(width=112),
            "wrong grid tile": lambda r: first(r).update(gridTile=[5, 5]),
            "interval without sidecar": lambda r: first(r).update(sourceInterval={"startUtc": "2026-01-01T00:00:00Z",
                                                                                   "endUtc": "2026-01-01T00:00:01Z"}),
            "naive capture time": lambda r: first(r).update(generatedAt="2026-10-01T12:00:00"),
            "unsorted tiles": lambda r: r["tiles"].reverse(),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.assert_refused(record, change)
        with self.subTest("reversed source interval"):
            self.assert_refused(staged, lambda r: first(r)["sourceInterval"].update(startUtc="2026-09-30T11:00:00Z"))
        present = [c for t in record["tiles"] for c in t["present"]]
        for label, expected in {"pipeline": {"pipelineKey": "3" * 64}, "atlas": {"atlasSha256": "4" * 64},
                                "lighting": {"bakedLighting": False}, "published chunk missing": {"publishedChunks": present[1:]},
                                "published extra chunk": {"publishedChunks": present + [[99, 99]]},
                                "snapshots": {"snapshots": ["5" * 64]}, "unknown key": {"world": "uie"}}.items():
            with self.subTest(expected=label):
                self.assert_refused(record, lambda r: None, expected=expected)

    def test_malformed_types_are_refused(self):
        record = self.record()
        cases = {
            "attempt as text": lambda r: r.update(attempt="1"),
            "attempt zero": lambda r: r.update(attempt=0),
            "lighting as int": lambda r: r.update(bakedLighting=1),
            "jobId not hex": lambda r: r.update(jobId="job-1"),
            "tiles as dict": lambda r: r.update(tiles={}),
            "float chunk": lambda r: r["tiles"][0]["present"].__setitem__(0, [-6.0, -6]),
            "bool coverage": lambda r: r["tiles"][0]["coverage"].update(unsupportedBlocks=False),
            "nan bounds": lambda r: r["tiles"][0]["bounds"]["min"].__setitem__(0, float("nan")),
            "extra key": lambda r: r.update(publishedFromGeneration="6" * 64),
            "missing key": lambda r: r.pop("aggregate"),
            "tile extra key": lambda r: r["tiles"][0].update(note="x"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.assert_refused(record, change)
        with self.assertRaises(AreaRecordError):
            validate_area_record("not a record")

    # ------------------------------------------------------------------ task92 regressions

    def test_aggregate_grid_and_core_values_are_type_exact(self):
        record = self.record()
        tile = lambda r: next(t for t in r["tiles"] if t["gridTile"] == [-1, -1])
        cases = {
            "aggregate count as bool-like zero": lambda r: r["aggregate"]["coverage"].update(unsupportedBlocks=False),
            "aggregate chunks as float": lambda r: r["aggregate"].update(chunks=72.0),
            "aggregate chunks as text": lambda r: r["aggregate"].update(chunks="72"),
            "aggregate bound as float": lambda r: r["aggregate"]["bounds"]["min"].__setitem__(0, -96.0),
            "aggregate names as null": lambda r: r["aggregate"]["coverage"].update(unsupportedNames=None),
            "grid tile floats": lambda r: tile(r).update(gridTile=[-1.0, -1.0]),
            "grid tile bools": lambda r: next(t for t in r["tiles"] if t["gridTile"] == [0, -1]).update(gridTile=[False, -1]),
            "grid tile short": lambda r: tile(r).update(gridTile=[-1]),
            "core outside int32": lambda r: tile(r)["core"].update(x=2 ** 31),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.assert_refused(record, change)
        self.assertEqual(validate_area_record(record), record, "the untouched record still validates")

    def test_expected_values_are_type_checked_and_errors_are_area_record_errors(self):
        record = self.record()
        present = [c for t in record["tiles"] for c in t["present"]]
        snapshots = [t["snapshotSha256"] for t in record["tiles"]]
        bad = {
            "lighting as int": {"bakedLighting": 1},
            "float coordinate": {"publishedChunks": [[float(present[0][0]), present[0][1]], *present[1:]]},
            "bool coordinate": {"publishedChunks": [[True, 0], *present[1:]]},
            "nested coordinate": {"publishedChunks": [[[1, 2], 3]]},
            "unhashable coordinate": {"publishedChunks": [[[1], [2]]]},
            "string coordinate": {"publishedChunks": ["ab"]},
            "duplicate coordinate": {"publishedChunks": present + present[:1]},
            "chunks not a list": {"publishedChunks": {"x": 1}},
            "snapshots None": {"snapshots": None},
            "snapshots not hex": {"snapshots": ["x"] * len(snapshots)},
            "pipeline not hex": {"pipelineKey": "abc"},
            "atlas as int": {"atlasSha256": 5},
        }
        for label, expected in bad.items():
            with self.subTest(label), self.assertRaises(AreaRecordError):
                validate_area_record(record, expected=expected)
        for expected in ("not a dict", [1]):
            with self.subTest(expected=expected), self.assertRaises(AreaRecordError):
                validate_area_record(record, expected=expected)
        good = {"bakedLighting": True, "publishedChunks": [tuple(c) for c in present], "snapshots": snapshots}
        self.assertEqual(validate_area_record(record, expected=good), record)

    def test_staged_sidecar_without_an_interval_keeps_its_hash_and_records_no_time(self):
        plan, _, raw = self.attempt("nointerval")
        record = self.build(plan, raw)
        for tile in record["tiles"]:
            sidecar = plan / "inputs" / tile["job"] / "staged-source.json"
            self.assertEqual((tile["stagedSourceSha256"], tile["sourceInterval"]), (sha(sidecar.read_bytes()), None))
        self.assertEqual(validate_area_record(record), record)

        def interval_without_sidecar(r):
            r["tiles"][0]["stagedSourceSha256"] = None
            r["tiles"][0]["sourceInterval"] = {"startUtc": "2026-01-01T00:00:00Z", "endUtc": "2026-01-01T00:00:01Z"}
        self.assert_refused(record, interval_without_sidecar)

    def test_malformed_interval_in_a_sidecar_is_refused(self):
        plan, _, raw = self.attempt("badinterval")
        with self.assertRaises(AreaRecordError):
            self.build(plan, raw)

    def test_sidecar_checks_apply_without_an_interval(self):
        import area_records
        directory = self.batches / "A"
        snapshot = sha((directory / "snapshot.bin").read_bytes())
        job = {"id": "job", "snapshotSha256": snapshot}
        good = sidecar_raw(directory)
        self.assertIsNone(area_records._staged_interval(good, sha(good), job))
        for label, raw in {"not observed-stable": sidecar_raw(directory, consistency="offline-consistent"),
                           "other snapshot": sidecar_raw(directory, snapshot="7" * 64),
                           "not JSON": b"{", "not an object": b"[]",
                           "malformed interval": sidecar_raw(directory, {"startUtc": "x", "endUtc": "y"})}.items():
            with self.subTest(label), self.assertRaises(AreaRecordError):
                area_records._staged_interval(raw, sha(raw), job)


if __name__ == "__main__":
    unittest.main()
