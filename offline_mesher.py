"""Offline block mesher: UIE2 batch + model catalog -> per-chunk meshes in today's web schema. No Minecraft.

No Java, Forge, LWJGL or OpenGL: pure Python over the batch volume (offline_volume) and the explicit model catalog
(offline_catalog). Output per core chunk: {positions, uvs, colors, indices, transparentParts:[...]} in world
coordinates, exactly the schema the viewer and publish_generation already accept.

Rules
- Visible faces only: a face is skipped when the neighbour is a solid full cube (catalog `occludes`), or when the
  neighbour is the same see-through block (leaf/glass/liquid joins). Cutout blocks (leaves, glass, plants) never
  hide other blocks.
- Surface mode (default, caves=False): faces towards enclosed air are dropped. Air is "outside" when it is above
  the column's highest solid block or connected to such air through non-solid cells inside the batch window.
  Cave interiors are therefore not stored; caves=True keeps every visible face (selected areas / cut view).
- Lighting is baked into vertex colours like the client capture: Minecraft face shade (top 1.0, bottom 0.5,
  N/S 0.8, E/W 0.6), per-vertex ambient occlusion from solid neighbours, a cover factor for faces whose air
  neighbour is below the column's solid top, and biome tint (vanilla grass/foliage colour maps) where the
  catalog says the block is tinted.
- Unknown blocks: catalog fallback = magenta-tinted cube, counted by name/meta/coordinates (never dropped).
"""
from __future__ import annotations

import math
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

import png_io
from offline_catalog import Catalog
from offline_volume import HEIGHT, Volume

# (dx, dy, dz) per Minecraft side: 0 down, 1 up, 2 north(-z), 3 south(+z), 4 west(-x), 5 east(+x)
DIRS = ((0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1), (-1, 0, 0), (1, 0, 0))
SHADE = (0.5, 1.0, 0.8, 0.8, 0.6, 0.6)
AO_LEVELS = (0.55, 0.7, 0.85, 1.0)
COVER = 0.62          # faces whose air neighbour lies under the solid top of its column (overhangs, cave mouths)
# Quad corners per side as (x, y, z) offsets of a unit cell, ordered BL, BR, TR, TL as seen from outside (CCW).
CORNERS = (
    ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)),   # down: right=+x, up=+z
    ((0, 1, 1), (1, 1, 1), (1, 1, 0), (0, 1, 0)),   # up: right=+x, up=-z
    ((1, 0, 0), (0, 0, 0), (0, 1, 0), (1, 1, 0)),   # north: right=-x
    ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)),   # south: right=+x
    ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)),   # west: right=+z
    ((1, 0, 1), (1, 0, 0), (1, 1, 0), (1, 1, 1)),   # east: right=-z
)
# Vanilla 1.7.10 biome temperature/rainfall (ids 0-39, mutated variants +128 share the base values).
BIOME_CLIMATE = {
    0: (0.5, 0.5), 1: (0.8, 0.4), 2: (2.0, 0.0), 3: (0.2, 0.3), 4: (0.7, 0.8), 5: (0.25, 0.8), 6: (0.8, 0.9),
    7: (0.5, 0.5), 8: (2.0, 0.0), 9: (0.5, 0.5), 10: (0.0, 0.5), 11: (0.0, 0.5), 12: (0.0, 0.5), 13: (0.0, 0.5),
    14: (0.9, 1.0), 15: (0.9, 1.0), 16: (0.8, 0.4), 17: (2.0, 0.0), 18: (0.7, 0.8), 19: (0.25, 0.8), 20: (0.2, 0.3),
    21: (0.95, 0.9), 22: (0.95, 0.9), 23: (0.95, 0.8), 24: (0.5, 0.5), 25: (0.2, 0.3), 26: (0.05, 0.3),
    27: (0.6, 0.6), 28: (0.6, 0.6), 29: (0.7, 0.8), 30: (-0.5, 0.4), 31: (-0.5, 0.4), 32: (0.3, 0.8), 33: (0.3, 0.8),
    34: (0.2, 0.3), 35: (1.2, 0.0), 36: (1.0, 0.0), 37: (2.0, 0.0), 38: (2.0, 0.0), 39: (2.0, 0.0)}


