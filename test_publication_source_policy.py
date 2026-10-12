"""Staged-source policy chain through publish_generation. Stdlib only.

The existing publisher fixtures are reused by composition: test_publish_generation is imported as a module
(not its TestCase class), so its tests are not collected a second time from this file."""
import hashlib
import json
import unittest
from unittest.mock import patch

import publish_generation as publication
import test_publish_generation as base
from publish_generation import PublicationError

sha = lambda raw: hashlib.sha256(raw).hexdigest()


def sidecar(snapshot, change=None):
    doc = {"format": "UIE_STAGED_SOURCE1",
           "policy": {"acceptedConsistency": "observed-stable-files", "atomicWorldSnapshot": False,
                      "purpose": "best-effort map export explicitly accepted by the caller"},
           "warning": "best-effort map export; not a consistent world snapshot",
           "tile": {"x": 0, "z": 0},
           "stagedTile": {"consistency": "observed-stable-files", "atomicWorldSnapshot": False,
                          "sourceInterval": {"startUtc": "2026-09-30T10:00:00.000001Z", "endUtc": "2026-09-30T10:00:05Z"}},
           "batch": {"snapshotSha256": snapshot, "files": {"snapshot.bin": snapshot, "snapshot.tiles.json": "a" * 64,
                                                            "provenance.json": "b" * 64}}}
    if change:
        change(doc)
    return json.dumps(doc, indent=2).encode()


class SourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.h = base.PublicationTests("setUp")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def staged(self, name, x=0, change=None, raw=None):
        batch = self.h.batch(name, x)
        raw = raw if raw is not None else sidecar(batch["snapshotSha256"], change)
        (batch["geometry"] / "staged-source.json").write_bytes(raw)
        batch["stagedSource"] = {"file": "staged-source.json", "sha256": sha(raw)}
        return batch, raw

    def complete(self, pointer, store=None):
        directory = (store or self.h.store) / "generations" / pointer["generation"]
        raw = (directory / "COMPLETE.json").read_bytes()
        return directory, raw, json.loads(raw)

    def test_sidecar_bytes_are_preserved_and_bound_into_the_inventory(self):
        a, raw = self.staged("a")
        b = self.h.batch("b", 1)
        pointer = self.h.publish([a, b])
        directory, complete_raw, complete = self.complete(pointer)
        self.assertEqual(sha(complete_raw), pointer["generation"], "the generation id covers sourcePolicies")
        name = f"provenance/{a['snapshotSha256']}-{sha(raw)}.json"
        self.assertEqual((directory / name).read_bytes(), raw, "exact sidecar bytes")
        self.assertEqual(complete["files"][name], sha(raw))
        self.assertEqual(complete["sourcePolicies"], [{"snapshotSha256": a["snapshotSha256"], "file": name,
                                                       "sha256": sha(raw), "consistency": "observed-stable-files",
                                                       "atomicWorldSnapshot": False}])
        self.assertEqual(complete["pipelineIdentitySource"], "caller-assertion", "the pipeline identity is not faked")
        manifest = json.loads((directory / "manifest.json").read_bytes())
        self.assertEqual(set(manifest), {"format", "title", "sample", "generatedAt", "atlas", "bakedLighting", "bounds",
                                         "coverage", "chunks", "blockInfo"}, "display manifest format unchanged")

    def test_reordered_batches_give_the_same_generation(self):
        a, _ = self.staged("a")
        b, _ = self.staged("b", 1)
        first = self.h.publish([a, b])
        self.h.store = self.h.root / "published2"
        second = self.h.publish([b, a])
        self.assertEqual(first, second)
        _, _, complete = self.complete(second)
        self.assertEqual([p["snapshotSha256"] for p in complete["sourcePolicies"]],
                         sorted([a["snapshotSha256"], b["snapshotSha256"]]))

    def test_specs_without_the_option_publish_exactly_as_before(self):
        a, b = self.h.batch("a"), self.h.batch("b", 1)
        plain = self.h.publish([a, b])
        _, _, complete = self.complete(plain)
        self.assertNotIn("sourcePolicies", complete)
        self.assertFalse(any(name.startswith("provenance/") for name in complete["files"]))
        # An explicit None is the same as omitting the option.
        self.h.store = self.h.root / "published2"
        self.assertEqual(self.h.publish([{**a, "stagedSource": None}, {**b, "stagedSource": None}]), plain)
        # A stray sidecar file next to a plain spec is neither read nor published.
        (a["geometry"] / "staged-source.json").write_bytes(b"not read")
        self.h.store = self.h.root / "published3"
        self.assertEqual(self.h.publish([a, b]), plain)

    def test_wrong_hash_policy_or_snapshot_is_refused_and_keeps_the_old_pointer(self):
        previous = self.h.publish([self.h.batch("a")])
        pointer_raw = (self.h.store / "current.json").read_bytes()
        cases = {
            "spec hash": dict(spec=lambda s: s.update(sha256="0" * 64)),
            "spec file": dict(spec=lambda s: s.update(file="other.json")),
            "spec extra key": dict(spec=lambda s: s.update(extra=1)),
            "spec not a dict": dict(spec="staged-source.json"),
            "missing sidecar": dict(remove=True),
            "upgraded consistency": dict(change=lambda d: d["policy"].update(acceptedConsistency="offline-consistent")),
            "atomic policy": dict(change=lambda d: d["policy"].update(atomicWorldSnapshot=True)),
            "atomic tile": dict(change=lambda d: d["stagedTile"].update(atomicWorldSnapshot=True)),
            "tile consistency": dict(change=lambda d: d["stagedTile"].update(consistency="offline-consistent")),
            "foreign snapshot": dict(change=lambda d: d["batch"].update(snapshotSha256="c" * 64)),
            "foreign snapshot file": dict(change=lambda d: d["batch"]["files"].update({"snapshot.bin": "c" * 64})),
            "no warning": dict(change=lambda d: d.pop("warning")),
            "empty warning": dict(change=lambda d: d.update(warning=" ")),
            "reversed interval": dict(change=lambda d: d["stagedTile"]["sourceInterval"].update(startUtc="2026-09-30T11:00:00Z")),
            "naive timestamp": dict(change=lambda d: d["stagedTile"]["sourceInterval"].update(endUtc="2026-09-30T10:00:05")),
            "wrong format": dict(change=lambda d: d.update(format="UIE_STAGED_SOURCE2")),
            "not json": dict(raw=b"{not json"),
            "nan": dict(raw=b'{"format": NaN}'),
        }
        for i, (label, case) in enumerate(cases.items()):
            with self.subTest(label):
                batch, _ = self.staged(f"b{i}", 1, change=case.get("change"), raw=case.get("raw"))
                if "spec" in case:
                    if callable(case["spec"]):
                        case["spec"](batch["stagedSource"])
                    else:
                        batch["stagedSource"] = case["spec"]
                if case.get("remove"):
                    (batch["geometry"] / "staged-source.json").unlink()
                with self.assertRaises((PublicationError, FileNotFoundError)):
                    self.h.publish([self.h.batch(f"a{i}"), batch], previous["generation"])
                self.assertEqual((self.h.store / "current.json").read_bytes(), pointer_raw)
                self.assertEqual(list(self.h.store.glob(".staging-*")), [])
                self.assertFalse((self.h.store / ".publish.lock").exists())

    def test_sidecar_changed_after_read_is_detected(self):
        previous = self.h.publish([self.h.batch("a")])
        pointer_raw = (self.h.store / "current.json").read_bytes()
        batch, raw = self.staged("b", 1)
        write = publication._write
        changed = False

        def fault(path, data):
            nonlocal changed
            write(path, data)
            if path.parent.name == "chunks" and not changed:
                changed = True
                (batch["geometry"] / "staged-source.json").write_bytes(raw + b" ")
        with patch.object(publication, "_write", fault):
            # The staged batch comes first: its sidecar is read, then changed by the first chunk write.
            with self.assertRaisesRegex(PublicationError, "changed"):
                self.h.publish([batch, self.h.batch("a2")], previous["generation"])
        self.assertTrue(changed)
        self.assertEqual((self.h.store / "current.json").read_bytes(), pointer_raw)
        self.assertEqual(list(self.h.store.glob(".staging-*")), [])


if __name__ == "__main__":
    unittest.main()
