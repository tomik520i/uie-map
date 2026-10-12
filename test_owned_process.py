"""Tests for owned_process. Linux cases start short real Python children (each sleeps at most 60 s, so even a
failed cleanup cannot leave them running long); they never start Minecraft. Stdlib only."""
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import owned_process as op

LINUX = sys.platform.startswith("linux") and hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
HERE = Path(__file__).resolve().parent
OTHER_BOOT = "00000000-0000-4000-8000-000000000000"

READY = ("import os, sys\n"
         "def ready(text=''):\n"
         "    with open(sys.argv[1] + '.tmp', 'w') as handle:\n"
         "        handle.write(text)\n"
         "    os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n")
SLEEPER = READY + "import time\nready(str(os.getpid()))\ntime.sleep(60)\n"
IGNORES_TERM = READY + "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nready()\ntime.sleep(60)\n"
# Starts a grandchild in its own session (it inherits the nonce) and keeps running itself.
SETSID_PARENT = ("import subprocess, sys, time\n"
                 "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]], start_new_session=True)\n"
                 "time.sleep(60)\n")
# A short supervisor that launches with the PERSISTED scope (unchanged) and exits without stopping it.
LAUNCHER = ("import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import owned_process\n"
            "with open(sys.argv[2], 'rb') as handle:\n"
            "    scope = json.loads(handle.read())\n"
            "with open(sys.argv[3], 'wb') as log:\n"
            "    owned_process.launch_owned([sys.executable, '-c', sys.argv[4], sys.argv[5]], cwd=sys.argv[6],\n"
            "                               log=log, scope=scope)\n")


def fake_scope(**change):
    return {"nonce": secrets.token_hex(16), "bootId": OTHER_BOOT, "notBeforeTicks": 0, **change}


def exited(pidfd, timeout=0.0):
    import select
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(int(timeout * 1000)))


class ArgumentTests(unittest.TestCase):
    """Run on every platform: nothing may be started for invalid arguments or an unsupported platform."""

    def test_invalid_scopes_and_arguments_are_refused_before_launch(self):
        good = fake_scope()   # schema-valid; every case below fails before the boot is checked
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(op.subprocess, "Popen", side_effect=AssertionError("launched")) as popen:
            command = [sys.executable, "-c", "pass"]
            scopes = {
                "not a dict": "x" * 32, "missing key": {"nonce": good["nonce"], "bootId": OTHER_BOOT},
                "extra key": {**good, "pid": 1}, "uppercase nonce": {**good, "nonce": "A" * 32},
                "short nonce": {**good, "nonce": "a" * 31}, "bytes nonce": {**good, "nonce": b"a" * 32},
                "newline nonce": {**good, "nonce": good["nonce"] + "\n"}, "boot id": {**good, "bootId": "x"},
                "uppercase boot id": {**good, "bootId": OTHER_BOOT.upper().replace("0", "A")},
                "bool ticks": {**good, "notBeforeTicks": True}, "negative ticks": {**good, "notBeforeTicks": -1},
                "float ticks": {**good, "notBeforeTicks": 1.0}, "huge ticks": {**good, "notBeforeTicks": 2 ** 53},
            }
            for label, scope in scopes.items():
                for call in (lambda: op.launch_owned(command, cwd=temp, log=subprocess.DEVNULL, scope=scope),
                             lambda: op.inspect_owned(scope), lambda: op.stop_owned(scope)):
                    with self.subTest(label), self.assertRaisesRegex(op.OwnedProcessError, "scope"):
                        call()
            cases = {
                "string command": dict(command="python -c pass"),
                "empty command": dict(command=[]),
                "NUL argument": dict(command=[sys.executable, "-c", "pass\0"]),
                "missing cwd": dict(cwd=os.path.join(temp, "missing")),
                "pipe constant": dict(log=subprocess.PIPE),
                "path log": dict(log=os.path.join(temp, "log")),
                "other nonce in env": dict(env={op.NONCE_ENV: secrets.token_hex(16)}),
                "non-string env": dict(env={"A": 1}),
            }
            for label, change in cases.items():
                with self.subTest(label), self.assertRaises(op.OwnedProcessError):
                    op.launch_owned(**{"command": command, "cwd": temp, "log": subprocess.DEVNULL, "scope": good,
                                       **change})
            for label, call in {"negative timeout": lambda: op.stop_owned(good, term_timeout=-1),
                                "huge timeout": lambda: op.stop_owned(good, kill_timeout=1e9),
                                "nan timeout": lambda: op.stop_owned(good, term_timeout=float("nan")),
                                "integer process": lambda: op.stop_owned(good, process=1234),
                                "mock process": lambda: op.stop_owned(good, process=mock.Mock())}.items():
                with self.subTest(label), self.assertRaises(op.OwnedProcessError):
                    call()
            popen.assert_not_called()

    @unittest.skipIf(LINUX, "the fail-closed platform check is only observable off Linux")
    def test_unsupported_platform_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(op.subprocess, "Popen", side_effect=AssertionError("launched")) as popen:
            with self.assertRaises(op.UnsupportedPlatformError):
                op.create_scope()
            scope = fake_scope()
            with self.assertRaises(op.UnsupportedPlatformError):
                op.launch_owned([sys.executable, "-c", "pass"], cwd=temp, log=subprocess.DEVNULL, scope=scope)
            for call in (op.inspect_owned, op.stop_owned):
                with self.assertRaises(op.UnsupportedPlatformError):
                    call(scope)
            popen.assert_not_called()


