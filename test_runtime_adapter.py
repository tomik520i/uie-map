"""Tests for runtime_adapter with a tiny real profile tree and a real prepare_warm_plan output (synthetic UIE2
batches from the existing helpers). Nothing is launched, queued or published; no Minecraft. Stdlib only."""
import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import capture_batch_file as cbf
import warm_worker as ww
from owned_process import NONCE_ENV
from pipeline_identity import canonical_json, create_pipeline_identity, validate_pipeline_identity
from prepare_warm_plan import prepare_warm_plan
from runtime_adapter import AdapterError, make_prepare
from test_prepare_warm_plan import build_world
from warm_worker import PermanentPrepareError

POSIX = os.name == "posix"
sha = lambda raw: hashlib.sha256(raw).hexdigest()
TREE = {"client/mods/a.jar": b"mod a", "client/mods/sub/b.jar": b"mod b" * 100, "client/config/forge.cfg": b"x=1\n",
        "client/exporter.jar": b"exporter", "jre/bin/java": b"#!java\n", "jre/lib/libjvm.so": b"\x7fELF",
        "secret.txt": b"token=do-not-copy", "client/saves/creds.txt": b"undeclared",
        "client/libraries/lib.jar": b"library"}
RUNTIME = {
    "roots": {"mods": "client/mods", "config": "client/config", "jre": "jre"},
    "files": {"exporter": "client/exporter.jar"},
    "links": {},
    "command": ["nice", "-n", "10", "@RUNTIME@/jre/bin/java", "-Duie.capture.plan=@PLAN@/plan.json",
                "-Duie.out=@OUTPUT@", "{}"],
    "env": {"LIBGL_ALWAYS_SOFTWARE": "1", "LD_LIBRARY_PATH": "@RUNTIME@/jre/lib"},
    "cwd": "client",
    "executables": ["jre/bin/java"],
}


def profile_for(profile_dir, runtime):
    """A real identity scan of the source tree, bound to runtime through options.runtime."""
    record = create_pipeline_identity({k: profile_dir / v for k, v in runtime["roots"].items()},
                                      {k: profile_dir / v for k, v in runtime["files"].items()},
                                      {"runtime": runtime, "java": "8"})
    return {"identity": record, "runtime": copy.deepcopy(runtime)}


def bound(profile, runtime):
    """Same identity with options.runtime replaced and the key recomputed (for pure static checks)."""
    identity = copy.deepcopy(profile["identity"])
    identity["identity"]["options"]["runtime"] = runtime
    identity["pipelineKey"] = sha(canonical_json(identity["identity"]))
    return {"identity": identity, "runtime": runtime}


