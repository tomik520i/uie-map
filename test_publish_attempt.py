"""Tests for publish_attempt: real SQLite queue, real prepare_warm_plan output with a declared identity,
worker-shaped attempt files (VERIFIED.json/state.json written with warm_worker's own helpers) and synthetic
GeometryCapture-shaped results. Real publish_generation, batch_block_info and staged-source validators.
No Minecraft. Linux only."""
import collections
import gzip
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

LINUX = sys.platform.startswith("linux") and hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
sha = lambda raw: hashlib.sha256(raw).hexdigest()
STAMP = "2026-10-01T12:00:00Z"


def make_staged(directory):
    """A complete UIE_STAGED_SOURCE1 sidecar (the schema publish_generation.staged_source_record checks, as in
    test_publication_source_policy), bound to the batch's real COMPLETE file hashes."""
    complete = json.loads((directory / "COMPLETE").read_bytes())
    snapshot = sha((directory / "snapshot.bin").read_bytes())
    sidecar = {"format": "UIE_STAGED_SOURCE1",
               "policy": {"acceptedConsistency": "observed-stable-files", "atomicWorldSnapshot": False,
                          "purpose": "best-effort map export explicitly accepted by the caller"},
               "warning": "best-effort map export; not a consistent world snapshot",
               "tile": {"x": 0, "z": 0},
               "stagedTile": {"consistency": "observed-stable-files", "atomicWorldSnapshot": False,
                              "sourceInterval": {"startUtc": "2026-09-30T10:00:00Z", "endUtc": "2026-09-30T10:00:05Z"}},
               "batch": {"snapshotSha256": snapshot,
                         "files": {name: sha((directory / name).read_bytes())
                                   for name in ("snapshot.bin", "snapshot.tiles.json", "provenance.json")}}}
    raw = json.dumps(sidecar, indent=2).encode()
    assert sidecar["batch"]["files"]["snapshot.bin"] == complete["files"]["snapshot.bin"] == snapshot
    (directory / "staged-source.json").write_bytes(raw)
    complete["stagedSource"] = {"file": "staged-source.json", "sha256": sha(raw)}
    (directory / "COMPLETE").write_bytes(json.dumps(complete).encode())


def same_length_mesh_change(root):
    """Change one coordinate of one mesh to another valid value of the same byte length."""
    path = next(p for p in sorted(root.rglob("chunk_*.json")) if "blocks" not in p.parts)
    raw = path.read_bytes()
    changed = raw.replace(b", 64, ", b", 65, ", 1)
    assert changed != raw and len(changed) == len(raw)
    path.write_bytes(changed)


