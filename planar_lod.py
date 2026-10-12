"""Planar LOD experiment: exact union of recognized axis-aligned unit faces, merged per plane and colour.

simplify_axis_aligned(mesh, sample_color, *, is_opaque=None, color_steps=8)
    -> {"coarse": mesh, "fallback": mesh, "stats": {...}}

Unlike surface_lod (a heightfield, which invents walls under floating islands and stays disabled), nothing
here builds a surface envelope. Only consecutive triangle pairs that are confidently one opaque unit square
are replaced:
    - both triangles have three distinct vertices with integer coordinates;
    - together they have exactly four corners forming a 1x1 square on an integer axis-aligned plane;
    - their shared edge is the square's diagonal (so they cover it exactly once, without overlap);
    - both have the same winding (one of the six axis orientations);
    - their UV bounds lie in [0,1];
    - is_opaque(u0, v0, u1, v1) returns True. The main part is alpha-tested, so cut-out textures (leaves,
      grates) are not opaque; without a predicate (None, the default) nothing is merged;
    - no other recognized square has the same cell, plane and orientation. Duplicates go to the fallback
      unchanged instead of changing overlap semantics.
Each merged square gets one colour: sample_color(u0, v0, u1, v1), called only for accepted faces, returns the
texture average as linear RGB in [0,1]. It is multiplied by the linear average of the vertex colours
(authored in sRGB, as in app.js), converted to sRGB and rounded to the nearest of color_steps evenly spaced
sRGB levels (0 and 1 are exact; error per channel <= 1/(2*(color_steps-1))). Squares are then merged
greedily into rectangles only within the same plane, orientation and colour level, and only over cells that
exist: holes are never filled, nothing bridges planes, no vertex moves off its plane, no skirts. The coarse
mesh is untextured (uvs are 0) with the original six-axis winding.

Everything else (sloped, non-unit, non-integer, mismatched winding, unsampleable UVs, faces not explicitly
opaque, duplicates, and the whole main part when it is marked transparent) stays in the fallback as exact
original triangle values, reindexed. transparentParts are never merged and are copied unchanged.

This is an experiment for one chunk mesh at a time: not a full-world LOD, not web integration. On jagged
or modded geometry the reduction can be modest.
"""
from __future__ import annotations

import math

from surface_lod import _validate_part, linear_to_srgb, srgb_to_linear

MAX_TRIANGLES = 1_000_000          # per part
MAX_COORD = 1 << 30
_CYCLIC = {0: (1, 2), 1: (2, 0), 2: (0, 1)}   # (b, c) with e_b x e_c = +e_axis


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _validate(mesh):
    if not isinstance(mesh, dict):
        raise ValueError("mesh must be a dict")
    positions, uvs, colors, indices = _validate_part(mesh, 0)
    if len(indices) // 3 > MAX_TRIANGLES:
        raise ValueError(f"mesh has more than {MAX_TRIANGLES} triangles")
    if "transparent" in mesh and type(mesh["transparent"]) is not bool:
        raise ValueError("mesh.transparent must be a boolean")
    parts = mesh.get("transparentParts", [])
    if not isinstance(parts, list):
        raise ValueError("transparentParts must be a list")
    for i, part in enumerate(parts, start=1):
        _validate_part(part, i)
        if "transparentParts" in part:
            raise ValueError("nested transparentParts are not allowed")
        if len(part["indices"]) // 3 > MAX_TRIANGLES:
            raise ValueError(f"transparentParts[{i - 1}] has more than {MAX_TRIANGLES} triangles")
    return positions, uvs, colors, indices, parts


def _point(positions, i):
    """Integer corner of vertex i, or None when any coordinate is not an exact integer."""
    point = []
    for v in positions[3 * i:3 * i + 3]:
        if isinstance(v, int):
            iv = v
        elif v.is_integer():
            iv = int(v)
        else:
            return None
        if abs(iv) > MAX_COORD:
            return None
        point.append(iv)
    return tuple(point)


