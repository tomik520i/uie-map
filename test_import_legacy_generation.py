"""Tests for import_legacy_generation. Stdlib only; a synthetic two-chunk legacy export with one partial
boundary block chunk, shaped like web/data (manifest format 1, chunk_<x>_<z>.json, blocks/<sha>/...)."""
import gzip
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import import_legacy_generation as legacy
from import_legacy_generation import LegacyImportError, import_legacy_generation
from publish_generation import PublicationError

SNAPSHOT = "4aab164d54671b1d714161a7b0b16bc0f299050e7d20cc54fd6a52c8f35fbae9"
PNG = b"\x89PNG\r\n\x1a\n legacy atlas"
sha = lambda raw: hashlib.sha256(raw).hexdigest()


def mesh(x, z):
    return {"positions": [x * 16, 64, z * 16, x * 16 + 1, 64, z * 16, x * 16, 64, z * 16 + 1],
            "uvs": [0, 0, 1, 0, 0, 1], "colors": [1] * 9, "indices": [0, 1, 2],
            "transparentParts": [{"positions": [x * 16, 62, z * 16, x * 16 + 1, 62, z * 16, x * 16, 62, z * 16 + 1],
                                  "uvs": [0, 0, 1, 0, 0, 1], "colors": [0.5] * 9, "indices": [0, 1, 2]}]}