def tree(root):
    return {str(p.relative_to(root)): sha(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


@unittest.skipUnless(LINUX, "publication of worker attempts needs Linux (flock, owned_process)")
class PublishAttemptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import capture_batch_file as cbf
        from pipeline_identity import create_pipeline_identity
        from prepare_warm_plan import prepare_warm_plan
        from test_atlas_identity import layout, png
        from test_prepare_warm_plan import build_world
        cls._temp = tempfile.TemporaryDirectory()
        base = Path(cls._temp.name).resolve()
        level, region = build_world(base / "world")
        (base / "batches").mkdir()
        batches = []
        for name, tile in (("A", (-1, -1)), ("B", (0, -1))):
            cbf.create_batch(level, region, *cbf.tile_areas(*tile), base / "batches" / name)
            batches.append(str(base / "batches" / name))
        staged = []
        for name in ("A", "B"):
            directory = base / "batches" / f"{name}-staged"
            shutil.copytree(base / "batches" / name, directory)
            make_staged(directory)
            staged.append(str(directory))
        (base / "declared").mkdir()
        (base / "declared" / "exporter.jar").write_bytes(b"exporter")
        record = create_pipeline_identity({}, {"exporter": base / "declared" / "exporter.jar"}, {"java": "8"})
        cls.key = record["pipelineKey"]
        cls.templates = {}
        # Single-tile plans for area updates (test_area_carry_forward.make_staged: chosen source intervals).
        from test_area_carry_forward import EARLY, LATE, make_staged as staged_at
        level2, region2 = build_world(base / "world2", missing={(-5, -5)})
        updates = {}
        for name, (lvl, reg, tile, interval) in {"u-A": (level, region, (-1, -1), EARLY),
                                                 "u-B": (level, region, (0, -1), EARLY),
                                                 "u-A2": (level2, region2, (-1, -1), LATE),
                                                 "u-A2-early": (level2, region2, (-1, -1), EARLY)}.items():
            cbf.create_batch(lvl, reg, *cbf.tile_areas(*tile), base / "batches" / name)
            staged_at(base / "batches" / name, interval)
            updates[name] = [str(base / "batches" / name)]
        for kind, inputs in (("plain", batches), ("staged", staged), *updates.items()):
            cls.templates[kind] = base / f"template-{kind}"
            prepare_warm_plan(inputs, cls.templates[kind], world="uie", dimension="0", pipeline_key=cls.key,
                              min_free_bytes=0, pipeline_identity=record)
        cls.atlas = png(32, 32)
        cls.layout = json.dumps(layout(cls.atlas)).encode()

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        import owned_process as op
        import publish_attempt as pa
        import warm_worker as ww
        self.op, self.pa, self.ww = op, pa, ww
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()
        for name in ("attempts", "store", "work", "ledger", "status", "other"):
            (self.dir / name).mkdir()
        self.use("plain")

    def use(self, kind):
        """A fresh queue holding the one job of the chosen prepared plan."""
        from render_queue import RenderQueue
        self.template = self.templates[kind]
        self.plan_sha = sha((self.template / "plan.json").read_bytes())
        self.job_id = sha((self.template / "COMPLETE").read_bytes())
        self.queue = RenderQueue(self.dir / f"queue-{kind}.db", "uie", "0", self.key, max_attempts=3)
        self.addCleanup(self.queue.close)
        self.queue.enqueue([{"id": self.job_id, "payload": {"planSha256": self.plan_sha}}])

    # ------------------------------------------------------------------ fixtures

    def render(self, plan, generated_at, unsupported):
        jobs = json.loads((plan / "jobs.json").read_bytes())["jobs"]
        for job, info in zip(json.loads((plan / "plan.json").read_bytes())["jobs"], jobs):
            geometry = plan / "results" / job["id"] / "geometry"
            geometry.mkdir(parents=True)
            (geometry / "atlas.png").write_bytes(self.atlas)
            (geometry / "atlas-layout.json").write_bytes(self.layout)
            chunks = []
            for x, z in info["presentCoreChunks"]:
                mesh = {"positions": [x * 16, 64, z * 16, x * 16 + 1, 64, z * 16, x * 16, 64, z * 16 + 1],
                        "uvs": [0, 0, 1, 0, 0, 1], "colors": [1] * 9, "indices": [0, 1, 2]}
                (geometry / f"chunk_{x}_{z}.json").write_bytes(json.dumps(mesh).encode())
                chunks.append({"file": f"chunk_{x}_{z}.json", "x": x, "z": z})
            core = info["core"]
            manifest = {"format": 1, "snapshotFormat": "UIE2", "snapshotSha256": job["snapshotSha256"],
                        "atlas": "atlas.png", "atlasLayout": "atlas-layout.json",
                        "atlasLayoutSha256": sha(self.layout), "bakedLighting": True, "chunks": chunks,
                        "bounds": {"min": [core["x"], 0, core["z"]], "max": [core["x"] + 96, 256, core["z"] + 96]},
                        "coverage": {"renderedBlocks": 10, "unsupportedBlocks": unsupported,
                                     "unsupportedNames": ["mod:x"] if unsupported else []}}
            if generated_at is not None:
                manifest["generatedAt"] = generated_at
            raw = json.dumps(manifest).encode()
            (geometry / "manifest.json").write_bytes(raw)
            (geometry.parent / "receipt.json").write_bytes(json.dumps({
                "format": "UIE_WARM_RESULT1", "id": job["id"], "snapshotSha256": job["snapshotSha256"],
                "state": "captured", "manifestSha256": sha(raw), "elapsedSeconds": 0.5}).encode())

    def attempt(self, generated_at=STAMP, unsupported=0, scope=None, state_change=None, verified_change=None,
                allow_unsupported=False):
        """What warm_worker leaves after a verified success, with the queue job completed by its result.
        state_change/verified_change edit the documents before they are written; the queue result binds
        whatever VERIFIED.json bytes were written."""
        import verify_rendered_plan as vrp
        claim = self.queue.claim(time.time())
        name = f"{claim['id']}-{claim['attempt']}-{claim['token']}"
        attempt = self.dir / "attempts" / name
        plan = attempt / "plan"
        attempt.mkdir()
        shutil.copytree(self.template, plan)
        shutil.copyfile(plan / "COMPLETE", plan / "PLAN-COMPLETE.json")
        self.render(plan, generated_at, unsupported)
        report = vrp.verify_rendered_plan(plan, expected_plan_sha256=self.plan_sha, marker_name="PLAN-COMPLETE.json",
                                          allow_unsupported=allow_unsupported)
        verified = {"format": self.ww.VERIFIED_FORMAT, "jobId": claim["id"], "attempt": claim["attempt"],
                    "planSha256": self.plan_sha, "pipelineKey": self.key, "report": report}
        if verified_change:
            verified_change(verified)
        raw = self.ww._write_json(attempt / "VERIFIED.json", verified)
        state = {"format": self.ww.STATE_FORMAT, "jobId": claim["id"], "attempt": claim["attempt"],
                 "token": claim["token"], "queue": {"worldId": "uie", "dimension": "0", "pipelineKey": self.key},
                 "phase": "completed", "scope": scope, "planDir": "plan", "planSha256": self.plan_sha, "error": None}
        if state_change:
            state_change(state)
        self.ww._write_json(attempt / "state.json", state)
        self.queue.complete(claim["id"], claim["token"], {"attempt": name, "verifiedSha256": sha(raw),
                                                          "planSha256": self.plan_sha, "pipelineKey": self.key})
        return attempt

    def publish(self, **kwargs):
        kwargs.setdefault("store", self.dir / "store")
        kwargs.setdefault("status_file", self.dir / "status" / "render-status.json")
        kwargs.setdefault("min_free_bytes", 0)
        return self.pa.publish_verified_attempt(self.queue, self.dir / "attempts", self.job_id,
                                                work_dir=self.dir / "work", ledger_dir=self.dir / "ledger", **kwargs)

    def assert_untouched(self):
        self.assertEqual(os.listdir(self.dir / "store"), [], "store unchanged")
        self.assertEqual(os.listdir(self.dir / "work"), [], "own staging removed")
        self.assertEqual([p for p in os.listdir(self.dir / "ledger") if p.endswith(".json")], [], "no ledger record")

    def status(self):
        return json.loads((self.dir / "status" / "render-status.json").read_text())

    def ledger_path(self):
        return self.dir / "ledger" / f"{self.queue.list_jobs()[0]['result']['verifiedSha256']}.json"

    # ------------------------------------------------------------------ success and repeat

    def test_publishes_area_into_empty_candidate_and_repeat_is_proven_not_republished(self):
        attempt = self.attempt()
        before, job_before = tree(attempt), self.queue.list_jobs()
        result = self.publish()
        self.assertEqual(result["outcome"], "published", result)
        self.assertLessEqual(result["stageBytes"], result["stageBound"], "staging stayed within its preflight bound")
        jobs = json.loads((attempt / "plan" / "jobs.json").read_bytes())["jobs"]
        expected = {tuple(c) for info in jobs for c in info["presentCoreChunks"]}
        self.assertEqual(result["chunks"], len(expected))
        self.assertEqual(result["payloadsCompared"], 2 * len(expected), "every mesh and block payload")
        store = self.dir / "store"
        pointer = json.loads((store / "current.json").read_bytes())
        generation = store / "generations" / pointer["generation"]
        manifest = json.loads((generation / "manifest.json").read_bytes())
        self.assertEqual({(c["x"], c["z"]) for c in manifest["chunks"]}, expected)
        for entry in manifest["chunks"][:3]:   # independent spot check against the attempt's own mesh bytes
            job = next(i for i in jobs if [entry["x"], entry["z"]] in i["presentCoreChunks"])
            source = attempt / "plan" / "results" / job["id"] / "geometry" / f"chunk_{entry['x']}_{entry['z']}.json"
            self.assertEqual(gzip.decompress((generation / (entry["file"] + ".gz")).read_bytes()), source.read_bytes())
        self.assertEqual(tree(attempt), before, "the verified attempt is unchanged")
        self.assertEqual(self.queue.list_jobs(), job_before, "the queue is unchanged")
        self.assertEqual(os.listdir(self.dir / "work"), [])
        ledger = json.loads(self.ledger_path().read_bytes())
        self.assertEqual((ledger["phase"], ledger["generation"]), ("published", pointer["generation"]))
        status = self.status()
        self.assertEqual((status["state"], status["completedBatches"], status["totalBatches"]), ("idle", None, None))
        self.assertIn("not the full world", status["message"])
        published_status = dict(status)

        listing = tree(store)
        again = self.publish()
        self.assertEqual((again["outcome"], again["generation"], again["statusRepaired"]),
                         ("already-published", pointer["generation"], False))
        self.assertEqual(again["payloadsCompared"], 2 * len(expected))
        self.assertEqual(tree(store), listing, "nothing new was written")
        self.assertEqual(self.status(), published_status, "a correct status is left alone")

    def test_repeat_repairs_a_stale_status_with_the_recorded_publication_time(self):
        self.attempt()
        self.publish()
        recorded = json.loads(self.ledger_path().read_bytes())["publishedAt"]
        path = self.dir / "status" / "render-status.json"
        path.write_text(json.dumps({"format": "UIE_RENDER_STATUS1", "state": "publishing",
                                    "updatedAt": "2026-10-01T00:00:00Z", "completedBatches": None,
                                    "totalBatches": None, "etaSeconds": None, "lastPublishedAt": None,
                                    "message": "stale after a crash"}))
        self.assertTrue(self.publish()["statusRepaired"])
        status = self.status()
        from datetime import datetime, timezone
        expected = datetime.fromisoformat(recorded).astimezone(timezone.utc).isoformat(timespec="milliseconds")
        self.assertEqual((status["state"], status["lastPublishedAt"]), ("idle", expected.replace("+00:00", "Z")))
        self.assertIn("not the full world", status["message"])

    def test_staged_source_provenance_is_carried_byte_exactly(self):
        self.use("staged")
        attempt = self.attempt()
        before = tree(attempt)
        result = self.publish()
        self.assertEqual(result["outcome"], "published", result)
        root = self.dir / "store" / "generations" / result["generation"]
        policies = json.loads((root / "COMPLETE.json").read_bytes())["sourcePolicies"]
        originals = {sha(p.read_bytes()): p.read_bytes() for p in (attempt / "plan" / "inputs").glob("*/staged-source.json")}
        self.assertEqual(len(policies), 2)
        self.assertEqual({p["sha256"] for p in policies}, set(originals))
        for policy in policies:
            self.assertEqual((root / policy["file"]).read_bytes(), originals[policy["sha256"]])
        self.assertEqual(tree(attempt), before)
        self.assertEqual(self.publish()["outcome"], "already-published", "sidecars are part of the proven output")

    # ------------------------------------------------------------------ refusals before the store changes

    def test_tampered_or_unverified_attempts_are_refused_before_the_store_changes(self):
        def verified(attempt):
            with open(attempt / "VERIFIED.json", "ab") as stream:
                stream.write(b" ")

        def chunk(attempt):
            path = next((attempt / "plan" / "results").glob("*/geometry/chunk_*.json"))
            mesh = json.loads(path.read_bytes())
            mesh["positions"][0] += 1.5
            path.write_bytes(json.dumps(mesh, indent=1).encode())

        cases = (("VERIFIED.json edited", verified, {}, "VERIFIED"),
                 ("chunk edited after verification", chunk, {}, "fresh"),
                 ("unsupported blocks", None, {"unsupported": 2}, "not verified"),
                 ("missing generatedAt", None, {"generated_at": None}, "generatedAt"),
                 ("naive generatedAt", None, {"generated_at": "2026-10-01T12:00:00"}, "generatedAt"),
                 ("state: other attempt", None, {"state_change": lambda s: s.update(attempt=99)}, "attempt, token"),
                 ("state: other token", None, {"state_change": lambda s: s.update(token="0" * 32)}, "attempt, token"),
                 ("VERIFIED: other job", None, {"verified_change": lambda v: v.update(jobId="0" * 64)}, "name this job"),
                 ("VERIFIED: other attempt", None, {"verified_change": lambda v: v.update(attempt=7)}, "name this job"),
                 ("VERIFIED: other plan", None, {"verified_change": lambda v: v.update(planSha256="1" * 64)},
                  "name this job"),
                 ("VERIFIED: other pipeline", None, {"verified_change": lambda v: v.update(pipelineKey="2" * 64)},
                  "name this job"),
                 ("VERIFIED: extra field", None, {"verified_change": lambda v: v.update(note="x")}, "name this job"))
        for label, change, kwargs, pattern in cases:
            with self.subTest(label):
                self.setUp()
                attempt = self.attempt(**kwargs)
                if change:
                    change(attempt)
                with self.assertRaisesRegex(self.pa.PublishAttemptError, pattern):
                    self.publish()
                self.assert_untouched()

    def test_same_size_mesh_change_between_verification_and_copy_is_refused(self):
        self.attempt()
        original = self.pa._stage

        def mutate(plan, stage, jobs):
            same_length_mesh_change(plan / "results")
            return original(plan, stage, jobs)
        with mock.patch.object(self.pa, "_stage", mutate):
            with self.assertRaisesRegex(self.pa.PublishAttemptError, "changed since verification"):
                self.publish()
        self.assert_untouched()

    def test_staged_copies_changed_before_the_publisher_runs_are_refused(self):
        original = self.pa._rebind
        for label, change in (("mesh copy", same_length_mesh_change),
                              ("generated block file",
                               lambda stage: next(stage.rglob("blocks/*/chunk_*.json")).write_bytes(b"{}\n")),
                              ("extra staged file", lambda stage: (next(stage.iterdir()) / "extra.json").write_text("{}"))):
            with self.subTest(label):
                self.setUp()
                self.attempt()

                def tamper(plan, stage, staged, report, plan_sha):
                    change(stage)
                    return original(plan, stage, staged, report, plan_sha)
                with mock.patch.object(self.pa, "_rebind", tamper), \
                        mock.patch.object(self.pa, "publish_generation", side_effect=AssertionError("published")):
                    with self.assertRaisesRegex(self.pa.PublishAttemptError, "before publication"):
                        self.publish()
                self.assert_untouched()
                self.assertFalse((self.dir / "store" / "current.json").exists())

    def test_only_an_empty_candidate_store_and_a_succeeded_job_are_accepted(self):
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "succeeded"):
            self.publish()   # job still queued
        self.attempt()
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "not expected_revision"):
            self.publish(expected_revision="a" * 64)
        (self.dir / "store" / "current.json").write_text("{}")
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "not empty"):
            self.publish()
        self.assertEqual((self.dir / "store" / "current.json").read_text(), "{}", "foreign pointer kept")
        (self.dir / "store" / "current.json").unlink()
        for label, kwargs in (("store inside attempts", {"store": self.dir / "attempts"}),
                              ("status inside store", {"status_file": self.dir / "store" / "s.json"}),
                              ("negative budget", {"stage_max_bytes": -1}),
                              ("no reserve", {"min_free_bytes": None})):
            with self.subTest(label), self.assertRaises(self.pa.PublishAttemptError):
                self.publish(**kwargs)
        self.assert_untouched()

    def test_stage_budget_and_reserve_are_checked_before_anything_is_written(self):
        self.attempt()
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "stage_max_bytes"):
            self.publish(stage_max_bytes=1000)
        self.assert_untouched()
        usage = collections.namedtuple("usage", "total used free")
        with mock.patch.object(self.pa.shutil, "disk_usage", return_value=usage(10 ** 9, 10 ** 9 - 4096, 4096)):
            with self.assertRaisesRegex(self.pa.PublishAttemptError, "reserve"):
                self.publish()
        self.assert_untouched()
        self.assertEqual(self.publish(stage_max_bytes=10 ** 9)["outcome"], "published")

    def test_running_attempt_processes_are_refused(self):
        scope = self.op.create_scope()
        with open(self.dir / "child.log", "wb") as log:
            child = self.op.launch_owned([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.dir,
                                         log=log, scope=scope)
        try:
            until = time.monotonic() + 5
            while not self.op.inspect_owned(scope) and time.monotonic() < until:
                time.sleep(0.02)
            self.attempt(scope=scope)
            with self.assertRaisesRegex(self.pa.PublishAttemptError, "still running"):
                self.publish()
            self.assert_untouched()
        finally:
            self.op.stop_owned(scope, term_timeout=1, kill_timeout=3, process=child)
        self.assertEqual(self.publish()["outcome"], "published", "publishable once the attempt has stopped")

    # ------------------------------------------------------------------ ledger and store binding

    def test_failure_before_the_store_changes_can_be_retried(self):
        self.attempt()
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "store is unchanged"):
            self.publish(max_write_bytes=1000)
        self.assert_untouched()
        self.assertEqual(self.status()["state"], "error")
        self.assertIsNone(self.status()["lastPublishedAt"])
        self.assertEqual(self.publish()["outcome"], "published")

    def test_ledger_write_failure_reports_error_and_publishes_nothing(self):
        self.attempt()
        with mock.patch.object(self.pa.ww, "_write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(self.pa.PublishAttemptError, "nothing was published"):
                self.publish()
        self.assert_untouched()
        self.assertEqual(self.status()["state"], "error")

    def test_interrupted_or_malformed_ledgers_fail_closed(self):
        self.attempt()
        result = self.queue.list_jobs()[0]["result"]
        start = {"format": "UIE_ATTEMPT_PUBLICATION1", "phase": "publishing", "jobId": self.job_id,
                 "attempt": result["attempt"], "verifiedSha256": result["verifiedSha256"],
                 "planSha256": result["planSha256"], "store": str(self.dir / "store"),
                 "startedAt": "2026-10-01T12:00:00Z"}
        for label, record, pattern in (("interrupted", start, "interrupted"),
                                       ("missing field", {k: v for k, v in start.items() if k != "startedAt"}, "malformed"),
                                       ("another attempt", {**start, "verifiedSha256": "3" * 64}, "another attempt"),
                                       ("another store", {**start, "store": str(self.dir / "other")}, "another store")):
            with self.subTest(label):
                self.ledger_path().write_text(json.dumps(record))
                with self.assertRaisesRegex(self.pa.PublishAttemptError, pattern):
                    self.publish()
                self.assertEqual(os.listdir(self.dir / "store"), [])
        self.ledger_path().unlink()
        self.publish()
        published = json.loads(self.ledger_path().read_bytes())
        for label, change, pattern in (
                ("no pointer hash", lambda d: d.pop("pointerSha256"), "malformed"),
                ("no chunk count", lambda d: d.pop("chunks"), "malformed"),
                ("bad chunk count", lambda d: d.update(chunks="72"), "malformed"),
                ("other generation", lambda d: d.update(generation="4" * 64), "does not describe"),
                ("other manifest", lambda d: d.update(manifestSha256="5" * 64), "does not describe"),
                ("other pointer bytes", lambda d: d.update(pointerSha256="6" * 64), "not this attempt")):
            with self.subTest(label):
                doc = json.loads(json.dumps(published))
                change(doc)
                self.ledger_path().write_text(json.dumps(doc))
                with self.assertRaisesRegex(self.pa.PublishAttemptError, pattern):
                    self.publish()
        self.ledger_path().write_text(json.dumps(published))
        pointer = self.dir / "store" / "current.json"
        pointer.write_bytes(pointer.read_bytes() + b"\n")   # no longer the recorded pointer bytes
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "not this attempt"):
            self.publish()

    def forge(self, attempt, record_change):
        """Replace the candidate store with a generation from the SAME staged payloads, published untracked
        (record_change None: an older candidate) or with a changed area record; point the ledger at it exactly."""
        import area_records
        from publish_generation import publish_generation
        plan, raw = attempt / "plan", (attempt / "VERIFIED.json").read_bytes()
        stage = self.dir / "other" / "stage"
        stage.mkdir()
        staged = self.pa._stage(plan, stage, self.pa._plan_jobs(plan, json.loads(raw)["report"]))
        record = None
        if record_change is not None:
            record = area_records.build_area_record(plan, raw, verified_sha256=sha(raw), job_id=self.job_id, attempt=1)
            record_change(record)
        forged = self.dir / "other" / "store"
        pointer = publish_generation(staged["specs"], forged, world="uie", dimension="0", pipeline_key=self.key,
                                     expected_chunks=staged["expected"], compress_chunks=True, min_free_bytes=0,
                                     area_record=record)
        store = self.dir / "store"
        shutil.rmtree(store)
        shutil.copytree(forged, store)
        ledger = json.loads(self.ledger_path().read_bytes())
        ledger.update(generation=pointer["generation"], manifestSha256=pointer["manifestSha256"],
                      pointerSha256=sha((store / "current.json").read_bytes()))
        self.ledger_path().write_text(json.dumps(ledger))

    def test_candidate_carries_the_exact_area_record_and_a_repeat_compares_it(self):
        import area_records
        attempt = self.attempt()
        result = self.publish()
        raw = (attempt / "VERIFIED.json").read_bytes()
        expected = area_records.build_area_record(attempt / "plan", raw, verified_sha256=sha(raw),
                                                  job_id=self.job_id, attempt=1)
        inventory = json.loads((self.dir / "store" / "generations" / result["generation"] / "COMPLETE.json").read_bytes())
        self.assertEqual(inventory["areas"], [expected])
        for label, change, pattern in (("other area record", lambda r: r.update(attempt=2), "not this attempt's record"),
                                       ("older candidate without areas", None, "no area record")):
            with self.subTest(label):
                if label != "other area record":
                    self.setUp()
                    attempt = self.attempt()
                    self.publish()
                self.forge(attempt, change)
                with self.assertRaisesRegex(self.pa.PublishAttemptError, pattern):
                    self.publish()

    def test_accepted_unsupported_blocks_are_published_with_the_same_policy(self):
        import area_records
        attempt = self.attempt(unsupported=2, allow_unsupported=True)
        verified = json.loads((attempt / "VERIFIED.json").read_bytes())
        self.assertEqual(verified["report"]["unsupportedPolicy"], "allowed")
        result = self.publish()
        self.assertEqual(result["outcome"], "published", result)
        inventory = json.loads((self.dir / "store" / "generations" / result["generation"] / "COMPLETE.json").read_bytes())
        area = inventory["areas"][0]
        self.assertEqual(area["aggregate"]["coverage"]["unsupportedBlocks"], 4, "still counted (2 per tile)")
        self.assertEqual(area["aggregate"]["coverage"]["unsupportedNames"], ["mod:x"])
        manifest = json.loads((self.dir / "store" / "generations" / result["generation"] / "manifest.json").read_bytes())
        self.assertEqual(manifest["coverage"]["unsupportedBlocks"], 4)
        self.assertEqual(self.publish()["outcome"], "already-published")
        # A VERIFIED.json that silently drops the policy no longer matches the fresh re-verification.
        self.setUp()
        self.attempt(unsupported=2, allow_unsupported=True,
                     verified_change=lambda v: v["report"].pop("unsupportedPolicy"))
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "not verified or differs"):
            self.publish()
        self.assert_untouched()

    def test_replace_mode_needs_full_coverage_and_keeps_the_old_generation(self):
        store = self.dir / "store"
        self.attempt()
        old = self.publish()["generation"]                      # 72 chunks (tiles A and B), e.g. an untracked LIVE map
        self.use("u-A")
        self.attempt()
        before = tree(store)
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "smaller cut never replaces"):
            self.publish(expected_revision=old, replace=True)   # 36 chunks would shrink the map
        self.assertEqual(tree(store), before)
        self.assertFalse(self.ledger_path().exists())
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "needs expected_revision"):
            self.publish(replace=True)
        self.use("staged")
        self.attempt()
        (store / "publication-audits").mkdir()                   # other records of a LIVE store are tolerated
        new = self.publish(expected_revision=old, replace=True)
        self.assertEqual([new["outcome"], new["chunks"]], ["published", 72])
        self.assertEqual(json.loads((store / "current.json").read_bytes())["generation"], new["generation"])
        self.assertEqual(sorted(os.listdir(store / "generations")), sorted([old, new["generation"]]), "old kept")
        inventory = json.loads((store / "generations" / new["generation"] / "COMPLETE.json").read_bytes())
        self.assertEqual(len(inventory["areas"]), 1)
        self.assertNotIn("areaBase", inventory)
        repeat = self.publish(expected_revision=old, replace=True)
        self.assertEqual([repeat["outcome"], repeat["generation"]], ["already-published", new["generation"]])

    def test_area_update_carries_unchanged_tiles_and_replaces_only_later_whole_tiles(self):
        store = self.dir / "store"
        def inventory(generation):
            return json.loads((store / "generations" / generation / "COMPLETE.json").read_bytes())
        self.use("u-A")
        self.attempt()
        g1 = self.publish()["generation"]
        self.use("u-B")
        self.attempt()
        added = self.publish(expected_revision=g1)
        self.assertEqual([added["outcome"], added["chunks"]], ["published", 72])
        g2 = added["generation"]
        inv1, inv2 = inventory(g1), inventory(g2)
        self.assertEqual(inv2["areas"][0], inv1["areas"][0], "base record kept verbatim")
        self.assertEqual([len(inv2["areas"]), inv2["supersededTiles"], inv2["areaBase"]], [2, [], g1])
        self.assertTrue(all(inv2["files"][n] == h for n, h in inv1["files"].items() if n.startswith(("chunks/", "blocks/"))))
        repeat = self.publish(expected_revision=g1)
        self.assertEqual([repeat["outcome"], repeat["generation"], repeat["chunks"]], ["already-published", g2, 72])
        self.assertEqual(repeat["payloadsCompared"], 144)

        self.use("u-A2-early")
        self.attempt()
        before = tree(store)
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "not observed after"):
            self.publish(expected_revision=g2)
        self.assertEqual(tree(store), before)
        self.assertFalse(self.ledger_path().exists(), "refused before any ledger record")

        self.use("u-A2")
        self.attempt()
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "not expected_revision"):
            self.publish(expected_revision=g1)   # stale base
        self.assertEqual(tree(store), before)
        replaced = self.publish(expected_revision=g2)
        self.assertEqual([replaced["outcome"], replaced["chunks"]], ["published", 71], "deleted chunk removed")
        inv3 = inventory(replaced["generation"])
        self.assertEqual(inv3["areas"], [inv2["areas"][1], inv3["areas"][1]], "A's record dropped, B carried")
        self.assertEqual([inv3["supersededTiles"], inv3["areaBase"]], [[], g2])
        self.assertEqual(json.loads((store / "current.json").read_bytes())["generation"], replaced["generation"])
        self.assertEqual(sorted(os.listdir(store / "generations")), sorted([g1, g2, replaced["generation"]]),
                         "older generations are kept, never deleted")

    def test_a_store_with_the_same_chunks_but_different_geometry_is_not_accepted_as_published(self):
        # Publish the real attempt, then replace the store with a generation built from the same plan, chunk set,
        # snapshots and pipeline whose one mesh differs, and point the ledger at it byte-exactly.
        from batch_block_info import attach_batch_block_info
        from publish_generation import publish_generation
        attempt = self.attempt()
        self.publish()
        plan = attempt / "plan"
        forged_inputs, specs, chunks = self.dir / "other" / "inputs", [], []
        jobs = json.loads((plan / "jobs.json").read_bytes())["jobs"]
        for job, info in zip(json.loads((plan / "plan.json").read_bytes())["jobs"], jobs):
            target = forged_inputs / job["id"]
            shutil.copytree(plan / "results" / job["id"] / "geometry", target)
            (target / "atlas-layout.json").unlink()
            attach_batch_block_info(plan / job["input"], target / "manifest.json")
            specs.append({"geometry": str(target), "snapshotSha256": job["snapshotSha256"],
                          "coreChunks": info["presentCoreChunks"]})
            chunks += info["presentCoreChunks"]
        same_length_mesh_change(forged_inputs)
        forged_store = self.dir / "other" / "store"
        pointer = publish_generation(specs, forged_store, world="uie", dimension="0", pipeline_key=self.key,
                                     expected_chunks=chunks, compress_chunks=True, min_free_bytes=0)
        store = self.dir / "store"
        shutil.rmtree(store)
        shutil.copytree(forged_store, store)
        ledger = json.loads(self.ledger_path().read_bytes())
        ledger.update(generation=pointer["generation"], manifestSha256=pointer["manifestSha256"],
                      pointerSha256=sha((store / "current.json").read_bytes()))
        self.ledger_path().write_text(json.dumps(ledger))
        with self.assertRaisesRegex(self.pa.PublishAttemptError, "not this attempt's payload"):
            self.publish()


if __name__ == "__main__":
    unittest.main()
