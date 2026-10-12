"""Tests for warm_worker on Linux with a real SQLite RenderQueue, a real prepare_warm_plan output bound to a
real pipeline identity, owned_process and verify_rendered_plan. The capture client is a short Python fixture
child that writes WarmBatch-shaped receipts and GeometryCapture-shaped geometry (it sleeps at most 30 s).
No Minecraft. Stdlib only."""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

LINUX = sys.platform.startswith("linux") and hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
HERE = Path(__file__).resolve().parent
OPTIONS = {"java": "8", "jvm": ["-Xmx4G"]}
sha = lambda raw: hashlib.sha256(raw).hexdigest()

# Fixture capture client: argv = plan_dir fixture_dir mode. Modes: ok, unsupported (write everything, then
# idle until stopped), crash (one receipt, exit 3), hang (nothing, sleep). Files are written atomically.
CHILD = r'''
import hashlib, json, os, sys, time
plan_dir, fixture, mode = sys.argv[1], sys.argv[2], sys.argv[3]
sha = lambda raw: hashlib.sha256(raw).hexdigest()
def put(path, raw):
    with open(path + ".tmp", "wb") as handle:
        handle.write(raw)
    os.replace(path + ".tmp", path)
if mode == "hang":
    time.sleep(30)
    sys.exit(0)
plan = json.load(open(os.path.join(plan_dir, "plan.json")))["jobs"]
jobs = json.load(open(os.path.join(plan_dir, "jobs.json")))["jobs"]
atlas = open(os.path.join(fixture, "atlas.png"), "rb").read()
layout = open(os.path.join(fixture, "atlas-layout.json"), "rb").read()
for job, info in zip(plan, jobs):
    geometry = os.path.join(plan_dir, "results", job["id"], "geometry")
    os.makedirs(geometry)
    put(os.path.join(geometry, "atlas.png"), atlas)
    put(os.path.join(geometry, "atlas-layout.json"), layout)
    chunks = []
    for x, z in info["presentCoreChunks"]:
        mesh = {"positions": [x * 16, 64, z * 16, x * 16 + 1, 64, z * 16, x * 16, 64, z * 16 + 1],
                "uvs": [0, 0, 1, 0, 0, 1], "colors": [1] * 9, "indices": [0, 1, 2]}
        put(os.path.join(geometry, "chunk_%d_%d.json" % (x, z)), json.dumps(mesh).encode())
        chunks.append({"file": "chunk_%d_%d.json" % (x, z), "x": x, "z": z})
    core = info["core"]
    unsupported = 1 if mode == "unsupported" else 0
    manifest = json.dumps({"format": 1, "snapshotFormat": "UIE2", "snapshotSha256": job["snapshotSha256"],
                           "atlas": "atlas.png", "atlasLayout": "atlas-layout.json", "atlasLayoutSha256": sha(layout),
                           "bakedLighting": True, "chunks": chunks,
                           "bounds": {"min": [core["x"], 0, core["z"]], "max": [core["x"] + 96, 256, core["z"] + 96]},
                           "coverage": {"renderedBlocks": 1, "unsupportedBlocks": unsupported,
                                        "unsupportedNames": ["mod:thing"] if unsupported else []}}).encode()
    put(os.path.join(geometry, "manifest.json"), manifest)
    put(os.path.join(plan_dir, "results", job["id"], "receipt.json"), json.dumps({
        "format": "UIE_WARM_RESULT1", "id": job["id"], "snapshotSha256": job["snapshotSha256"], "state": "captured",
        "manifestSha256": sha(manifest), "elapsedSeconds": 0.1}).encode())
    if mode == "crash":
        sys.exit(3)
put(os.path.join(plan_dir, "worker-status.json"), json.dumps({"format": "UIE_WARM_RESULT1", "state": "captured",
    "completed": len(plan), "total": len(plan), "finished": True}).encode())
time.sleep(30)   # like WarmBatch: idle after draining until the supervisor stops it
'''