class Colormap:
    def __init__(self, grass_png: Path, foliage_png: Path):
        self.maps = {}
        for kind, path in (("grass", grass_png), ("foliage", foliage_png)):
            w, h, c, px = png_io.decode(Path(path).read_bytes())
            self.maps[kind] = (w, c, px)
        self.cache = {}
        self.unknown_biomes = Counter()

    def tint(self, kind: str, biome: int, fallback: tuple) -> tuple:
        climate = BIOME_CLIMATE.get(biome if biome < 128 else biome - 128)
        if climate is None:
            # A modded biome has no climate entry: use THIS block's exported
            # colour. Caching by biome alone would reuse the first leaf/grass
            # species seen by a worker and make output depend on job order.
            self.unknown_biomes[biome] += 1
            return fallback
        key = (kind, biome)
        hit = self.cache.get(key)
        if hit is None:
            temp = min(1.0, max(0.0, climate[0]))
            rain = min(1.0, max(0.0, climate[1])) * temp
            w, c, px = self.maps[kind]
            i = int((1.0 - temp) * 255.0)
            j = int((1.0 - rain) * 255.0)
            o = (j * w + i) * c
            hit = (px[o] / 255.0, px[o + 1] / 255.0, px[o + 2] / 255.0)
            self.cache[key] = hit
        return hit


class Part:
    __slots__ = ("positions", "uvs", "colors", "indices")

    def __init__(self):
        self.positions, self.uvs, self.colors, self.indices = [], [], [], []

    def quad(self, corners, uv, colors):
        base = len(self.positions) // 3
        for (x, y, z) in corners:
            self.positions += (x, y, z)
        u0, v0, u1, v1 = uv
        self.uvs += (u0, v1, u1, v1, u1, v0, u0, v0)
        for c in colors:
            self.colors += c
        self.indices += (base, base + 1, base + 2, base, base + 2, base + 3)

    def doc(self) -> dict:
        r = lambda v: float(f"{v:.4f}")
        return {"positions": [r(v) for v in self.positions], "uvs": [float(f"{v:.7g}") for v in self.uvs],
                "colors": [float(f"{v:.3f}") for v in self.colors], "indices": self.indices}


