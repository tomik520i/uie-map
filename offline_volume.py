"""Voxel volume of one UIE2 batch (capture_batch) for the offline renderer. Stdlib only, no Minecraft.

The batch already carries everything the offline renderer may read about the world: the input window (core +
halo), the world's own registry (numeric id -> registry name), block id + metadata for y 0..255 and the biome
of every column, plus which chunks are present. Nothing else (no light, no tile-entity NBT).

Volume(batch) indexes columns like the batch: x from area.x, z from area.z.
    vol.block(x, y, z) -> (id, meta)    world coordinates; outside the window or absent chunk -> (-1, 0)
"""
from __future__ import annotations

from array import array

import capture_batch as cb

HEIGHT = 256
COLUMN = 1 + HEIGHT * 3


class Volume:
    def __init__(self, batch: cb.BatchSnapshot):
        a = batch.area
        self.x0, self.z0, self.width, self.depth = a.x, a.z, a.width, a.depth
        self.core = batch.core
        self.registry = dict(batch.registry)
        self.present = frozenset(batch.present_chunks)
        n = self.width * self.depth
        self.biomes = bytearray(n)
        self.metas = bytearray(n * HEIGHT)
        ids = bytearray(n * HEIGHT * 2)
        body = batch.body
        for col in range(n):
            off = col * COLUMN
            self.biomes[col] = body[off]
            data = body[off + 1:off + COLUMN]
            base = col * HEIGHT
            ids[2 * base:2 * base + 2 * HEIGHT:2] = data[0::3]
            ids[2 * base + 1:2 * base + 2 * HEIGHT:2] = data[1::3]
            self.metas[base:base + HEIGHT] = data[2::3]
        self.ids = array("H")
        self.ids.frombytes(bytes(ids))
        if self.ids.itemsize != 2:
            raise RuntimeError("array('H') must be 16-bit")
        import sys
        if sys.byteorder == "little":
            self.ids.byteswap()   # the batch stores big-endian ids
        self._present_cols = bytearray(n)
        for col in range(n):
            x, z = self.x0 + col % self.width, self.z0 + col // self.width
            self._present_cols[col] = 1 if ((x >> 4), (z >> 4)) in self.present else 0

    def column_index(self, x: int, z: int) -> int:
        """Column number or -1 outside the window / in an absent chunk."""
        lx, lz = x - self.x0, z - self.z0
        if not (0 <= lx < self.width and 0 <= lz < self.depth):
            return -1
        col = lz * self.width + lx
        return col if self._present_cols[col] else -1

    def block(self, x: int, y: int, z: int) -> tuple[int, int]:
        if not 0 <= y < HEIGHT:
            return (0, 0)
        col = self.column_index(x, z)
        if col < 0:
            return (-1, 0)
        i = col * HEIGHT + y
        return self.ids[i], self.metas[i]

    def biome(self, x: int, z: int) -> int:
        col = self.column_index(x, z)
        return self.biomes[col] if col >= 0 else 0

    def core_chunks(self) -> list[tuple[int, int]]:
        c = self.core
        return sorted(((cx, cz) for cz in range(c.z >> 4, (c.z + c.depth) >> 4)
                       for cx in range(c.x >> 4, (c.x + c.width) >> 4) if (cx, cz) in self.present),
                      key=lambda k: (k[1], k[0]))


def load(path) -> Volume:
    from pathlib import Path
    return Volume(cb.parse_batch(Path(path).read_bytes()))
