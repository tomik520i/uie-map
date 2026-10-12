"""Frozen runtime profile of the offline renderer for runtime_adapter/warm_worker (replaces the client profile).

    python3 offline_profile.py --code DIR --catalog DIR --out PROFILE_DIR [--caves]

PROFILE_DIR/runtime/code      offline renderer modules (pure Python)
PROFILE_DIR/runtime/catalog   catalog.json, atlas.png, atlas-layout.json, grass.png, foliage.png
PROFILE_DIR/profile.json      {identity, runtime}: declared-inputs pipeline identity of exactly these files and
                              the command `/usr/bin/python3 -u @RUNTIME@/code/offline_batch.py ...` (no Java)
The pipeline key therefore changes whenever renderer code, catalog, atlas or colour maps change, which
invalidates every rendered tile bound to the old key (publication refuses mixed pipelines).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from offline_render import RENDERER
from pipeline_identity import create_pipeline_identity

CODE_FILES = ["offline_batch.py", "offline_render.py", "offline_mesher.py", "offline_catalog.py", "offline_volume.py",
              "capture_batch.py", "capture_snapshot.py", "reader.py", "png_io.py"]
CATALOG_FILES = ["catalog.json", "atlas.png", "atlas-layout.json", "grass.png", "foliage.png"]


def build_profile(code: Path, catalog: Path, out: Path, caves: bool = False, python: str = "/usr/bin/python3") -> dict:
    runtime_dir = out / "runtime"
    (runtime_dir / "code").mkdir(parents=True, exist_ok=False)
    (runtime_dir / "catalog").mkdir()
    for name in CODE_FILES:
        shutil.copyfile(code / name, runtime_dir / "code" / name)
    for name in CATALOG_FILES:
        shutil.copyfile(catalog / name, runtime_dir / "catalog" / name)
    command = [python, "-u", "@RUNTIME@/code/offline_batch.py", "--plan", "@PLAN@",
               "--catalog-dir", "@RUNTIME@/catalog", "--reports", "@OUTPUT@"] + (["--caves"] if caves else [])
    runtime = {"roots": {"code": "code", "catalog": "catalog"}, "files": {}, "links": {}, "command": command,
               "env": {"PYTHONDONTWRITEBYTECODE": "1"}, "cwd": "code", "executables": []}
    options = {"profile": "uie-offline/1", "renderer": RENDERER, "caves": caves, "runtime": runtime}
    identity = create_pipeline_identity({"code": runtime_dir / "code", "catalog": runtime_dir / "catalog"}, {}, options)
    profile = {"identity": identity, "runtime": runtime}
    (out / "profile.json").write_text(json.dumps(profile, indent=1), encoding="utf-8")
    return profile


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, type=Path)
    ap.add_argument("--catalog", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--caves", action="store_true")
    a = ap.parse_args(argv)
    profile = build_profile(a.code.resolve(), a.catalog.resolve(), a.out.resolve(), a.caves)
    print(json.dumps({"pipelineKey": profile["identity"]["pipelineKey"], "profile": str(a.out / "profile.json")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
