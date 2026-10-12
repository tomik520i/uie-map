"""Whole-world overview colours: average top-face colour per block, measured from a published generation.

For every upward-facing triangle of every detail mesh, the block directly below the face centroid is looked up
in the generation's UIE_BLOCKS1 data, and atlas texel (at the UV centroid; texture flipY=false, so v=0 is the
top row) x vertex colour is accumulated, weighted by the triangle's area. Fully transparent texels are skipped.
This needs no Minecraft client; it only knows blocks that occur on top faces in rendered areas. Anything else
gets a fallback colour in overview_tiles.py (and is counted as such there).

    python overview_colors.py GENERATION_DIR OUT.json
Output UIE_OVERVIEW_COLORS1: {source, atlasSha256, byMeta: {"name|meta": [r,g,b,weight]}, byName: {...}}.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import sys
from pathlib import Path

import png_io


def _load_json(root: Path, name: str):
    path = root / name
    if path.exists():
        return json.loads(path.read_bytes())
    return json.loads(gzip.decompress((root / (name + ".gz")).read_bytes()))


class BlockLookup:
    """Expanded UIE_BLOCKS1 document of one chunk: (wx, y, wz) -> (name, meta) or None."""

    def __init__(self, doc: dict):
        self.x, self.z, self.width, self.depth = doc["x"], doc["z"], doc["width"], doc["depth"]
        registry = doc["registry"]
        ids = bytearray()
        metas = bytearray()
        self.names = {}
        for n, block, meta in doc["runs"]:
            self.names.setdefault(block, registry[str(block)])
            ids.extend(block.to_bytes(2, "big") * n)
            metas.extend(bytes([meta]) * n)
        self.ids, self.metas = ids, metas

    def at(self, wx: int, y: int, wz: int):
        lx, lz = wx - self.x, wz - self.z
        if not (0 <= lx < self.width and 0 <= lz < self.depth and 0 <= y < 256):
            return None
        i = (lz * self.width + lx) * 256 + y
        block = (self.ids[2 * i] << 8) | self.ids[2 * i + 1]
        return self.names[block], self.metas[i]


def measure(generation: Path) -> dict:
    generation = Path(generation)
    manifest = json.loads((generation / "manifest.json").read_bytes())
    atlas_raw = (generation / manifest["atlas"]).read_bytes()
    width, height, channels, pixels = png_io.decode(atlas_raw)
    if channels != 4:
        raise ValueError("atlas must be RGBA")
    blocks = {(e["x"], e["z"]): e["file"] for e in manifest["blockInfo"]["chunks"]}
    sums: dict[tuple[str, int], list[float]] = {}
    faces = skipped = 0
    for entry in manifest["chunks"]:
        key = (entry["x"], entry["z"])
        if key not in blocks:
            continue
        lookup = BlockLookup(_load_json(generation, blocks[key]))
        mesh = _load_json(generation, entry["file"])
        for part in [mesh, *mesh.get("transparentParts", [])]:
            pos, uv, col, idx = part["positions"], part["uvs"], part["colors"], part["indices"]
            for t in range(0, len(idx), 3):
                a, b, c = idx[t], idx[t + 1], idx[t + 2]
                ax, ay, az = pos[3 * a:3 * a + 3]
                bx, by, bz = pos[3 * b:3 * b + 3]
                cx, cy, cz = pos[3 * c:3 * c + 3]
                ux, uy, uz = bx - ax, by - ay, bz - az
                vx, vy, vz = cx - ax, cy - ay, cz - az
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                area = math.sqrt(nx * nx + ny * ny + nz * nz)
                if area <= 1e-9 or ny / area < 0.9:
                    continue
                gx, gy, gz = (ax + bx + cx) / 3, (ay + by + cy) / 3, (az + bz + cz) / 3
                block = lookup.at(math.floor(gx), math.floor(gy - 0.02), math.floor(gz))
                if block is None or block[0] == "minecraft:air":
                    skipped += 1
                    continue
                u = (uv[2 * a] + uv[2 * b] + uv[2 * c]) / 3
                v = (uv[2 * a + 1] + uv[2 * b + 1] + uv[2 * c + 1]) / 3
                px = min(width - 1, max(0, int(u * width)))
                py = min(height - 1, max(0, int(v * height)))
                o = (py * width + px) * 4
                if pixels[o + 3] == 0:
                    skipped += 1
                    continue
                shade = [(col[3 * a + k] + col[3 * b + k] + col[3 * c + k]) / 3 for k in range(3)]
                acc = sums.setdefault(block, [0.0, 0.0, 0.0, 0.0])
                w = area / 2
                for k in range(3):
                    acc[k] += pixels[o + k] * min(1.0, max(0.0, shade[k])) * w
                acc[3] += w
                faces += 1
    by_meta, by_name = {}, {}
    for (name, meta), (r, g, b, w) in sorted(sums.items()):
        by_meta[f"{name}|{meta}"] = [round(r / w), round(g / w), round(b / w), round(w, 2)]
        n = by_name.setdefault(name, [0.0, 0.0, 0.0, 0.0])
        for k, value in enumerate((r, g, b, w)):
            n[k] += value
    by_name = {name: [round(r / w), round(g / w), round(b / w), round(w, 2)] for name, (r, g, b, w) in by_name.items()}
    return {"format": "UIE_OVERVIEW_COLORS1", "source": str(generation.name), "atlasSha256": hashlib.sha256(atlas_raw).hexdigest(),
            "faces": faces, "skippedFaces": skipped, "byMeta": by_meta, "byName": by_name}


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    doc = measure(Path(argv[0]))
    Path(argv[1]).write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps({k: doc[k] for k in ("faces", "skippedFaces")} | {"blocks": len(doc["byName"]), "variants": len(doc["byMeta"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
