"""Benchmark the offline renderer on real batches (surface and caves modes) against client references.

    python offline_benchmark.py --catalog-dir DIR --out DIR --batch NAME=snapshot.bin[=reference_geometry_dir] ...

Per batch and mode: seconds, seconds/chunk, triangles, raw JSON bytes, gzip bytes (level 6, like publication),
fallback/assumed block counts and, with a reference, visible-face recall/precision (offline_compare). Peak RSS
of the whole process is reported at the end (Linux: ru_maxrss).
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

import offline_compare
from offline_render import Renderer, peak_rss_bytes


def gzip_bytes(geometry: Path) -> int:
    total = 0
    for p in geometry.glob("chunk_*.json"):
        total += len(gzip.compress(p.read_bytes(), 6, mtime=0))
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--batch", action="append", required=True)
    ap.add_argument("--modes", default="surface,caves")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    renderers = {mode: Renderer(a.catalog_dir, caves=(mode == "caves")) for mode in a.modes.split(",")}
    rows = []
    for spec in a.batch:
        parts = spec.split("=")
        name, batch = parts[0], Path(parts[1])
        reference = Path(parts[2]) if len(parts) > 2 else None
        for mode, renderer in renderers.items():
            geometry = a.out / f"{name}-{mode}"
            report = renderer.render(batch, geometry)
            row = {"batch": name, "mode": mode, "chunks": report["chunks"], "seconds": report["seconds"]["total"],
                   "secondsPerChunk": report["secondsPerChunk"], "triangles": report["triangles"],
                   "rawJsonBytes": report["rawJsonBytes"], "gzipBytes": gzip_bytes(geometry),
                   "fallbackBlocks": sum(report["fallbackBlocks"].values()),
                   "assumedBlocks": sum(report["assumedBlocks"].values()),
                   "renderedBlocks": report["renderedBlocks"], "culledCaveFaces": report["culledCaveFaces"]}
            if reference is not None and mode == "caves":
                cmp_out = a.out / f"compare-{name}.json"
                offline_compare.main(["--reference", str(reference), "--offline", str(geometry), "--batch", str(batch),
                                      "--layout", str(a.catalog_dir / "atlas-layout.json"),
                                      "--catalog-dir", str(a.catalog_dir), "--out", str(cmp_out)])
                c = json.loads(cmp_out.read_text(encoding="utf-8"))
                row.update({"recall": c["recall"], "precision": c["precision"], "referenceFaces": c["referenceFaces"]})
                ref_manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
                ref_tris = ref_bytes = ref_gz = 0
                for e in ref_manifest["chunks"]:
                    raw = (reference / e["file"]).read_bytes()
                    ref_bytes += len(raw)
                    ref_gz += len(gzip.compress(raw, 6, mtime=0))
                    doc = json.loads(raw)
                    for p in [doc, *doc.get("transparentParts", [])]:
                        ref_tris += len(p["indices"]) // 3
                row.update({"referenceTriangles": ref_tris, "referenceRawBytes": ref_bytes, "referenceGzipBytes": ref_gz})
            rows.append(row)
            print(json.dumps(row), flush=True)
    summary = {"format": "UIE_OFFLINE_BENCHMARK1", "rows": rows, "peakRssBytes": peak_rss_bytes(),
               "renderers": {m: r.identity for m, r in renderers.items()}, "finishedAt": time.time()}
    (a.out / "benchmark.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({"peakRssBytes": summary["peakRssBytes"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