@unittest.skipUnless(LINUX, "process ownership cases need Linux with pidfd_open and pidfd_send_signal")
class OwnedProcessLinuxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()
        self.scopes, self.processes, self.pidfds = [], [], []
        self.addCleanup(self.cleanup)   # runs before the directory cleanup

    def cleanup(self):
        """Stop only what this test owns, even after an assertion failure; report a failed cleanup."""
        errors = []
        for scope in self.scopes:
            try:
                op.stop_owned(scope, term_timeout=1, kill_timeout=3)
            except Exception as exc:   # still try the handles below
                errors.append(exc)
        for pidfd in self.pidfds:   # pidfds refer to exactly the test's processes, never a reused PID
            try:
                if not exited(pidfd):
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(pidfd)
        for process in self.processes:
            if process.poll() is None:
                process.kill()   # our unreaped direct child: its PID cannot have been reused
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def scope(self):
        scope = op.create_scope()
        self.scopes.append(scope)
        return scope

    def launch(self, code, *args, scope=None, env=None):
        scope = scope or self.scope()
        log = open(self.dir / f"{scope['nonce']}-{len(self.processes)}.log", "wb")
        self.addCleanup(log.close)
        process = op.launch_owned([sys.executable, "-c", code, *args], cwd=self.dir, log=log, scope=scope, env=env)
        self.processes.append(process)
        return scope, process

    def ready(self, path, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                self.fail(f"{path.name} did not appear within {timeout} s")
            time.sleep(0.02)
        return path.read_text()

    def pidfd(self, pid):
        pidfd = os.pidfd_open(pid)
        self.pidfds.append(pidfd)
        return pidfd

    # ------------------------------------------------------------------ scope

    def test_scope_is_plain_json_for_this_boot_and_stale_or_future_scopes_are_refused(self):
        scope = op.create_scope()
        self.assertEqual(set(scope), {"nonce", "bootId", "notBeforeTicks"})
        self.assertEqual(json.loads(json.dumps(scope)), scope)
        with open("/proc/sys/kernel/random/boot_id") as handle:
            self.assertEqual(scope["bootId"], handle.read().strip())
        self.assertLessEqual(scope["notBeforeTicks"], op._uptime_ticks() - op.ROUNDING_TICKS)
        self.assertNotEqual(op.create_scope()["nonce"], scope["nonce"])
        with mock.patch.object(op.subprocess, "Popen", side_effect=AssertionError("launched")) as popen:
            for label, bad, error in (("other boot", {**scope, "bootId": OTHER_BOOT}, op.StaleScopeError),
                                      ("future cutoff", {**scope, "notBeforeTicks": op._uptime_ticks() + 10_000},
                                       op.OwnedProcessError)):
                for call in (lambda: op.launch_owned([sys.executable, "-c", "pass"], cwd=self.dir,
                                                     log=subprocess.DEVNULL, scope=bad),
                             lambda: op.inspect_owned(bad), lambda: op.stop_owned(bad)):
                    with self.subTest(label), self.assertRaises(error):
                        call()
            popen.assert_not_called()

    # ------------------------------------------------------------------ launch and stop

    def test_ordinary_child_is_terminated_and_reaped(self):
        scope, process = self.launch(SLEEPER, str(self.dir / "ready"))
        self.ready(self.dir / "ready")
        self.assertEqual(op.inspect_owned(scope), [{"pid": process.pid}])
        with self.assertRaisesRegex(op.OwnedProcessError, "already carried"):
            self.launch(SLEEPER, str(self.dir / "second"), scope=scope)
        result = op.stop_owned(scope, term_timeout=5, kill_timeout=5, process=process)
        self.assertEqual(result, {"stopped": True, "live": [], "terminated": [process.pid], "killed": [],
                                  "escalated": False, "directChildWithoutNonce": False,
                                  "returncode": -signal.SIGTERM})
        self.assertEqual(process.returncode, -signal.SIGTERM)
        self.assertEqual(op.inspect_owned(scope), [])

    def test_inspect_and_stop_return_pids_only(self):
        secret = "s3cret-" + secrets.token_hex(8)
        scope, process = self.launch(SLEEPER, str(self.dir / "ready"), env={**os.environ, "UIE_TEST_SECRET": secret})
        self.ready(self.dir / "ready")
        found = op.inspect_owned(scope)
        self.assertEqual(found, [{"pid": process.pid}])
        result = op.stop_owned(scope, term_timeout=5, kill_timeout=5, process=process)
        for text in (repr(found), repr(result)):
            self.assertNotIn(secret, text)
            self.assertNotIn(scope["nonce"], text)

    def test_log_must_be_a_writable_regular_file(self):
        scope = self.scope()
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb") as pipe, \
                open(self.dir / "read-only.log", "wb") as created, open(self.dir / "read-only.log", "rb") as readonly, \
                mock.patch.object(op.subprocess, "Popen", side_effect=AssertionError("launched")) as popen:
            for label, log, pattern in (("real pipe", pipe, "regular file"), ("read-only", readonly, "writing")):
                with self.subTest(label), self.assertRaisesRegex(op.OwnedProcessError, pattern):
                    op.launch_owned([sys.executable, "-c", "pass"], cwd=self.dir, log=log, scope=scope)
            popen.assert_not_called()

    def test_child_ignoring_sigterm_is_killed(self):
        scope, process = self.launch(IGNORES_TERM, str(self.dir / "ready"))
        self.ready(self.dir / "ready")
        started = time.monotonic()
        result = op.stop_owned(scope, term_timeout=0.5, kill_timeout=5, process=process)
        self.assertLess(time.monotonic() - started, 5.5)
        self.assertTrue(result["escalated"])
        self.assertEqual((result["terminated"], result["killed"]), ([process.pid], [process.pid]))
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertEqual(op.inspect_owned(scope), [])

    def test_setsid_descendant_keeps_the_nonce_and_is_stopped(self):
        scope, process = self.launch(SETSID_PARENT, str(self.dir / "ready"), SLEEPER)
        grandchild = int(self.ready(self.dir / "ready"))
        pidfd = self.pidfd(grandchild)
        self.assertNotEqual(os.getsid(grandchild), os.getsid(process.pid), "the descendant left the session")
        self.assertEqual(op.inspect_owned(scope), [{"pid": pid} for pid in sorted((process.pid, grandchild))])
        result = op.stop_owned(scope, term_timeout=5, kill_timeout=5, process=process)
        self.assertEqual(result["terminated"], sorted((process.pid, grandchild)))
        self.assertTrue(exited(pidfd), "the grandchild has exited")
        self.assertEqual(op.inspect_owned(scope), [])

    def test_unrelated_and_lookalike_nonces_survive(self):
        scope, process = self.launch(SLEEPER, str(self.dir / "one"))
        # Different nonce, but with lookalike entries that a substring search would confuse with ours.
        lookalike = {**os.environ, "X_" + op.NONCE_ENV: scope["nonce"], "UIE_NOTE": f"{op.NONCE_ENV}={scope['nonce']}"}
        other, survivor = self.launch(SLEEPER, str(self.dir / "two"), env=lookalike)
        self.ready(self.dir / "one")
        self.ready(self.dir / "two")
        self.assertEqual(op.inspect_owned(scope), [{"pid": process.pid}])
        op.stop_owned(scope, term_timeout=5, kill_timeout=5, process=process)
        self.assertIsNone(survivor.poll())
        self.assertEqual(op.inspect_owned(other), [{"pid": survivor.pid}])
        op.stop_owned(other, term_timeout=5, kill_timeout=5, process=survivor)
        self.assertEqual(op.inspect_owned(other), [])

    def test_popen_handle_is_bound_to_its_scope(self):
        scope, process = self.launch(SLEEPER, str(self.dir / "one"))
        other, survivor = self.launch(SLEEPER, str(self.dir / "two"))
        foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(foreign)
        self.ready(self.dir / "one")
        self.ready(self.dir / "two")
        with mock.patch.object(op, "_scan", side_effect=AssertionError("scanned")), \
                mock.patch.object(op, "_signal", side_effect=AssertionError("signalled")):
            for label, handle in (("other scope's Popen", survivor), ("foreign Popen", foreign)):
                with self.subTest(label), self.assertRaisesRegex(op.OwnedProcessError, "launch_owned returned"):
                    op.stop_owned(scope, process=handle)
        self.assertEqual([p.poll() for p in (process, survivor, foreign)], [None, None, None])
        op.stop_owned(scope, term_timeout=5, kill_timeout=5, process=process)
        op.stop_owned(other, term_timeout=5, kill_timeout=5, process=survivor)

    def test_recovery_from_the_persisted_scope_after_the_launcher_exited(self):
        scope = self.scope()
        (self.dir / "scope.json").write_text(json.dumps(scope))   # persisted before launch
        env = {k: v for k, v in os.environ.items() if k != op.NONCE_ENV}
        subprocess.run([sys.executable, "-c", LAUNCHER, str(HERE), str(self.dir / "scope.json"),
                        str(self.dir / "child.log"), SLEEPER, str(self.dir / "ready"), str(self.dir)],
                       env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, check=True)
        orphan = int(self.ready(self.dir / "ready"))
        pidfd = self.pidfd(orphan)
        restored = json.loads((self.dir / "scope.json").read_text())
        self.assertEqual(restored, scope, "the recovery uses the same scope, not a fresh cutoff")
        self.assertEqual(op.inspect_owned(restored), [{"pid": orphan}])
        result = op.stop_owned(restored, term_timeout=5, kill_timeout=5)   # no Popen handle, only the scope
        self.assertEqual((result["stopped"], result["terminated"], result["returncode"]), (True, [orphan], None))
        self.assertTrue(exited(pidfd))
        self.assertEqual(op.inspect_owned(restored), [])

    # ------------------------------------------------------------------ unreadable processes

    def unreadable(self, pids, calls):
        real = op._read_environ

        def read(pid):
            if pid in pids:
                calls.append(pid)
                raise PermissionError(13, "Permission denied")
            return real(pid)
        return mock.patch.object(op, "_read_environ", read)

    def test_unreadable_process_older_than_the_scope_is_skipped_without_reading(self):
        # A same-UID process started before the scope (like an unrelated sshd session or server) is skipped.
        older, server = self.launch(SLEEPER, str(self.dir / "server"))
        self.ready(self.dir / "server")
        time.sleep(0.2)   # well past the tick rounding margin
        scope, process = self.launch(SLEEPER, str(self.dir / "ready"))
        self.ready(self.dir / "ready")
        calls = []
        with self.unreadable({server.pid}, calls):
            self.assertEqual(op.inspect_owned(scope), [{"pid": process.pid}])
            op.stop_owned(scope, term_timeout=5, kill_timeout=5, process=process)
        self.assertEqual(calls, [], "the older process's environment was never read")
        self.assertIsNone(server.poll())
        op.stop_owned(older, term_timeout=5, kill_timeout=5, process=server)

    def test_unreadable_process_at_or_after_the_cutoff_fails_closed(self):
        scope, process = self.launch(SLEEPER, str(self.dir / "ready"))
        self.ready(self.dir / "ready")
        cutoff = scope["notBeforeTicks"]
        real_start = op._start_ticks
        for label, ticks in (("newer", cutoff + 1), ("equal boundary", cutoff), ("older", cutoff - 1)):
            with self.subTest(label):
                calls = []
                start = lambda pid, t=ticks: t if pid == process.pid else real_start(pid)
                with self.unreadable({process.pid}, calls), mock.patch.object(op, "_start_ticks", start):
                    if ticks < cutoff:
                        self.assertEqual(op.inspect_owned(scope), [], "exempt, so not found")
                        self.assertEqual(calls, [])
                    else:
                        with self.assertRaisesRegex(op.ScanError, "unreadable"):
                            op.inspect_owned(scope)
                        with self.assertRaises(op.ScanError):
                            op.stop_owned(scope, term_timeout=1, kill_timeout=1, process=process)
                        self.assertIsNone(process.poll(), "nothing was signalled on a partial scan")
        op.stop_owned(scope, term_timeout=5, kill_timeout=5, process=process)
        self.assertEqual(op.inspect_owned(scope), [])

    def test_briefly_unreadable_process_is_read_after_the_grace_and_persistent_one_is_named(self):
        scope, process = self.launch(SLEEPER, str(self.dir / "ready"))
        self.ready(self.dir / "ready")
        real = op._read_environ
        failures = {"left": 3}

        def flaky(pid):
            if pid == process.pid and failures["left"]:
                failures["left"] -= 1
                raise PermissionError(13, "Permission denied")
            return real(pid)
        with mock.patch.object(op, "_read_environ", flaky):
            self.assertEqual(op.inspect_owned(scope), [{"pid": process.pid}], "transient denial is retried")
        self.assertEqual(failures["left"], 0)
        calls = []
        with self.unreadable({process.pid}, calls), mock.patch.object(op, "UNREADABLE_GRACE_SECONDS", 0.2):
            with self.assertRaisesRegex(op.ScanError, r"comm=\S+ exe=\S+"):
                op.inspect_owned(scope)
        self.assertGreater(len(calls), 1, "retried within the grace period")
        self.assertIsNone(process.poll(), "nothing was signalled on a partial scan")
        op.stop_owned(scope, term_timeout=5, kill_timeout=5, process=process)
        self.assertEqual(op.inspect_owned(scope), [])


if __name__ == "__main__":
    unittest.main()
