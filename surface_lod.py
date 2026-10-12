"""Offline prototype: coarse terrain surface from ACTUAL exported chunk geometry. Pure standard library.

rasterize(parts, x, z, width, depth, sample_color, accept_hit) -> field
    parts         iterable of mesh dicts {positions, uvs, colors, indices} (the exporter's chunk / transparent
                  part format: world-space positions, atlas UVs, sRGB vertex tints in [0,1], indexed triangles).
                  May be a generator; each part is fully validated before it is used, malformed parts raise
                  ValueError (nothing is silently treated as air).
    x, z          integer world origin of the field; width, depth integers 1..128 (block columns).
    sample_color  callback(uv_bounds) -> linear RGB of the texture in [0,1]. uv_bounds = (u0, v0, u1, v1),
                  the bounds of the winning triangle's three UVs: the caller averages the sprite region
                  (alpha-weighted, cached); this is intentionally a distance colour, not a point sample.
                  Called once per column that has a winning accepted hit, never otherwise.
    accept_hit    callback(world_x_center, world_y, world_z_center) -> bool; False rejects a hit (e.g. clouds
                  verified against the snapshot). May be called several times per column (every candidate
                  higher than the current best of that column).

    Only upward-facing triangles are used: normal (b-a) x (c-a) with a positive Y component relative to its
    length (real winding; degenerate, vertical and downward triangles are skipped). Each triangle is
    projected onto XZ and evaluated at the centres (i+0.5, j+0.5) of the columns it covers (barycentric,
    small epsilon so shared edges do not open gaps). Per column the highest accepted hit wins; on exact ties
    the first seen wins. The colour is linear_to_srgb(barycentric average of srgb_to_linear(vertex tint) *
    sample_color(uv_bounds)).

    field = {"x", "z", "width", "depth",
             "samples": [None | {"height": float, "color": [r, g, b]}]}   z-major, index (wz-z)*width + (wx-x)
    None means no accepted upward geometry in that column; it is never turned into ground at height 0.

build_mesh(field, step=4, *, max_edge_rise=None) -> {"positions", "uvs", "colors", "indices", "stats"}
    step in {1, 2, 4, 8, 16}; field x, z, width and depth must be divisible by step (spatially aligned grid).
    Grid vertex (gx, gz) at every step: height and colour are the average of the present fine columns around
    it ((gx-1|gx, gz-1|gz) inside the field; colours averaged in linear space, stored as sRGB in [0,1]).
    A coarse cell emits two upward triangles only if ALL its fine columns are present; unknown areas are
    never bridged. Neighbouring cells share vertex indices. uvs are 0 (untextured material), colors sRGB,
    no normals. stats = {step, finePresent, fineMissing, cells, cellsEmitted, vertices, triangles,
    maxFineEdgeRise, maxCoarseCellRise, coarseEligible}.

    Publication guard (fail-closed): a single height field cannot represent floating or layered surfaces;
    it joins e.g. a floating island to the ground below with a huge false wall. The mesh is always returned
    unchanged as a DIAGNOSTIC; nothing is removed, cut or re-sloped. Eligibility is reported separately:
        maxFineEdgeRise    largest |height difference| between x- or z-adjacent PRESENT fine columns
                           (catches spikes that fall between coarse grid vertices)
        maxCoarseCellRise  largest max-min of the four corner heights of an emitted coarse cell
                           (catches stretched coarse faces)
        coarseEligible     True only if max_edge_rise is a number, no fine sample is missing and both
                           rises are <= max_edge_rise. max_edge_rise=None (default) means diagnostic only
                           and always yields False. max_edge_rise must be a finite number >= 0.
    The guard is deliberately conservative: it may reject real steep cliffs, and passing it does NOT prove that
    a multi-layer world is representable by this mesh. Callers must never replace detailed geometry with a mesh
    whose coarseEligible is False. Floating or overlapping surfaces need separate topology/layers or the
    detailed fallback; this guard only prevents accidental publication of this known limitation.

Limitations: surface only (no caves, overhangs or undersides); cloud and other rejections come solely from
accept_hit; no alpha test, so cut-out textures (leaves, fences) count as solid; vertices on the field's outer
border only see columns inside this field, so real cross-tile seams need a shared padded source field; no
skirts, no viewer integration, not a production seam solution.
"""
from __future__ import annotations

import math

MAX_SPAN = 128
STEPS = (1, 2, 4, 8, 16)
_EDGE_EPS = 1e-9      # barycentric tolerance: shared edges hit both triangles instead of neither
_UP_EPS = 1e-6        # |ny| / |n| below this is treated as vertical


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_rect(x, z, width, depth) -> None:
    if not (_is_int(x) and _is_int(z)):
        raise ValueError("x and z must be integers")
    for name, span in (("width", width), ("depth", depth)):
        if not _is_int(span) or not 1 <= span <= MAX_SPAN:
            raise ValueError(f"{name} must be an integer 1..{MAX_SPAN}")


