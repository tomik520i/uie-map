"""Automatic overview refresh: incremental stage A, publication with hard-linked unchanged tiles, pruning."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import overview_update as ou
import publish_overview as po
from test_prepare_warm_plan import build_world


class OverviewUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        build_world(self.base / "world")
        build_world(self.base / "world2", missing={(-5, -5)})
        self.colors = self.base / "colors.json"
        self.colors.write_text(json.dumps({"source": "test", "byMeta": {}, "byName": {}}))
        self.state, self.store = self.base / "state", self.base / "store"

    def run_update(self):
        return ou.update(self.base / "world", self.state, self.colors, self.store, workers=1, keep=2)

    def test_first_run_publishes_and_an_unchanged_world_publishes_nothing(self):
        first = self.run_update()
        self.assertTrue(first["published"])
        self.assertEqual(first["changedRegions"], 4)
        second = self.run_update()
        self.assertEqual((second["published"], second["changedRegions"], second["build"]),
                         (False, 0, first["build"]))
        self.assertEqual(json.loads((self.state / "status.json").read_text())["reason"], "no region changed")
        self.assertEqual([p for p in self.state.iterdir() if p.name.startswith("tiles-")], [], "temp tiles removed")

    def test_changed_region_is_rebuilt_alone_and_unchanged_tiles_are_hard_links(self):
        first = self.run_update()
        # Region (-1,-1) changes: take the world2 version (one chunk missing) with a fresh mtime.
        target = self.base / "world" / "region" / "r.-1.-1.mca"
        shutil.copyfile(self.base / "world2" / "region" / "r.-1.-1.mca", target)
        os.utime(target, (time.time() + 5, time.time() + 5))
        second = self.run_update()
        self.assertEqual(second["changedRegions"], 1)
        self.assertTrue(second["published"])
        self.assertNotEqual(second["build"], first["build"])
        old, new = self.store / "builds" / first["build"], self.store / "builds" / second["build"]
        same = new / "L0" / "0_0.png"
        self.assertEqual(os.stat(same).st_ino, os.stat(old / "L0" / "0_0.png").st_ino, "unchanged tile is a hard link")
        self.assertNotEqual((new / "L0" / "-1_-1.png").read_bytes(), (old / "L0" / "-1_-1.png").read_bytes())
        self.assertEqual(json.loads((self.store / "current.json").read_text())["build"], second["build"])

    def test_prune_keeps_the_newest_and_never_the_current(self):
        builds = []
        region = self.base / "world" / "region" / "r.-1.-1.mca"
        variants = [self.base / "original.mca", self.base / "world2" / "region" / "r.-1.-1.mca"]
        shutil.copyfile(region, variants[0])
        for i in range(4):
            if i:
                # alternate the content of one region so every run is a new build
                shutil.copyfile(variants[i % 2], region)
                os.utime(region, (time.time() + 10 * i,) * 2)
            status = self.run_update()
            builds.append(status["build"])
            time.sleep(0.05)
        left = {p.name for p in (self.store / "builds").iterdir()}
        self.assertIn(builds[-1], left, "current kept")
        self.assertLessEqual(len(left), 2)
        self.assertEqual(json.loads((self.store / "current.json").read_text())["build"], builds[-1])
        with self.assertRaises((po.OverviewPublishError, OSError)):
            po.publish(self.base / "nope", self.store, builds[-1])
        self.assertEqual(json.loads((self.store / "current.json").read_text())["build"], builds[-1], "pointer kept")


class OverviewStatusTests(unittest.TestCase):
    """Task 112: atomic running/done/error status with stages, heartbeat and preserved last publication.
    Surface, tile and publication steps are replaced by fakes (the real pipeline is covered above)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.state, self.store = self.base / "state", self.base / "store"
        self.store.mkdir()
        self.writes = []
        self.mirrored = []
        self.fail_mirror = False
        self.prune_error = None
        self.run_json = {"ok": 2, "failed": []}
        self.surface_hook = None
        self.tiles_hook = None
        self.builds = 0

    # fakes
    def surface(self, args):
        if self.surface_hook:
            self.surface_hook()
        out = Path(args[args.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "run.json").write_text(json.dumps(self.run_json))
        return 0 if not self.run_json["failed"] else 2

    def tiles(self, _surface, _colors, tiles, _workers):
        tiles.mkdir(parents=True)
        if self.tiles_hook:
            self.tiles_hook()
        return {"levels": 3}

    def publish(self, _tiles, store, _current):
        self.builds += 1
        build = f"b{self.builds}"
        (store / "current.json").write_text(json.dumps({"build": build}))
        return {"build": build, "tiles": 5}

    def record(self, path, doc):
        """State-file writes go to self.writes, public mirror writes to self.mirrored (same order expected)."""
        if self.fail_mirror and Path(path).parent == self.store:
            raise OSError("mirror not writable")
        (self.writes if Path(path).parent == self.state else self.mirrored).append(json.loads(json.dumps(doc)))
        return self.real_write(path, doc)

    def mirror(self):
        return json.loads((self.store / "status.json").read_text(encoding="utf-8"))

    def run_update(self, **kw):
        from unittest.mock import patch
        self.real_write = ou._atomic_write_json
        with (patch.object(ou.ovs, "main", side_effect=self.surface),
              patch.object(ou.ovt, "build", side_effect=self.tiles),
              patch.object(ou.po, "publish", side_effect=self.publish),
              patch.object(ou.po, "current_build", side_effect=lambda s: json.loads((s / "current.json").read_text())["build"]),
              patch.object(ou.po, "prune", side_effect=self.prune),
              patch.object(ou, "_atomic_write_json", side_effect=self.record)):
            return ou.update(self.base / "world", self.state, self.base / "colors.json", self.store, workers=1,
                             keep=2, **kw)

    def status(self):
        return json.loads((self.state / "status.json").read_text(encoding="utf-8"))

    def assert_no_heartbeat_thread(self):
        self.assertEqual([t for t in threading.enumerate() if t.name == ou.HEARTBEAT_THREAD], [], "no lingering thread")

    def wait_for_beats(self, stage, count=2, timeout=5.0):
        deadline = time.monotonic() + timeout
        while sum(1 for w in self.writes if w.get("stage") == stage and w["state"] == "running") < count + 1:
            if time.monotonic() > deadline:
                raise AssertionError(f"no heartbeat during {stage}")
            time.sleep(0.01)

    def test_heartbeat_during_slow_stages_then_done_with_known_counts(self):
        self.surface_hook = lambda: self.wait_for_beats("surface")
        self.tiles_hook = lambda: self.wait_for_beats("tiles")
        done = self.run_update(heartbeat_seconds=0.02)
        running = [w for w in self.writes if w["state"] == "running"]
        self.assertEqual(running[0]["stage"], "surface")
        self.assertIsNone(running[0]["finishedAt"])
        self.assertEqual(running[0]["heartbeatSeconds"], 0.02)
        surface_beats = [w["updatedAt"] for w in running if w["stage"] == "surface"]
        self.assertGreaterEqual(len(surface_beats), 3)
        self.assertEqual(surface_beats, sorted(surface_beats), "heartbeat moves forward")
        self.assertIn("tiles", [w["stage"] for w in running])
        self.assertIn("publish", [w["stage"] for w in running])
        self.assertEqual(next(w for w in running if w["stage"] == "tiles")["changedRegions"], 2,
                         "counts are published as soon as they are known")
        self.assertFalse(any("percent" in key.lower() for w in self.writes for key in w), "no invented percentage")
        self.assertEqual((done["state"], done["stage"], done["complete"]), ("done", "finished", True))
        self.assertEqual((done["published"], done["build"], done["levels"], done["tiles"]), (True, "b1", 3, 5))
        self.assertEqual(done["lastPublication"]["build"], "b1")
        self.assertLessEqual(done["lastPublication"]["publishedAt"], done["finishedAt"], "time of the publication itself")
        self.assertLessEqual(done["startedAt"], done["finishedAt"])
        self.assertNotIn("warning", done)
        self.assertEqual(self.status(), done)
        self.assertEqual(self.mirror(), done, "the public mirror holds the same final document")
        self.assertEqual(self.mirrored, self.writes, "every state write (heartbeats included) was mirrored in order")
        self.assert_no_heartbeat_thread()
        writes = len(self.writes)
        time.sleep(0.1)
        self.assertEqual(len(self.writes), writes, "no write after the final status")

    def test_error_persists_stage_and_message_keeps_last_publication_and_rethrows(self):
        first = self.run_update()
        def boom():
            raise RuntimeError("tile renderer crashed " + "x" * 400)
        self.tiles_hook = boom
        with self.assertRaises(RuntimeError):
            self.run_update(heartbeat_seconds=0.02)
        failed = self.status()
        self.assertEqual((failed["state"], failed["stage"]), ("error", "tiles"))
        self.assertTrue(failed["error"].startswith("RuntimeError: tile renderer crashed"))
        self.assertEqual(len(failed["error"]), 300)
        self.assertIsNotNone(failed["finishedAt"])
        self.assertEqual(failed["changedRegions"], 2, "known progress kept")
        self.assertEqual(failed["lastPublication"], first["lastPublication"], "last good publication preserved")
        self.assertEqual(failed["lastSuccess"]["build"], "b1")
        self.assertEqual(failed["currentBuild"], "b1", "the store still serves the previous build")
        self.assertNotIn("published", failed, "the failed attempt claims no publication")
        self.assertEqual([p for p in self.state.iterdir() if p.name.startswith("tiles-")], [], "temp tiles removed")
        self.assert_no_heartbeat_thread()

    def test_publication_then_prune_or_cleanup_failure_reports_the_new_serving_build(self):
        self.run_update()                                          # b1 published
        for label in ("prune", "cleanup"):
            with self.subTest(label):
                with self.assertRaises(OSError):
                    self.run_failing(label)
                failed = self.status()
                expected = f"b{self.builds}"
                self.assertEqual((failed["state"], failed["stage"]), ("error", "publish"))
                self.assertIn(f"{label} failed", failed["error"])
                self.assertEqual((failed["published"], failed["build"], failed["currentBuild"]), (True, expected, expected),
                                 "the store already serves the new build")
                self.assertEqual(failed["lastPublication"]["build"], expected)
                self.assertIsNotNone(failed["lastPublication"]["publishedAt"])
                self.assertNotEqual(failed["lastSuccess"]["build"], expected, "but the attempt did not succeed")
                self.assertEqual(self.mirror(), failed)
                self.assert_no_heartbeat_thread()
                for leftover in self.state.glob("tiles-*"):
                    self.real_rmtree(leftover)

    def run_failing(self, label):
        """Publication succeeds; then pruning (unchanged semantics, here a fake) or the temp tile cleanup fails."""
        from unittest.mock import patch
        self.real_rmtree = shutil.rmtree
        if label == "prune":
            self.prune_error = OSError("prune failed")
            try:
                return self.run_update()
            finally:
                self.prune_error = None
        with patch.object(ou.shutil, "rmtree", side_effect=OSError("cleanup failed")):
            return self.run_update()

    def prune(self, _store, _keep):
        if self.prune_error:
            raise self.prune_error
        return []

    def test_zero_changed_with_failed_regions_is_not_no_change(self):
        self.run_update()
        self.run_json = {"ok": 0, "failed": [["r.0.0.mca", "unreadable"]]}
        done = self.run_update()
        self.assertEqual((done["state"], done["published"], done["complete"]), ("done", False, False))
        self.assertNotEqual(done["reason"], "no region changed")
        self.assertIn("1 regions failed", done["reason"])
        self.assertIn("previous build is kept", done["reason"])
        self.assertIn("warning", done)

    def test_unreadable_store_pointer_is_this_attempts_error(self):
        from unittest.mock import patch
        first = self.run_update()
        with patch.object(ou.po, "current_build", side_effect=ValueError("corrupt current.json")):
            with self.assertRaises(ValueError):
                ou.update(self.base / "world", self.state, self.base / "colors.json", self.store, workers=1)
        failed = self.status()
        self.assertEqual((failed["state"], failed["stage"]), ("error", "surface"))
        self.assertIn("corrupt current.json", failed["error"])
        self.assertIsNone(failed["currentBuild"], "unknown, not the previous success")
        self.assertEqual(failed["lastSuccess"], first["lastSuccess"])
        self.assertEqual(self.mirror(), failed)
        self.assert_no_heartbeat_thread()

    def test_mirror_write_failure_never_publishes_an_invented_status(self):
        self.fail_mirror = True
        with self.assertRaises(OSError):
            self.run_update()
        state = self.status()
        self.assertEqual(state["state"], "error")
        self.assertIn("mirror not writable", state["error"])
        self.assertFalse((self.store / "status.json").exists(), "nothing was mirrored")
        self.assert_no_heartbeat_thread()

    def test_error_in_the_surface_stage(self):
        def fail():
            raise OSError("region unreadable")
        self.surface_hook = fail
        with self.assertRaises(OSError):
            self.run_update()
        self.assertEqual((self.status()["state"], self.status()["stage"]), ("error", "surface"))
        self.assertIsNone(self.status()["lastPublication"], "nothing was ever published")
        self.assert_no_heartbeat_thread()

    def test_failed_regions_are_an_explicit_partial_warning(self):
        self.run_json = {"ok": 1, "failed": [["r.0.0.mca", "boom"], ["r.1.0.mca", "boom"]]}
        done = self.run_update()
        self.assertEqual((done["state"], done["complete"], done["failedRegions"]), ("done", False, 2))
        self.assertIn("2 regions failed", done["warning"])
        self.assertFalse(done["lastSuccess"]["complete"])

    def test_chunk_errors_are_partial_even_with_successful_regions(self):
        self.run_json = {"ok": 1, "failed": [], "chunkErrors": 3}
        done = self.run_update()
        self.assertFalse(done["complete"])
        self.assertEqual(done["chunkErrors"], 3)
        self.assertIn("3 chunk read errors", done["warning"])

    def test_unchanged_world_keeps_the_last_publication_time(self):
        first = self.run_update()
        self.run_json = {"ok": 0, "failed": []}
        self.writes = []
        second = self.run_update()
        self.assertEqual((second["published"], second["reason"], second["state"]), (False, "no region changed", "done"))
        self.assertEqual(second["lastPublication"], first["lastPublication"], "publication time not refreshed")
        self.assertGreaterEqual(second["lastSuccess"]["finishedAt"], first["lastSuccess"]["finishedAt"])
        self.assertEqual({w["stage"] for w in self.writes}, {"surface", "finished"}, "no tile or publish stage")

    def test_legacy_status_is_carried_as_unknown_time(self):
        self.state.mkdir()
        (self.state / "status.json").write_text(json.dumps(
            {"format": "UIE_OVERVIEW_UPDATE1", "lastRunAt": "2026-10-01T10:00:00Z", "changedRegions": 3,
             "failedRegions": 0, "surfaceExitCode": 0, "published": True, "build": "old"}))
        (self.store / "current.json").write_text(json.dumps({"build": "old"}))
        self.run_json = {"ok": 0, "failed": []}
        done = self.run_update()
        self.assertEqual(done["lastPublication"],
                         {"publishedAt": None, "lastRunAt": "2026-10-01T10:00:00Z", "build": "old"})

    def test_atomic_write_never_leaves_a_partial_file(self):
        path = self.base / "status.json"
        ou._atomic_write_json(path, {"format": "UIE_OVERVIEW_UPDATE1", "state": "done"})
        from unittest.mock import patch
        with patch.object(ou.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                ou._atomic_write_json(path, {"format": "UIE_OVERVIEW_UPDATE1", "state": "running"})
        self.assertEqual(json.loads(path.read_text())["state"], "done")
        self.assertEqual([p.name for p in self.base.iterdir() if p.name.endswith(".tmp")], [])

    def test_lock_busy_does_not_touch_the_active_status(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest("fcntl lock semantics are Linux-only")
        self.state.mkdir()
        active = {"format": "UIE_OVERVIEW_UPDATE1", "state": "running", "stage": "tiles"}
        (self.state / "status.json").write_text(json.dumps(active))
        with open(self.state / "update.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code = ou.main(["--world", str(self.base / "w"), "--state", str(self.state),
                            "--colors", str(self.base / "c.json"), "--store", str(self.store)])
        self.assertEqual(code, 0)
        self.assertEqual(self.status(), active)


if __name__ == "__main__":
    unittest.main()
