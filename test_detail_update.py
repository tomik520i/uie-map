"""Unit tests for detail_update helpers (no server, no world). Stdlib only, Windows/Linux."""
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import capture_batch_file as cbf
import detail_update as du


class StageError(Exception):
    pass


class StageRetryTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.staged = Path(self._temp.name) / "staged"
        self.sleeps = []

    def tearDown(self):
        self._temp.cleanup()

    def stage_failing(self, failures, message="region/r.0.0.mca: changed while it was copied"):
        calls = []

        def stage(level_dat, region_dir, staged, tile_x, tile_z):
            calls.append((tile_x, tile_z))
            self.assertFalse(staged.exists(), "a rejected partial copy is removed before the retry")
            staged.mkdir()
            if len(calls) <= failures:
                raise StageError(message)
        return stage, calls

    def run_stage(self, stage):
        return du._stage_stable(stage, StageError, Path("level.dat"), Path("region"), self.staged, 1, 2,
                                sleep=self.sleeps.append)

    def test_retries_while_the_game_saves(self):
        stage, calls = self.stage_failing(2)
        self.assertEqual(self.run_stage(stage), 2)
        self.assertEqual(calls, [(1, 2)] * 3)
        self.assertEqual(self.sleeps, [du.STAGE_RETRY_SECONDS, 2 * du.STAGE_RETRY_SECONDS])

    def test_gives_up_after_the_last_attempt(self):
        stage, calls = self.stage_failing(du.STAGE_ATTEMPTS)
        with self.assertRaises(StageError):
            self.run_stage(stage)
        self.assertEqual(len(calls), du.STAGE_ATTEMPTS)

    def test_other_stage_errors_fail_at_once(self):
        stage, calls = self.stage_failing(1, message="region/r.0.0.mca: too large")
        with self.assertRaises(StageError):
            self.run_stage(stage)
        self.assertEqual((len(calls), self.sleeps), (1, []))


PIPELINE = ("prepare_warm_plan", "publish_attempt", "render_queue", "runtime_adapter", "stage_saved_tile",
            "staged_batch", "warm_worker")


def fake_pipeline():
    """Stand-ins for the render pipeline modules: any use fails the test (nothing may be staged or rendered)."""
    modules = {name: types.ModuleType(name) for name in PIPELINE}
    fail = MagicMock(side_effect=AssertionError("pipeline must not run"))
    modules["prepare_warm_plan"].prepare_warm_plan = fail
    modules["publish_attempt"].publish_verified_attempt = fail
    modules["render_queue"].RenderQueue = fail
    modules["runtime_adapter"].make_prepare = fail
    modules["stage_saved_tile"].StageError = type("StageError", (Exception,), {})
    modules["stage_saved_tile"].stage_saved_tile = fail
    modules["staged_batch"].create_staged_batch = fail
    modules["warm_worker"].run_one = fail
    return modules, fail


class BoundsTests(unittest.TestCase):
    def test_world_border_bounds_use_the_96_block_tile_window(self):
        area, _ = cbf.tile_areas(du.MAX_TILE, du.MAX_TILE)
        self.assertLessEqual(area.x + area.width, du.WORLD_BORDER)
        area, _ = cbf.tile_areas(du.MIN_TILE, du.MIN_TILE)
        self.assertGreaterEqual(area.x, -du.WORLD_BORDER)
        beyond, _ = cbf.tile_areas(du.MAX_TILE + 1, 0)
        self.assertGreater(beyond.x + beyond.width, du.WORLD_BORDER)
        below, _ = cbf.tile_areas(du.MIN_TILE - 1, 0)
        self.assertLess(below.x, -du.WORLD_BORDER)

    def test_valid_rectangles(self):
        self.assertEqual(du.validate_rect((1, 1, 2, 2)), (1, 1, 2, 2))      # the LIVE 2x2 area (blocks 96..288)
        self.assertEqual(du.validate_rect([0, 0, 15, 15]), (0, 0, 15, 15))  # exactly 256 tiles
        self.assertEqual(du.validate_rect((du.MIN_TILE, 5, du.MIN_TILE, 5)), (du.MIN_TILE, 5, du.MIN_TILE, 5))
        self.assertEqual(du.validate_rect((du.MAX_TILE,) * 4), (du.MAX_TILE,) * 4)

    def test_invalid_rectangles_are_refused_without_enumeration(self):
        for rect in [(1, 1, 2), (1, 1, 2, 2, 3), "1,1,2,2", None, (True, 0, 1, 1), (0.0, 0, 1, 1), ("0", 0, 1, 1),
                     (2, 0, 1, 1), (0, 2, 1, 1), (du.MAX_TILE + 1,) * 4, (du.MIN_TILE - 1, 0, 0, 0),
                     (0, 0, 16, 15), (0, 0, 256, 0),
                     (du.MIN_TILE, du.MIN_TILE, du.MAX_TILE, du.MAX_TILE)]:          # ~4e11 tiles: must not hang
            with self.subTest(rect=rect), self.assertRaises(du.DetailUpdateError):
                du.validate_rect(rect)

    def test_max_tiles_limit(self):
        self.assertEqual((du.validate_max_tiles(1), du.validate_max_tiles(16)), (1, 16))
        for value in (0, 17, -1, True, 1.5, "4", None):
            with self.subTest(value=value), self.assertRaises(du.DetailUpdateError):
                du.validate_max_tiles(value)

    def test_parse_rect_is_strict(self):
        self.assertEqual(du.parse_rect("-3,1,-2,2"), (-3, 1, -2, 2))
        for text in ("1,2,3", "1,2,3,4,5", "1, 2,3,4", "1.0,2,3,4", "a,b,c,d", "", "1,,2,3", "99999999,0,0,0"):
            with self.subTest(text=text), self.assertRaises(du.DetailUpdateError):
                du.parse_rect(text)

    def test_plan_todo_never_drops_full_mode_tiles(self):
        tiles = [(x, 0) for x in range(20)]
        self.assertEqual(du.plan_todo("full", tiles[:16], [], 16), tiles[:16])
        with self.assertRaisesRegex(du.DetailUpdateError, "smaller cut"):
            du.plan_todo("full", tiles, [], 16)
        self.assertEqual(du.plan_todo("update", tiles, tiles[3:], 4), tiles[3:7], "update keeps the budget slice")
        self.assertEqual(du.plan_todo("update", tiles, [], 4), [])


class UpdateBoundsTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        self.world, self.state, self.store = root / "world-absent", root / "state", root / "store"
        self.profile = root / "profile"
        self.profile.mkdir()
        (self.profile / "profile.json").write_text(json.dumps({"identity": {"pipelineKey": "key"}}))
        self.store.mkdir()

    def track(self, key="key"):
        """A current generation with area records of `key` (update mode for that pipeline)."""
        generation = self.store / "generations" / "g1"
        generation.mkdir(parents=True)
        (generation / "COMPLETE.json").write_text(json.dumps({"pipelineKey": key, "areas": [{"tiles": []}]}))
        (self.store / "current.json").write_text(json.dumps({"generation": "g1"}))

    def run_update(self, rect, max_tiles=16):
        modules, fail = fake_pipeline()
        fingerprint = MagicMock(return_value="fp")
        with patch.dict(sys.modules, modules), patch.object(du, "tile_fingerprint", fingerprint):
            result = du.update(self.world, self.state, self.profile, self.store, rect, None, max_tiles)
        return result, fingerprint, fail

    def assert_refused_untouched(self, rect, max_tiles=16):
        with patch.dict(sys.modules, {name: None for name in PIPELINE}), \
                patch.object(du, "tile_fingerprint", MagicMock(side_effect=AssertionError("world read"))):
            with self.assertRaises(du.DetailUpdateError):
                du.update(self.world, self.state, self.profile, self.store, rect, None, max_tiles)
        self.assertFalse(self.state.exists(), "no state directory, status or fingerprints")

    def test_first_full_run_over_budget_stages_nothing(self):
        self.assert_refused_untouched((0, 0, 4, 3))                      # 20 tiles > 16, empty store
        self.assert_refused_untouched((1, 1, 2, 2), max_tiles=3)          # 4 tiles > 3

    def test_full_replacement_of_another_pipeline_over_budget_stages_nothing(self):
        self.track(key="other-pipeline")
        self.assert_refused_untouched((0, 0, 4, 3))

    def test_invalid_rectangle_or_limit_writes_nothing(self):
        for rect, limit in [((0, 0, 99, 99), 16), ((2, 0, 1, 1), 16), ((1, 1, 2, 2), 0), ((1, 1, 2, 2), 17),
                            ((1, 1, 2, 2), True)]:
            with self.subTest(rect=rect, limit=limit):
                self.assert_refused_untouched(rect, limit)

    def test_update_mode_keeps_incremental_behaviour(self):
        self.track()
        tiles = [(tx, tz) for tz in range(0, 4) for tx in range(0, 5)]      # 20 tiles > max_tiles is fine here
        self.state.mkdir()
        (self.state / "fingerprints.json").write_text(json.dumps(
            {"pipelineKey": "key", "tiles": {f"{tx},{tz}": "fp" for tx, tz in tiles}}))
        status, fingerprint, fail = self.run_update((0, 0, 4, 3))
        self.assertEqual((status["mode"], status["tiles"], status["changedTiles"], status["published"]),
                         ("update", 20, 0, False))
        self.assertEqual(status["reason"], "no tile changed")
        self.assertEqual(fingerprint.call_count, 20)
        fail.assert_not_called()
        self.assertTrue((self.state / "status.json").exists())

    def test_working_storage_refusal_prevents_staging(self):
        from detail_storage import DetailStorageError
        modules, fail = fake_pipeline()
        with patch.dict(sys.modules, modules), patch.object(du, 'tile_fingerprint', return_value='changed'), \
                patch('detail_storage.require_run_capacity', side_effect=DetailStorageError('budget reached')):
            with self.assertRaisesRegex(du.DetailUpdateError, 'budget reached'):
                du.update(self.world, self.state, self.profile, self.store, (1,1,1,1), None, 1)
        fail.assert_not_called()
        self.assertFalse((self.state/'runs').exists())

    def test_cli_rejects_bounds_before_creating_state(self):
        base = ["--world", str(self.world), "--state", str(self.state), "--profile", str(self.profile),
                "--store", str(self.store)]
        for extra in (["--tiles", "0,0,99,99"], ["--tiles", "1,1,2"], ["--tiles", "1,1,2,2", "--max-tiles", "17"],
                      ["--tiles", "1,1,2,2", "--max-tiles", "0"], ["--tiles", "1,1,2,2", "--max-tiles", "x"]):
            with self.subTest(extra=extra):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    du.main(base + extra)
                self.assertEqual(raised.exception.code, 2)
                self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
