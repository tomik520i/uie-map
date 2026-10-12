"""Publish an overview tile build (overview_tiles.py output) into an overview store. Stdlib only.

    store/builds/<sha256(index.json)>/index.json, L<k>/...    immutable, never overwritten
    store/current.json  {"format": "UIE_OVERVIEW_POINTER1", "build", "indexSha256", "publishedAt", "tiles"}

The build is copied to a private temporary directory inside the store, every file is re-hashed against the
source, then renamed into place; only then is current.json replaced atomically. Older builds are kept.
An existing build directory with the same id must hold identical bytes. The pointer can only move forward from
the expected previous build (compare-and-swap), so a stale publication cannot replace a newer one.

    python3 publish_overview.py TILES_DIR STORE [--expected-build <id>|none]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

POINTER = "UIE_OVERVIEW_POINTER1"
TILE = re.compile(r"L([0-9]|1[0-9]|2[0-3])/-?[0-9]{1,6}_-?[0-9]{1,6}(\.h)?\.png")


class OverviewPublishError(Exception):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _files(root: Path) -> dict[str, str]:
    """Every publishable file (index.json + tile PNGs) -> sha256. Anything else is refused."""
    result = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise OverviewPublishError(f"symlink in build: {p}")
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel != "index.json" and not TILE.fullmatch(rel):
            raise OverviewPublishError(f"unexpected file in build: {rel}")
        result[rel] = _sha(p)
    return result


def _expected_files(index: dict) -> set[str]:
    names = {"index.json"}
    for level, tiles in index["tiles"].items():
        for tx, tz in tiles:
            names.add(f"L{level}/{tx}_{tz}.png")
            names.add(f"L{level}/{tx}_{tz}.h.png")
    return names


def current_build(store: Path):
    pointer = store / "current.json"
    if not pointer.exists():
        return None
    doc = json.loads(pointer.read_bytes())
    if doc.get("format") != POINTER:
        raise OverviewPublishError("invalid current overview pointer")
    return doc["build"]


def publish(tiles: Path, store: Path, expected_build) -> dict:
    tiles, store = tiles.resolve(), store.resolve()
    index_raw = (tiles / "index.json").read_bytes()
    index = json.loads(index_raw)
    if index.get("format") != "UIE_OVERVIEW1":
        raise OverviewPublishError("not a UIE_OVERVIEW1 build")
    files = _files(tiles)
    if set(files) != _expected_files(index):
        raise OverviewPublishError("build files differ from its index")
    build = hashlib.sha256(index_raw).hexdigest()
    store.mkdir(parents=True, exist_ok=True)
    (store / "builds").mkdir(exist_ok=True)
    if current_build(store) != expected_build:
        raise OverviewPublishError("stale overview publication base")
    target = store / "builds" / build
    if target.exists():
        if _files(target) != files:
            raise OverviewPublishError("existing build with this id differs")
    else:
        temp = store / f".staging-{uuid.uuid4().hex}"
        previous = store / "builds" / expected_build if expected_build else None
        previous_files = _files(previous) if previous is not None and previous.is_dir() else {}

        def link_or_copy(src, dst):
            # Unchanged tiles are hard links to the previous build (immutable), so hourly builds stay small.
            rel = Path(src).relative_to(tiles).as_posix()
            if previous_files.get(rel) is not None and previous_files[rel] == files.get(rel):
                try:
                    os.link(previous / rel, dst)
                    return dst
                except OSError:
                    pass
            return shutil.copy2(src, dst)
        try:
            shutil.copytree(tiles, temp, copy_function=link_or_copy)
            if _files(temp) != files:
                raise OverviewPublishError("copied build differs from its source")
            os.rename(temp, target)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
    pointer = {"format": POINTER, "build": build, "indexSha256": build,
               "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
               "tiles": len(files) - 1}
    raw = (json.dumps(pointer, sort_keys=True) + "\n").encode()
    tmp = store / f".current-{uuid.uuid4().hex}.tmp"
    with open(tmp, "xb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, store / "current.json")
    return pointer


def prune(store: Path, keep: int = 3) -> list[str]:
    """Delete old builds of THIS overview store only: never the current one, always the `keep` newest by mtime.
    Only directories named by a 64-hex build id directly inside store/builds are considered."""
    store = store.resolve()
    builds = store / "builds"
    current = current_build(store)
    candidates = [p for p in builds.iterdir()
                  if p.is_dir() and not p.is_symlink() and re.fullmatch(r"[0-9a-f]{64}", p.name)
                  and p.resolve().parent == builds]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for p in candidates[keep:]:
        if p.name == current:
            continue
        shutil.rmtree(p)
        removed.append(p.name)
    return removed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tiles", type=Path)
    ap.add_argument("store", type=Path)
    ap.add_argument("--expected-build", default="none")
    a = ap.parse_args(argv)
    expected = None if a.expected_build == "none" else a.expected_build
    print(json.dumps(publish(a.tiles, a.store, expected), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
