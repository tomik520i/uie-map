"""Tests for verify_rendered_plan. Stdlib only. A real two-tile plan (prepare_warm_plan over real UIE2
batches) gets synthetic WarmBatch receipts and GeometryCapture-shaped geometry with a bound atlas."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import atlas_identity
import capture_batch_file as cbf
import prepare_warm_plan as pwp
import verify_rendered_plan as vrp
from prepare_warm_plan import prepare_warm_plan
from test_atlas_identity import layout, png
from test_prepare_warm_plan import build_world
from verify_rendered_plan import RenderVerificationError, verify_rendered_plan

sha = lambda raw: hashlib.sha256(raw).hexdigest()
PNG = png(32, 32)
KEY = "c" * 64


def mesh(x, z, transparent=False):
    doc = {"positions": [x * 16, 64, z * 16, x * 16 + 1, 64, z * 16, x * 16, 64, z * 16 + 1],
           "uvs": [0, 0, 1, 0, 0, 1], "colors": [1] * 9, "indices": [0, 1, 2]}
    if transparent:
        doc["transparentParts"] = [{**doc, "colors": [0.5] * 9}]
    return doc


class RenderedPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        base = Path(cls._temp.name).resolve()
        level, region = build_world(base / "world")
        (base / "batches").mkdir()
        batches = []
        for name, tile in (("A", (-1, -1)), ("B", (0, -1))):
            cbf.create_batch(level, region, *cbf.tile_areas(*tile), base / "batches" / name)
            batches.append(str(base / "batches" / name))
        cls.template = base / "plan"
        prepare_warm_plan(batches, cls.template, world="uie", dimension="0", pipeline_key=KEY, min_free_bytes=0)
        shutil.copyfile(cls.template / "COMPLETE", cls.template / "PLAN-COMPLETE.json")

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "plan"
        shutil.copytree(self.template, self.root)
        self.plan_sha = sha((self.root / "plan.json").read_bytes())
        self.plan = json.loads((self.root / "plan.json").read_bytes())["jobs"]
        self.jobs = json.loads((self.root / "jobs.json").read_bytes())["jobs"]

    # ------------------------------------------------------------------ fixture render

    def render(self, atlas_for=lambda i: PNG, lighting_for=lambda i: True, unsupported_for=lambda i: 0):
        for i, (job, info) in enumerate(zip(self.plan, self.jobs)):
            geometry = self.root / "results" / job["id"] / "geometry"
            geometry.mkdir(parents=True)
            atlas = atlas_for(i)
            (geometry / "atlas.png").write_bytes(atlas)
            layout_raw = json.dumps(layout(atlas)).encode()
            (geometry / "atlas-layout.json").write_bytes(layout_raw)
            chunks = []
            for n, (x, z) in enumerate(info["presentCoreChunks"]):
                name = f"chunk_{x}_{z}.json"
                (geometry / name).write_bytes(json.dumps(mesh(x, z, transparent=(i == 0 and n == 0))).encode())
                chunks.append({"file": name, "x": x, "z": z})
            core = info["core"]
            unsupported = unsupported_for(i)
            manifest = {"format": 1, "title": "UIE", "sample": True, "generatedAt": "2026-09-30T12:00:00Z",
                        "atlas": "atlas.png", "chunks": chunks, "bakedLighting": lighting_for(i),
                        "atlasLayout": "atlas-layout.json", "atlasLayoutSha256": sha(layout_raw),
                        "snapshotFormat": "UIE2", "snapshotSha256": job["snapshotSha256"],
                        "focus": [core["x"] + 48.0, 64, core["z"] + 48.0],
                        "bounds": {"min": [core["x"], 0, core["z"]], "max": [core["x"] + 96, 256, core["z"] + 96]},
                        "coverage": {"renderedBlocks": 100, "unsupportedBlocks": unsupported,
                                     "unsupportedNames": ["mod:thing"] if unsupported else [], "emptyBlocks": 5}}
            (geometry / "unsupported.json").write_text("{}")
            (geometry / "empty.json").write_text("{}")
            self.write_manifest(i, manifest)
        return self

    def write_manifest(self, i, manifest, receipt_change=None):
        job = self.plan[i]
        raw = json.dumps(manifest).encode()
        (self.root / "results" / job["id"] / "geometry" / "manifest.json").write_bytes(raw)
        receipt = {"format": "UIE_WARM_RESULT1", "id": job["id"], "snapshotSha256": job["snapshotSha256"],
                   "state": "captured", "completed": i, "total": len(self.plan), "manifestSha256": sha(raw),
                   "elapsedSeconds": 1.5}
        if receipt_change:
            receipt_change(receipt)
        (self.root / "results" / job["id"] / "receipt.json").write_bytes(json.dumps(receipt).encode())

    def rewrite_plan(self, change_plan=None, change_jobs=None, extra_files=()):
        """Rewrite plan.json/jobs.json and keep the marker and the expected plan SHA consistent with them."""
        marker_path = self.root / "PLAN-COMPLETE.json"
        marker = json.loads(marker_path.read_bytes())
        for name, change in (("plan.json", change_plan), ("jobs.json", change_jobs)):
            doc = json.loads((self.root / name).read_bytes())
            if change:
                change(doc)
            raw = json.dumps(doc, indent=2).encode()
            (self.root / name).write_bytes(raw)
            marker["files"][name] = sha(raw)
        for relative in extra_files:
            marker["files"][relative] = sha((self.root / relative).read_bytes())
        marker_path.write_bytes(json.dumps(marker).encode())
        self.plan_sha = marker["files"]["plan.json"]
        self.plan = json.loads((self.root / "plan.json").read_bytes())["jobs"]
        self.jobs = json.loads((self.root / "jobs.json").read_bytes())["jobs"]

    def manifest(self, i):
        return json.loads((self.geometry(i) / "manifest.json").read_bytes())

    def geometry(self, i):
        return self.root / "results" / self.plan[i]["id"] / "geometry"

    def verify(self, **kwargs):
        kwargs.setdefault("expected_plan_sha256", self.plan_sha)
        return verify_rendered_plan(self.root, **kwargs)

    def rejects(self, pattern=None):
        with self.assertRaisesRegex(RenderVerificationError, pattern or ""):
            self.verify()

    # ------------------------------------------------------------------ tests

    def test_report_binds_mesh_bytes_even_when_size_and_triangle_count_stay_equal(self):
        self.render()
        before = self.verify()
        name = self.manifest(0)["chunks"][0]["file"]
        path = self.geometry(0) / name
        raw = path.read_bytes()
        changed = raw.replace(b"64", b"65", 1)
        self.assertNotEqual(raw, changed)
        self.assertEqual(len(raw), len(changed))
        path.write_bytes(changed)
        after = self.verify()
        self.assertEqual(before["triangles"], after["triangles"])
        self.assertEqual(before["meshBytes"], after["meshBytes"])
        self.assertNotEqual(before, after)
        self.assertEqual(before["perJob"][0]["geometryFiles"][name], sha(raw))
        self.assertEqual(after["perJob"][0]["geometryFiles"][name], sha(changed))

    def test_complete_render_is_verified_with_honest_summary(self):
        self.render()
        report = self.verify()
        self.assertTrue(report["verified"])
        self.assertEqual(report["reasons"], [])
        self.assertEqual((report["jobs"], report["coreChunks"]), (2, 72))
        self.assertEqual(report["triangles"], 73, "one chunk has an extra transparent triangle")
        mesh_bytes = sum(p.stat().st_size for p in self.root.glob("results/*/geometry/chunk_*.json"))
        self.assertEqual(report["meshBytes"], mesh_bytes)
        self.assertEqual(report["elapsedSeconds"], 3.0)
        self.assertEqual(report["atlasSha256"], sha(PNG))
        loaded, _ = atlas_identity.load_layout(self.geometry(0) / "atlas.png", self.geometry(0) / "atlas-layout.json")
        self.assertEqual(report["layoutIdentitySha256"], atlas_identity.layout_identity(loaded))
        self.assertIs(report["bakedLighting"], True)
        self.assertEqual(report["coverage"], {"renderedBlocks": 200, "unsupportedBlocks": 0, "emptyBlocks": 10,
                                              "unsupportedNames": []})
        self.assertEqual([j["sourcePolicy"] for j in report["perJob"]], [j["sourcePolicy"] for j in self.jobs])
        self.assertIn("globally coherent", report["note"])

    def test_plan_marker_and_input_tampering_is_rejected(self):
        self.render()
        with self.assertRaisesRegex(RenderVerificationError, "expected plan"):
            self.verify(expected_plan_sha256="0" * 64)
        jobs = self.root / "jobs.json"
        original = jobs.read_bytes()
        jobs.write_bytes(original + b" ")
        self.rejects("jobs.json does not match")
        jobs.write_bytes(original)
        snapshot = self.root / self.plan[0]["input"]
        raw = bytearray(snapshot.read_bytes())
        raw[-1] ^= 1
        snapshot.write_bytes(bytes(raw))
        self.rejects("does not match")

    def test_chunk_set_and_mesh_errors_are_rejected(self):
        def halo(m):
            m["chunks"].append({"file": "chunk_-7_-3.json", "x": -7, "z": -3})
            (self.geometry(0) / "chunk_-7_-3.json").write_bytes(json.dumps(mesh(-7, -3)).encode())
        cases = {
            "missing chunk": (lambda m: m["chunks"].pop(), "missing"),
            "halo chunk": (halo, "extra"),
            "duplicate chunk": (lambda m: m["chunks"].append(dict(m["chunks"][0])), "duplicate"),
            "wrong snapshot": (lambda m: m.update(snapshotSha256="1" * 64), "UIE2 snapshot"),
            "uie1 manifest": (lambda m: m.update(snapshotFormat="UIE1"), "UIE2 snapshot"),
            "lighting string": (lambda m: m.update(bakedLighting="yes"), "bakedLighting"),
            "negative coverage": (lambda m: m["coverage"].update(renderedBlocks=-1), "coverage"),
            "reversed bounds": (lambda m: m["bounds"].update(min=[500, 0, 0]), "bounds"),
            "layout hash": (lambda m: m.update(atlasLayoutSha256="2" * 64), "atlasLayoutSha256"),
            "no layout": (lambda m: m.pop("atlasLayout"), "atlasLayout"),
        }
        for label, (change, pattern) in cases.items():
            with self.subTest(label):
                self.setUp()
                self.render()
                m = self.manifest(0)
                change(m)
                self.write_manifest(0, m)
                self.rejects(pattern)
        self.setUp()
        self.render()
        first = self.manifest(0)["chunks"][0]["file"]
        (self.geometry(0) / first).unlink()
        self.rejects("missing")
        self.setUp()
        self.render()
        (self.geometry(1) / first.replace("-", "9")).write_text("{}")  # unlisted extra file
        self.rejects("unexpected files")
        self.setUp()
        self.render()
        bad = mesh(-6, -6)
        bad["indices"] = [0, 1, 5]
        (self.geometry(0) / self.manifest(0)["chunks"][0]["file"]).write_bytes(json.dumps(bad).encode())
        self.rejects("indices")

    def test_mixed_atlas_and_lighting_are_rejected(self):
        self.render(atlas_for=lambda i: PNG if i == 0 else png(32, 32, 7))
        self.rejects("mixed atlas PNG")
        self.setUp()
        self.render(lighting_for=lambda i: i == 0)
        self.rejects("mixed bakedLighting")

    def test_incomplete_or_failed_results_are_rejected(self):
        self.render()
        shutil.rmtree(self.root / "results" / self.plan[1]["id"])
        self.rejects("incomplete")
        receipts = {
            "error state": lambda r: r.update(state="error", error="IOException: boom"),
            "other snapshot": lambda r: r.update(snapshotSha256="3" * 64),
            "other id": lambda r: r.update(id="x"),
            "manifest hash": lambda r: r.update(manifestSha256="4" * 64),
            "negative time": lambda r: r.update(elapsedSeconds=-1),
        }
        for label, change in receipts.items():
            with self.subTest(label):
                self.setUp()
                self.render()
                self.write_manifest(0, self.manifest(0), receipt_change=change)
                self.rejects()
        self.setUp()
        self.render()
        (self.root / "results" / "stranger").mkdir()
        self.rejects("not plan jobs")

    def test_unsupported_blocks_make_the_report_unverified_unless_allowed(self):
        self.render(unsupported_for=lambda i: 3 if i == 0 else 0)
        report = self.verify()
        self.assertFalse(report["verified"])
        self.assertEqual(report["reasons"], ["3 unsupported block(s) in the render"])
        self.assertEqual(report["coverage"]["unsupportedNames"], ["mod:thing"])
        self.assertNotIn("unsupportedPolicy", report, "strict reports stay byte-identical")
        allowed = self.verify(allow_unsupported=True)
        self.assertTrue(allowed["verified"])
        self.assertEqual(allowed["unsupportedPolicy"], "allowed", "the policy is recorded for later re-verifications")
        self.assertEqual({k: v for k, v in allowed.items() if k not in ("unsupportedPolicy", "verified", "reasons")},
                         {k: v for k, v in report.items() if k not in ("verified", "reasons")})

    def test_distinct_jobs_with_overlapping_cores_are_rejected(self):
        # A second job id with the same UIE2 input as job 0: every hash, receipt and manifest is consistent,
        # only the two parsed input cores overlap. prepare_warm_plan would refuse this; the verifier must too.
        source, new = self.plan[0]["id"], "overlapping-job"
        shutil.copytree(self.root / "inputs" / source, self.root / "inputs" / new)
        input_path = f"inputs/{new}/snapshot.bin"
        self.rewrite_plan(
            lambda d: d["jobs"].append({**d["jobs"][0], "id": new, "input": input_path}),
            lambda d: d["jobs"].append({**json.loads(json.dumps(d["jobs"][0])), "id": new, "input": input_path}),
            [f"inputs/{new}/{name}" for name in self.jobs[0]["files"]])
        self.render()
        self.assertEqual(len(self.plan), 3)
        self.rejects("overlapping core chunks")

    def test_malformed_arguments_and_plan_ids_are_refused_cleanly(self):
        self.render()
        for bad in ("yes", 1, None):
            with self.subTest(allow_unsupported=bad), self.assertRaisesRegex(RenderVerificationError, "allow_unsupported"):
                self.verify(allow_unsupported=bad)
        # An unhashable id must be a verification error, not a TypeError from set().
        self.rewrite_plan(lambda d: d["jobs"][0].update(id=["not", "a", "string"]))
        self.rejects("job ids")

    def test_non_string_manifest_chunk_file_is_refused(self):
        self.render()
        m = self.manifest(0)
        m["chunks"][0]["file"] = ["chunk.json"]
        self.write_manifest(0, m)
        self.rejects("invalid chunk entry")

    def test_source_policy_must_bind_its_sidecar(self):
        self.render()
        self.rewrite_plan(change_jobs=lambda d: d['jobs'][0].update(sourcePolicy={
            'stagedSource': True, 'stagedSourceSha256': 'a' * 64}))
        self.rejects("source policy sidecar hash differs")

    def test_ore_sidecar_uses_the_prepare_warm_plan_limit(self):
        self.assertEqual(vrp.LIMIT_TILES, pwp.LIMITS["snapshot.tiles.json"])
        self.assertGreater(vrp.LIMIT_TILES, vrp.LIMIT_JSON)
        self.render()
        # The ore sidecar is read with its own limit, not the general JSON limit.
        with mock.patch.object(vrp, "LIMIT_TILES", 10):
            self.rejects("snapshot.tiles.json is larger than 10 bytes")
        self.assertTrue(self.verify()["verified"])

    def test_symlinked_mesh_is_rejected(self):
        self.render()
        target = self.geometry(0) / self.manifest(0)["chunks"][0]["file"]
        outside = self.root.parent / "outside.json"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        try:
            os.symlink(outside, target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        self.rejects("link")


if __name__ == "__main__":
    unittest.main()