class RuntimeAdapterTests(unittest.TestCase):
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

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()
        self.profile_dir = self.dir / "profile"
        for relative, raw in TREE.items():
            (self.profile_dir / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.profile_dir / relative).write_bytes(raw)
        self.use(RUNTIME)
        self.attempts = 0

    def use(self, runtime):
        self.profile = profile_for(self.profile_dir, runtime)
        self.plan_dir = self.dir / f"plan-{self.profile['identity']['pipelineKey'][:12]}"
        prepare_warm_plan(self.batches, self.plan_dir, world="uie", dimension="0", min_free_bytes=0,
                          pipeline_key=self.profile["identity"]["pipelineKey"],
                          pipeline_identity=self.profile["identity"])
        self.plan_sha = sha((self.plan_dir / "plan.json").read_bytes())
        self.claim = {"id": "a" * 64, "attempt": 1, "payload": {"planSha256": self.plan_sha}}

    def attempt(self):
        self.attempts += 1
        path = self.dir / "attempts" / str(self.attempts)
        path.mkdir(parents=True)
        return path

    def prepare(self, **kwargs):
        return make_prepare(self.plan_dir, self.profile_dir, self.profile, **kwargs)

    # ------------------------------------------------------------------ tests

    def test_staged_attempt_is_accepted_by_the_worker_and_matches_a_fresh_scan(self):
        attempt = self.attempt()
        spec = self.prepare()(self.claim, attempt)
        queue = SimpleNamespace(world_id="uie", dimension="0", pipeline_key=self.profile["identity"]["pipelineKey"])
        checked = ww._validate_spec(spec, self.claim, queue, attempt)
        self.assertEqual(len(checked["jobs"]), 2)
        runtime, plan, output = attempt / "runtime", attempt / "plan", attempt / "output"
        self.assertEqual(sorted(p.name for p in attempt.iterdir()), ["output", "plan", "runtime"])
        rescan = profile_for(runtime, RUNTIME)["identity"]
        self.assertEqual(rescan, self.profile["identity"], "independent scan of the copied runtime")
        self.assertEqual(spec["currentIdentity"], rescan)
        self.assertEqual(spec["command"], ["nice", "-n", "10", str(runtime / "jre/bin/java"),
                                           f"-Duie.capture.plan={plan}/plan.json", f"-Duie.out={output}", "{}"])
        self.assertEqual((spec["env"]["LD_LIBRARY_PATH"], spec["env"]["LIBGL_ALWAYS_SOFTWARE"]),
                         (f"{runtime}/jre/lib", "1"))
        self.assertNotIn(NONCE_ENV, spec["env"])
        self.assertEqual(spec["cwd"], str(runtime / "client"))
        for undeclared in ("secret.txt", "client/saves"):
            self.assertFalse((runtime / undeclared).exists(), f"{undeclared} must not be copied")
        self.assertEqual((plan / "COMPLETE").read_bytes(), (plan / "PLAN-COMPLETE.json").read_bytes())
        self.assertFalse((plan / "results").exists())
        if POSIX:
            self.assertEqual(os.stat(runtime / "jre/bin/java").st_mode & 0o777, 0o755)
            self.assertEqual(os.stat(runtime / "client/mods/a.jar").st_mode & 0o777, 0o644)
            for directory in (runtime / "jre", runtime / "client", runtime / "client/mods/sub", plan / "inputs"):
                self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700, directory)

    def test_changed_sources_or_a_different_plan_are_refused_before_anything_is_written(self):
        cases = {
            "mod changed": lambda: (self.profile_dir / "client/mods/a.jar").write_bytes(b"mod X"),
            "undeclared file in a root": lambda: (self.profile_dir / "client/mods/extra.jar").write_bytes(b"x"),
            "plan input changed": lambda: next(self.plan_dir.glob("inputs/*/snapshot.bin")).write_bytes(b"x"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                prepare = self.prepare()
                change()
                attempt = self.attempt()
                with self.assertRaises(PermanentPrepareError):
                    prepare(self.claim, attempt)
                self.assertEqual(list(attempt.iterdir()), [], "nothing was staged")
        attempt = self.attempt()
        with self.assertRaisesRegex(PermanentPrepareError, "planSha256|plan"):
            self.prepare()({**self.claim, "payload": {"planSha256": "0" * 64}}, attempt)

    def test_runtime_must_be_bound_to_the_identity_exactly(self):
        changed = copy.deepcopy(RUNTIME)
        changed["command"].append("--extra")
        for label, profile in {"runtime differs from options.runtime": {**self.profile, "runtime": changed},
                               "extra profile key": {**self.profile, "notes": "x"},
                               "identity tampered": {**self.profile, "identity": {**self.profile["identity"],
                                                                                  "pipelineKey": "0" * 64}}}.items():
            with self.subTest(label), self.assertRaises(PermanentPrepareError):
                make_prepare(self.plan_dir, self.profile_dir, profile)
        # Bound, but the plan was prepared for the other identity: refused at staging.
        prepare = make_prepare(self.plan_dir, self.profile_dir, bound(self.profile, changed))
        with self.assertRaisesRegex(PermanentPrepareError, "not bound"):
            prepare(self.claim, self.attempt())

    def test_unsafe_paths_aliases_placeholders_and_env_are_refused(self):
        def change(**updates):
            runtime = copy.deepcopy(RUNTIME)
            for key, value in updates.items():
                if key in ("mods", "jre"):
                    runtime["roots"][key] = value
                elif key == "exporter":
                    runtime["files"]["exporter"] = value
                else:
                    runtime[key] = value(runtime[key]) if callable(value) else value
            return runtime
        cases = {
            "dot-dot root": change(mods="client/../mods"), "absolute root": change(mods="/client/mods"),
            "backslash root": change(mods="client\\mods"), "empty segment": change(mods="client//mods"),
            "file inside a root": change(exporter="client/mods/exporter.jar"),
            "alias inside a root": change(links={"client/mods/java": "jre"}),
            "alias onto a declared path": change(links={"jre": "client/mods"}),
            "alias to undeclared files": change(links={"client/java": "client/saves"}),
            "alias chain": change(links={"client/java": "jre", "client/j2": "client/java"}),
            "unknown placeholder": change(command=lambda c: c + ["@HOME@/x"]),
            "runtime arg not copied": change(command=lambda c: c[:3] + ["@RUNTIME@/jre/bin/missing"]),
            "reserved env": change(env={NONCE_ENV: "x"}), "bad env name": change(env={"A-B": "1"}),
            "cwd escapes": change(cwd="../client"), "cwd not in the copy": change(cwd="client/saves"),
            "undeclared executable": change(executables=["jre/bin/missing"]),
            "extra runtime key": {**RUNTIME, "shell": True},
            # Drive and drive-relative paths would escape the base when joined on Windows.
            "drive root": change(mods="C:/outside"), "drive-relative root": change(mods="C:client/mods"),
            "drive file": change(exporter="C:exporter.jar"), "drive alias": change(links={"C:/x": "jre"}),
            "drive alias target": change(links={"client/java": "C:jre"}), "drive cwd": change(cwd="C:client"),
            "drive executable": change(executables=["C:jre/bin/java"]),
            "drive runtime arg": change(command=lambda c: c[:3] + ["@RUNTIME@/C:/x"]),
        }
        for label, runtime in cases.items():
            with self.subTest(label), self.assertRaises(PermanentPrepareError) as raised:
                make_prepare(self.plan_dir, self.profile_dir, bound(self.profile, runtime))
            self.assertNotIn("options.runtime", str(raised.exception), "refused by the path rule, not the binding")
        inside = self.profile_dir / "attempt-inside"
        inside.mkdir()
        try:
            with self.assertRaisesRegex(PermanentPrepareError, "overlaps"):
                self.prepare()(self.claim, inside)
        finally:
            inside.rmdir()

    def test_identity_name_with_a_colon_below_a_root_is_refused(self):
        profile = bound(self.profile, copy.deepcopy(RUNTIME))
        files = profile["identity"]["identity"]["files"]
        files.append({"name": "mods/C:evil.jar", "bytes": 1, "sha256": "0" * 64})
        files.sort(key=lambda f: f["name"])
        profile["identity"]["pipelineKey"] = sha(canonical_json(profile["identity"]["identity"]))
        validate_pipeline_identity(profile["identity"])   # a well-formed record; the copy path is what fails
        with self.assertRaisesRegex(PermanentPrepareError, "identity file"):
            make_prepare(self.plan_dir, self.profile_dir, profile)

    def test_game_dir_above_declared_roots_is_accepted(self):
        # Regression (real profile): '@RUNTIME@/client' as --gameDir above client/mods and client/libraries.
        runtime_record = copy.deepcopy(RUNTIME)
        runtime_record["roots"]["libraries"] = "client/libraries"
        runtime_record["command"] += ["--gameDir", "@RUNTIME@/client"]
        self.use(runtime_record)
        attempt = self.attempt()
        spec = self.prepare()(self.claim, attempt)
        runtime = attempt / "runtime"
        queue = SimpleNamespace(world_id="uie", dimension="0", pipeline_key=self.profile["identity"]["pipelineKey"])
        ww._validate_spec(spec, self.claim, queue, attempt)
        self.assertEqual(spec["command"][-2:], ["--gameDir", str(runtime / "client")])
        self.assertEqual(spec["cwd"], str(runtime / "client"))
        self.assertTrue((runtime / "client").is_dir() and not (runtime / "client").is_symlink())
        self.assertTrue((runtime / "client/libraries/lib.jar").is_file())
        self.assertFalse((runtime / "client/saves").exists())
        # Only directories the copy creates qualify; uncopied paths stay refused.
        for uncopied in ("@RUNTIME@/client/saves", "@RUNTIME@/elsewhere", "@RUNTIME@/client/mods/missing.jar"):
            bad = copy.deepcopy(runtime_record)
            bad["command"].append(uncopied)
            with self.subTest(uncopied), self.assertRaisesRegex(PermanentPrepareError, "copy creates"):
                make_prepare(self.plan_dir, self.profile_dir, bound(self.profile, bad))

    def test_byte_budget_deadline_disk_reserve_and_existing_subdirectories(self):
        with self.assertRaisesRegex(PermanentPrepareError, "max_copy_bytes"):
            self.prepare(max_copy_bytes=1000)(self.claim, self.attempt())
        attempt = self.attempt()
        with self.assertRaises(AdapterError) as raised:   # transient: the worker retries in a fresh attempt
            self.prepare(timeout=1e-6)(self.claim, attempt)
        self.assertIn("deadline", str(raised.exception))
        self.assertNotIsInstance(raised.exception, PermanentPrepareError)
        with self.assertRaisesRegex(AdapterError, "reserve"):
            self.prepare(min_free_bytes=2 ** 62)(self.claim, self.attempt())
        attempt = self.attempt()
        (attempt / "runtime").mkdir()
        with self.assertRaisesRegex(PermanentPrepareError, "already exists"):
            self.prepare()(self.claim, attempt)
        for bad in ({"max_copy_bytes": -1}, {"min_free_bytes": 1.5}, {"timeout": 0}, {"timeout": float("inf")}):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.prepare(**bad)

    @unittest.skipUnless(POSIX, "symlink aliases and exact modes are POSIX cases")
    def test_internal_alias_is_recreated_and_source_aliases_must_point_exactly(self):
        os.symlink("../jre", self.profile_dir / "client/java")
        runtime_record = copy.deepcopy(RUNTIME)
        runtime_record["links"] = {"client/java": "jre"}
        runtime_record["command"][3] = "@RUNTIME@/client/java/bin/java"
        self.use(runtime_record)
        prepare = self.prepare()
        attempt = self.attempt()
        spec = prepare(self.claim, attempt)
        runtime = attempt / "runtime"
        self.assertTrue((runtime / "client/java").is_symlink())
        self.assertEqual(os.readlink(runtime / "client/java"), "../jre")
        self.assertEqual(os.path.realpath(runtime / "client/java"), str(runtime / "jre"))
        self.assertEqual(spec["command"][3], str(runtime / "client/java/bin/java"))
        self.assertEqual(os.stat(spec["command"][3]).st_mode & 0o777, 0o755, "executable through the alias")
        self.assertEqual(spec["currentIdentity"], self.profile["identity"])
        for label, target in (("other directory", "../client/mods"), ("outside the profile", str(self.dir))):
            with self.subTest(label):
                (self.profile_dir / "client/java").unlink()
                os.symlink(target, self.profile_dir / "client/java")
                with self.assertRaisesRegex(PermanentPrepareError, "does not point exactly"):
                    prepare(self.claim, self.attempt())


if __name__ == "__main__":
    unittest.main()
