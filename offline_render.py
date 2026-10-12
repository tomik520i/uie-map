"""Render one UIE2 batch offline into a geometry directory in the client capture's layout. No Minecraft.

    python offline_render.py --batch DIR/snapshot.bin --catalog-dir DIR --out GEOMETRY_DIR [--caves]

catalog-dir holds assets.json, atlas-layout.json, atlas.png, grass.png and foliage.png (existing exported files).
Output: manifest.json (format 1, snapshotFormat UIE2, renderer identity), chunk_<x>_<z>.json, atlas.png,
atlas-layout.json, render-report.json (timings, peak RSS, fallbacks with coordinates, faces, bytes).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import capture_batch as cb
from offline_catalog import Catalog
from offline_mesher import Colormap, Mesher
from offline_volume import Volume

RENDERER = "uie-offline-mesher/1"


def peak_rss_bytes():
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak * 1024 if sys.platform.startswith("linux") else peak
    except ImportError:
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return pmc.PeakWorkingSetSize
        except Exception:
            return None


class Renderer:
    def __init__(self, catalog_dir: Path, caves: bool = False):
        d = Path(catalog_dir)
        self.dir = d
        # Workers use the compact compiled catalog (same models, ~10x less memory); the raw exports are the fallback.
        if (d / "catalog.json").exists():
            self.catalog = Catalog.load_compiled(d / "catalog.json")
        else:
            self.catalog = Catalog.load(d / "assets.json", d / "atlas-layout.json")
        self.colormap = Colormap(d / "grass.png", d / "foliage.png")
        self.caves = caves
        self.mesher = Mesher(self.catalog, self.colormap, caves=caves)
        code = b"".join(Path(__file__).with_name(n).read_bytes()
                        for n in ("offline_render.py", "offline_mesher.py", "offline_catalog.py", "offline_volume.py"))
        extra = b"".join((d / n).read_bytes() for n in ("grass.png", "foliage.png"))
        self.identity = hashlib.sha256(self.catalog.identity.encode() + hashlib.sha256(code).digest()
                                       + hashlib.sha256(extra).digest() + (b"caves" if caves else b"surface")).hexdigest()

    def render(self, batch_path: Path, out: Path, write_report: bool = True) -> dict:
        started = time.perf_counter()
        raw = Path(batch_path).read_bytes()
        snapshot_sha = hashlib.sha256(raw).hexdigest()
        vol = Volume(cb.parse_batch(raw))
        loaded = time.perf_counter()
        result = self.mesher.mesh_batch(vol)
        meshed = time.perf_counter()
        out.mkdir(parents=True, exist_ok=True)
        chunks, raw_bytes, triangles, vertices = [], 0, 0, 0
        for (cx, cz), doc in sorted(result["chunks"].items(), key=lambda kv: (kv[0][1], kv[0][0])):
            name = f"chunk_{cx}_{cz}.json"
            data = json.dumps(doc, separators=(",", ":")).encode()
            (out / name).write_bytes(data)
            raw_bytes += len(data)
            for p in [doc, *doc.get("transparentParts", [])]:
                triangles += len(p["indices"]) // 3
                vertices += len(p["positions"]) // 3
            chunks.append({"file": name, "x": cx, "z": cz})
        shutil.copyfile(self.dir / "atlas.png", out / "atlas.png")
        shutil.copyfile(self.dir / "atlas-layout.json", out / "atlas-layout.json")
        layout_sha = hashlib.sha256((out / "atlas-layout.json").read_bytes()).hexdigest()
        stats = result["stats"]
        core = vol.core
        unsupported = dict(sorted(((f"{n}|{m}", c) for (n, m), c in stats["fallbackBlocks"].items()),
                                  key=lambda kv: -kv[1]))
        manifest = {"format": 1, "title": "UIE · offline render", "sample": True,
                    "generatedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "atlas": "atlas.png", "chunks": chunks, "bakedLighting": True, "atlasLayout": "atlas-layout.json",
                    "atlasLayoutSha256": layout_sha, "snapshotFormat": "UIE2", "snapshotSha256": snapshot_sha,
                    "bounds": {"min": [core.x, 0, core.z], "max": [core.x + core.width, 256, core.z + core.depth]},
                    "coverage": {"renderedBlocks": stats["renderedBlocks"],
                                 "unsupportedBlocks": sum(stats["fallbackBlocks"].values()),
                                 "unsupportedNames": sorted({n for (n, _m) in stats["fallbackBlocks"]}),
                                 "emptyBlocks": sum(stats["invisibleBlocks"].values())},
                    "renderer": {"name": RENDERER, "identity": self.identity, "catalog": self.catalog.identity,
                                 "caves": self.caves}}
        (out / "manifest.json").write_bytes(json.dumps(manifest, indent=1).encode())
        report = {"format": "UIE_OFFLINE_RENDER_REPORT1", "renderer": RENDERER, "identity": self.identity,
                  "snapshotSha256": snapshot_sha, "chunks": len(chunks), "caves": self.caves,
                  "seconds": {"load": round(loaded - started, 3), "mesh": round(meshed - loaded, 3),
                              "write": round(time.perf_counter() - meshed, 3),
                              "total": round(time.perf_counter() - started, 3)},
                  "secondsPerChunk": round((time.perf_counter() - started) / max(1, len(chunks)), 4),
                  "peakRssBytes": peak_rss_bytes(), "triangles": triangles, "vertices": vertices,
                  "rawJsonBytes": raw_bytes, "faces": stats["faces"], "culledCaveFaces": stats["culledCaveFaces"],
                  "renderedBlocks": stats["renderedBlocks"],
                  "fallbackBlocks": unsupported,
                  "fallbackExamples": {f"{n}|{m}": list(v) for (n, m), v in stats["fallbackExamples"].items()},
                  "assumedBlocks": dict(sorted(((f"{n}|{m}", c) for (n, m), c in stats["assumedBlocks"].items()),
                                               key=lambda kv: -kv[1])),
                  "unknownBiomes": dict(self.colormap.unknown_biomes.most_common(20))}
        if write_report:   # the worker contract allows only manifest, atlas, layout and chunks in geometry/
            (out / "render-report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, type=Path)
    ap.add_argument("--catalog-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--caves", action="store_true")
    a = ap.parse_args(argv)
    report = Renderer(a.catalog_dir, a.caves).render(a.batch, a.out)
    print(json.dumps({k: v for k, v in report.items() if k not in ("fallbackExamples",)}, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
