"""Tests for pipeline_identity.create_pipeline_identity. Stdlib only."""
import collections
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline_identity as pid
from pipeline_identity import PipelineIdentityError, create_pipeline_identity

DEFAULT = object()   # sentinel: the helper's default options; None is then passed through as a bad value
TOTAL = 5 + 5000 + 6 + 6 + 8   # bytes of the tree() inputs


class CountingStream:
    """Wraps a file object and counts the bytes actually read."""
    def __init__(self, stream, counter):
        self.stream, self.counter = stream, counter
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.stream.close()
    def fileno(self):
        return self.stream.fileno()
    def read(self, size=-1):
        data = self.stream.read(size)
        self.counter[0] += len(data)
        return data


class PipelineIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()   # real path: linked ancestors are refused

    def tree(self, name, order=None, mtime=None):
        """mods/ with nested and hidden files, config/, plus a standalone exporter jar."""
        base = self.root / name
        files = {"mods/a.jar": b"mod a", "mods/sub/b.jar": b"mod b" * 1000, "mods/.hidden.cfg": b"hidden",
                 "config/forge.cfg": b"cfg=1\n", "exporter.jar": b"exporter"}
        for relative in (order or list(files)):
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[relative])
            if mtime is not None:
                os.utime(path, (mtime, mtime))
        (base / "assets").mkdir()   # an empty root
        return base

    def identity(self, base, options=DEFAULT, **kwargs):
        return create_pipeline_identity(
            {"mods": base / "mods", "config": base / "config", "assets": base / "assets"},
            {"exporter": base / "exporter.jar"}, {"java": "8", "jvm": ["-Xmx4G"]} if options is DEFAULT else options,
            **kwargs)

    def test_identity_is_canonical_and_complete(self):
        result = self.identity(self.tree("one"))
        identity = result["identity"]
        self.assertEqual(result["format"], "UIE_PIPELINE_IDENTITY1")
        self.assertEqual(identity["format"], "UIE_RENDER_INPUTS1")
        self.assertEqual(identity["roots"], ["assets", "config", "mods"], "the empty root is recorded")
        self.assertEqual([f["name"] for f in identity["files"]],
                         ["config/forge.cfg", "exporter", "mods/.hidden.cfg", "mods/a.jar", "mods/sub/b.jar"])
        self.assertEqual(identity["files"][4], {"name": "mods/sub/b.jar", "bytes": 5000,
                                                "sha256": hashlib.sha256(b"mod b" * 1000).hexdigest()})
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        self.assertEqual(result["pipelineKey"], hashlib.sha256(canonical.encode()).hexdigest())
        self.assertNotIn(str(self.root), json.dumps(result), "no absolute paths")
        self.assertEqual(set(result), {"format", "pipelineKey", "identity"})

    def test_key_is_stable_across_location_mtime_order_and_option_order(self):
        a = self.identity(self.tree("one", mtime=1_000_000))
        files = ["exporter.jar", "config/forge.cfg", "mods/sub/b.jar", "mods/.hidden.cfg", "mods/a.jar"]
        b = self.identity(self.tree("two/deeper", order=files, mtime=2_000_000),
                          options={"jvm": ["-Xmx4G"], "java": "8"})
        self.assertEqual(a["pipelineKey"], b["pipelineKey"])
        self.assertEqual(a, b)

    def test_any_declared_change_changes_the_key(self):
        reference = self.identity(self.tree("ref"))["pipelineKey"]
        changes = {
            "mod content": lambda base: (base / "mods/a.jar").write_bytes(b"mod A"),
            "config": lambda base: (base / "config/forge.cfg").write_bytes(b"cfg=2\n"),
            "added file": lambda base: (base / "assets/pack.zip").write_bytes(b"pack"),
            "removed file": lambda base: (base / "mods/.hidden.cfg").unlink(),
            "renamed file": lambda base: (base / "mods/a.jar").rename(base / "mods/c.jar"),
            "exporter": lambda base: (base / "exporter.jar").write_bytes(b"exporter2"),
        }
        keys = {reference}
        for i, (label, change) in enumerate(changes.items()):
            with self.subTest(label):
                base = self.tree(f"case{i}")
                change(base)
                key = self.identity(base)["pipelineKey"]
                self.assertNotIn(key, keys)
                keys.add(key)
        base = self.tree("options")
        self.assertNotEqual(self.identity(base, options={"java": "8", "jvm": ["-Xmx6G"]})["pipelineKey"], reference)
        self.assertNotEqual(self.identity(base, options={"java": 8, "jvm": ["-Xmx4G"]})["pipelineKey"], reference)
        without_empty = create_pipeline_identity({"mods": base / "mods", "config": base / "config"},
                                                 {"exporter": base / "exporter.jar"}, {"java": "8", "jvm": ["-Xmx4G"]})
        self.assertNotEqual(without_empty["pipelineKey"], reference, "an empty root is part of the identity")

    def test_options_are_copied_and_strictly_validated(self):
        base = self.tree("one")
        options = {"java": "8", "nested": {"list": [1, 2.5, None, True]}}
        result = self.identity(base, options=options)
        options["nested"]["list"].append("later")
        self.assertEqual(result["identity"]["options"]["nested"]["list"], [1, 2.5, None, True])
        bad = [[], "x", None, {"a": (1, 2)}, {"a": float("nan")}, {"a": float("inf")}, {1: "int key"},
               {"a": b"bytes"}, {"a": {1, 2}}, {"a": 2 ** 60}, collections.OrderedDict(a=1), {"a": object()}]
        for options in bad:
            with self.subTest(options=repr(options)), self.assertRaises(PipelineIdentityError):
                self.identity(base, options=options)
        deep = current = {}
        for _ in range(70):
            current["x"] = {}
            current = current["x"]
        with self.assertRaises(PipelineIdentityError):
            self.identity(base, options=deep)

    def test_budgets_are_enforced(self):
        base = self.tree("one")
        with self.assertRaisesRegex(PipelineIdentityError, "max_files"):
            self.identity(base, max_files=4)
        self.identity(base, max_files=5)
        with self.assertRaisesRegex(PipelineIdentityError, "max_total_bytes"):
            self.identity(base, max_total_bytes=TOTAL - 1)
        self.identity(base, max_total_bytes=TOTAL)
        for kwargs in ({"max_files": -1}, {"max_files": True}, {"max_total_bytes": 1.5}, {"max_total_bytes": 2 ** 60}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PipelineIdentityError):
                self.identity(base, **kwargs)

    def test_labels_paths_and_overlaps_are_refused(self):
        base = self.tree("one")
        mods, config, jar = base / "mods", base / "config", base / "exporter.jar"
        cases = {
            "uppercase label": ({"Mods": mods}, {}),
            "digit label": ({"1mods": mods}, {}),
            "slash label": ({"a/b": mods}, {}),
            "long label": ({"a" * 65: mods}, {}),
            "label collision": ({"mods": mods}, {"mods": jar}),
            "nested roots": ({"mods": mods, "sub": mods / "sub"}, {}),
            "same root twice": ({"mods": mods, "again": mods}, {}),
            "file inside root": ({"mods": mods}, {"modjar": mods / "a.jar"}),
            "same file twice": ({}, {"a": jar, "b": jar}),
            "missing root": ({"none": base / "none"}, {}),
            "missing file": ({}, {"none": base / "none.jar"}),
            "root is a file": ({"jar": jar}, {}),
            "file is a directory": ({}, {"config": config}),
            "nothing declared": ({}, {}),
        }
        for label, (roots, files) in cases.items():
            with self.subTest(label), self.assertRaises(PipelineIdentityError):
                create_pipeline_identity(roots, files, {})
        with self.assertRaises(PipelineIdentityError):
            create_pipeline_identity([("mods", mods)], {}, {})

    def test_symlinks_and_special_files_are_refused(self):
        base = self.tree("one")
        outside = self.root / "outside.jar"
        outside.write_bytes(b"outside")
        try:
            os.symlink(outside, base / "mods" / "linked.jar")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        with self.assertRaisesRegex(PipelineIdentityError, "symlink"):
            self.identity(base)
        (base / "mods" / "linked.jar").unlink()
        os.symlink(self.root, base / "mods" / "linkdir", target_is_directory=True)
        with self.assertRaisesRegex(PipelineIdentityError, "symlink"):
            self.identity(base)
        (base / "mods" / "linkdir").unlink() if os.name == "posix" else os.rmdir(base / "mods" / "linkdir")
        alias = self.root / "alias"
        os.symlink(base, alias, target_is_directory=True)
        with self.assertRaisesRegex(PipelineIdentityError, "symlink"):
            create_pipeline_identity({"mods": alias / "mods"}, {}, {})
        with self.assertRaisesRegex(PipelineIdentityError, "symlink"):
            create_pipeline_identity({}, {"jar": alias / "exporter.jar"}, {})
        if hasattr(os, "mkfifo"):
            os.mkfifo(base / "mods" / "pipe")
            with self.assertRaisesRegex(PipelineIdentityError, "not a regular file"):
                self.identity(base)

    @unittest.skipUnless(os.name == "posix", "backslash is a separator on Windows")
    def test_backslash_file_name_is_refused(self):
        # Such a name could not be told apart from a path in a record, and validate_pipeline_identity refuses it.
        base = self.tree("one")
        (base / "mods" / "a\\b.jar").write_bytes(b"x")
        with self.assertRaisesRegex(PipelineIdentityError, "backslash"):
            self.identity(base)

    def test_changes_between_the_two_passes_are_rejected(self):
        changes = {
            "content": lambda base: (base / "mods/a.jar").write_bytes(b"mod X"),
            "same size different content": lambda base: (base / "config/forge.cfg").write_bytes(b"cfg=9\n"),
            "added": lambda base: (base / "assets/new.zip").write_bytes(b"new"),
            "removed": lambda base: (base / "mods/sub/b.jar").unlink(),
            "exporter": lambda base: (base / "exporter.jar").write_bytes(b"EXPORTER"),
        }
        for i, (label, change) in enumerate(changes.items()):
            with self.subTest(label):
                base = self.tree(f"race{i}")
                with mock.patch.object(pid, "_after_first_pass", lambda base=base, change=change: change(base)):
                    with self.assertRaisesRegex(PipelineIdentityError, "changed between the two passes"):
                        self.identity(base)

    def counted(self):
        """Patch os.fdopen in the module so every read is counted; returns the counter."""
        counter = [0]
        real = os.fdopen
        patcher = mock.patch.object(pid.os, "fdopen", lambda fd, *a, **k: CountingStream(real(fd, *a, **k), counter))
        patcher.start()
        self.addCleanup(patcher.stop)
        return counter

    def test_file_grown_after_the_size_preflight_is_refused_before_reading(self):
        base = self.tree("grow")
        counter = self.counted()
        grow = lambda: (base / "mods/a.jar").write_bytes(b"x" * (5 << 20))   # 5 MiB after the preflight
        with mock.patch.object(pid, "_after_preflight", grow):
            with self.assertRaisesRegex(PipelineIdentityError, "max_total_bytes"):
                self.identity(base, max_total_bytes=TOTAL)
        self.assertLessEqual(counter[0], TOTAL + 1, "the grown file is never read beyond the budget")

    def test_file_growing_while_it_is_read_stops_at_the_budget(self):
        base = self.tree("growing")
        counter = self.counted()
        real_open = os.open
        grown = []

        def open_and_grow(path, flags, *args):
            fd = real_open(path, flags, *args)
            if str(path).endswith("a.jar") and not grown:
                grown.append(True)
                with open(path, "ab") as stream:   # after the per-file size check, before reading
                    stream.write(b"y" * (5 << 20))
            return fd
        with mock.patch.object(pid.os, "open", open_and_grow):
            with self.assertRaisesRegex(PipelineIdentityError, "max_total_bytes"):
                self.identity(base, max_total_bytes=TOTAL)
        self.assertTrue(grown)
        self.assertLessEqual(counter[0], TOTAL + 1, "reading stops at remaining + 1 bytes")

    def test_root_replaced_by_a_link_between_passes_is_refused(self):
        base = self.tree("swap")
        probe = self.root / "probe"
        try:
            os.symlink(base, probe, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")

        def swap():
            # Identical content, but the declared root path now goes through a link.
            (base / "mods").rename(base / "mods-real")
            os.symlink(base / "mods-real", base / "mods", target_is_directory=True)
        with mock.patch.object(pid, "_after_first_pass", swap):
            with self.assertRaisesRegex(PipelineIdentityError, "symlink"):
                self.identity(base)

    def test_inputs_are_only_read(self):
        base = self.tree("one")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob("*") if p.is_file()}
        self.identity(base)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["one"], "no output files")


if __name__ == "__main__":
    unittest.main()