def blocks(x0, z0, width=16, depth=16):
    total = width * depth * 256
    return {"format": "UIE_BLOCKS1", "x": x0, "z": z0, "width": width, "depth": depth, "height": 256,
            "registry": {"0": "minecraft:air", "1": "minecraft:stone"}, "runs": [[total - 10, 1, 0], [10, 0, 0]]}


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # Real path: symlinked ancestors (e.g. /var -> /private/var) are refused by the importer.
        self.root = Path(temp.name).resolve()
        self.source = self.root / "data"
        self.store = self.root / "store"
        self.source.mkdir()
        # Chunk 10,10 has full block data; chunk 11,10 is cropped at the export boundary (x 176..183).
        self.write_json("chunk_10_10.json", mesh(10, 10))
        self.write_json("chunk_11_10.json", mesh(11, 10))
        self.write_json(f"blocks/{SNAPSHOT}/chunk_10_10.json", blocks(160, 160))
        self.write_json(f"blocks/{SNAPSHOT}/chunk_11_10.json", blocks(176, 160, width=8))
        (self.source / "atlas.png").write_bytes(PNG)
        self.manifest = {
            "format": 1, "title": "UIE · skutečný svět", "sample": True, "generatedAt": "2026-09-26T03:36:58.607Z",
            "atlas": "atlas.png",
            "chunks": [{"file": "chunk_10_10.json", "x": 10, "z": 10}, {"file": "chunk_11_10.json", "x": 11, "z": 10}],
            "bakedLighting": True, "focus": [168.0, 77, 168.0],
            "bounds": {"min": [159.8, 0.0, 159.8], "max": [184.2, 240.0, 176.2]},
            "coverage": {"renderedBlocks": 138848, "unsupportedBlocks": 2, "unsupportedNames": ["mod:thing"],
                         "emptyBlocks": 6473},
            "blockInfo": {"format": "UIE_BLOCKS1", "snapshotSha256": SNAPSHOT, "chunks": [
                {"x": 10, "z": 10, "file": f"blocks/{SNAPSHOT}/chunk_10_10.json"},
                {"x": 11, "z": 10, "file": f"blocks/{SNAPSHOT}/chunk_11_10.json"}]},
        }
        self.save_manifest()

    def write_json(self, name, value):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(value).encode())

    def save_manifest(self):
        (self.source / "manifest.json").write_bytes(json.dumps(self.manifest, ensure_ascii=False).encode())
        self.expected = sha((self.source / "manifest.json").read_bytes())

    def run_import(self, store=None, **kwargs):
        kwargs.setdefault("expected_manifest_sha256", self.expected)
        kwargs.setdefault("min_free_bytes", 0)
        return import_legacy_generation(self.source, store or self.store, world="uie", dimension="0", **kwargs)

    def source_bytes(self):
        return {p.relative_to(self.source).as_posix(): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}

    def assert_nothing_published(self, store=None):
        store = store or self.store
        self.assertFalse((store / "current.json").exists())
        self.assertEqual(list(store.glob(".import-staging-*")), [])
        self.assertEqual(list(store.glob(".current-*")), [])
        self.assertFalse((store / ".publish.lock").exists())
        generations = store / "generations"
        self.assertTrue(not generations.exists() or not any(generations.iterdir()))

    # ------------------------------------------------------------------ success

    def test_import_preserves_bytes_metadata_and_records_provenance(self):
        before = self.source_bytes()
        pointer = self.run_import()
        self.assertEqual(self.source_bytes(), before, "source is only read")
        self.assertEqual(json.loads((self.store / "current.json").read_bytes()), pointer)
        self.assertEqual(pointer["format"], "UIE_REVISION1")
        self.assertEqual((pointer["world"], pointer["dimension"]), ("uie", "0"))
        directory = self.store / "generations" / pointer["generation"]
        complete_raw = (directory / "COMPLETE.json").read_bytes()
        self.assertEqual(sha(complete_raw), pointer["generation"])
        complete = json.loads(complete_raw)
        self.assertEqual(complete["format"], "UIE_GENERATION1")
        self.assertEqual(complete["importKind"], "legacy-static-export")
        self.assertEqual(complete["pipelineIdentitySource"], "legacy-export-inventory")
        self.assertEqual(complete["snapshots"], [])
        self.assertEqual(complete["sourceManifestSha256"], self.expected)
        self.assertEqual(complete["sourceFiles"], {name: sha(raw) for name, raw in before.items()})
        self.assertEqual(complete["partialBlockChunks"], [[11, 10]])
        self.assertEqual(complete["pipelineKey"], pointer["pipelineKey"])
        self.assertEqual(pointer["pipelineKey"], sha(legacy.canonical(
            {"importKind": "legacy-static-export", "sourceFiles": complete["sourceFiles"]})))
        self.assertNotIn("snapshotFormat", complete)
        for name, digest in complete["files"].items():
            self.assertEqual(sha((directory / name).read_bytes()), digest, name)

        manifest_raw = (directory / "manifest.json").read_bytes()
        self.assertEqual(sha(manifest_raw), pointer["manifestSha256"])
        manifest = json.loads(manifest_raw)
        for key in ("format", "title", "sample", "generatedAt", "bakedLighting", "focus", "bounds", "coverage"):
            self.assertEqual(manifest[key], self.manifest[key], key)
        self.assertNotIn("snapshotFormat", manifest, "a legacy export is never labelled UIE2")
        self.assertEqual(manifest["atlas"], "atlas.png")
        self.assertEqual((directory / "atlas.png").read_bytes(), PNG)
        self.assertEqual(manifest["blockInfo"]["snapshotSha256"], SNAPSHOT)

        sources = {(c["x"], c["z"]): c["file"] for c in self.manifest["chunks"]}
        block_sources = {(c["x"], c["z"]): c["file"] for c in self.manifest["blockInfo"]["chunks"]}
        for refs, originals, kind in ((manifest["chunks"], sources, "chunks"),
                                      (manifest["blockInfo"]["chunks"], block_sources, "blocks")):
            self.assertEqual({(c["x"], c["z"]) for c in refs}, set(originals))
            for ref in refs:
                original = before[originals[(ref["x"], ref["z"])]]
                self.assertEqual(ref["file"], f"{kind}/{ref['x']}_{ref['z']}-{sha(original)}.json")
                self.assertFalse((directory / ref["file"]).exists(), "stored only compressed")
                stored = (directory / (ref["file"] + ".gz")).read_bytes()
                self.assertEqual(gzip.decompress(stored), original, "exact bytes preserved")
                self.assertEqual(stored[4:8], b"\x00\x00\x00\x00", "gzip mtime 0")
                self.assertEqual(stored[9], 255, "gzip OS byte 255")
                self.assertEqual(complete["files"][ref["file"] + ".gz"], sha(stored))
        self.assertEqual(set(complete["files"]), {"atlas.png", "manifest.json"}
                         | {r["file"] + ".gz" for r in manifest["chunks"] + manifest["blockInfo"]["chunks"]})
        self.assertFalse((self.store / ".publish.lock").exists())
        self.assertEqual(list(self.store.glob(".import-staging-*")) + list(self.store.glob(".current-*")), [])

    def test_repeat_into_a_fresh_store_is_deterministic(self):
        first = self.run_import()
        second = self.run_import(self.root / "store2")
        self.assertEqual(first, second)

    def test_export_without_block_data_imports_without_block_info(self):
        del self.manifest["blockInfo"]
        self.save_manifest()
        pointer = self.run_import()
        directory = self.store / "generations" / pointer["generation"]
        self.assertNotIn("blockInfo", json.loads((directory / "manifest.json").read_bytes()))
        self.assertEqual(json.loads((directory / "COMPLETE.json").read_bytes())["partialBlockChunks"], [])

    # ------------------------------------------------------------------ refusals

    def test_stale_expected_manifest_is_refused(self):
        with self.assertRaisesRegex(LegacyImportError, "expected_manifest_sha256"):
            self.run_import(expected_manifest_sha256="0" * 64)
        self.assert_nothing_published()
        with self.assertRaises(LegacyImportError):
            self.run_import(expected_manifest_sha256="A" * 64)

    def test_initial_store_only(self):
        self.store.mkdir()
        (self.store / "current.json").write_bytes(b"existing pointer")
        with self.assertRaisesRegex(LegacyImportError, "initial store"):
            self.run_import()
        self.assertEqual((self.store / "current.json").read_bytes(), b"existing pointer")
        self.assertFalse((self.store / ".publish.lock").exists())

    def test_raced_current_is_never_replaced(self):
        rename = os.rename

        def racing_rename(source, target):
            rename(source, target)
            (self.store / "current.json").write_bytes(b"foreign pointer")
        with mock.patch.object(legacy.os, "rename", racing_rename):
            with self.assertRaisesRegex(LegacyImportError, "concurrently"):
                self.run_import()
        self.assertEqual((self.store / "current.json").read_bytes(), b"foreign pointer")
        self.assertEqual(list(self.store.glob(".current-*")), [])
        self.assertFalse((self.store / ".publish.lock").exists())

    def test_active_lock_is_refused(self):
        self.store.mkdir()
        (self.store / ".publish.lock").write_text("active")
        with self.assertRaisesRegex(LegacyImportError, "lock"):
            self.run_import()
        self.assertTrue((self.store / ".publish.lock").exists(), "a foreign lock is never removed")

    def test_block_sets_must_match_geometry(self):
        for label, change in {
            "missing": lambda m: m["blockInfo"]["chunks"].pop(),
            "extra": lambda m: m["blockInfo"]["chunks"].append({"x": 12, "z": 10, "file": "extra.json"}),
            "moved": lambda m: m["blockInfo"]["chunks"][1].update(x=12),
        }.items():
            with self.subTest(label):
                self.setUp()
                change(self.manifest)
                self.save_manifest()
                with self.assertRaisesRegex(LegacyImportError, "block chunks differ"):
                    self.run_import()
                self.assert_nothing_published()

    def test_invalid_mesh_and_block_data_are_refused(self):
        partial = f"blocks/{SNAPSHOT}/chunk_11_10.json"
        cases = {
            "bad index": ("chunk_10_10.json", {**mesh(10, 10), "indices": [0, 1, 3]}),
            "nonfinite mesh": ("chunk_10_10.json", '{"positions":[1e999,0,0],"uvs":[0,0],"colors":[1,1,1],"indices":[]}'),
            "runs short": (partial, {**blocks(176, 160, width=8), "runs": [[100, 1, 0]]}),
            "runs as full chunk": (partial, {**blocks(176, 160, width=8), "runs": [[65536, 1, 0]]}),
            "origin outside chunk": (partial, blocks(170, 160, width=8)),
            "crossing chunk edge": (partial, blocks(184, 160, width=16)),
            "width 17": (partial, blocks(176, 160, width=17)),
            "unknown field": (partial, {**blocks(176, 160, width=8), "extra": 1}),
            "meta 16": (partial, {**blocks(176, 160, width=8), "runs": [[32768, 1, 16]]}),
            "unregistered id": (partial, {**blocks(176, 160, width=8), "runs": [[32768, 7, 0]]}),
            "height 255": (partial, {**blocks(176, 160, width=8), "height": 255}),
        }
        for label, (name, value) in cases.items():
            with self.subTest(label):
                self.setUp()
                if isinstance(value, str):
                    (self.source / name).write_text(value)
                else:
                    self.write_json(name, value)
                with self.assertRaises(LegacyImportError):
                    self.run_import()
                self.assert_nothing_published()

    def test_invalid_metadata_is_refused(self):
        cases = {
            "not a sample": lambda m: m.update(sample=False),
            "naive timestamp": lambda m: m.update(generatedAt="2026-09-26T03:36:58"),
            "unknown field": lambda m: m.update(extra=1),
            "uie2 label": lambda m: m.update(snapshotFormat="UIE2"),
            "reversed bounds": lambda m: m["bounds"].update(min=[300, 0, 0]),
            "bad focus": lambda m: m.update(focus=[1, 2]),
            "bad block snapshot": lambda m: m["blockInfo"].update(snapshotSha256="xyz"),
            "negative coverage": lambda m: m["coverage"].update(renderedBlocks=-1),
            "unknown coverage": lambda m: m["coverage"].update(other=1),
            "duplicate chunk": lambda m: m["chunks"].append({"file": "chunk_10_10.json", "x": 10, "z": 10}),
            "string coordinate": lambda m: m["chunks"][0].update(x="10"),
            "lighting string": lambda m: m.update(bakedLighting="yes"),
            "format 2": lambda m: m.update(format=2),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                change(self.manifest)
                self.save_manifest()
                with self.assertRaises(LegacyImportError):
                    self.run_import()
                self.assert_nothing_published()
        self.setUp()
        raw = json.dumps(self.manifest).replace('"bounds": {"min": [159.8', '"bounds": {"min": [NaN', 1)
        (self.source / "manifest.json").write_text(raw)
        with self.assertRaisesRegex(LegacyImportError, "non-finite"):
            self.run_import(expected_manifest_sha256=sha(raw.encode()))
        duplicate = json.dumps(self.manifest)[:-1] + ', "title": "again"}'
        (self.source / "manifest.json").write_text(duplicate)
        with self.assertRaisesRegex(LegacyImportError, "duplicate"):
            self.run_import(expected_manifest_sha256=sha(duplicate.encode()))

    def test_source_changed_during_import_is_refused(self):
        write = legacy._write
        changed = False

        def fault(path, raw):
            nonlocal changed
            write(path, raw)
            if path.name.endswith(".gz") and not changed:
                changed = True
                # Already read and staged; still valid JSON, but its bytes (and hash) differ now.
                with (self.source / "chunk_10_10.json").open("ab") as stream:
                    stream.write(b" ")
        with mock.patch.object(legacy, "_write", fault):
            with self.assertRaisesRegex(LegacyImportError, "changed"):
                self.run_import()
        self.assert_nothing_published()

    def test_unsafe_paths_and_symlinks_are_refused(self):
        outside = self.root / "outside.json"
        outside.write_bytes(json.dumps(mesh(10, 10)).encode())
        for label, file in {"parent": "../outside.json", "absolute": str(outside).replace("\\", "/"),
                            "dot": "./chunk_10_10.json", "backslash": "blocks\\x.json"}.items():
            with self.subTest(label):
                self.setUp()
                self.manifest["chunks"][0]["file"] = file
                self.save_manifest()
                with self.assertRaises(LegacyImportError):
                    self.run_import()
                self.assert_nothing_published()
        # Symlinks: final component, directory component and the source root itself.
        self.setUp()
        try:
            (self.source / "chunk_10_10.json").unlink()
            os.symlink(outside, self.source / "chunk_10_10.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        with self.assertRaisesRegex(LegacyImportError, "symlink"):
            self.run_import()
        self.assert_nothing_published()
        self.setUp()
        real_blocks = self.root / "real-blocks"
        shutil.move(str(self.source / "blocks"), real_blocks)
        os.symlink(real_blocks, self.source / "blocks", target_is_directory=True)
        with self.assertRaisesRegex(LegacyImportError, "symlink"):
            self.run_import()
        self.assert_nothing_published()
        self.setUp()
        link = self.root / "linked-data"
        os.symlink(self.source, link, target_is_directory=True)
        with self.assertRaisesRegex(LegacyImportError, "symlink"):
            import_legacy_generation(link, self.store, world="uie", dimension="0",
                                     expected_manifest_sha256=self.expected, min_free_bytes=0)

    def symlink_or_skip(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=Path(target).is_dir())
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")

    def import_into(self, source, store):
        return import_legacy_generation(source, store, world="uie", dimension="0",
                                        expected_manifest_sha256=self.expected, min_free_bytes=0)

    def test_symlinked_ancestors_of_source_and_store_are_refused(self):
        alias = self.root / "alias"
        self.symlink_or_skip(self.root, alias)
        with self.assertRaisesRegex(LegacyImportError, "symlink or reparse point"):
            self.import_into(alias / "data", self.store)
        # Store below a linked ancestor, including parts that do not exist yet: refused before any mkdir.
        for store in (alias / "store", alias / "nested" / "store"):
            with self.subTest(store=store), self.assertRaisesRegex(LegacyImportError, "symlink or reparse point"):
                self.import_into(self.source, store)
        self.assertFalse((self.root / "store").exists())
        self.assertFalse((self.root / "nested").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_junction_ancestor_is_refused_on_windows(self):
        import _winapi
        junction = self.root / "junction"
        try:
            _winapi.CreateJunction(str(self.root), str(junction))
        except OSError:
            self.skipTest("junctions are not available")
        self.addCleanup(os.rmdir, junction)  # removes only the junction, before the temp directory
        with self.assertRaisesRegex(LegacyImportError, "reparse point"):
            self.import_into(junction / "data", self.store)
        with self.assertRaisesRegex(LegacyImportError, "reparse point"):
            self.import_into(self.source, junction / "store")
        self.assertFalse((self.root / "store").exists())

    def reference_generation(self):
        pointer = self.run_import(self.root / "measure")
        return pointer, self.root / "measure" / "generations" / pointer["generation"]

    def test_existing_identical_generation_is_verified_and_published(self):
        pointer, built = self.reference_generation()
        shutil.copytree(built, self.store / "generations" / pointer["generation"])
        self.assertEqual(self.run_import(), pointer)
        self.assertEqual(json.loads((self.store / "current.json").read_bytes()), pointer)
        self.assertEqual(list(self.store.glob(".import-staging-*")) + list(self.store.glob(".current-*")), [])

    def test_existing_generation_with_redirected_payload_is_refused(self):
        pointer, built = self.reference_generation()
        chunk = next(built.glob("chunks/*.gz")).relative_to(built)
        for label in ("payload file", "COMPLETE.json", "chunks directory"):
            with self.subTest(label):
                store = self.root / f"store-{label.split()[0].split('.')[0]}"
                target = store / "generations" / pointer["generation"]
                shutil.copytree(built, target)
                # Every redirection points at byte-identical content, so only the link itself is at fault.
                if label == "payload file":
                    link, original = target / chunk, built / chunk
                elif label == "COMPLETE.json":
                    link, original = target / "COMPLETE.json", built / "COMPLETE.json"
                else:
                    link, original = target / "chunks", built / "chunks"
                if link.is_dir():
                    shutil.rmtree(link)
                else:
                    link.unlink()
                self.symlink_or_skip(original, link)
                with self.assertRaisesRegex(LegacyImportError, "cannot be verified safely"):
                    self.import_into(self.source, store)
                self.assertFalse((store / "current.json").exists())
                self.assertTrue(os.path.lexists(link), "existing data is never deleted")
                self.assertEqual(list(store.glob(".import-staging-*")), [])
                self.assertFalse((store / ".publish.lock").exists())

    def test_overlapping_store_is_refused(self):
        with self.assertRaisesRegex(LegacyImportError, "overlap"):
            self.run_import(self.source / "store")
        self.assertFalse((self.source / "store").exists(), "nothing is created inside the source")
        with self.assertRaisesRegex(LegacyImportError, "overlap"):
            import_legacy_generation(self.source, self.root, world="uie", dimension="0",
                                     expected_manifest_sha256=self.expected, min_free_bytes=0)

    def test_write_budget_and_disk_reserve_are_checked_before_promotion(self):
        reference = self.run_import(self.root / "measure")
        measure = self.root / "measure"
        total = sum(p.stat().st_size for p in (measure / "generations" / reference["generation"]).rglob("*") if p.is_file())
        total += (measure / "current.json").stat().st_size
        # Everything but the pointer fits: the preflight must refuse before the stage becomes a generation.
        with self.assertRaisesRegex(LegacyImportError, "budget"):
            self.run_import(max_write_bytes=total - 1)
        self.assert_nothing_published()
        with self.assertRaisesRegex(LegacyImportError, "budget"):
            self.run_import(max_write_bytes=100)
        self.assert_nothing_published()
        with mock.patch.object(legacy.shutil, "disk_usage", return_value=SimpleNamespace(free=10)):
            with self.assertRaisesRegex(LegacyImportError, "reserve"):
                self.run_import(min_free_bytes=100)
        self.assert_nothing_published()
        self.assertEqual(self.run_import(max_write_bytes=total), reference)

    def test_invalid_arguments(self):
        for kwargs in ({"expected_manifest_sha256": None}, {"min_free_bytes": -1}, {"min_free_bytes": True},
                       {"max_write_bytes": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LegacyImportError):
                self.run_import(**kwargs)
        with self.assertRaises(LegacyImportError):
            import_legacy_generation(self.source, self.store, world=" ", dimension="0",
                                     expected_manifest_sha256=self.expected)
        self.assertTrue(issubclass(LegacyImportError, PublicationError))


if __name__ == "__main__":
    unittest.main()
