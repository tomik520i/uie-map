"""Whole-world overview, stages B+C: colour + height tiles in a quadtree of zoom levels. Stdlib only.

Input: a stage-A directory (overview_surface.py: r.X.Z.surface + registry.json) and a colour table
(overview_colors.py). Output directory:

    L0/<rx>_<rz>.png      512x512 RGBA, 1 px = 1 block column of region (rx, rz); alpha 0 = no chunk
    L0/<rx>_<rz>.h.png    512x512 grey, topmost block Y
    Lk/<tx>_<tz>(.h).png  512x512, tile (tx, tz) covers 2^k x 2^k regions (1 px = 2^k blocks), averaged
    index.json            UIE_OVERVIEW1: levels, tiles per level, block bounds, colour coverage report

Colour per column: byMeta "name|meta", else byName, else a keyword/hash fallback (counted per name in
index.json "fallbackColumns" - a diagnostic of blocks the colour table does not know yet). Relief shading
compares each column's height with its north-west neighbour, like common 2D map renderers.

    python3 overview_tiles.py --surface DIR --colors colors.json --out DIR [--workers 8]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import overview_surface as ovs
import png_io

SIZE = 512
KEYWORDS = [("lava", (210, 100, 30)), ("water", (40, 60, 150)), ("ice", (160, 190, 230)), ("snow", (240, 245, 250)),
            ("leaves", (45, 80, 35)), ("leaf", (45, 80, 35)), ("grass", (80, 120, 55)), ("sand", (215, 200, 150)),
            ("log", (110, 85, 55)), ("wood", (140, 110, 70)), ("plank", (160, 130, 80)), ("dirt", (120, 85, 60)),
            ("clay", (160, 165, 175)), ("gravel", (135, 130, 125)), ("ore", (130, 125, 120)),
            ("stone", (128, 128, 128)), ("rock", (120, 120, 120)), ("flower", (180, 80, 120)),
            ("glass", (200, 220, 230)), ("brick", (150, 90, 80)), ("wool", (220, 220, 220))]


# Hand-picked colours for frequent surface blocks the measured table does not know (approximate in-game look).
# "name" applies to every meta; "name|meta" to one variant. Measured colours always take precedence.
STAINED_CLAY = [(209, 178, 161), (161, 83, 37), (149, 88, 108), (113, 108, 137), (186, 133, 35), (103, 117, 52),
                (161, 78, 78), (57, 42, 35), (135, 106, 97), (86, 91, 91), (118, 70, 86), (74, 59, 91), (77, 51, 35),
                (75, 82, 42), (142, 60, 46), (37, 22, 16)]
MANUAL = {"minecraft:ice": (150, 180, 245), "minecraft:packed_ice": (140, 170, 235), "minecraft:snow": (245, 250, 255),
          "minecraft:snow_layer": (240, 246, 252), "minecraft:hardened_clay": (150, 92, 66),
          "minecraft:sand": (219, 207, 163), "minecraft:sandstone": (216, 203, 155), "minecraft:mycelium": (111, 99, 105),
          "minecraft:netherrack": (111, 54, 52), "minecraft:obsidian": (20, 18, 30), "minecraft:soul_sand": (84, 64, 51),
          "minecraft:clay": (160, 166, 179), "minecraft:farmland": (115, 75, 45), "minecraft:pumpkin": (200, 120, 30),
          "minecraft:cactus": (85, 125, 45), "minecraft:melon_block": (110, 145, 30), "minecraft:end_stone": (220, 222, 158),
          "BiomesOPlenty:hardDirt": (112, 82, 62), "BiomesOPlenty:hardSand": (190, 166, 112),
          "BiomesOPlenty:cragRock": (112, 104, 96), "BiomesOPlenty:driedDirt": (142, 112, 80),
          "BiomesOPlenty:mud": (75, 60, 50), "BiomesOPlenty:ash": (70, 68, 66), "BiomesOPlenty:originGrass": (95, 165, 70),
          "etfuturum:coarse_dirt": (117, 85, 60), "etfuturum:podzol": (92, 64, 30)}
MANUAL.update({f"minecraft:stained_hardened_clay|{m}": rgb for m, rgb in enumerate(STAINED_CLAY)})


def fallback(name: str) -> tuple[int, int, int]:
    low = name.lower()
    for word, rgb in KEYWORDS:
        if word in low:
            return rgb
    h = hashlib.sha256(name.encode()).digest()
    return tuple(90 + h[i] % 70 for i in range(3))


class Palette:
    def __init__(self, colors: dict, registry: dict):
        self.by_meta = {k: tuple(v[:3]) for k, v in colors["byMeta"].items()}
        self.by_name = {k: tuple(v[:3]) for k, v in colors["byName"].items()}
        self.names = {int(k): v for k, v in registry["blocks"].items()}
        self.cache: dict[tuple[int, int], tuple[tuple[int, int, int], str | None]] = {}

    def color(self, block: int, meta: int):
        """((r,g,b), fallback name or None)."""
        key = (block, meta)
        hit = self.cache.get(key)
        if hit is None:
            name = self.names.get(block, f"#{block}")
            rgb = (self.by_meta.get(f"{name}|{meta}") or self.by_name.get(name)
                   or MANUAL.get(f"{name}|{meta}") or MANUAL.get(name))
            hit = (rgb, None) if rgb else (fallback(name), name)
            self.cache[key] = hit
        return hit


def render_region(surface_path: Path, palette: Palette) -> tuple[bytes, bytes, dict]:
    """(rgba, height, fallback counts) for one stage-A region."""
    header, g = ovs.read_surface(surface_path)
    present, height, ids, metas = g["present"], g["height"], g["ids"], g["metas"]
    rgba = bytearray(SIZE * SIZE * 4)
    counts: dict[str, int] = {}
    for z in range(SIZE):
        row = z * SIZE
        for x in range(SIZE):
            cell = row + x
            if not present[cell]:
                continue
            block = (ids[2 * cell] << 8) | ids[2 * cell + 1]
            if block == 0:
                continue
            (r, gg, b), missing = palette.color(block, metas[cell])
            if missing:
                counts[missing] = counts.get(missing, 0) + 1
            h = height[cell]
            ref = cell - SIZE - 1 if x and z else cell
            if present[ref]:
                shade = 1.0 + max(-0.3, min(0.3, (h - height[ref]) * 0.06))
            else:
                shade = 1.0
            o = cell * 4
            rgba[o] = min(255, int(r * shade))
            rgba[o + 1] = min(255, int(gg * shade))
            rgba[o + 2] = min(255, int(b * shade))
            rgba[o + 3] = 255
    # Height PNG = ground (canopy excluded), so forests are coloured but never become towers.
    return bytes(rgba), bytes(g.get("ground", height)), {"region": header["region"], "fallback": counts,
                                        "observedAt": header["source"]["observedAt"]}


def _level0(job):
    surface_path, out, colors_path, registry_path = job
    palette = Palette(json.loads(Path(colors_path).read_text()), json.loads(Path(registry_path).read_text()))
    rgba, height, info = render_region(Path(surface_path), palette)
    rx, rz = info["region"]
    if not any(rgba[3::4]):
        return {**info, "empty": True}
    (Path(out) / "L0" / f"{rx}_{rz}.png").write_bytes(png_io.encode(SIZE, SIZE, 4, rgba))
    (Path(out) / "L0" / f"{rx}_{rz}.h.png").write_bytes(png_io.encode(SIZE, SIZE, 1, height))
    return {**info, "empty": False}


def _parent(job):
    """Average 2x2 child tiles of level k-1 into tile (tx, tz) of level k."""
    out, k, tx, tz = job
    src = Path(out) / f"L{k - 1}"
    rgba = bytearray(SIZE * SIZE * 4)
    height = bytearray(SIZE * SIZE)
    half = SIZE // 2
    for dz in (0, 1):
        for dx in (0, 1):
            path = src / f"{2 * tx + dx}_{2 * tz + dz}.png"
            if not path.exists():
                continue
            _, _, _, child = png_io.decode(path.read_bytes())
            _, _, _, ch = png_io.decode((src / f"{2 * tx + dx}_{2 * tz + dz}.h.png").read_bytes())
            for z in range(half):
                for x in range(half):
                    acc = [0, 0, 0]
                    hs = n = 0
                    for sz in (0, 1):
                        for sx in (0, 1):
                            c = (2 * z + sz) * SIZE + 2 * x + sx
                            if child[4 * c + 3]:
                                n += 1
                                acc[0] += child[4 * c]
                                acc[1] += child[4 * c + 1]
                                acc[2] += child[4 * c + 2]
                                hs += ch[c]
                    if n:
                        o = (dz * half + z) * SIZE + dx * half + x
                        rgba[4 * o:4 * o + 4] = bytes((acc[0] // n, acc[1] // n, acc[2] // n, 255))
                        height[o] = hs // n
    if not any(rgba[3::4]):
        return None
    dst = Path(out) / f"L{k}"
    (dst / f"{tx}_{tz}.png").write_bytes(png_io.encode(SIZE, SIZE, 4, rgba))
    (dst / f"{tx}_{tz}.h.png").write_bytes(png_io.encode(SIZE, SIZE, 1, height))
    return [tx, tz]


def build(surface: Path, colors: Path, out: Path, workers: int = 4, regions=None) -> dict:
    started = time.monotonic()
    out.mkdir(parents=True, exist_ok=True)
    (out / "L0").mkdir(exist_ok=True)
    registry = surface / "registry.json"
    paths = sorted(surface.glob("r.*.surface"))
    if regions is not None:
        paths = [p for p in paths if p.name in regions]
    jobs = [(str(p), str(out), str(colors), str(registry)) for p in paths]
    pool = None
    if workers > 1:
        from multiprocessing import Pool
        pool = Pool(workers)
    run = pool.imap_unordered if pool else map
    results = list(run(_level0, jobs))
    tiles = {0: sorted(r["region"] for r in results if not r["empty"])}
    fallback_columns: dict[str, int] = {}
    for r in results:
        for name, n in r["fallback"].items():
            fallback_columns[name] = fallback_columns.get(name, 0) + n
    k = 0
    # Floor division never merges tile -1 with tile 0, so the pyramid ends when every tile lies in the
    # 2x2 block around the origin (at most 4 top tiles), not at one tile.
    while tiles[k] and not all(tx in (-1, 0) and tz in (-1, 0) for tx, tz in tiles[k]):
        k += 1
        (out / f"L{k}").mkdir(exist_ok=True)
        parents = sorted({(tx // 2, tz // 2) for tx, tz in tiles[k - 1]})
        made = list(run(_parent, [(str(out), k, tx, tz) for tx, tz in parents]))
        tiles[k] = sorted(t for t in made if t is not None)
    if pool:
        pool.close()
        pool.join()
    xs = [t[0] for t in tiles[0]]
    zs = [t[1] for t in tiles[0]]
    observed = [r["observedAt"] for r in results]
    index = {"format": "UIE_OVERVIEW1", "tileSize": SIZE, "levels": len(tiles),
             "tiles": {str(level): t for level, t in tiles.items()},
             "blockBounds": {"min": [min(xs) * SIZE, min(zs) * SIZE], "max": [(max(xs) + 1) * SIZE, (max(zs) + 1) * SIZE]},
             "source": {"consistency": "observed-stable-files", "atomicWorldSnapshot": False,
                        "observedFrom": min(o["startUtc"] for o in observed), "observedTo": max(o["endUtc"] for o in observed),
                        "colors": json.loads(colors.read_text()).get("source")},
             "fallbackColumns": dict(sorted(fallback_columns.items(), key=lambda kv: -kv[1])),
             "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
             "elapsedSeconds": round(time.monotonic() - started, 1)}
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--surface", required=True, type=Path)
    ap.add_argument("--colors", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    index = build(a.surface.resolve(), a.colors.resolve(), a.out.resolve(), a.workers)
    print(json.dumps({k: index[k] for k in ("levels", "blockBounds", "elapsedSeconds")}
                     | {"tiles": {k: len(v) for k, v in index["tiles"].items()},
                        "topFallback": list(index["fallbackColumns"].items())[:15]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