def _validate_part(part, index: int):
    where = f"parts[{index}]"
    if not isinstance(part, dict):
        raise ValueError(f"{where} is not a dict")
    arrays = {}
    for key in ("positions", "uvs", "colors", "indices"):
        value = part.get(key)
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{where}.{key} must be a list")
        arrays[key] = value
    positions, uvs, colors, indices = arrays["positions"], arrays["uvs"], arrays["colors"], arrays["indices"]
    if len(positions) % 3:
        raise ValueError(f"{where}.positions length is not a multiple of 3")
    n = len(positions) // 3
    if len(uvs) != 2 * n or len(colors) != 3 * n or len(indices) % 3:
        raise ValueError(f"{where}: array lengths do not match {n} vertices")
    if not all(_is_num(v) for v in positions) or not all(_is_num(v) for v in uvs):
        raise ValueError(f"{where}: positions/uvs must be finite numbers")
    if not all(_is_num(v) and 0.0 <= v <= 1.0 for v in colors):
        raise ValueError(f"{where}: colors must be numbers in [0,1]")
    if not all(_is_int(i) and 0 <= i < n for i in indices):
        raise ValueError(f"{where}: index outside 0..{n - 1}")
    return positions, uvs, colors, indices


def _edge(ax, az, bx, bz, px, pz) -> float:
    return (bx - ax) * (pz - az) - (bz - az) * (px - ax)


def rasterize(parts, x, z, width, depth, sample_color, accept_hit) -> dict:
    """Highest accepted upward-facing surface per block column; see the module docstring."""
    _check_rect(x, z, width, depth)
    if not callable(sample_color) or not callable(accept_hit):
        raise ValueError("sample_color and accept_hit must be callable")
    best: list = [None] * (width * depth)  # (height, uv_bounds, linear tint)
    for part_index, part in enumerate(parts):
        positions, uvs, colors, indices = _validate_part(part, part_index)
        for t in range(0, len(indices), 3):
            ia, ib, ic = indices[t], indices[t + 1], indices[t + 2]
            ax, ay, az = positions[3 * ia:3 * ia + 3]
            bx, by, bz = positions[3 * ib:3 * ib + 3]
            cx, cy, cz = positions[3 * ic:3 * ic + 3]
            ux, uy, uz = bx - ax, by - ay, bz - az
            vx, vy, vz = cx - ax, cy - ay, cz - az
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if length < 1e-12 or ny <= _UP_EPS * length:
                continue  # degenerate, vertical or downward
            area = _edge(ax, az, bx, bz, cx, cz)  # == -ny, negative for upward faces
            i0 = max(x, math.ceil(min(ax, bx, cx) - 0.5 - _EDGE_EPS))
            i1 = min(x + width - 1, math.floor(max(ax, bx, cx) - 0.5 + _EDGE_EPS))
            j0 = max(z, math.ceil(min(az, bz, cz) - 0.5 - _EDGE_EPS))
            j1 = min(z + depth - 1, math.floor(max(az, bz, cz) - 0.5 + _EDGE_EPS))
            if i0 > i1 or j0 > j1:
                continue
            triangle = None
            for j in range(j0, j1 + 1):
                pz = j + 0.5
                for i in range(i0, i1 + 1):
                    px = i + 0.5
                    wa = _edge(bx, bz, cx, cz, px, pz) / area
                    wb = _edge(cx, cz, ax, az, px, pz) / area
                    wc = _edge(ax, az, bx, bz, px, pz) / area
                    if wa < -_EDGE_EPS or wb < -_EDGE_EPS or wc < -_EDGE_EPS:
                        continue
                    y = ay + wb * (by - ay) + wc * (cy - ay)  # exact for level faces
                    slot = (j - z) * width + (i - x)
                    if best[slot] is not None and y <= best[slot][0]:
                        continue  # not higher; ties keep the first hit
                    accepted = accept_hit(px, y, pz)
                    if not isinstance(accepted, bool):
                        raise ValueError("accept_hit must return a bool")
                    if not accepted:
                        continue
                    if triangle is None:
                        us = (uvs[2 * ia], uvs[2 * ib], uvs[2 * ic])
                        vs = (uvs[2 * ia + 1], uvs[2 * ib + 1], uvs[2 * ic + 1])
                        tints = [[srgb_to_linear(colors[3 * k + ch]) for ch in range(3)] for k in (ia, ib, ic)]
                        triangle = ((min(us), min(vs), max(us), max(vs)), tints)
                    bounds, tints = triangle
                    tint = [tints[0][ch] + wb * (tints[1][ch] - tints[0][ch]) + wc * (tints[2][ch] - tints[0][ch])
                            for ch in range(3)]
                    best[slot] = (y, bounds, tint)
    samples = []
    for entry in best:
        if entry is None:
            samples.append(None)
            continue
        y, bounds, tint = entry
        texture = sample_color(bounds)
        if (not isinstance(texture, (list, tuple)) or len(texture) != 3
                or not all(_is_num(v) and 0.0 <= v <= 1.0 for v in texture)):
            raise ValueError(f"sample_color must return three numbers in [0,1], got {texture!r}")
        samples.append({"height": float(y),
                        "color": [linear_to_srgb(max(0.0, tint[ch]) * texture[ch]) for ch in range(3)]})
    return {"x": x, "z": z, "width": width, "depth": depth, "samples": samples}


