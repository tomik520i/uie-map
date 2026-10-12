"""Tests for render_queue.py on real SQLite files in a temporary directory.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_render_queue.py" -v
"""
import hashlib
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import render_queue as rq  # noqa: E402
from render_queue import ConflictError, IdentityMismatchError, QueueBusyError, RenderQueue, StaleClaimError  # noqa: E402


def h(text):
    return hashlib.sha256(str(text).encode()).hexdigest()


PIPE = h("pipeline")
A, B, C = h("tile a"), h("tile b"), h("tile c")


def job(job_id, **payload):
    return {"id": job_id, "payload": payload or {"tile": job_id[:6]}}


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Cleanups run last-in-first-out: queues opened later are closed before the directory is removed.
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "queue.sqlite"

    def open(self, world="uie", dimension="overworld", pipeline=PIPE, **kwargs):
        queue = RenderQueue(self.path, world, dimension, pipeline, **kwargs)
        self.addCleanup(queue.close)
        return queue


class IdentityTests(QueueTestCase):
    def test_close_reopen_keeps_jobs_and_states(self):
        with self.open() as q:
            q.enqueue([job(A), job(B)])
            claimed = q.claim(10)
            q.complete(claimed["id"], claimed["token"], {"files": 3}, now=11)
        q = self.open()
        self.assertEqual(q.stats(), {"queued": 1, "running": 0, "succeeded": 1, "failed": 0, "total": 2})
        done = q.list_jobs("succeeded")[0]
        self.assertEqual((done["id"], done["result"], done["finishedAt"], done["attempt"]), (A, {"files": 3}, 11.0, 1))

    def test_other_identity_is_rejected(self):
        self.open().close()
        for kwargs in ({"world": "other"}, {"dimension": "nether"}, {"pipeline": h("pipeline 2")}, {"max_attempts": 5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(IdentityMismatchError):
                self.open(**kwargs)
        self.open().close()

    def test_same_fingerprint_in_other_world_file_is_rejected(self):
        with self.open() as q:
            q.enqueue([job(A)])
        with self.assertRaisesRegex(IdentityMismatchError, "world_id"):
            self.open(world="copy-2")
        self.assertEqual(self.open().stats()["total"], 1)

    def test_constructor_validation(self):
        for kwargs in ({"world": ""}, {"world": 5}, {"dimension": "x\x00"}, {"pipeline": PIPE.upper()},
                       {"pipeline": "abc"}, {"max_attempts": 0}, {"max_attempts": True}, {"max_attempts": 2.0},
                       {"busy_timeout": math.nan}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.open(**kwargs)

    def test_closed_queue_refuses_operations(self):
        q = self.open()
        q.close()
        q.close()
        with self.assertRaises(rq.QueueError):
            q.stats()


class EnqueueTests(QueueTestCase):
    def test_idempotent_by_canonical_payload(self):
        q = self.open()
        self.assertEqual(q.enqueue([{"id": A, "payload": {"b": 1, "a": [1, 2]}}]), {"added": 1, "existing": 0})
        self.assertEqual(q.enqueue([{"id": A, "payload": {"a": [1, 2], "b": 1}}]), {"added": 0, "existing": 1})
        self.assertEqual(q.stats()["total"], 1)

    def test_conflict_rejects_whole_batch(self):
        q = self.open()
        q.enqueue([job(A, x=1)])
        with self.assertRaises(ConflictError):
            q.enqueue([job(B), job(A, x=2), job(C)])
        self.assertEqual([j["id"] for j in q.list_jobs()], [A], "nothing from the failed batch was stored")
        with self.assertRaises(ConflictError):
            q.enqueue([job(B, x=1), job(B, x=2)])
        q.enqueue([job(B, x=1), job(B, x=1)])
        self.assertEqual(q.stats()["total"], 2)

    def test_malformed_jobs_are_rejected_before_writing(self):
        q = self.open()
        bad = [
            [{"id": A.upper(), "payload": {}}], [{"id": A[:-1], "payload": {}}], [{"id": A}],
            [{"id": A, "payload": {}, "extra": 1}], [{"id": A, "payload": []}], [{"id": A, "payload": {"v": math.nan}}],
            [{"id": A, "payload": {"v": math.inf}}], [{"id": A, "payload": {"v": (1, 2)}}], [{"id": A, "payload": {1: "x"}}],
            [{"id": A, "payload": {"v": object()}}], [{"id": A, "payload": {"v": "x" * (1 << 20)}}], "jobs", None,
        ]
        for jobs in bad:
            with self.subTest(jobs=str(jobs)[:60]), self.assertRaises(ValueError):
                q.enqueue([job(B)] + jobs if isinstance(jobs, list) else jobs)
        self.assertEqual(q.stats()["total"], 0)

    def test_empty_enqueue_and_reopen_preserve_failed_ledger(self):
        q = self.open(max_attempts=1)
        q.enqueue([job(A)])
        claimed = q.claim(0)
        self.assertEqual(q.fail(A, claimed["token"], "boom", 1), "failed")
        q.enqueue([])
        q.enqueue([job(A)])  # same inventory again: the failure is not forgotten
        q.close()
        q = self.open(max_attempts=1)
        failed = q.list_jobs("failed")
        self.assertEqual([(j["id"], j["error"], j["attempt"]) for j in failed], [(A, "boom", 1)])
        self.assertIsNone(q.claim(100))


class ClaimTests(QueueTestCase):
    def test_order_single_running_and_no_reclaim_after_success(self):
        q = self.open()
        q.enqueue([job(A), job(B)])
        first = q.claim(0)
        self.assertEqual((first["id"], first["attempt"], first["payload"]), (A, 1, {"tile": A[:6]}))
        self.assertRegex(first["token"], r"^[0-9a-f]{32}$")
        self.assertIsNone(q.claim(0), "at most one running job")
        q.complete(A, first["token"], {"ok": True}, now=1)
        second = q.claim(1)
        self.assertEqual(second["id"], B)
        self.assertNotEqual(second["token"], first["token"])
        q.complete(B, second["token"], {"ok": True}, now=2)
        self.assertIsNone(q.claim(1000), "succeeded jobs are never claimed again")
        self.assertEqual(q.enqueue([job(A)]), {"added": 0, "existing": 1})
        self.assertEqual(q.stats()["succeeded"], 2)

    def test_tokens_are_checked(self):
        q = self.open()
        q.enqueue([job(A)])
        claimed = q.claim(0)
        with self.assertRaises(StaleClaimError):
            q.complete(A, "0" * 32, {}, now=1)
        with self.assertRaises(StaleClaimError):
            q.fail(B, claimed["token"], "x", 1)
        q.complete(A, claimed["token"], {"n": 1}, now=1)
        for call in (lambda: q.complete(A, claimed["token"], {"n": 2}, now=2), lambda: q.fail(A, claimed["token"], "x", 2)):
            with self.assertRaises(StaleClaimError):
                call()
        self.assertEqual(q.list_jobs()[0]["result"], {"n": 1})

    def test_retry_backoff_and_max_attempts(self):
        q = self.open(max_attempts=2)
        q.enqueue([job(A)])
        first = q.claim(100)
        self.assertEqual(q.fail(A, first["token"], "timeout", 100, retry_delay=10), "queued")
        self.assertIsNone(q.claim(109.999))
        second = q.claim(110)
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(q.fail(A, second["token"], "timeout again", 111, retry_delay=0), "failed")
        self.assertIsNone(q.claim(10_000))
        self.assertEqual(q.stats(), {"queued": 0, "running": 0, "succeeded": 0, "failed": 1, "total": 1})

    def test_error_text_is_bounded(self):
        q = self.open(max_attempts=1)
        q.enqueue([job(A)])
        claimed = q.claim(0)
        q.fail(A, claimed["token"], "e" * 5000, 1)
        error = q.list_jobs()[0]["error"]
        self.assertEqual(len(error), rq.MAX_ERROR_CHARS)
        self.assertTrue(error.endswith("…"))

    def test_invalid_numbers_and_results(self):
        q = self.open()
        q.enqueue([job(A)])
        for now in (math.nan, math.inf, -math.inf, True, "1", None):
            with self.subTest(now=now), self.assertRaises(ValueError):
                q.claim(now)
        claimed = q.claim(0)
        for bad in ({"retry_delay": -1}, {"retry_delay": math.nan}, {"now": math.inf}):
            args = {"now": 1, "retry_delay": 5, **bad}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                q.fail(A, claimed["token"], "x", args["now"], retry_delay=args["retry_delay"])
        with self.assertRaises(ValueError):
            q.fail(A, claimed["token"], None, 1)
        for result in ({"v": math.nan}, [], {"v": (1,)}):
            with self.assertRaises(ValueError):
                q.complete(A, claimed["token"], result, now=1)
        with self.assertRaises(ValueError):
            q.recover_running(math.nan)
        self.assertEqual(q.stats()["running"], 1, "rejected calls changed nothing")


class RecoveryAndConcurrencyTests(QueueTestCase):
    def test_retry_time_overflow_preserves_running_claim(self):
        q = self.open()
        q.enqueue([job(A)])
        claimed = q.claim(0)
        with self.assertRaises(ValueError):
            q.fail(A, claimed["token"], "retry", 1e308, retry_delay=1e308)
        with self.assertRaises(ValueError):
            q.recover_running(1e308, retry_delay=1e308)
        self.assertEqual(q.stats()["running"], 1)
        q.complete(A, claimed["token"], {"verified": True}, now=1)
        self.assertEqual(q.stats()["succeeded"], 1)

    def test_recovery_invalidates_the_old_token(self):
        q = self.open(max_attempts=2)
        q.enqueue([job(A)])
        lost = q.claim(0)
        q.close()  # simulated crash: the worker never reports back
        q = self.open(max_attempts=2)
        self.assertIsNone(q.claim(5), "claim never steals a running job")
        self.assertEqual(q.recover_running(5, retry_delay=3), [{"id": A, "state": "queued"}])
        with self.assertRaises(StaleClaimError):
            q.complete(A, lost["token"], {"late": True}, now=6)
        self.assertIsNone(q.claim(7))
        again = q.claim(8)
        self.assertEqual(again["attempt"], 2)
        self.assertIn("interrupted", q.list_jobs()[0]["error"])
        self.assertEqual(q.recover_running(9), [{"id": A, "state": "failed"}], "attempts exhausted")
        self.assertEqual(q.recover_running(10), [])
        with self.assertRaises(StaleClaimError):
            q.fail(A, again["token"], "late", 11)

    def test_two_instances_share_one_running_slot_and_lock(self):
        first = self.open()
        second = self.open(busy_timeout=0.05)
        first.enqueue([job(A), job(B)])
        claimed = first.claim(0)
        self.assertIsNone(second.claim(0), "the other instance sees the running job")
        first._conn.execute("BEGIN IMMEDIATE")  # another writer holds the lock
        try:
            with self.assertRaises(QueueBusyError):
                second.claim(0)
            with self.assertRaises(QueueBusyError):
                second.enqueue([job(C)])
        finally:
            first._conn.execute("ROLLBACK")
        second.complete(A, claimed["token"], {"by": "second"}, now=1)
        with self.assertRaises(StaleClaimError):
            first.complete(A, claimed["token"], {"by": "first"}, now=2)
        self.assertEqual(first.claim(2)["id"], B)
        self.assertEqual(second.stats(), {"queued": 0, "running": 1, "succeeded": 1, "failed": 0, "total": 2})

    def test_running_claim_returns_the_live_token_only_there(self):
        q = self.open(max_attempts=3)
        q.enqueue([job(A)])
        self.assertIsNone(q.running_claim())
        claimed = q.claim(0)
        self.assertEqual(q.running_claim(), claimed, "same id, attempt, token and payload as the claim")
        for entry in q.list_jobs():
            self.assertNotIn("token", entry)
            self.assertNotIn(claimed["token"], repr(entry))
        self.assertNotIn(claimed["token"], repr(q.stats()))
        q.recover_running(1)
        self.assertIsNone(q.running_claim(), "the recovered token is no longer live")
        again = q.claim(2)
        self.assertEqual(q.running_claim()["token"], again["token"])
        self.assertNotEqual(again["token"], claimed["token"])
        with self.assertRaises(StaleClaimError):
            q.complete(A, claimed["token"], {}, now=3)
        q.complete(A, again["token"], {"ok": True}, now=3)
        self.assertIsNone(q.running_claim())
        q.close()
        with self.assertRaises(rq.QueueError):
            q.running_claim()

    def test_database_path_is_the_canonical_file(self):
        q = self.open()
        self.assertEqual(q.database_path(), os.path.realpath(self.path))
        alias = Path(self._tmp.name) / "alias.sqlite"
        try:
            os.symlink(self.path, alias)
        except (OSError, NotImplementedError):
            alias = None
        if alias is not None:
            through_alias = RenderQueue(alias, "uie", "overworld", PIPE)
            self.addCleanup(through_alias.close)
            self.assertEqual(through_alias.database_path(), q.database_path(), "an alias resolves to the same file")
        memory = RenderQueue(":memory:", "uie", "overworld", PIPE)
        self.addCleanup(memory.close)
        self.assertIsNone(memory.database_path())
        memory.close()
        with self.assertRaises(rq.QueueError):
            memory.database_path()

    def test_event_sequence_with_retry_and_success(self):
        q = self.open(max_attempts=3)
        self.assertEqual(q.enqueue([job(A, tile=[0, 0]), job(B, tile=[1, 0])]), {"added": 2, "existing": 0})
        a1 = q.claim(100)
        self.assertEqual((a1["id"], a1["attempt"]), (A, 1))
        self.assertEqual(q.fail(A, a1["token"], "Java batch exited 1", 101, retry_delay=30), "queued")
        b1 = q.claim(102)
        self.assertEqual(b1["id"], B, "A is not due yet, B runs")
        q.complete(B, b1["token"], {"chunks": 36, "sha256": h("b")}, now=120)
        self.assertIsNone(q.claim(130), "A is due at 131")
        a2 = q.claim(131)
        self.assertEqual((a2["id"], a2["attempt"], a2["payload"]), (A, 2, {"tile": [0, 0]}))
        q.complete(A, a2["token"], {"chunks": 36, "sha256": h("a")}, now=150)
        self.assertIsNone(q.claim(1000))
        jobs = {j["id"]: j for j in q.list_jobs()}
        self.assertEqual((jobs[A]["state"], jobs[A]["attempt"], jobs[A]["error"]), ("succeeded", 2, "Java batch exited 1"))
        self.assertEqual((jobs[B]["state"], jobs[B]["attempt"], jobs[B]["error"]), ("succeeded", 1, None))
        self.assertEqual(jobs[A]["result"], {"chunks": 36, "sha256": h("a")})
        self.assertEqual([j["seq"] for j in q.list_jobs()], sorted(j["seq"] for j in q.list_jobs()))
        self.assertEqual(q.stats(), {"queued": 0, "running": 0, "succeeded": 2, "failed": 0, "total": 2})
        with self.assertRaises(ValueError):
            q.list_jobs("done")


if __name__ == "__main__":
    unittest.main()