def staging_prepare(config, current_identity=None, spec_change=None):
    """Test staging adapter: copies the prepared plan and makes a fresh identity scan of the declared tree."""
    from pipeline_identity import create_pipeline_identity

    def prepare(claim, attempt_dir):
        plan = attempt_dir / "plan"
        shutil.copytree(config["template"], plan)
        shutil.copyfile(plan / "COMPLETE", plan / "PLAN-COMPLETE.json")
        (attempt_dir / "client").mkdir()
        declared = Path(config["declared"])
        current = current_identity or create_pipeline_identity(
            {"mods": declared / "mods"}, {"exporter": declared / "exporter.jar"}, OPTIONS)
        mode = config["modes"][min(claim["attempt"], len(config["modes"])) - 1]
        spec = {"planDir": str(plan), "cwd": str(attempt_dir / "client"),
                "command": [sys.executable, config["child"], str(plan), config["fixture"], mode],
                "expectedPlanSha256": config["planSha256"], "currentIdentity": current}
        if spec_change:
            spec_change(spec, attempt_dir)
        return spec
    return prepare


def worker_main(config_path):
    """Entry point of the separate worker process used by the recovery test."""
    import warm_worker
    from render_queue import RenderQueue
    config = json.loads(Path(config_path).read_text())
    with RenderQueue(config["db"], "uie", "0", config["key"], max_attempts=3) as queue:
        warm_worker.run_one(queue, config["attempts"], prepare=staging_prepare(config), timeout=60,
                            poll_interval=0.05)


