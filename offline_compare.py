"""Compare an offline geometry directory with the client-captured reference of the same batch, face by face.

    python offline_compare.py --reference REF_GEOMETRY --offline OUT_GEOMETRY --batch snapshot.bin --catalog-dir DIR

Each quad (two triangles sharing an edge, as both renderers emit) is reduced to a key: the block cell it belongs to
(centroid moved half a voxel against the normal), its axis-aligned normal and the sprite under its UV centre.
Reported per block type (registry name|meta of the cell): reference faces, offline faces, matched (same cell,
normal and sprite), and "geometry only" matches (same cell and normal, other sprite). Non-axis faces (plants,
tilted models) are compared by cell only. This measures coverage and texture agreement, not pixel equality.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

import capture_batch as cb
from offline_volume import Volume


class SpriteIndex:
    """UV point -> sprite name, from the atlas layout."""

    def __init__(self, layout: dict):
        self.rects = [(s["uv"], name) for name, s in layout["registered"].items() if s.get("state") == "placed"]

    def build(self):
        self.grid = defaultdict(list)
        for (u0, v0, u1, v1), name in self.rects:
            for gu in range(int(min(u0, u1) * 64), int(max(u0, u1) * 64) + 1):
                for gv in range(int(min(v0, v1) * 64), int(max(v0, v1) * 64) + 1):
                    self.grid[(gu, gv)].append(((min(u0, u1), min(v0, v1), max(u0, u1), max(v0, v1)), name))
        return self

    def at(self, u, v):
        for (u0, v0, u1, v1), name in self.grid.get((int(u * 64), int(v * 64)), ()):
            if u0 <= u <= u1 and v0 <= v <= v1:
                return name
        return None


def quads(doc):
    for part in [doc, *doc.get("transparentParts", [])]:
        pos, uv, idx = part["positions"], part["uvs"], part["indices"]
        for t in range(0, len(idx) - 5, 6):
            corners = set(idx[t:t + 6])
            if len(corners) != 4:
                # not a quad pair: fall back to single triangles
                for k in (t, t + 3):
                    yield [idx[k], idx[k + 1], idx[k + 2]], pos, uv
                continue
            yield sorted(corners), pos, uv


def face_key(vs, pos, uv, sprites):
    pts = [(pos[3 * v], pos[3 * v + 1], pos[3 * v + 2]) for v in vs]
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    cz = sum(p[2] for p in pts) / len(pts)
    a, b, c = pts[0], pts[1], pts[2]
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    w = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
    length = math.sqrt(sum(x * x for x in n)) or 1.0
    n = tuple(x / length for x in n)
    axis = max(range(3), key=lambda i: abs(n[i]))
    aligned = abs(n[axis]) > 0.99
    us = sum(uv[2 * v] for v in vs) / len(vs)
    vv = sum(uv[2 * v + 1] for v in vs) / len(vs)
    centre = (round(cx, 2), round(cy, 2), round(cz, 2))
    if aligned:
        # Winding differs between renderers, so the owner is decided later from the two cells on either side.
        lo, hi = [cx, cy, cz], [cx, cy, cz]
        lo[axis] -= 0.01
        hi[axis] += 0.01
        cells = (tuple(math.floor(v) for v in lo), tuple(math.floor(v) for v in hi))
        return cells, (axis, 0), sprites.at(us, vv), centre
    cell = (math.floor(cx), math.floor(cy), math.floor(cz))
    return (cell, cell), (-1, 0), sprites.at(us, vv), centre


def collect(geometry: Path, sprites):
    manifest = json.loads((geometry / "manifest.json").read_text(encoding="utf-8"))
    faces = Counter()
    for entry in manifest["chunks"]:
        doc = json.loads((geometry / entry["file"]).read_text(encoding="utf-8"))
        for vs, pos, uv in quads(doc):
            cells, normal, sprite, centre = face_key(vs, pos, uv, sprites)
            faces[(cells, normal, centre, sprite)] += 1
    return faces


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True, type=Path)
    ap.add_argument("--offline", required=True, type=Path)
    ap.add_argument("--batch", required=True, type=Path)
    ap.add_argument("--layout", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--catalog-dir", type=Path, help="assets.json + atlas-layout.json: classify hidden faces")
    a = ap.parse_args(argv)
    sprites = SpriteIndex(json.loads(a.layout.read_text(encoding="utf-8"))).build()
    vol = Volume(cb.parse_batch(a.batch.read_bytes()))
    ref, off = collect(a.reference, sprites), collect(a.offline, sprites)
    hidden = Counter()
    if a.catalog_dir:
        from offline_catalog import Catalog
        catalog = Catalog.load(a.catalog_dir / "assets.json", a.catalog_dir / "atlas-layout.json")

        def occludes(cell):
            block_id, meta = vol.block(*cell)
            return block_id > 0 and catalog.model(vol.registry.get(block_id, ""), meta).occludes
        for key in list(ref):
            cells, normal, centre, sprite = key
            enclosed = normal[0] >= 0 and cells[0] != cells[1] and occludes(cells[0]) and occludes(cells[1])
            world_bottom = normal[0] == 1 and centre[1] == 0.0
            if enclosed or world_bottom:
                hidden["enclosed" if enclosed else "worldBottom"] += ref.pop(key)

    def name(cells):
        # the face belongs to the solid/visible side: prefer a non-air cell (the first if both are blocks)
        for cell in cells:
            block_id, meta = vol.block(*cell)
            if block_id > 0:
                return f"{vol.registry.get(block_id, '?')}|{meta}"
        return "air"
    per = defaultdict(lambda: Counter())
    ref_geo = Counter((c, n, ce) for (c, n, ce, s), k in ref.items() for _ in range(k))
    off_geo = Counter((c, n, ce) for (c, n, ce, s), k in off.items() for _ in range(k))
    for key, k in ref.items():
        per[name(key[0])]["reference"] += k
        per[name(key[0])]["matched"] += min(k, off.get(key, 0))
    for key, k in off.items():
        per[name(key[0])]["offline"] += k
    for key, k in ref_geo.items():
        per[name(key[0])]["geometryMatched"] += min(k, off_geo.get(key, 0))
    total = Counter()
    for c in per.values():
        total.update(c)
    rows = sorted(({"block": b, **c} for b, c in per.items()), key=lambda r: -r.get("reference", 0))
    report = {"hiddenReferenceFacesIgnored": dict(hidden), "referenceFaces": total["reference"], "offlineFaces": total["offline"],
              "matched": total["matched"], "geometryMatched": total["geometryMatched"],
              "recall": round(total["matched"] / max(1, total["reference"]), 4),
              "geometryRecall": round(total["geometryMatched"] / max(1, total["reference"]), 4),
              "precision": round(total["matched"] / max(1, total["offline"]), 4),
              "geometryPrecision": round(total["geometryMatched"] / max(1, total["offline"]), 4),
              "perBlock": rows}
    if a.out:
        a.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "perBlock"}, indent=1))
    for r in rows[:40]:
        ref_n = r.get("reference", 0)
        print(f"{r['block'][:55]:55s} ref {ref_n:7d} off {r.get('offline', 0):7d} "
              f"match {r.get('matched', 0) / max(1, ref_n):5.2f} geo {r.get('geometryMatched', 0) / max(1, ref_n):5.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
