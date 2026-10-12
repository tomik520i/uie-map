"""Tests for world_hires.py (UIH1 tiles, job geometry). Stdlib only, Windows/Linux."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import world_hires as wh


def quad(x, y, z, transparent=False):
    part = {"positions": [x, y, z, x + 1, y, z, x + 1, y + 0.8889, z + 1, x, y + 1, z + 1],
            "uvs": [0.1, 0.2, 0.3, 0.2, 0.3, 0.1, 0.1, 0.1], "colors": [1.0, 0.5, 0.25] * 4,
            "indices": [0, 1, 2, 0, 2, 3]}
    if transparent:
        part["transparent"] = True
    return part


class HiresTileTests(unittest.TestCase):
    def test_roundtrip_keeps_geometry_within_quantisation(self):
        doc = {**quad(-63.75, 70, -1), "transparentParts": [quad(-60, 62, -10, True)]}
        data = wh.encode_tile(-64, -64, [doc])
        tile = wh.decode_tile(data)
        self.assertEqual(tile["origin"], (-64, -64))
        (p0, u0, c0), (p1, u1, c1) = tile["groups"]
        for got, want in ((p0, doc["positions"]), (p1, doc["transparentParts"][0]["positions"])):
            self.assertEqual(len(got), len(want))
            self.assertTrue(all(abs(a - b) <= 0.5 / wh.SCALE for a, b in zip(got, want)), (got, want))
        self.assertTrue(all(abs(a - b) < 1e-4 for a, b in zip(u0, doc["uvs"])))
        self.assertTrue(all(abs(a - b) <= 0.5 / 255 for a, b in zip(c0, doc["colors"])))

    def test_empty_tile_is_none_and_output_is_deterministic(self):
        self.assertIsNone(wh.encode_tile(0, 0, [{"positions": [], "uvs": [], "colors": [], "indices": []}]))
        doc = quad(1, 64, 1)
        self.assertEqual(wh.encode_tile(0, 0, [doc]), wh.encode_tile(0, 0, [doc]))

    def test_vertices_outside_the_tile_are_refused(self):
        with self.assertRaises(wh.HiresError):
            wh.encode_tile(0, 0, [quad(600, 64, 1)])

    def test_job_areas_and_tiles_align(self):
        area, core = wh.job_area(-1, 2)
        self.assertEqual((core.x, core.z, core.width), (-512, 1024, 512))
        self.assertEqual((area.x, area.width), (-528, 544))
        self.assertEqual(wh.JOB % wh.TILE, 0)
        t_area, t_core = wh.tile_area(-1, 0)
        self.assertEqual((t_core.x, t_core.width, t_area.x, t_area.width), (-64, 64, -80, 96))


class HiresBuildTests(unittest.TestCase):
    def test_failed_job_is_retried_even_when_unchanged_and_clean_job_is_skipped(self):
        import multiprocessing
        import offline_catalog
        import reader

        submitted = []

        class Pool:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def imap_unordered(self, _worker, jobs):
                submitted.append(list(jobs))
                for jx, jz, fp in submitted[-1]:
                    record = {"job": [jx, jz], "fingerprint": fp, "tiles": {"0,0": "abc123abc123"},
                              "chunks": 1, "quads": 1, "bytes": 1, "fallback": 0, "errors": []}
                    (out / "jobs" / f"{jx}_{jz}.json").write_text(json.dumps(record), encoding="utf-8")
                    yield record

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            world, catalog, out = base / "world", base / "catalog", base / "out"
            world.mkdir(); catalog.mkdir()
            (world / "level.dat").write_bytes(b"test")
            (catalog / "atlas.png").write_bytes(b"atlas")
            (catalog / "atlas-layout.json").write_text("{}", encoding="utf-8")
            with (patch.object(wh, "plan_jobs", return_value=[(0, 0)]),
                  patch.object(wh, "fingerprint", return_value="same-source"),
                  patch.object(reader, "parse_level_dat", return_value=None),
                  patch.object(reader, "parse_fml_registry", return_value=SimpleNamespace(blocks={})),
                  patch.object(offline_catalog.Catalog, "load_compiled", return_value=SimpleNamespace(identity="test")),
                  patch.object(multiprocessing, "get_context", return_value=SimpleNamespace(Pool=Pool)),
                  patch.object(wh.shutil, "disk_usage", return_value=SimpleNamespace(free=wh.MIN_FREE_BYTES + 1))):
                first = wh.build(world, catalog, out, workers=1)
                self.assertEqual(len(submitted[-1]), 1, "first build renders the region")
                self.assertEqual(first["done"], 1)
                second = wh.build(world, catalog, out, workers=1)
                self.assertEqual(submitted[-1], [], "unchanged clean region is skipped")
                self.assertEqual(second["skippedUnchanged"], 1)

                job_path = out / "jobs" / "0_0.json"
                failed = json.loads(job_path.read_text(encoding="utf-8"))
                failed["errors"] = [[0, 0, "mesh failed"]]
                job_path.write_text(json.dumps(failed), encoding="utf-8")
                third = wh.build(world, catalog, out, workers=1)
                self.assertEqual(len(submitted[-1]), 1, "same fingerprint with tile errors is retried")
                self.assertEqual(third["skippedUnchanged"], 0)
                self.assertEqual(json.loads(job_path.read_text(encoding="utf-8"))["errors"], [])


class HiresStatusTests(unittest.TestCase):
    """Task 106: heartbeat, tile failures of this run, processed vs skipped regions, honest final/crash status."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.world, self.catalog, self.out = base / "world", base / "catalog", base / "out"
        self.world.mkdir(); self.catalog.mkdir()
        (self.world / "level.dat").write_bytes(b"test")
        (self.catalog / "atlas.png").write_bytes(b"atlas")
        (self.catalog / "atlas-layout.json").write_text("{}", encoding="utf-8")
        self.sources = {}
        self.plan = [(0, 0), (1, 0)]

    def record(self, jx, jz, fp, tiles=None, errors=()):
        """Like render_job: writes tile files and the job record, returns the record."""
        rec = {"job": [jx, jz], "fingerprint": fp, "tiles": {}, "chunks": 1, "quads": 1, "bytes": 0, "fallback": 0,
               "errors": [list(e) for e in errors]}
        for key, data in (tiles or {}).items():
            (self.out / "tiles" / f"{key.replace(',', '_')}.uih").write_bytes(data)
            rec["tiles"][key] = hashlib.sha256(data).hexdigest()[:12]
        (self.out / "jobs" / f"{jx}_{jz}.json").write_text(json.dumps(rec), encoding="utf-8")
        return rec

    def build(self, results, max_jobs=None, clock=None, free_bytes=None, **options):
        """results(jobs) -> iterator handed out by Pool.imap_unordered."""
        import multiprocessing
        import offline_catalog
        import reader

        class Pool:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def terminate(self):
                pass

            def imap_unordered(self, _worker, jobs):
                return results(list(jobs))

        fingerprint = lambda _dir, area: self.sources[((area.x + wh.HALO) // wh.JOB, (area.z + wh.HALO) // wh.JOB)]
        patches = [patch.object(wh, "plan_jobs", return_value=self.plan),
                   patch.object(wh, "fingerprint", side_effect=fingerprint),
                   patch.object(reader, "parse_level_dat", return_value=None),
                   patch.object(reader, "parse_fml_registry", return_value=SimpleNamespace(blocks={})),
                   patch.object(offline_catalog.Catalog, "load_compiled", return_value=SimpleNamespace(identity="t")),
                   patch.object(multiprocessing, "get_context", return_value=SimpleNamespace(Pool=Pool)),
                   patch.object(wh.shutil, "disk_usage", return_value=SimpleNamespace(free=wh.MIN_FREE_BYTES + 1 if free_bytes is None else free_bytes))]
        if clock:
            patches.append(patch.object(wh.time, "time", side_effect=clock))
        for p in patches:
            p.start()
        try:
            return wh.build(self.world, self.catalog, self.out, workers=1, max_jobs=max_jobs,**options)
        finally:
            for p in reversed(patches):
                p.stop()

    def status_file(self):
        return json.loads((self.out / "status.json").read_text(encoding="utf-8"))

    def test_heartbeat_is_refreshed_while_waiting_for_slow_workers(self):
        import multiprocessing
        ticks = iter(range(1000, 100000, 7))            # a real clock that advances 7 s per reading
        seen = []
        harness = self

        class SlowResults:
            def __init__(self, jobs):
                self.jobs, self.waits = list(jobs), 0

            def next(self, timeout):
                assert 0 < timeout <= wh.HEARTBEAT_SECONDS, timeout
                status = harness.status_file()
                seen.append((status["state"], status["done"], status["updatedAt"]))
                self.waits += 1
                if self.waits <= 2:
                    raise multiprocessing.TimeoutError()     # nothing finished within the wait
                if not self.jobs:
                    raise StopIteration
                jx, jz, fp = self.jobs.pop(0)
                return harness.record(jx, jz, fp, {f"{jx * 8},{jz * 8}": b"tile"})

        self.sources = {(0, 0): "s", (1, 0): "s"}
        status = self.build(SlowResults, clock=lambda: next(ticks))
        beats = [updated for state, done, updated in seen[:3]]
        self.assertEqual([s[:2] for s in seen[:3]], [("running", 0)] * 3)
        self.assertLess(beats[0], beats[1])
        self.assertLess(beats[1], beats[2], "each timeout rewrote the status with a new heartbeat")
        self.assertEqual((status["state"], status["done"], status["total"]), ("done", 2, 2))
        self.assertGreaterEqual(status["updatedAt"], status["finishedAt"])
        self.assertTrue(status["planComplete"])
        self.assertEqual(self.status_file(), status)

    def test_tile_failures_are_aggregated_and_a_retained_old_tile_is_not_success(self):
        self.sources = {(0, 0): "s1", (1, 0): "s1"}
        self.build(lambda jobs: iter([self.record(0, 0, jobs[0][2], {"0,0": b"A1", "1,0": b"B1"}),
                                      self.record(1, 0, jobs[1][2], {"8,0": b"C1"})]))
        self.sources = {(0, 0): "s2", (1, 0): "s2"}
        status = self.build(lambda jobs: iter([
            self.record(0, 0, jobs[0][2], {"1,0": b"B2"},
                        errors=[(0, 0, "TileError: source read failed for 1 chunk(s)"), (2, 0, "RuntimeError: mesh failed")]),
            self.record(1, 0, jobs[1][2], {"8,0": b"C2"})]))
        self.assertEqual(status["state"], "done", "the run finished ...")
        self.assertEqual((status["failedJobs"], status["failedTiles"]), (1, 2), "... with reported tile failures")
        self.assertEqual(status["errors"][0], {"job": [0, 0], "tile": [0, 0],
                                               "message": "TileError: source read failed for 1 chunk(s)"})
        self.assertEqual(status["staleTiles"], 1, "tile 0,0 keeps its previous good file, counted as stale")
        self.assertFalse(status["planComplete"], "a retained old tile is never reported as complete")
        index = json.loads((self.out / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["stale"], ["0,0"])
        self.assertEqual(self.status_file()["failedTiles"], 2)

    def test_error_list_is_bounded_but_every_failure_is_counted(self):
        self.plan = [(0, 0)]
        self.sources = {(0, 0): "s"}
        errors = [(i, 0, "x" * 500) for i in range(50)]
        status = self.build(lambda jobs: iter([self.record(0, 0, jobs[0][2], errors=errors)]))
        self.assertEqual(status["failedTiles"], 50)
        self.assertEqual(len(status["errors"]), wh.MAX_STATUS_ERRORS)
        self.assertEqual(len(status["errors"][0]["message"]), 300)

    def test_processed_skipped_and_not_scheduled_regions_are_distinguished(self):
        self.sources = {(0, 0): "s1", (1, 0): "s1"}
        first = self.build(lambda jobs: iter([self.record(jx, jz, fp) for jx, jz, fp in jobs]))
        self.assertEqual((first["plannedJobs"], first["total"], first["skippedUnchanged"]), (2, 2, 0))
        self.assertTrue(first["planComplete"])
        unchanged = self.build(lambda jobs: iter([]))
        self.assertEqual((unchanged["total"], unchanged["done"], unchanged["skippedUnchanged"]), (0, 0, 2))
        self.assertTrue(unchanged["planComplete"], "both regions have clean current records")
        self.sources = {(0, 0): "s2", (1, 0): "s2"}
        limited = self.build(lambda jobs: iter([self.record(jx, jz, fp) for jx, jz, fp in jobs]), max_jobs=1)
        self.assertEqual((limited["total"], limited["done"], limited["notScheduled"]), (1, 1, 1))
        self.assertEqual(limited["state"], "done")
        self.assertFalse(limited["planComplete"], "a changed region was not rendered in this run")

    def test_crash_keeps_known_progress_and_failures(self):
        self.sources = {(0, 0): "s", (1, 0): "s"}
        def crashing(jobs):
            yield self.record(0, 0, jobs[0][2], errors=[(0, 0, "RuntimeError: mesh failed")])
            raise RuntimeError("worker pool died")
        with self.assertRaises(RuntimeError):
            self.build(crashing)
        status = self.status_file()
        self.assertTrue(status["state"].startswith("stopped: build failed: RuntimeError"))
        self.assertEqual((status["done"], status["total"], status["failedTiles"]), (1, 2, 1))
        self.assertFalse(status["planComplete"])
        self.assertIn("finishedAt", status)
        self.assertGreaterEqual(status["updatedAt"], status["startedAt"])


if __name__ == "__main__":
    unittest.main()