def _validate_field(field) -> list:
    if not isinstance(field, dict):
        raise ValueError("field must be a dict")
    _check_rect(field.get("x"), field.get("z"), field.get("width"), field.get("depth"))
    samples = field.get("samples")
    if not isinstance(samples, (list, tuple)) or len(samples) != field["width"] * field["depth"]:
        raise ValueError("samples must be a list of width*depth entries")
    for k, sample in enumerate(samples):
        if sample is None:
            continue
        color = sample.get("color") if isinstance(sample, dict) else None
        if (not isinstance(sample, dict) or not _is_num(sample.get("height"))
                or not isinstance(color, (list, tuple)) or len(color) != 3
                or not all(_is_num(v) and 0.0 <= v <= 1.0 for v in color)):
            raise ValueError(f"samples[{k}] must be None or {{height, color[3] in [0,1]}}")
    return samples


def build_mesh(field: dict, step: int = 4, *, max_edge_rise=None) -> dict:
    """Coarse indexed surface mesh plus fail-closed eligibility stats; see the module docstring."""
    samples = _validate_field(field)
    if step not in STEPS or not _is_int(step):
        raise ValueError(f"step must be one of {STEPS}")
    if max_edge_rise is not None and not (_is_num(max_edge_rise) and max_edge_rise >= 0):
        raise ValueError("max_edge_rise must be None or a finite number >= 0")
    x, z, width, depth = field["x"], field["z"], field["width"], field["depth"]
    if any(v % step for v in (x, z, width, depth)):
        raise ValueError(f"field origin and size must be divisible by step {step}")

    def fine(i: int, j: int):
        return samples[j * width + i] if 0 <= i < width and 0 <= j < depth else None

    vertex_index: dict = {}
    positions, colors = [], []

    def vertex(gi: int, gj: int) -> int:
        key = (gi, gj)
        if key not in vertex_index:
            around = [s for s in (fine(gi - 1, gj - 1), fine(gi, gj - 1), fine(gi - 1, gj), fine(gi, gj)) if s]
            height = sum(s["height"] for s in around) / len(around)
            color = [linear_to_srgb(sum(srgb_to_linear(s["color"][ch]) for s in around) / len(around))
                     for ch in range(3)]
            vertex_index[key] = len(positions) // 3
            positions.extend([float(x + gi), height, float(z + gj)])
            colors.extend(color)
        return vertex_index[key]

    indices = []
    cells_x, cells_z = width // step, depth // step
    emitted = 0
    max_cell_rise = 0.0
    for cj in range(cells_z):
        for ci in range(cells_x):
            i0, j0 = ci * step, cj * step
            if any(fine(i, j) is None for j in range(j0, j0 + step) for i in range(i0, i0 + step)):
                continue
            v00, v10 = vertex(i0, j0), vertex(i0 + step, j0)
            v01, v11 = vertex(i0, j0 + step), vertex(i0 + step, j0 + step)
            indices.extend([v00, v01, v10, v10, v01, v11])  # both triangles face +Y
            corners = [positions[3 * v + 1] for v in (v00, v10, v01, v11)]
            max_cell_rise = max(max_cell_rise, max(corners) - min(corners))
            emitted += 1
    max_fine_rise = 0.0
    for j in range(depth):
        for i in range(width):
            here = fine(i, j)
            if here is None:
                continue
            for neighbour in (fine(i + 1, j), fine(i, j + 1)):
                if neighbour is not None:
                    max_fine_rise = max(max_fine_rise, abs(neighbour["height"] - here["height"]))
    present = sum(1 for s in samples if s is not None)
    eligible = (max_edge_rise is not None and present == len(samples)
                and max_fine_rise <= max_edge_rise and max_cell_rise <= max_edge_rise)
    return {
        "positions": positions,
        "uvs": [0.0] * (2 * (len(positions) // 3)),
        "colors": colors,
        "indices": indices,
        "stats": {"step": step, "finePresent": present, "fineMissing": len(samples) - present,
                  "cells": cells_x * cells_z, "cellsEmitted": emitted,
                  "vertices": len(positions) // 3, "triangles": len(indices) // 3,
                  "maxFineEdgeRise": float(max_fine_rise), "maxCoarseCellRise": float(max_cell_rise),
                  "coarseEligible": eligible},
    }