class Mesher:
    def __init__(self, catalog: Catalog, colormap: Colormap, caves: bool = False, leaves: str = "fancy"):
        self.catalog, self.colormap, self.caves = catalog, colormap, caves
        if leaves not in ("fancy", "fast"):
            raise ValueError("leaves must be 'fancy' or 'fast'")
        self.leaves = leaves
        sprites = catalog.sprites
        self.grass_overlay = tuple(sprites["grass_side_overlay"]["uv"]) if "grass_side_overlay" in sprites else None

    # ------------------------------------------------------------------ volume preparation
    def _prepare(self, vol: Volume):
        """Per batch: model per registry id/meta (lazy), solid mask, column solid tops, outside-air mask."""
        models = {}

        def model(block_id, meta):
            key = (block_id, meta)
            m = models.get(key)
            if m is None:
                m = models[key] = self.catalog.model(vol.registry.get(block_id, f"#{block_id}"), meta)
            return m
        n = vol.width * vol.depth
        solid = bytearray(n * HEIGHT)
        tops = [-1] * n
        ids, metas = vol.ids, vol.metas
        occl_cache = {}
        for col in range(n):
            if not vol._present_cols[col]:
                continue
            base = col * HEIGHT
            top = -1
            for y in range(HEIGHT):
                block_id = ids[base + y]
                if not block_id:
                    continue
                key = (block_id, metas[base + y])
                occ = occl_cache.get(key)
                if occ is None:
                    occ = occl_cache[key] = model(*key).occludes
                if occ:
                    solid[base + y] = 1
                    top = y
            tops[col] = top
        outside = None if self.caves else self._outside(vol, solid, tops)
        return model, solid, tops, outside

    def _outside(self, vol: Volume, solid: bytearray, tops: list) -> bytearray:
        """Air cells at or below their column's solid top that connect to open sky through non-solid cells."""
        w, d = vol.width, vol.depth
        seen = bytearray(w * d * HEIGHT)
        queue = deque()
        for col in range(w * d):
            if tops[col] < 0:
                continue
            lx, lz = col % w, col // w
            for nx, nz in ((lx - 1, lz), (lx + 1, lz), (lx, lz - 1), (lx, lz + 1)):
                if not (0 <= nx < w and 0 <= nz < d):
                    continue
                ncol = nz * w + nx
                for y in range(max(tops[ncol] + 1, 0), tops[col] + 1):
                    i = col * HEIGHT + y
                    if not solid[i] and not seen[i]:
                        seen[i] = 1
                        queue.append(i)
        while queue:
            i = queue.popleft()
            col, y = divmod(i, HEIGHT)
            lx, lz = col % w, col // w
            for j, ok in ((i - 1, y > 0), (i + 1, y < HEIGHT - 1), (i - HEIGHT, lx > 0), (i + HEIGHT, lx < w - 1),
                          (i - w * HEIGHT, lz > 0), (i + w * HEIGHT, lz < d - 1)):
                if ok and not solid[j] and not seen[j]:
                    jcol, jy = divmod(j, HEIGHT)
                    if jy > tops[jcol]:
                        continue   # open sky already; not needed in the mask
                    seen[j] = 1
                    queue.append(j)
        return seen

    # ------------------------------------------------------------------ meshing
    def mesh_batch(self, vol: Volume) -> dict:
        started = time.perf_counter()
        model, solid, tops, outside = self._prepare(vol)
        self._model, self._solid = model, solid   # neighbour lookups for connected shapes (fences, walls, panes)
        stats = {"renderedBlocks": 0, "fallbackBlocks": Counter(), "assumedBlocks": Counter(),
                 "invisibleBlocks": Counter(), "fallbackExamples": {}, "faces": 0, "culledCaveFaces": 0}
        chunks = {}
        for cx, cz in vol.core_chunks():
            chunks[(cx, cz)] = self._mesh_chunk(vol, cx, cz, model, solid, tops, outside, stats)
        stats["seconds"] = round(time.perf_counter() - started, 3)
        return {"chunks": chunks, "stats": stats}

    def _mesh_chunk(self, vol, cx, cz, model, solid, tops, outside, stats) -> dict:
        opaque, translucent = Part(), Part()
        w = vol.width
        ids, metas = vol.ids, vol.metas
        for z in range(cz * 16, cz * 16 + 16):
            for x in range(cx * 16, cx * 16 + 16):
                col = vol.column_index(x, z)
                if col < 0:
                    continue
                base = col * HEIGHT
                biome = vol.biomes[col]
                for y in range(HEIGHT):
                    block_id = ids[base + y]
                    if not block_id:
                        continue
                    meta = metas[base + y]
                    m = model(block_id, meta)
                    if m.kind == "none":
                        stats["invisibleBlocks"][(vol.registry.get(block_id), meta)] += 1
                        continue
                    i = base + y
                    # A fully enclosed block draws nothing: skip the per-face work early.
                    if solid[i] and solid[i - 1 if y else i] and (y == HEIGHT - 1 or solid[i + 1]) \
                            and solid[i - HEIGHT] and solid[i + HEIGHT] and solid[i - w * HEIGHT] and solid[i + w * HEIGHT]:
                        continue
                    emitted = self._emit(vol, x, y, z, i, block_id, meta, m, biome, solid, tops, outside,
                                         opaque, translucent, stats)
                    if emitted:
                        stats["renderedBlocks"] += 1
                        if m.tier == "fallback":
                            key = (vol.registry.get(block_id), meta)
                            stats["fallbackBlocks"][key] += 1
                            stats["fallbackExamples"].setdefault(key, (x, y, z, m.reason))
                        elif m.tier == "assumed":
                            stats["assumedBlocks"][(vol.registry.get(block_id), meta)] += 1
        doc = opaque.doc()
        if translucent.indices:
            tp = translucent.doc()
            tp["transparent"] = True
            doc["transparentParts"] = [tp]
        return doc

    def _air_outside(self, vol, i, outside, tops, ncol, ny) -> bool:
        if outside is None:
            return True
        return ny > tops[ncol] or bool(outside[i])

    def _tint(self, m, biome, side):
        if m.tint == "none":
            return (1.0, 1.0, 1.0)
        if m.tint == "const":
            return m.color
        if m.kind == "cube" and m.tint == "grass" and side != 1:
            return (1.0, 1.0, 1.0)   # grass blocks: only the top is biome-coloured
        return self.colormap.tint(m.tint, biome, m.color)

    def _emit(self, vol, x, y, z, i, block_id, meta, m, biome, solid, tops, outside, opaque, translucent, stats):
        kind = m.kind
        part = translucent if m.opacity == "translucent" else opaque
        if kind in ("cube", "fallback", "slab", "layer", "stairs", "box", "fence", "wall", "pane"):
            return self._emit_boxes(vol, x, y, z, i, block_id, meta, m, biome, solid, tops, outside, part, stats)
        if kind == "lily":
            return self._emit_flat(x, y + 0.015, z, m, biome, part)
        if kind == "ladder":
            return self._emit_ladder(x, y, z, m, biome, part)
        if kind == "liquid":
            return self._emit_liquid(vol, x, y, z, i, block_id, m, biome, solid, tops, outside, part, stats)
        if kind == "cross":
            return self._emit_cross(x, y, z, m, biome, part, vol.registry.get(block_id, ""))
        if kind == "vine":
            return self._emit_vine(x, y, z, m, biome, part)
        return False

    def _connects(self, vol, x, y, z, kind) -> bool:
        block_id, meta = vol.block(x, y, z)
        if block_id <= 0:
            return False
        col = vol.column_index(x, z)
        if col >= 0 and self._solid[col * HEIGHT + y]:
            return True
        other = self._model(block_id, meta).kind
        return other == kind or (kind == "fence" and other == "fence")

    def _connected_boxes(self, m, vol, x, y, z):
        kind = m.kind
        sides = {(0, -1): self._connects(vol, x, y, z - 1, kind), (0, 1): self._connects(vol, x, y, z + 1, kind),
                 (-1, 0): self._connects(vol, x - 1, y, z, kind), (1, 0): self._connects(vol, x + 1, y, z, kind)}
        if kind == "fence":
            post, w, rails = (6 / 16, 10 / 16), (7 / 16, 9 / 16), [(6 / 16, 9 / 16), (12 / 16, 15 / 16)]
            boxes = [((post[0], 0, post[0]), (post[1], 1, post[1]))]
        elif kind == "wall":
            post, w, rails = (4 / 16, 12 / 16), (5 / 16, 11 / 16), [(0, 13 / 16)]
            boxes = [((post[0], 0, post[0]), (post[1], 1, post[1]))]
        else:   # pane: a thin centre column; isolated panes form a plus like the client
            post, w, rails = (7 / 16, 9 / 16), (7 / 16, 9 / 16), [(0, 1)]
            boxes = [((post[0], 0, post[0]), (post[1], 1, post[1]))]
            if not any(sides.values()):
                sides = {k: True for k in sides}
        for (dx, dz), on in sides.items():
            if not on:
                continue
            for (y0, y1) in rails:
                if dx:
                    x0, x1 = (0, post[0]) if dx < 0 else (post[1], 1)
                    boxes.append(((x0, y0, w[0]), (x1, y1, w[1])))
                else:
                    z0, z1 = (0, post[0]) if dz < 0 else (post[1], 1)
                    boxes.append(((w[0], y0, z0), (w[1], y1, z1)))
        return boxes

    def _boxes(self, m, vol=None, x=0, y=0, z=0):
        if m.kind == "box":
            return m.shape["boxes"]
        if m.kind in ("fence", "wall", "pane"):
            return self._connected_boxes(m, vol, x, y, z)
        if m.kind == "slab":
            return [((0, 0.5, 0), (1, 1, 1))] if m.shape["top"] else [((0, 0, 0), (1, 0.5, 1))]
        if m.kind == "layer":
            return [((0, 0, 0), (1, m.shape["height"], 1))]
        if m.kind == "stairs":
            return self._stairs_boxes(m, vol, x, y, z)
        return [((0, 0, 0), (1, 1, 1))]

    def _stair_at(self, vol, x, y, z):
        """(facing, upside) of a stairs block in the cell, else None."""
        if vol is None:
            return None
        block_id, meta = vol.block(x, y, z)
        if block_id <= 0:
            return None
        other = self._model(block_id, meta)
        return (other.shape["facing"], other.shape["upside"]) if other.kind == "stairs" else None

    def _stairs_boxes(self, m, vol, x, y, z):
        """1.7.10 BlockStairs shapes: half slab + back quarter, shortened at outer corners (func_150145_e) and with
        an extra front quarter at inner corners (func_150144_f). meta&3: 0 east, 1 west, 2 south, 3 north back."""
        d, up = m.shape["facing"], m.shape["upside"]
        low, high = ((0.5, 1), (0, 0.5)) if up else ((0, 0.5), (0.5, 1))
        boxes = [((0, low[0], 0), (1, low[1], 1))]
        same = lambda dx, dz: self._stair_at(vol, x + dx, y, z + dz) == (d, up)

        def turned(dx, dz):
            s = self._stair_at(vol, x + dx, y, z + dz)
            return s[0] if s is not None and s[1] == up else None

        # Back quarter; an outer corner keeps only the half towards the turned stair behind it.
        bx, bz = {0: ([0.5, 1], [0, 1]), 1: ([0, 0.5], [0, 1]), 2: ([0, 1], [0.5, 1]), 3: ([0, 1], [0, 0.5])}[d]
        behind = {0: (1, 0), 1: (-1, 0), 2: (0, 1), 3: (0, -1)}[d]
        front = (-behind[0], -behind[1])
        n = turned(*behind)
        if d in (0, 1):
            if n == 3 and not same(0, 1):
                bz[1] = 0.5
            elif n == 2 and not same(0, -1):
                bz[0] = 0.5
        else:
            if n == 1 and not same(1, 0):
                bx[1] = 0.5
            elif n == 0 and not same(-1, 0):
                bx[0] = 0.5
        boxes.append(((bx[0], high[0], bz[0]), (bx[1], high[1], bz[1])))
        # Inner corner: an extra quarter in the front half, on the side of the turned stair in front.
        n = turned(*front)
        extra = None
        if d in (0, 1):
            fx = [0, 0.5] if d == 0 else [0.5, 1]
            if n == 3 and not same(0, -1):
                extra = (fx, [0, 0.5])
            elif n == 2 and not same(0, 1):
                extra = (fx, [0.5, 1])
        else:
            fz = [0, 0.5] if d == 2 else [0.5, 1]
            if n == 1 and not same(-1, 0):
                extra = ([0, 0.5], fz)
            elif n == 0 and not same(1, 0):
                extra = ([0.5, 1], fz)
        if extra:
            (ex, ez) = extra
            boxes.append(((ex[0], high[0], ez[0]), (ex[1], high[1], ez[1])))
        return boxes

    def _emit_boxes(self, vol, x, y, z, i, block_id, meta, m, biome, solid, tops, outside, part, stats):
        emitted = False
        boxes = self._boxes(m, vol, x, y, z)
        full = len(boxes) == 1 and boxes[0] == ((0, 0, 0), (1, 1, 1))
        for side, (dx, dy, dz) in enumerate(DIRS):
            nx, ny, nz = x + dx, y + dy, z + dz
            if 0 <= ny < HEIGHT:
                ncol = vol.column_index(nx, nz)
                if ncol >= 0:
                    j = ncol * HEIGHT + ny
                    if full and solid[j]:
                        continue
                    if full and m.opacity != "solid" and vol.ids[j] == block_id                             and (self.leaves == "fast" or m.tint != "foliage"):
                        continue   # glass/ice joins (and leaves in fast mode); fancy leaves keep inner faces
                    if not self._air_outside(vol, j, outside, tops, ncol, ny):
                        stats["culledCaveFaces"] += 1
                        continue
                    covered = ny <= tops[ncol]
                else:
                    covered = False
            else:
                if ny < 0:
                    continue   # world bottom is never seen from the map
                covered = False
            tint = self._tint(m, biome, side)
            base_shade = SHADE[side] * (COVER if covered else 1.0)
            uv = m.faces[side]
            for (x0, y0, z0), (x1, y1, z1) in boxes:
                if not full and not self._box_face_exposed(side, (x0, y0, z0), (x1, y1, z1)):
                    continue
                corners = self._box_corners(side, x, y, z, (x0, y0, z0), (x1, y1, z1))
                sub = self._sub_uv(uv, side, (x0, y0, z0), (x1, y1, z1))
                if full:
                    ao = self._ao(vol, solid, x, y, z, side)
                    colors = [(tint[0] * base_shade * a, tint[1] * base_shade * a, tint[2] * base_shade * a) for a in ao]
                else:
                    c = (tint[0] * base_shade, tint[1] * base_shade, tint[2] * base_shade)
                    colors = [c, c, c, c]
                part.quad(corners, sub, colors)
                stats["faces"] += 1
                emitted = True
                if full and side > 1 and m.tint == "grass" and self.grass_overlay is not None                         and m.faces[1] != m.faces[side]:
                    # Fancy grass: the side gets a second, biome-tinted overlay quad (client order: base, overlay).
                    g = self.colormap.tint("grass", biome, m.color)
                    overlay = [(g[0] * c[0], g[1] * c[1], g[2] * c[2]) for c in colors]
                    part.quad(corners, self.grass_overlay, overlay)
                    stats["faces"] += 1
        return emitted

    @staticmethod
    def _box_face_exposed(side, lo, hi) -> bool:
        return True   # partial shapes draw all their faces (inner faces are cheap and keep shapes closed)

    @staticmethod
    def _box_corners(side, x, y, z, lo, hi):
        out = []
        for (cx, cy, cz) in CORNERS[side]:
            out.append((x + (hi[0] if cx else lo[0]), y + (hi[1] if cy else lo[1]), z + (hi[2] if cz else lo[2])))
        return out

    @staticmethod
    def _sub_uv(uv, side, lo, hi):
        """Sub-rectangle of the sprite for a partial box face (texture stays world-aligned like the client)."""
        u0, v0, u1, v1 = uv
        du, dv = u1 - u0, v1 - v0
        if side in (0, 1):
            a0, a1, b0, b1 = lo[0], hi[0], lo[2], hi[2]
        elif side in (2, 3):
            a0, a1, b0, b1 = lo[0], hi[0], 1 - hi[1], 1 - lo[1]
        else:
            a0, a1, b0, b1 = lo[2], hi[2], 1 - hi[1], 1 - lo[1]
        if (a0, a1, b0, b1) == (0, 1, 0, 1):
            return uv
        return (u0 + du * a0, v0 + dv * b0, u0 + du * a1, v0 + dv * b1)

    def _ao(self, vol, solid, x, y, z, side):
        """Per-corner ambient occlusion from the 8 cells around the face's outer neighbour."""
        dx, dy, dz = DIRS[side]
        px, py, pz = x + dx, y + dy, z + dz
        # two in-plane axes of the face
        axes = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
        axes = [a for a in axes if a != (abs(dx), abs(dy), abs(dz))]
        (ax, ay, az), (bx, by, bz) = axes

        def s(ox, oy, oz):
            yy = py + oy
            if not 0 <= yy < HEIGHT:
                return 0
            col = vol.column_index(px + ox, pz + oz)
            return solid[col * HEIGHT + yy] if col >= 0 else 0
        out = []
        for (cx, cy, cz) in CORNERS[side]:
            # corner offset along each axis: -1 at the low edge, +1 at the high edge
            ca = (cx if ax else cy if ay else cz) * 2 - 1
            cb = (cx if bx else cy if by else cz) * 2 - 1
            s1 = s(ax * ca, ay * ca, az * ca)
            s2 = s(bx * cb, by * cb, bz * cb)
            c = s(ax * ca + bx * cb, ay * ca + by * cb, az * ca + bz * cb)
            level = 0 if (s1 and s2) else 3 - (s1 + s2 + c)
            out.append(AO_LEVELS[level])
        return out

    def _fluid(self, vol, block_id):
        """Still and flowing variants of one liquid share a key (minecraft:water/flowing_water, lava/flowing_lava)."""
        name = vol.registry.get(block_id, "")
        return name.replace("flowing_", "").replace(".flowing", "").replace(".still", "")

    def _liquid_corner(self, vol, fluid, cx, y, cz, solid):
        """Vanilla RenderBlocks.getLiquidHeight for the block corner at world (cx, cz): the 4 cells around it;
        the same liquid above any of them makes it full, sources (meta 0 or >= 8) weigh 10x, non-solid cells count
        as empty, solid cells are ignored."""
        total, count = 0.0, 0
        for bx, bz in ((cx, cz), (cx - 1, cz), (cx, cz - 1), (cx - 1, cz - 1)):
            above, _ = vol.block(bx, y + 1, bz)
            if above > 0 and self._fluid(vol, above) == fluid:
                return 1.0
            bid, bmeta = vol.block(bx, y, bz)
            if bid < 0:
                continue
            if bid > 0 and self._fluid(vol, bid) == fluid:
                pct = ((0 if bmeta >= 8 else bmeta) + 1) / 9.0
                if bmeta >= 8 or bmeta == 0:
                    total += pct * 10.0
                    count += 10
                total += pct
                count += 1
            else:
                col = vol.column_index(bx, bz)
                if not (col >= 0 and solid[col * HEIGHT + y]):
                    total += 1.0
                    count += 1
        return 1.0 - total / count if count else None

    def _emit_liquid(self, vol, x, y, z, i, block_id, m, biome, solid, tops, outside, part, stats):
        level = m.shape.get("level", 0)
        fluid = self._fluid(vol, block_id)
        above_id, _ = vol.block(x, y + 1, z)
        same_above = above_id > 0 and self._fluid(vol, above_id) == fluid
        # Vanilla heights: a source is 8/9 high, flowing level L is 1 - (L + 1) / 9; liquid below liquid is full.
        height = 1.0 if same_above else max(0.1, 1.0 - (level + 1) / 9.0)
        # Sloped surface: per-corner heights at the block corners (x|x+1, z|z+1).
        corner_h = {}
        for ox in (0, 1):
            for oz in (0, 1):
                h = None if same_above else self._liquid_corner(vol, fluid, x + ox, y, z + oz, solid)
                corner_h[(ox, oz)] = height if h is None else max(0.0, min(1.0, h))
        emitted = False
        for side, (dx, dy, dz) in enumerate(DIRS):
            nx, ny, nz = x + dx, y + dy, z + dz
            nid, _ = vol.block(nx, ny, nz)
            if nid < 0 or (nid > 0 and self._fluid(vol, nid) == fluid):
                continue
            ncol = vol.column_index(nx, nz)
            if ncol >= 0 and 0 <= ny < HEIGHT:
                j = ncol * HEIGHT + ny
                if solid[j]:
                    continue
                if not self._air_outside(vol, j, outside, tops, ncol, ny):
                    stats["culledCaveFaces"] += 1
                    continue
            elif ny < 0:
                continue
            uv = m.faces[side]
            corners = self._box_corners(side, x, y, z, (0, 0, 0), (1, 1, 1))
            # Top vertices take their corner height (the top face slopes; side faces follow its edge).
            corners = [(cx, y + corner_h[(round(cx - x), round(cz - z))], cz) if cy > y + 0.5 else (cx, cy, cz)
                       for (cx, cy, cz) in corners]
            side_h = max(cy for (_, cy, _) in corners) - y if side > 1 else height
            shade = SHADE[side]
            c = (shade, shade, shade)
            part.quad(corners, self._sub_uv(uv, side, (0, 0, 0), (1, side_h, 1)) if side > 1 else uv, [c] * 4)
            stats["faces"] += 1
            emitted = True
        return emitted

    @staticmethod
    def _tallgrass_offset(x, y, z):
        """Vanilla RenderBlocks.renderBlockCrossedSquares jitter for tall grass (Java int/long overflow)."""
        def i32(v):
            v &= 0xFFFFFFFF
            return v - (1 << 32) if v >= 1 << 31 else v

        def i64(v):
            v &= 0xFFFFFFFFFFFFFFFF
            return v - (1 << 64) if v >= 1 << 63 else v
        h = i64(i32(x * 3129871) ^ i64(z * 116129781) ^ y)
        h = i64(i64(h * h * 42317861) + i64(h * 11))
        ox = (((h >> 16) & 15) / 15.0 - 0.5) * 0.5
        oy = (((h >> 20) & 15) / 15.0 - 1.0) * 0.2
        oz = (((h >> 24) & 15) / 15.0 - 0.5) * 0.5
        return ox, oy, oz

    def _emit_cross(self, x, y, z, m, biome, part, name="") -> bool:
        tint = self._tint(m, biome, 2)
        c = (tint[0] * 0.9, tint[1] * 0.9, tint[2] * 0.9)
        uv = m.faces[2]
        ox = oy = oz = 0.0
        if name == "minecraft:tallgrass" or name.startswith("BiomesOPlenty:foliage"):
            ox, oy, oz = self._tallgrass_offset(x, y, z)
        a, b = 0.05, 0.95
        bx, by, bz = x + ox, y + oy, z + oz
        for (p0, p1) in (((a, a), (b, b)), ((a, b), (b, a))):
            q = [(bx + p0[0], by, bz + p0[1]), (bx + p1[0], by, bz + p1[1]), (bx + p1[0], by + 1, bz + p1[1]),
                 (bx + p0[0], by + 1, bz + p0[1])]
            part.quad(q, uv, [c] * 4)
            part.quad([q[1], q[0], q[3], q[2]], uv, [c] * 4)   # back side (FrontSide culling in the viewer)
        return True

    def _emit_flat(self, x, y, z, m, biome, part) -> bool:
        tint = self._tint(m, biome, 1)
        q = [(x, y, z + 1), (x + 1, y, z + 1), (x + 1, y, z), (x, y, z)]
        part.quad(q, m.faces[1], [tint] * 4)
        part.quad([q[1], q[0], q[3], q[2]], m.faces[1], [tint] * 4)
        return True

    def _emit_ladder(self, x, y, z, m, biome, part) -> bool:
        e = 1 / 16
        facing = m.shape.get("facing", 2)
        planes = {2: [(x + 1, y, z + 1 - e), (x, y, z + 1 - e), (x, y + 1, z + 1 - e), (x + 1, y + 1, z + 1 - e)],
                  3: [(x, y, z + e), (x + 1, y, z + e), (x + 1, y + 1, z + e), (x, y + 1, z + e)],
                  4: [(x + 1 - e, y, z), (x + 1 - e, y, z + 1), (x + 1 - e, y + 1, z + 1), (x + 1 - e, y + 1, z)],
                  5: [(x + e, y, z + 1), (x + e, y, z), (x + e, y + 1, z), (x + e, y + 1, z + 1)]}
        q = planes.get(facing, planes[2])
        c = (0.8, 0.8, 0.8)
        part.quad(q, m.faces[2], [c] * 4)
        part.quad([q[1], q[0], q[3], q[2]], m.faces[2], [c] * 4)
        return True

    def _emit_vine(self, x, y, z, m, biome, part) -> bool:
        sides = m.shape.get("sides", 0)
        tint = self._tint(m, biome, 2)
        c = (tint[0] * 0.8, tint[1] * 0.8, tint[2] * 0.8)
        uv = m.faces[2]
        e = 0.05
        planes = []
        if sides & 1:
            planes.append([(x, y, z + 1 - e), (x + 1, y, z + 1 - e), (x + 1, y + 1, z + 1 - e), (x, y + 1, z + 1 - e)])
        if sides & 2:
            planes.append([(x + e, y, z), (x + e, y, z + 1), (x + e, y + 1, z + 1), (x + e, y + 1, z)])
        if sides & 4:
            planes.append([(x + 1, y, z + e), (x, y, z + e), (x, y + 1, z + e), (x + 1, y + 1, z + e)])
        if sides & 8:
            planes.append([(x + 1 - e, y, z + 1), (x + 1 - e, y, z), (x + 1 - e, y + 1, z), (x + 1 - e, y + 1, z + 1)])
        if not planes:   # meta 0: hanging under a block
            planes.append([(x, y + 1 - e, z), (x + 1, y + 1 - e, z), (x + 1, y + 1 - e, z + 1), (x, y + 1 - e, z + 1)])
        for q in planes:
            part.quad(q, uv, [c] * 4)
            part.quad([q[1], q[0], q[3], q[2]], uv, [c] * 4)
        return True
