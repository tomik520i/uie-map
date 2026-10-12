"""Q9 publication continuity of world_hires.py: last good tile references, per-tile failures, strict source reads.

Stdlib only, Windows/Linux. The multiprocessing pool is replaced by an in-process scripted pool, so build() runs
its real planning, index and status code against files in a temporary directory.
    python -m unittest test_world_hires_continuity -v      (from work/uie-map)
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import world_hires as wh
from test_world_hires import quad


def sha12(data):
    return hashlib.sha256(data).hexdigest()[:12]


class BuildHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.world, self.catalog, self.out = base / "world", base / "catalog", base / "out"
        self.world.mkdir(); self.catalog.mkdir()
        (self.world / "level.dat").write_bytes(b"test")
        (self.catalog / "atlas.png").write_bytes(b"atlas-1")
        (self.catalog / "atlas-layout.json").write_text("{}", encoding="utf-8")
        self.catalog_identity = "catalog-1"
        self.sources = {}
        self.snapshots = []

    def tile(self, key):
        return self.out / "tiles" / f"{key.replace(',', '_')}.uih"

    def job(self, tiles=None, errors=()):
        """Script of one job like render_job: tiles {key: bytes | None (rendered empty)}, failed tile keys."""
        def run(jx, jz, fp):
            record = {"job": [jx, jz], "fingerprint": fp, "tiles": {}, "chunks": 1, "quads": 1, "bytes": 0,
                      "fallback": 0, "errors": [[*map(int, key.split(",")), "mesh failed"] for key in errors]}
            for key, data in (tiles or {}).items():
                if data is None:
                    self.tile(key).unlink(missing_ok=True)
                else:
                    self.tile(key).write_bytes(data)
                    record["tiles"][key] = sha12(data)
            (self.out / "jobs" / f"{jx}_{jz}.json").write_text(json.dumps(record), encoding="utf-8")
            return record
        return run

    def index(self):
        return json.loads((self.out / "index.json").read_text(encoding="utf-8"))

    def build(self, script, max_jobs=None, **options):
        harness = self

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
                for jx, jz, fp in list(jobs):
                    record = script[(jx, jz)](jx, jz, fp)
                    path = harness.out / "index.json"
                    harness.snapshots.append(json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)
                    yield record

        import multiprocessing
        import offline_catalog
        import reader
        fingerprint = lambda _dir, area: self.sources[((area.x + wh.HALO) // wh.JOB, (area.z + wh.HALO) // wh.JOB)]
        with (patch.object(wh, "plan_jobs", return_value=[(0, 0), (1, 0)]),
              patch.object(wh, "fingerprint", side_effect=fingerprint),
              patch.object(reader, "parse_level_dat", return_value=None),
              patch.object(reader, "parse_fml_registry", return_value=SimpleNamespace(blocks={})),
              patch.object(offline_catalog.Catalog, "load_compiled",
                           return_value=SimpleNamespace(identity=self.catalog_identity)),
              patch.object(multiprocessing, "get_context", return_value=SimpleNamespace(Pool=Pool)),
              patch.object(wh.shutil, "disk_usage", return_value=SimpleNamespace(free=wh.MIN_FREE_BYTES + 1))):
            self.snapshots = []
            return wh.build(self.world, self.catalog, self.out, workers=1, max_jobs=max_jobs, **options)

    def first_build(self):
        """Published state: job 0,0 -> tiles 0,0 (A1) and 1,0 (C1); job 1,0 -> tile 8,0 (B1)."""
        self.sources = {(0, 0): "s1", (1, 0): "s1"}
        self.build({(0, 0): self.job({"0,0": b"A1", "1,0": b"C1"}), (1, 0): self.job({"8,0": b"B1"})})
        index = self.index()
        self.assertEqual(index["tiles"], {"0,0": sha12(b"A1"), "1,0": sha12(b"C1"), "8,0": sha12(b"B1")})
        self.assertNotIn("stale", index)
        self.sources = {(0, 0): "s2", (1, 0): "s2"}     # both regions changed: both jobs re-render


class ContinuityTests(BuildHarness):
    def test_pending_region_failed_tile_and_empty_tile(self):
        self.first_build()
        status = self.build({(0, 0): self.job({"0,0": b"A2"}, errors=["1,0"]), (1, 0): self.job({"8,0": None})})
        # Intermediate index after job 0,0 while job 1,0 is still pending: nothing of the old map disappears.
        middle = self.snapshots[1]
        self.assertEqual(middle["tiles"], {"0,0": sha12(b"A2"), "1,0": sha12(b"C1"), "8,0": sha12(b"B1")})
        self.assertEqual(middle["stale"], ["1,0", "8,0"])
        # Final: 0,0 replaced, 1,0 failed -> previous good revision kept but stale, 8,0 rendered empty -> removed.
        final = self.index()
        self.assertEqual(final["tiles"], {"0,0": sha12(b"A2"), "1,0": sha12(b"C1")})
        self.assertEqual(final["stale"], ["1,0"])
        self.assertFalse(self.tile("8,0").exists())
        job = json.loads((self.out / "jobs" / "0_0.json").read_text(encoding="utf-8"))
        self.assertNotIn("1,0", job["tiles"], "a preserved tile is never recorded as rendered")
        self.assertEqual(job["errors"][0][:2], [1, 0], "the failure stays recorded for retry")
        self.assertEqual((status["state"], status["staleTiles"]), ("done", 1))

    def test_failed_tile_is_retried_and_then_becomes_current(self):
        self.first_build()
        self.build({(0, 0): self.job({"0,0": b"A2"}, errors=["1,0"]), (1, 0): self.job({"8,0": b"B1"})})
        self.sources = {(0, 0): "s2", (1, 0): "s2"}      # unchanged source, but job 0,0 has errors -> retried
        self.build({(0, 0): self.job({"0,0": b"A2", "1,0": b"C2"})})
        final = self.index()
        self.assertEqual(final["tiles"], {"0,0": sha12(b"A2"), "1,0": sha12(b"C2"), "8,0": sha12(b"B1")})
        self.assertNotIn("stale", final)

    def test_jobs_not_scheduled_this_run_keep_their_tiles(self):
        self.first_build()
        status = self.build({(0, 0): self.job({"0,0": b"A2", "1,0": b"C2"})}, max_jobs=1)
        final = self.index()
        self.assertEqual(final["tiles"]["8,0"], sha12(b"B1"))
        self.assertEqual(final["stale"], ["8,0"])
        self.assertEqual(status["total"], 1)

    def test_incompatible_atlas_or_renderer_never_carries_old_geometry(self):
        for change in ("atlas", "renderer"):
            with self.subTest(change=change):
                self.setUp()
                self.first_build()
                if change == "atlas":
                    (self.catalog / "atlas.png").write_bytes(b"atlas-2")
                else:
                    self.catalog_identity = "catalog-2"
                self.build({(0, 0): self.job(errors=["0,0", "1,0"])}, max_jobs=1)
                final = self.index()
                self.assertEqual(final["tiles"], {}, "neither failed nor pending tiles of the old identity")
                self.assertNotIn("stale", final)
                self.assertTrue(self.tile("8,0").exists(), "old files are not deleted, only not referenced")

    def test_corrupt_or_missing_old_tile_is_not_referenced(self):
        self.first_build()
        self.tile("1,0").write_bytes(b"tampered at the mutable path")
        self.tile("8,0").unlink()
        self.build({(0, 0): self.job({"0,0": b"A2"}, errors=["1,0"])}, max_jobs=1)
        final = self.index()
        self.assertEqual(final["tiles"], {"0,0": sha12(b"A2")})
        self.assertNotIn("stale", final)

    def test_tile_changed_during_the_run_is_rechecked(self):
        self.first_build()
        def job1(jx, jz, fp):
            # The job overwrote the mutable path and then failed: the old reference no longer matches the file.
            self.tile("8,0").write_bytes(b"B2 partially written before the tile failed")
            return self.job(errors=["8,0"])(jx, jz, fp)
        self.build({(0, 0): self.job({"0,0": b"A2", "1,0": b"C2"}), (1, 0): job1})
        self.assertEqual(self.snapshots[1]["tiles"]["8,0"], sha12(b"B1"), "verified while pending")
        final = self.index()
        self.assertNotIn("8,0", final["tiles"])
        self.assertNotIn("stale", final)

    def test_crashed_build_is_stopped_not_done_and_keeps_the_last_index(self):
        self.first_build()
        before = (self.out / "index.json").read_bytes()
        def crash(jx, jz, fp):
            raise RuntimeError("worker died")
        with self.assertRaises(RuntimeError):
            self.build({(0, 0): crash})
        status = json.loads((self.out / "status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["state"].startswith("stopped: build failed: RuntimeError"), status["state"])
        self.assertIn("finishedAt", status)
        self.assertEqual((self.out / "index.json").read_bytes(), before)


class RenderRowsTests(unittest.TestCase):
    """Per-tile outcomes in the worker: only a successful empty render deletes; every failure keeps the old file."""

    def test_each_failure_keeps_the_previous_file_and_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "tiles").mkdir()
            old = {tx: f"old-{tx}".encode() for tx in (1, 2, 3, 4, 5)}
            for tx, data in old.items():
                (out / "tiles" / f"{tx}_0.uih").write_bytes(data)

            def read_chunks(copies, strip, failed=None):
                if strip.z != -wh.HALO:
                    return {}
                failed.add((12, 0))      # a core chunk of tile 3,0 exists but cannot be read
                return {(cx, cz): object() for cx in range(24) for cz in range(4) if (cx, cz) != (12, 0)}

            class Mesher:
                def mesh_batch(self, origin):
                    tx, _tz = origin
                    if tx == 1:
                        raise RuntimeError("mesh failed")
                    if tx == 2:
                        return {"chunks": {}, "stats": {"fallbackBlocks": {}}}           # rendered, known empty
                    x = tx * 64 + (1000 if tx == 4 else 1)                               # 4: outside -> encoding error
                    return {"chunks": {(tx * 4, 0): quad(x, 64, 1)}, "stats": {"fallbackBlocks": {"x": 2}}}

            real_write = wh._atomic_write

            def atomic_write(path, data):
                if path.name == "5_0.uih":                                               # 5: write error
                    with patch.object(wh.os, "replace", side_effect=OSError("disk full")):
                        return real_write(path, data)
                return real_write(path, data)

            record = {"tiles": {}, "chunks": 0, "quads": 0, "bytes": 0, "fallback": 0, "errors": []}
            area, core = wh.job_area(0, 0)
            with (patch.object(wh, "read_chunks", side_effect=read_chunks),
                  patch.object(wh, "tile_area", side_effect=lambda tx, tz: (
                      wh._Rect(tx * 64 - 16, tz * 64 - 16, 96, 96), wh._Rect(tx * 64, tz * 64, 64, 64))),
                  patch.object(wh, "_batch", side_effect=lambda cb, a, c, window: (c.x // 64, c.z // 64)),
                  patch.object(wh, "_atomic_write", side_effect=atomic_write),
                  patch.dict(wh._W, {"mesher": Mesher()})):
                wh._render_rows(record, {}, area, core, out, SimpleNamespace(), lambda batch: batch)

            self.assertEqual(set(record["tiles"]), {"0,0"})
            self.assertEqual(record["tiles"]["0,0"], sha12((out / "tiles" / "0_0.uih").read_bytes()))
            errors = {(tx, tz): message for tx, tz, message in record["errors"]}
            self.assertEqual(set(errors), {(1, 0), (3, 0), (4, 0), (5, 0)})
            self.assertIn("source read failed", errors[(3, 0)])
            self.assertIn("HiresError", errors[(4, 0)])
            self.assertIn("disk full", errors[(5, 0)])
            self.assertFalse((out / "tiles" / "2_0.uih").exists(), "a successful empty render removes the tile")
            for tx in (1, 3, 4, 5):
                self.assertEqual((out / "tiles" / f"{tx}_0.uih").read_bytes(), old[tx], f"tile {tx},0 kept")
            self.assertEqual([p.name for p in (out / "tiles").iterdir() if p.name.endswith(".tmp")], [])
            self.assertEqual(record["fallback"], 2, "only successful tiles are counted")
            self.assertEqual(record["chunks"], 16 + 16, "tiles 0,0 and 2,0")


class StrictReadTests(unittest.TestCase):
    def fake_reader(self, open_error=None):
        import reader

        class Region:
            def __init__(self, copy, rx, rz):
                if open_error:
                    raise open_error

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def read_chunk_nbt(self, lx, lz):
                if (lx, lz) == (1, 0):
                    raise ValueError("damaged chunk")
                if (lx, lz) == (2, 0):
                    return None                                   # not generated: absent, not a failure
                return {"x": 99 if (lx, lz) == (3, 0) else lx, "z": lz}

        decode = lambda root: SimpleNamespace(x=root["x"], z=root["z"], biomes=bytes(256))
        return patch.multiple(reader, RegionFile=Region, decode_chunk=decode)

    def test_damaged_chunks_are_reported_only_in_strict_mode(self):
        area, copies = wh._Rect(0, 0, 64, 64), {(0, 0): Path("r.0.0.mca")}
        with self.fake_reader():
            loose = wh.read_chunks(copies, area)
            failed = set()
            strict = wh.read_chunks(copies, area, failed=failed)
        self.assertEqual(len(loose), 13)
        self.assertEqual(strict.keys(), loose.keys())
        self.assertEqual(failed, {(1, 0), (3, 0)})

    def test_unreadable_region_fails_its_chunks_in_strict_mode_and_raises_otherwise(self):
        area, copies = wh._Rect(0, 0, 64, 64), {(0, 0): Path("r.0.0.mca")}
        with self.fake_reader(open_error=OSError("bad header")):
            with self.assertRaises(OSError):
                wh.read_chunks(copies, area)
            failed = set()
            self.assertEqual(wh.read_chunks(copies, area, failed=failed), {})
        self.assertEqual(failed, {(cx, cz) for cx in range(4) for cz in range(4)})


if __name__ == "__main__":
    unittest.main()