@unittest.skipUnless(LINUX, "the warm worker integration needs Linux (flock, /proc, pidfd)")
class WarmWorkerTests(unittest.TestCase):
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
        declared = base / "declared"
        (declared / "mods").mkdir(parents=True)
        (declared / "mods" / "a.jar").write_bytes(b"mod a")
        (declared / "exporter.jar").write_bytes(b"exporter")
        cls.record = create_pipeline_identity({"mods": declared / "mods"}, {"exporter": declared / "exporter.jar"},
                                              OPTIONS)
        cls.key = cls.record["pipelineKey"]
        template = base / "template"
        prepare_warm_plan(batches, template, world="uie", dimension="0", pipeline_key=cls.key, min_free_bytes=0,
                          pipeline_identity=cls.record)
        fixture = base / "fixture"
        fixture.mkdir()
        atlas = png(32, 32)
        (fixture / "atlas.png").write_bytes(atlas)
        (fixture / "atlas-layout.json").write_bytes(json.dumps(layout(atlas)).encode())
        (fixture / "child.py").write_text(CHILD)
        cls.plan_sha = sha((template / "plan.json").read_bytes())
        cls.job_id = sha((template / "COMPLETE").read_bytes())
        cls.base_config = {"template": str(template), "declared": str(declared), "fixture": str(fixture),
                           "child": str(fixture / "child.py"), "planSha256": cls.plan_sha, "key": cls.key}

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        import owned_process as op
        import warm_worker as ww
        from render_queue import RenderQueue
        self.op, self.ww = op, ww
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()
        self.attempts = self.dir / "attempts"
        self.attempts.mkdir()
        self.workers = []
        self.queue = RenderQueue(self.dir / "queue.db", "uie", "0", self.key, max_attempts=3)
        self.addCleanup(self.queue.close)
        self.queue.enqueue([{"id": self.job_id, "payload": {"planSha256": self.plan_sha}}])
        self.addCleanup(self.cleanup)   # runs first

    def cleanup(self):
        """Stop only processes of this test's recorded scopes and its own worker processes."""
        for worker in self.workers:
            if worker.poll() is None:
                worker.kill()   # our unreaped direct child
            worker.wait(timeout=10)
        for state_path in self.attempts.glob("*/state.json"):
            scope = json.loads(state_path.read_text())["scope"]
            if scope is not None:
                try:
                    self.op.stop_owned(scope, term_timeout=1, kill_timeout=3)
                except self.op.StaleScopeError:
                    pass

    def config(self, *modes):
        return {**self.base_config, "modes": list(modes), "db": str(self.dir / "queue.db"),
                "attempts": str(self.attempts)}

    def run_one(self, *modes, **kwargs):
        kwargs.setdefault("prepare", staging_prepare(self.config(*modes)))
        kwargs.setdefault("poll_interval", 0.05)
        kwargs.setdefault("retry_delay", 0)
        kwargs.setdefault("timeout", 20)
        return self.ww.run_one(self.queue, self.attempts, **kwargs)

    def state(self, result):
        return json.loads((Path(result["attemptDir"]) / "state.json").read_text())

    def job(self):
        (job,) = self.queue.list_jobs()
        return job

    def assert_no_owned(self, state):
        if state["scope"] is not None:
            self.assertEqual(self.op.inspect_owned(state["scope"]), [])

    # ------------------------------------------------------------------ tests

    def test_success_waits_for_receipts_stops_the_idle_client_then_verifies(self):
        result = self.run_one("ok")
        self.assertEqual(result["outcome"], "succeeded", result)
        self.assertEqual(result["stop"]["returncode"], -signal.SIGTERM, "the client idled until it was stopped")
        self.assertFalse(result["stop"]["escalated"])
        attempt = Path(result["attemptDir"])
        prefix, token = attempt.name.rsplit("-", 1)
        self.assertEqual((prefix, len(token)), (f"{self.job_id}-1", 32), "jobId-attempt-fullToken")
        state = self.state(result)
        self.assertEqual(state["phase"], "completed")
        self.assert_no_owned(state)
        verified = (attempt / "VERIFIED.json").read_bytes()
        self.assertEqual(json.loads(verified)["report"]["verified"], True)
        job = self.job()
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["result"], {"attempt": attempt.name, "verifiedSha256": sha(verified),
                                         "planSha256": self.plan_sha, "pipelineKey": self.key})
        text = (attempt / "state.json").read_text()
        for secret in (self.base_config["child"], sys.executable):
            self.assertNotIn(secret, text, "no command in the attempt marker")
        self.assertTrue((attempt / "client.log").is_file())
        self.assertEqual(self.run_one("ok"), {"outcome": "idle"})

    def test_crash_is_retried_in_a_fresh_attempt_directory(self):
        first = self.run_one("crash", "ok")
        self.assertEqual((first["outcome"], first["permanent"]), ("retry", False), first)
        self.assertIn("exited", first["error"])
        self.assertEqual(self.state(first)["phase"], "failed")
        second = self.run_one("crash", "ok")
        self.assertEqual(second["outcome"], "succeeded", second)
        self.assertNotEqual(first["attemptDir"], second["attemptDir"])
        self.assertTrue(Path(first["attemptDir"]).is_dir(), "the failed attempt is retained")
        self.assertEqual(len(list(Path(first["attemptDir"], "plan", "results").iterdir())), 1, "partial, not reused")
        self.assertEqual((self.job()["state"], self.job()["attempt"]), ("succeeded", 2))

    def test_timeout_stops_the_client(self):
        result = self.run_one("hang", timeout=1.0)
        self.assertEqual(result["outcome"], "retry", result)
        self.assertIn("timeout", result["error"])
        self.assertEqual(result["stop"]["returncode"], -signal.SIGTERM)
        self.assert_no_owned(self.state(result))

    def test_invalid_prepare_or_identity_fails_permanently_without_launch(self):
        from pipeline_identity import canonical_json
        changed = json.loads(json.dumps(self.record))
        changed["identity"]["options"]["java"] = "17"
        changed["pipelineKey"] = sha(canonical_json(changed["identity"]))

        def outside(spec, attempt_dir):
            spec["cwd"] = str(self.dir)

        def linked(spec, attempt_dir):
            os.symlink(attempt_dir / "client", attempt_dir / "link")
            spec["cwd"] = str(attempt_dir / "link")

        def payload(spec, attempt_dir):
            spec["expectedPlanSha256"] = "0" * 64

        cases = {
            "fresh identity differs": dict(current_identity=changed),
            "cwd outside the attempt": dict(spec_change=outside),
            "cwd through a link": dict(spec_change=linked),
            "plan differs from the job payload": dict(spec_change=payload),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.queue.close()
                (self.dir / "queue.db").unlink()
                from render_queue import RenderQueue
                self.queue = RenderQueue(self.dir / "queue.db", "uie", "0", self.key, max_attempts=3)
                self.addCleanup(self.queue.close)
                self.queue.enqueue([{"id": self.job_id, "payload": {"planSha256": self.plan_sha}}])
                result = self.run_one(prepare=staging_prepare(self.config("ok"), **change))
                self.assertEqual((result["outcome"], result["permanent"]), ("failed", True), result)
                state = self.state(result)
                self.assertEqual((state["phase"], state["scope"]), ("failed", None), "nothing was launched")
                self.assertFalse((Path(result["attemptDir"]) / "client.log").exists())
                self.assertEqual((self.job()["state"], self.job()["attempt"]), ("failed", 1))

    def test_unsupported_coverage_is_a_permanent_failure(self):
        result = self.run_one("unsupported")
        self.assertEqual((result["outcome"], result["permanent"]), ("failed", True), result)
        self.assertIn("unsupported", result["error"])
        self.assertFalse((Path(result["attemptDir"]) / "VERIFIED.json").exists())
        self.assertEqual(self.state(result)["phase"], "failed")
        self.assertEqual((self.job()["state"], self.job()["attempt"]), ("failed", 1))

    def test_unsupported_coverage_succeeds_only_with_the_explicit_allowing_verifier(self):
        result = self.run_one("unsupported", verify=self.ww.verify_attempt_allowing_unsupported)
        self.assertEqual(result["outcome"], "succeeded", result)
        report = json.loads((Path(result["attemptDir"]) / "VERIFIED.json").read_bytes())["report"]
        self.assertEqual(report["unsupportedPolicy"], "allowed")
        self.assertGreater(report["coverage"]["unsupportedBlocks"], 0, "still counted and named")
        self.assertEqual(self.job()["state"], "succeeded")

    def test_second_worker_is_refused_by_the_lock(self):
        import fcntl
        fd = os.open(self.attempts / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(self.ww.WorkerBusyError):
                self.run_one("ok")
        finally:
            os.close(fd)
        self.assertEqual((self.job()["state"], self.job()["attempt"]), ("queued", 0), "nothing was claimed")
        self.assertEqual([p.name for p in self.attempts.iterdir()], ["worker.lock"])

    def test_running_job_without_its_attempt_is_blocked(self):
        self.queue.claim(time.time())   # a worker that died before writing state.json
        result = self.run_one("ok")
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertEqual(self.job()["state"], "running", "never requeued while unknown")

    def test_recovery_stops_the_orphan_from_the_persisted_scope_then_retries_fresh(self):
        config = self.config("hang", "ok")
        (self.dir / "config.json").write_text(json.dumps(config))
        code = (f"import sys; sys.path.insert(0, {str(HERE)!r}); import test_warm_worker as t; "
                f"t.worker_main({str(self.dir / 'config.json')!r})")
        worker = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.workers.append(worker)
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline:
            paths = list(self.attempts.glob("*/state.json"))
            if paths:
                state = json.loads(paths[0].read_text())
                if state["phase"] == "running":
                    break
            self.assertIsNone(worker.poll(), "the worker ended early")
            time.sleep(0.05)
        self.assertEqual(state and state["phase"], "running")
        self.assertEqual(len(self.op.inspect_owned(state["scope"])), 1, "the client is running")
        worker.kill()   # the worker dies without cleanup; its client keeps running as an orphan
        worker.wait(timeout=10)
        self.assertEqual(len(self.op.inspect_owned(state["scope"])), 1, "orphan still alive")

        recovered = self.run_one("hang", "ok")
        self.assertEqual(recovered["outcome"], "recovered", recovered)
        self.assertEqual(self.op.inspect_owned(state["scope"]), [])
        self.assertEqual(self.state(recovered)["phase"], "abandoned")
        self.assertEqual((self.job()["state"], self.job()["attempt"]), ("queued", 1))
        retried = self.run_one("hang", "ok")
        self.assertEqual(retried["outcome"], "succeeded", retried)
        self.assertNotEqual(retried["attemptDir"], recovered["attemptDir"])

    def crashed_attempt(self, root, token=None):
        """A running claim plus the attempt a dead worker left in root (state.json with a recorded scope)."""
        claim = self.queue.claim(time.time())
        token = token or claim["token"]
        attempt = root / f"{claim['id']}-{claim['attempt']}-{token}"
        attempt.mkdir()
        (attempt / "state.json").write_text(json.dumps({
            "format": "UIE_WARM_ATTEMPT1", "jobId": claim["id"], "attempt": claim["attempt"], "token": token,
            "queue": {"worldId": "uie", "dimension": "0", "pipelineKey": self.key}, "phase": "running",
            "scope": self.op.create_scope(), "planDir": "plan", "planSha256": self.plan_sha, "error": None}))
        return attempt

    def no_signals(self):
        return mock.patch.object(self.op, "stop_owned", side_effect=AssertionError("stop_owned was called"))

    def test_unavailable_cgroup_isolation_is_refused_before_claim_but_never_blocks_recovery(self):
        unavailable = mock.patch.object(self.op, "check_cgroup_isolation",
                                        side_effect=self.op.UnsupportedPlatformError("cgroup v1 only (fixture)"))
        never_prepare = lambda claim, attempt_dir: self.fail("prepare must not run")
        with unavailable:
            with self.assertRaisesRegex(self.ww.WarmWorkerError, "isolation is unavailable"):
                self.run_one(isolate_cgroup=True, prepare=never_prepare)
        self.assertEqual((self.job()["state"], self.job()["attempt"]), ("queued", 0), "no attempt was consumed")
        self.assertEqual([p.name for p in self.attempts.iterdir()], ["worker.lock"], "nothing was staged")
        # A dead worker's job is still recovered: stop_owned works from anywhere, isolation is not needed.
        self.crashed_attempt(self.attempts)
        with unavailable:
            self.assertEqual(self.run_one(isolate_cgroup=True, prepare=never_prepare)["outcome"], "recovered")
        self.assertEqual((self.job()["state"], self.job()["attempt"]), ("queued", 1))

    def test_isolated_run_records_its_cgroup_when_available(self):
        try:
            cgroup = self.op.check_cgroup_isolation()
        except self.op.UnsupportedPlatformError as exc:
            self.skipTest(f"no usable cgroup v2 isolation here: {exc}")
        result = self.run_one("ok", isolate_cgroup=True)
        self.assertEqual(result["outcome"], "succeeded", result)
        state = self.state(result)
        self.assertEqual(state["scope"]["cgroup"], cgroup)
        self.assert_no_owned(state)

    def test_recovery_blocks_when_cleanup_cannot_be_confirmed(self):
        attempt = self.crashed_attempt(self.attempts)
        with mock.patch.object(self.op, "stop_owned", side_effect=self.op.ScanError("unreadable newer process")):
            result = self.run_one("ok")
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertEqual(json.loads((attempt / "state.json").read_text())["phase"], "blocked")
        self.assertEqual(self.job()["state"], "running", "never requeued while possibly alive")
        self.assertEqual(self.run_one("ok")["outcome"], "recovered", "a later successful cleanup recovers it")

    def blocked_after_capture(self):
        """A real successful client run whose final cleanup could not be confirmed (phase blocked)."""
        real = self.op.stop_owned
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                real(*args, **kwargs)   # the tree really is stopped ...
                raise self.op.ScanError("process 1 belongs to this user ... unreadable (fixture)")   # ... but unconfirmed
            return real(*args, **kwargs)
        with mock.patch.object(self.op, "stop_owned", side_effect=flaky):
            result = self.run_one("ok")
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertEqual(self.state(result)["phase"], "blocked")
        self.assertEqual(self.job()["state"], "running")
        return result

    def test_blocked_attempt_with_complete_outputs_is_completed_after_confirmed_cleanup(self):
        blocked = self.blocked_after_capture()
        never_prepare = lambda claim, attempt_dir: self.fail("no new client run")
        recovered = self.run_one(prepare=never_prepare)
        self.assertEqual(recovered["outcome"], "succeeded", recovered)
        self.assertEqual(recovered["recoveredFromPhase"], "blocked")
        self.assertEqual(recovered["attemptDir"], blocked["attemptDir"], "same attempt, no re-render")
        job = self.job()
        self.assertEqual((job["state"], job["attempt"]), ("succeeded", 1))
        verified = (Path(blocked["attemptDir"]) / "VERIFIED.json").read_bytes()
        self.assertEqual(job["result"]["verifiedSha256"], hashlib.sha256(verified).hexdigest())
        self.assertEqual(json.loads(verified)["report"]["verified"], True)
        self.assertEqual(self.state(recovered)["phase"], "completed")

    def test_blocked_attempt_with_broken_outputs_is_still_rerendered(self):
        blocked = self.blocked_after_capture()
        receipt = next(Path(blocked["attemptDir"]).rglob("receipt.json"))
        receipt.unlink()
        recovered = self.run_one("ok")
        self.assertEqual(recovered["outcome"], "recovered", recovered)
        self.assertIn("reuseRefused", recovered)
        self.assertEqual(self.state(recovered)["phase"], "abandoned")
        self.assertEqual((self.job()["state"], self.job()["attempt"]), ("queued", 1), "fresh retry, nothing reused")

    def test_attempt_with_a_token_other_than_the_live_claim_is_blocked_without_signals(self):
        # Folder and state agree with each other, but not with the queue's live claim token.
        attempt = self.crashed_attempt(self.attempts, token="f" * 32)
        with self.no_signals():
            result = self.run_one("ok")
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn("live claim", result["error"])
        self.assertNotIn("f" * 32, json.dumps(result))
        self.assertEqual(json.loads((attempt / "state.json").read_text())["phase"], "running", "state untouched")
        self.assertEqual(self.job()["state"], "running")

        # Folder carries the live token but the state.json token differs: also blocked, no signals.
        live = self.queue.running_claim()["token"]
        other = self.attempts.parent / "attempts-2"
        other.mkdir()
        moved = other / attempt.name.replace("f" * 32, live)
        attempt.rename(moved)
        with self.no_signals():
            result = self.ww.run_one(self.queue, other, prepare=staging_prepare(self.config("ok")))
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn("invalid attempt state", result["error"])
        self.assertEqual(self.job()["state"], "running")

    def test_same_database_with_another_attempts_root_is_refused_by_the_queue_lock(self):
        # A first worker holds the queue-file lock (a plain flock, as run_one takes it). A second worker with a
        # different attempts root and a valid copied attempt state must not recover, signal or claim anything.
        import fcntl
        other = self.attempts.parent / "attempts-2"
        other.mkdir()
        attempt = self.crashed_attempt(other)
        lock_path = self.ww.queue_lock_path(self.queue)
        self.assertEqual(lock_path, Path(os.path.realpath(self.dir / "queue.db") + ".worker-lock"))
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.no_signals(), self.assertRaises(self.ww.WorkerBusyError):
                self.ww.run_one(self.queue, other, prepare=staging_prepare(self.config("ok")))
        finally:
            os.close(fd)
        self.assertEqual(self.job()["state"], "running")
        self.assertEqual(json.loads((attempt / "state.json").read_text())["phase"], "running", "nothing recovered")
        self.assertFalse((other / "worker.lock").exists(), "refused before the attempts-root lock")
        # Once the first worker is gone, the copied state is valid and recovery proceeds normally.
        recovered = self.ww.run_one(self.queue, other, prepare=staging_prepare(self.config("ok")))
        self.assertEqual(recovered["outcome"], "recovered", recovered)

    def test_queue_lock_needs_a_real_file_database_and_a_real_lock_file(self):
        from render_queue import RenderQueue
        with RenderQueue(":memory:", "uie", "0", self.key) as memory:
            with self.assertRaisesRegex(self.ww.WarmWorkerError, "file database"):
                self.ww.run_one(memory, self.attempts, prepare=staging_prepare(self.config("ok")))
        lock_path = self.ww.queue_lock_path(self.queue)
        os.symlink(self.dir / "elsewhere", lock_path)
        with self.assertRaisesRegex(self.ww.WarmWorkerError, "links are refused"):
            self.run_one("ok")
        self.assertFalse((self.dir / "elsewhere").exists(), "the link target was not created")
        self.assertEqual((self.job()["state"], self.job()["attempt"]), ("queued", 0))


class ArgumentTests(unittest.TestCase):
    """Pure validation that runs on every platform without a queue file or processes."""

    def test_bad_arguments_are_refused_before_anything_is_claimed(self):
        import warm_worker as ww
        with self.assertRaises(ww.WarmWorkerError):
            ww.run_one(object(), "/tmp", prepare=lambda c, d: None)
        with mock.patch.object(ww, "RenderQueue", object):
            for kwargs in ({"prepare": None}, {"prepare": print, "timeout": 0}, {"prepare": print, "poll_interval": 0},
                           {"prepare": print, "retry_delay": -1}, {"prepare": print, "timeout": float("nan")}):
                with self.subTest(kwargs), self.assertRaises(ww.WarmWorkerError):
                    ww.run_one(object(), "/tmp", **kwargs)


if __name__ == "__main__":
    unittest.main()
