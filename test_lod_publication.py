"""Optional LOD publication (publish_generation lod_directory). Stdlib only.

The existing publisher fixtures are reused by composition: test_publish_generation is imported as a module
(not its TestCase class), so its tests are not collected again from this file."""
import gzip
import hashlib
import json
import unittest
from unittest.mock import patch

import publish_generation as publication
import test_publish_generation as base
from publish_generation import PublicationError

sha = lambda raw: hashlib.sha256(raw).hexdigest()
ATLAS = b"\x89PNG\r\n\x1a\nfixture"   # the helper's default atlas bytes


def quad(x):
    return {"positions": [x * 16, 0, 0, x * 16 + 1, 0, 0, x * 16 + 1, 0, 1, x * 16, 0, 1], "uvs": [0.0] * 8,
            "colors": [0.5] * 12, "indices": [0, 1, 2, 0, 2, 3]}


def tri(x):
    return {"positions": [x * 16, 0, 0, x * 16 + 1, 0, 0, x * 16, 1, 0], "uvs": [0, 0, 1, 0, 0, 1],
            "colors": [1] * 9, "indices": [0, 1, 2]}


class LodPublicationTests(unittest.TestCase):
    def setUp(self):
        self.h = base.PublicationTests("setUp")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.a, self.b = self.h.batch("a"), self.h.batch("b", 1)
        self.count = 0

    def lod_dir(self, change=None, coarse_change=None):
        """lod.json + coarse files for batches a (x=0) and b (x=1), built from their real detail payloads."""
        self.count += 1
        root = self.h.root / f"lod{self.count}"
        (root / "lod").mkdir(parents=True)
        entries = []
        for batch in (self.a, self.b):
            x = batch["coreChunks"][0][0]
            source = sha((batch["geometry"] / "chunk.json").read_bytes())
            doc = {"format": "UIE_LOD_CHUNK1", "x": x, "z": 0, "sourceSha256": source, "atlasSha256": sha(ATLAS),
                   "colorSteps": 8, "untextured": quad(x), "textured": tri(x), "transparentParts": []}
            if coarse_change:
                coarse_change(x, doc)
            raw = json.dumps(doc).encode()
            name = f"lod/{x}_0-{sha(raw)}.json"
            (root / name).write_bytes(raw)
            entries.append({"x": x, "z": 0, "file": name, "sourceSha256": source,
                            "coarse": {"parts": [{"vertices": 4, "indices": 6}, {"vertices": 3, "indices": 3}]},
                            "detail": {"parts": [{"vertices": 3, "indices": 3}]}})
        lod = {"format": "UIE_LOD1", "method": "planar-unit-v1", "colorSteps": 8, "atlasSha256": sha(ATLAS),
               "chunks": entries}
        if change:
            change(lod)
        (root / "lod.json").write_bytes(json.dumps(lod).encode())
        return root

    def generation(self, pointer, store=None):
        directory = (store or self.h.store) / "generations" / pointer["generation"]
        return directory, json.loads((directory / "COMPLETE.json").read_bytes())

    def test_without_lod_the_publication_is_unchanged(self):
        plain = self.h.publish([self.a, self.b])
        self.h.store = self.h.root / "published2"
        self.assertEqual(self.h.publish([self.a, self.b], lod_directory=None), plain)
        directory, complete = self.generation(plain)
        manifest = json.loads((directory / "manifest.json").read_bytes())
        self.assertNotIn("lod", manifest)
        self.assertNotIn("atlasSha256", manifest)
        self.assertFalse(any(name.startswith("lod/") for name in complete["files"]))

    def test_lod_block_and_coarse_files_are_published_and_bound(self):
        lod_root = self.lod_dir()
        pointer = self.h.publish([self.a, self.b], compress_chunks=True, lod_directory=lod_root)
        directory, complete = self.generation(pointer)
        self.assertEqual(sha((directory / "COMPLETE.json").read_bytes()), pointer["generation"])
        manifest = json.loads((directory / "manifest.json").read_bytes())
        self.assertEqual(manifest["atlasSha256"], sha(ATLAS))
        self.assertEqual(manifest["lod"], json.loads((lod_root / "lod.json").read_bytes()))
        for entry in manifest["lod"]["chunks"]:
            stored = (directory / (entry["file"] + ".gz")).read_bytes()
            self.assertEqual(gzip.decompress(stored), (lod_root / entry["file"]).read_bytes(), "exact coarse bytes")
            self.assertEqual(complete["files"][entry["file"] + ".gz"], sha(stored))
            self.assertFalse((directory / entry["file"]).exists(), "stored like chunks (gzip only)")
        detail = {(c["x"], c["z"]): c["file"] for c in manifest["chunks"]}
        for entry in manifest["lod"]["chunks"]:
            self.assertTrue(detail[(entry["x"], entry["z"])].endswith(f"-{entry['sourceSha256']}.json"),
                            "sourceSha256 is the published detail payload hash")

    def test_integrity_failures_keep_the_current_generation(self):
        previous = self.h.publish([self.a, self.b])
        pointer_raw = (self.h.store / "current.json").read_bytes()

        def corrupt_coarse(root):
            path = next((root / "lod").iterdir())
            path.write_bytes(path.read_bytes() + b" ")
        cases = {
            "corrupt coarse bytes": dict(after=corrupt_coarse),
            "lod atlas": dict(change=lambda lod: lod.update(atlasSha256="e" * 64)),
            "coarse atlas": dict(coarse=lambda x, d: d.update(atlasSha256="e" * 64)),
            "source": dict(change=lambda lod: lod["chunks"][0].update(sourceSha256="f" * 64)),
            "coarse source": dict(coarse=lambda x, d: d.update(sourceSha256="f" * 64)),
            "detail counts": dict(change=lambda lod: lod["chunks"][0]["detail"]["parts"][0].update(vertices=4)),
            "coarse counts": dict(change=lambda lod: lod["chunks"][1]["coarse"]["parts"][0].update(indices=3)),
            "float count": dict(change=lambda lod: lod["chunks"][0]["detail"]["parts"][0].update(vertices=3.0)),
            "missing entry": dict(change=lambda lod: lod["chunks"].pop()),
            "duplicate entry": dict(change=lambda lod: lod["chunks"].append(dict(lod["chunks"][0]))),
            "unknown entry": dict(change=lambda lod: lod["chunks"][0].update(x=9)),
            "unsafe path": dict(change=lambda lod: lod["chunks"][0].update(file="lod/../../a/chunk.json")),
            "path coords": dict(change=lambda lod: lod["chunks"][0].update(file=lod["chunks"][1]["file"])),
            "invalid mesh index": dict(coarse=lambda x, d: d["untextured"].update(indices=[0, 1, 9, 0, 2, 3])),
            "nan position": dict(coarse=lambda x, d: d["textured"]["positions"].__setitem__(0, float("inf"))),
            "nested parts": dict(coarse=lambda x, d: d["textured"].update(transparentParts=[])),
            "untextured transparent": dict(coarse=lambda x, d: d["untextured"].update(transparent=True)),
            "colorSteps": dict(change=lambda lod: lod.update(colorSteps=16)),
            "extra key": dict(change=lambda lod: lod.update(extra=1)),
        }
        for label, case in cases.items():
            with self.subTest(label):
                root = self.lod_dir(change=case.get("change"), coarse_change=case.get("coarse"))
                if "after" in case:
                    case["after"](root)
                with self.assertRaises(PublicationError):
                    self.h.publish([self.a, self.b], previous["generation"], compress_chunks=True, lod_directory=root)
                self.assertEqual((self.h.store / "current.json").read_bytes(), pointer_raw)
                self.assertFalse((self.h.store / ".publish.lock").exists())
                self.assertEqual(list(self.h.store.glob(".staging-*")), [])
        with self.assertRaisesRegex(PublicationError, "overlap"):
            self.h.publish([self.a, self.b], previous["generation"], lod_directory=self.h.store / "lod")

    def test_lod_source_changed_during_assembly_is_detected(self):
        previous = self.h.publish([self.a, self.b])
        pointer_raw = (self.h.store / "current.json").read_bytes()
        root = self.lod_dir()
        write = publication._write
        changed = False

        def fault(path, data):
            nonlocal changed
            write(path, data)
            if path.parent.name == "lod" and not changed:   # lod.json was already read and hashed
                changed = True
                (root / "lod.json").write_bytes((root / "lod.json").read_bytes() + b" ")
        with patch.object(publication, "_write", fault):
            with self.assertRaisesRegex(PublicationError, "changed"):
                self.h.publish([self.a, self.b], previous["generation"], lod_directory=root)
        self.assertTrue(changed)
        self.assertEqual((self.h.store / "current.json").read_bytes(), pointer_raw)
        self.assertFalse((self.h.store / ".publish.lock").exists())


if __name__ == "__main__":
    unittest.main()
