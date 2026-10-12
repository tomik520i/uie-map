"""Optional declared-input identity bound to warm plans (pipeline_identity, prepare_warm_plan, verify_rendered_plan).

Real UIE2 batches are built once with the capture_batch_file test helpers; a real identity record is created
from a small declared tree; rendered results are synthetic but shaped like WarmBatch/GeometryCapture output.
No Minecraft is needed. Stdlib only."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import capture_batch_file as cbf
from pipeline_identity import PipelineIdentityError, canonical_json, create_pipeline_identity, validate_pipeline_identity
from prepare_warm_plan import WarmPlanError, prepare_warm_plan
from test_atlas_identity import layout, png
from test_prepare_warm_plan import build_world
from verify_rendered_plan import RenderVerificationError, verify_rendered_plan

sha = lambda raw: hashlib.sha256(raw).hexdigest()
PNG = png(32, 32)


def rekey(record):
    """Recompute pipelineKey so that only the structural check under test can fail."""
    record["pipelineKey"] = sha(canonical_json(record["identity"]))
    return record


class WarmPlanIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        base = Path(cls._temp.name).resolve()
        level, region = build_world(base / "world")
        (base / "batches").mkdir()
        cls.batches = []
        for name, tile in (("A", (-1, -1)), ("B", (0, -1))):
            cbf.create_batch(level, region, *cbf.tile_areas(*tile), base / "batches" / name)
            cls.batches.append(str(base / "batches" / name))
        declared = base / "declared"
        (declared / "mods" / "sub").mkdir(parents=True)
        (declared / "mods" / "a.jar").write_bytes(b"mod a")
        (declared / "mods" / "sub" / "b.jar").write_bytes(b"mod b")
        (declared / "exporter.jar").write_bytes(b"exporter")
        cls.record = create_pipeline_identity({"mods": declared / "mods"}, {"exporter": declared / "exporter.jar"},
                                              {"java": "8", "jvm": ["-Xmx4G"]})
        cls.key = cls.record["pipelineKey"]

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def plan(self, name, identity=None, key=None, **kwargs):
        output = self.root / name
        prepare_warm_plan(self.batches, output, world="uie", dimension="0", pipeline_key=key or self.key,
                          min_free_bytes=0, **({"pipeline_identity": identity} if identity is not None else {}), **kwargs)
        return output

    def assert_nothing_written(self, output):
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob(f".{output.name}.warm-*")), [])

    # ------------------------------------------------------------------ validator

    def test_validator_roundtrip_and_copy_isolation(self):
        record = copy.deepcopy(self.record)
        result = validate_pipeline_identity(record)
        self.assertEqual(result, self.record)
        self.assertEqual(validate_pipeline_identity(json.loads(json.dumps(result))), result, "JSON roundtrip")
        result["identity"]["options"]["jvm"].append("-Xmx8G")
        self.assertEqual(record, self.record, "changing the result does not reach the input")
        record["identity"]["files"][0]["bytes"] = 999
        self.assertEqual(result["identity"]["files"][0]["bytes"], self.record["identity"]["files"][0]["bytes"],
                         "changing the input does not reach the result")

    def test_validator_rejects_malformed_records(self):
        def name(new):
            return lambda r: r["identity"]["files"][0].update(name=new)
        cases = {
            "tampered key": (lambda r: r.update(pipelineKey="0" * 64), False),
            "extra field": (lambda r: r.update(extra=1), False),
            "format": (lambda r: r.update(format="UIE_PIPELINE_IDENTITY2"), False),
            "identity extra field": (lambda r: r["identity"].update(when="now"), True),
            "traversal": (name("mods/../x.jar"), True),
            "empty segment": (name("mods//x.jar"), True),
            "backslash": (name("mods\\x.jar"), True),
            "absolute": (name("/mods/x.jar"), True),
            "unknown root": (name("other/x.jar"), True),
            "file used as ancestor": (lambda r: r["identity"].update(files=[
                {"name": n, "bytes": 1, "sha256": "a" * 64}
                for n in ["mods/a", "mods/a-else", "mods/a/nested.jar"]]), True),
            "root/file label conflict": (lambda r: r["identity"].update(roots=["exporter", "mods"]), True),
            "unsorted names": (lambda r: r["identity"]["files"].reverse(), True),
            "duplicate name": (lambda r: r["identity"]["files"].append(dict(r["identity"]["files"][-1])), True),
            "bool bytes": (lambda r: r["identity"]["files"][0].update(bytes=True), True),
            "negative bytes": (lambda r: r["identity"]["files"][0].update(bytes=-1), True),
            "uppercase hash": (lambda r: r["identity"]["files"][0].update(sha256=r["identity"]["files"][0]["sha256"].upper()), True),
            "unsorted roots": (lambda r: r["identity"].update(roots=["mods", "aa"]), True),
            "tuple option": (lambda r: r["identity"]["options"].update(jvm=("-Xmx4G",)), False),
            "nan option": (lambda r: r["identity"]["options"].update(scale=float("nan")), False),
        }
        for label, (change, recompute) in cases.items():
            with self.subTest(label):
                record = copy.deepcopy(self.record)
                change(record)
                if recompute:
                    rekey(record)
                with self.assertRaises(PipelineIdentityError):
                    validate_pipeline_identity(record)

    # ------------------------------------------------------------------ prepare_warm_plan

    def test_key_mismatch_or_invalid_record_writes_nothing(self):
        with self.assertRaisesRegex(WarmPlanError, "does not equal"):
            self.plan("mismatch", identity=self.record, key="d" * 64)
        self.assert_nothing_written(self.root / "mismatch")
        bad = rekey(copy.deepcopy(self.record))
        bad["pipelineKey"] = "e" * 64
        with self.assertRaisesRegex(WarmPlanError, "invalid"):
            self.plan("invalid", identity=bad, key="e" * 64)
        self.assert_nothing_written(self.root / "invalid")

    def test_identity_plan_tracks_the_record_in_complete_and_jobs(self):
        output = self.plan("bound", identity=self.record)
        raw = (output / "pipeline-identity.json").read_bytes()
        self.assertEqual(validate_pipeline_identity(json.loads(raw)), self.record)
        complete = json.loads((output / "COMPLETE").read_bytes())
        self.assertEqual(complete["files"]["pipeline-identity.json"], sha(raw))
        jobs = json.loads((output / "jobs.json").read_bytes())
        self.assertEqual(jobs["pipelineKey"], self.key)
        self.assertEqual(jobs["pipelineKeySource"], "declared-inputs-sha256")
        self.assertEqual(jobs["pipelineIdentity"], {"file": "pipeline-identity.json", "sha256": sha(raw)})
        self.assertIn("rescan", jobs["pipelineKeyNote"])
        for relative, digest in complete["files"].items():
            self.assertEqual(sha((output / relative).read_bytes()), digest, relative)

    def test_legacy_output_is_unchanged(self):
        omitted = self.plan("omitted")
        explicit = self.plan("explicit", identity=None)
        for name in ("plan.json", "jobs.json", "COMPLETE"):
            self.assertEqual((omitted / name).read_bytes(), (explicit / name).read_bytes(), name)
        jobs = json.loads((omitted / "jobs.json").read_bytes())
        self.assertEqual(set(jobs), {"format", "world", "dimension", "pipelineKey", "pipelineKeySource",
                                     "pipelineKeyNote", "plan", "jobs"}, "the pre-identity jobs.json schema")
        self.assertEqual(jobs["pipelineKeySource"], "caller-assertion")
        self.assertEqual(jobs["pipelineKeyNote"],
                         "Asserted by the caller; not derived from mods, configuration or the capture client.")
        bound = json.loads((self.plan("bound", identity=self.record) / "plan.json").read_bytes())
        self.assertEqual(bound, json.loads((omitted / "plan.json").read_bytes()), "job ids depend on the key only")
        self.assertNotIn("pipelineIdentity", jobs)
        self.assertFalse((omitted / "pipeline-identity.json").exists())
        self.assertNotIn("pipeline-identity.json", json.loads((omitted / "COMPLETE").read_bytes())["files"])

    def test_write_budget_rollback_includes_the_identity_file(self):
        size = lambda out: sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
        legacy_total = size(self.plan("legacy"))
        total = size(self.plan("reference", identity=self.record))
        identity_size = (self.root / "reference" / "pipeline-identity.json").stat().st_size
        self.assertGreaterEqual(total - legacy_total, identity_size, "the identity file is charged to the budget")
        for name, budget in (("tight", total - 1), ("legacy-budget", legacy_total)):
            with self.subTest(name), self.assertRaisesRegex(WarmPlanError, "budget"):
                self.plan(name, identity=self.record, max_write_bytes=budget)
            self.assert_nothing_written(self.root / name)
        self.plan("exact", identity=self.record, max_write_bytes=total)

    # ------------------------------------------------------------------ verifier

    def render(self, output):
        """Synthetic captured results for every job, then the prepare COMPLETE copied as PLAN-COMPLETE.json."""
        plan = json.loads((output / "plan.json").read_bytes())["jobs"]
        jobs = json.loads((output / "jobs.json").read_bytes())["jobs"]
        for job, info in zip(plan, jobs):
            geometry = output / "results" / job["id"] / "geometry"
            geometry.mkdir(parents=True)
            (geometry / "atlas.png").write_bytes(PNG)
            layout_raw = json.dumps(layout(PNG)).encode()
            (geometry / "atlas-layout.json").write_bytes(layout_raw)
            chunks = []
            for x, z in info["presentCoreChunks"]:
                mesh = {"positions": [x * 16, 64, z * 16, x * 16 + 1, 64, z * 16, x * 16, 64, z * 16 + 1],
                        "uvs": [0, 0, 1, 0, 0, 1], "colors": [1] * 9, "indices": [0, 1, 2]}
                (geometry / f"chunk_{x}_{z}.json").write_bytes(json.dumps(mesh).encode())
                chunks.append({"file": f"chunk_{x}_{z}.json", "x": x, "z": z})
            core = info["core"]
            manifest = json.dumps({"format": 1, "snapshotFormat": "UIE2", "snapshotSha256": job["snapshotSha256"],
                                   "atlas": "atlas.png", "atlasLayout": "atlas-layout.json",
                                   "atlasLayoutSha256": sha(layout_raw), "bakedLighting": True, "chunks": chunks,
                                   "bounds": {"min": [core["x"], 0, core["z"]], "max": [core["x"] + 96, 256, core["z"] + 96]},
                                   "coverage": {"renderedBlocks": 1, "unsupportedBlocks": 0, "unsupportedNames": []}}).encode()
            (geometry / "manifest.json").write_bytes(manifest)
            (geometry.parent / "receipt.json").write_bytes(json.dumps({
                "format": "UIE_WARM_RESULT1", "id": job["id"], "snapshotSha256": job["snapshotSha256"],
                "state": "captured", "manifestSha256": sha(manifest), "elapsedSeconds": 1.0}).encode())
        shutil.copyfile(output / "COMPLETE", output / "PLAN-COMPLETE.json")
        return sha((output / "plan.json").read_bytes())

    def remark(self, output, relative, raw=None, drop=False):
        """Rewrite a tracked file (optional) and re-hash it in PLAN-COMPLETE.json, so only the binding can fail."""
        marker = json.loads((output / "PLAN-COMPLETE.json").read_bytes())
        if drop:
            del marker["files"][relative]
        else:
            if raw is not None:
                (output / relative).write_bytes(raw)
            marker["files"][relative] = sha((output / relative).read_bytes())
        (output / "PLAN-COMPLETE.json").write_bytes(json.dumps(marker).encode())

    def test_verifier_accepts_both_modes_and_fails_closed_on_the_binding(self):
        legacy = self.plan("legacy")
        report = verify_rendered_plan(legacy, expected_plan_sha256=self.render(legacy))
        self.assertTrue(report["verified"])
        self.assertEqual((report["pipelineKeySource"], report["pipelineIdentitySha256"]), ("caller-assertion", None))
        (legacy / "pipeline-identity.json").write_text(json.dumps(self.record))
        with self.assertRaisesRegex(RenderVerificationError, "caller-assertion"):
            verify_rendered_plan(legacy, expected_plan_sha256=sha((legacy / "plan.json").read_bytes()))

        template = self.plan("template", identity=self.record)
        plan_sha = self.render(template)
        report = verify_rendered_plan(template, expected_plan_sha256=plan_sha)
        self.assertTrue(report["verified"])
        self.assertEqual(report["pipelineKeySource"], "declared-inputs-sha256")
        self.assertEqual(report["pipelineKey"], self.key)
        self.assertEqual(report["pipelineIdentitySha256"], sha((template / "pipeline-identity.json").read_bytes()))

        def jobs_change(change):
            def apply(output):
                jobs = json.loads((output / "jobs.json").read_bytes())
                change(jobs)
                self.remark(output, "jobs.json", json.dumps(jobs).encode())
            return apply

        def tampered_record(output):
            record = json.loads((output / "pipeline-identity.json").read_bytes())
            record["identity"]["options"]["java"] = "17"   # pipelineKey left as it was
            raw = json.dumps(record).encode()
            self.remark(output, "pipeline-identity.json", raw)
            jobs_change(lambda j: j["pipelineIdentity"].update(sha256=sha(raw)))(output)

        cases = {
            "identity file edited, marker re-hashed": lambda o: self.remark(
                o, "pipeline-identity.json", (o / "pipeline-identity.json").read_bytes() + b" "),
            "identity untracked by the marker": lambda o: self.remark(o, "pipeline-identity.json", drop=True),
            "jobs key differs from the record": jobs_change(lambda j: j.update(pipelineKey="d" * 64)),
            "caller-assertion with a tracked identity": jobs_change(
                lambda j: (j.update(pipelineKeySource="caller-assertion"), j.pop("pipelineIdentity"))),
            "declared source without a binding": jobs_change(lambda j: j.pop("pipelineIdentity")),
            "unknown source": jobs_change(lambda j: j.update(pipelineKeySource="guess")),
            "record hash no longer canonical": tampered_record,
        }
        for i, (label, tamper) in enumerate(cases.items()):
            with self.subTest(label):
                output = self.root / f"case{i}"
                shutil.copytree(template, output)
                tamper(output)
                with self.assertRaises(RenderVerificationError):
                    verify_rendered_plan(output, expected_plan_sha256=plan_sha)


if __name__ == "__main__":
    unittest.main()