def _square(positions, tri_a, tri_b):
    """(axis, sign, plane, u0, v0) when the two triangles are exactly one unit square, else None."""
    triangles = []
    for tri in (tri_a, tri_b):
        points = [_point(positions, i) for i in tri]
        if None in points or len(set(points)) != 3:
            return None
        triangles.append(points)
    corners = set(triangles[0]) | set(triangles[1])
    if len(corners) != 4:
        return None
    axes = [a for a in range(3) if len({p[a] for p in corners}) == 1]
    if len(axes) != 1:
        return None
    axis = axes[0]
    b, c = _CYCLIC[axis]
    u0, v0 = min(p[b] for p in corners), min(p[c] for p in corners)
    if {(p[b], p[c]) for p in corners} != {(u0, v0), (u0 + 1, v0), (u0, v0 + 1), (u0 + 1, v0 + 1)}:
        return None
    s1, s2 = set(triangles[0]) & set(triangles[1])
    if s1[b] == s2[b] or s1[c] == s2[c]:
        return None  # shared edge is a side: the triangles overlap instead of tiling the square
    signs = set()
    for p0, p1, p2 in triangles:
        e1 = [p1[k] - p0[k] for k in range(3)]
        e2 = [p2[k] - p0[k] for k in range(3)]
        normal = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        signs.add(1 if normal[axis] > 0 else -1)
    if len(signs) != 1:
        return None
    return axis, signs.pop(), next(iter(corners))[axis], u0, v0


def _uv_bounds(uvs, refs):
    """(u0, v0, u1, v1) of a square, or None when the rectangle is outside the atlas [0,1] range."""
    us = [uvs[2 * i] for i in refs]
    vs = [uvs[2 * i + 1] for i in refs]
    bounds = (min(us), min(vs), max(us), max(vs))
    return bounds if all(0.0 <= value <= 1.0 for value in bounds) else None


def _opaque(is_opaque, bounds) -> bool:
    """Only an explicit True from the caller's predicate allows merging; no predicate means keep the face."""
    if is_opaque is None:
        return False
    answer = is_opaque(*bounds)
    if type(answer) is not bool:
        raise ValueError("is_opaque must return a bool")
    return answer


def _colour(uvs, colors, refs, bounds, sample_color, steps):
    """Nearest sRGB level per channel (0..steps-1) of texture average (linear) x linear mean vertex tint."""
    texture = sample_color(*bounds)
    if not isinstance(texture, (tuple, list)) or len(texture) != 3 \
            or not all(_number(t) and 0.0 <= t <= 1.0 for t in texture):
        raise ValueError("sample_color must return three linear RGB values in [0,1]")
    vertex = [sum(srgb_to_linear(colors[3 * i + k]) for i in refs) / len(refs) for k in range(3)]
    return tuple(min(steps - 1, max(0, round(linear_to_srgb(texture[k] * vertex[k]) * (steps - 1)))) for k in range(3))


def _emit(groups, steps):
    positions, uvs, colors, indices = [], [], [], []
    quads = 0
    for (axis, sign, plane, bins), cells in sorted(groups.items()):
        b, c = _CYCLIC[axis]
        colour = [q / (steps - 1) for q in bins]  # sRGB level; 0 and 1 are exact
        done = set()
        for v, u in sorted((v, u) for u, v in cells):
            if (u, v) in done:
                continue
            u1 = u
            while (u1 + 1, v) in cells and (u1 + 1, v) not in done:
                u1 += 1
            v1 = v
            while all((x, v1 + 1) in cells and (x, v1 + 1) not in done for x in range(u, u1 + 1)):
                v1 += 1
            done.update((x, y) for x in range(u, u1 + 1) for y in range(v, v1 + 1))
            base = len(positions) // 3
            for cu, cv in ((u, v), (u1 + 1, v), (u1 + 1, v1 + 1), (u, v1 + 1)):
                point = [0, 0, 0]
                point[axis], point[b], point[c] = plane, cu, cv
                positions += point
            uvs += [0.0] * 8
            colors += colour * 4
            indices += ([base, base + 1, base + 2, base, base + 2, base + 3] if sign > 0
                        else [base, base + 2, base + 1, base, base + 3, base + 2])
            quads += 1
    return {"positions": positions, "uvs": uvs, "colors": colors, "indices": indices}, quads


def _fallback(positions, uvs, colors, triangles, mesh, parts):
    remap, out_p, out_u, out_c, out_i = {}, [], [], [], []
    for tri in triangles:
        for i in tri:
            if i not in remap:
                remap[i] = len(remap)
                out_p += positions[3 * i:3 * i + 3]
                out_u += uvs[2 * i:2 * i + 2]
                out_c += colors[3 * i:3 * i + 3]
            out_i.append(remap[i])
    result = {"positions": list(out_p), "uvs": list(out_u), "colors": list(out_c), "indices": out_i}
    if "transparent" in mesh:
        result["transparent"] = mesh["transparent"]
    if "transparentParts" in mesh:
        result["transparentParts"] = [{k: list(v) if isinstance(v, (list, tuple)) else v for k, v in part.items()}
                                      for part in parts]
    return result


def simplify_axis_aligned(mesh, sample_color, *, is_opaque=None, color_steps=8) -> dict:
    if not callable(sample_color):
        raise ValueError("sample_color must be callable")
    if is_opaque is not None and not callable(is_opaque):
        raise ValueError("is_opaque must be None or callable")
    if not isinstance(color_steps, int) or isinstance(color_steps, bool) or not 2 <= color_steps <= 256:
        raise ValueError("color_steps must be an integer 2..256")
    positions, uvs, colors, indices, parts = _validate(mesh)
    triangles = [tuple(indices[k:k + 3]) for k in range(0, len(indices), 3)]
    mergeable = mesh.get("transparent") is not True
    squares, kept = {}, []
    k = 0
    while k < len(triangles):
        if mergeable and k + 1 < len(triangles):
            key = _square(positions, triangles[k], triangles[k + 1])
            if key is not None:
                # A geometric square is consumed as a pair; it merges only if its UVs are in range and the
                # caller's predicate explicitly says opaque. The colour sampler is called only then.
                refs = triangles[k] + triangles[k + 1]
                bounds = _uv_bounds(uvs, refs)
                bins = None
                if bounds is not None and _opaque(is_opaque, bounds):
                    bins = _colour(uvs, colors, refs, bounds, sample_color, color_steps)
                squares.setdefault(key, []).append((k, bins))
                k += 2
                continue
        kept.append(k)
        k += 1
    groups, duplicates, not_accepted = {}, 0, 0
    for (axis, sign, plane, u, v), found in squares.items():
        if len(found) > 1:  # same oriented cell twice: keep every original triangle, do not merge
            duplicates += len(found)
            kept += [t for first, _ in found for t in (first, first + 1)]
            continue
        first, bins = found[0]
        if bins is None:  # opacity unknown or false, or UVs outside the atlas: kept exactly
            not_accepted += 1
            kept += [first, first + 1]
            continue
        groups.setdefault((axis, sign, plane, bins), set()).add((u, v))
    kept.sort()
    coarse, quads = _emit(groups, color_steps)
    fallback = _fallback(positions, uvs, colors, [triangles[t] for t in kept], mesh, parts)
    transparent = sum(len(part["indices"]) // 3 for part in parts)
    return {"coarse": coarse, "fallback": fallback, "stats": {
        "inputTriangles": len(triangles), "recognizedSquares": sum(len(c) for c in groups.values()),
        "duplicateSquares": duplicates, "notAcceptedSquares": not_accepted,
        "fallbackTriangles": len(kept), "coarseQuads": quads,
        "coarseTriangles": 2 * quads, "transparentTriangles": transparent,
        "outputTriangles": 2 * quads + len(kept) + transparent}}
