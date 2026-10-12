"""Audit for the offline renderer: exported assets vs atlas layout vs world registry, and real block usage.

    python offline_audit.py --assets assets.json --layout atlas-layout.json --batches b1/snapshot.bin ... --out audit.json

1. Every face icon of every asset variant must resolve by name in the layout (registered or missing sprite) and
   carry the same UV; mismatches are listed.
2. World registry names (from the batches) without an asset entry are listed.
3. Usage in the batches' core areas: per (registry name, meta) the number of blocks and of VISIBLE blocks (at
   least one of the 6 neighbours is air, absent or not an opaque cube per the assets), with renderType/class.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import offline_volume as ov

DIRS = ((0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1), (-1, 0, 0), (1, 0, 0))


def audit_assets(assets: dict, layout: dict) -> dict:
    sprites = dict(layout["registered"])
    sprites.setdefault(layout["missing"]["iconName"], layout["missing"])
    missing, uv_diff, faces = collections.Counter(), collections.Counter(), 0
    for name, block in assets["blocks"].items():
        for variant in block["variants"]:
            for face in variant["faces"]:
                if face is None:
                    continue
                faces += 1
                sprite = sprites.get(face["name"])
                if sprite is None:
                    missing[face["name"]] += 1
                elif any(abs(a - b) > 1e-5 for a, b in zip(face["uv"], sprite["uv"])):
                    uv_diff[face["name"]] += 1
    return {"faces": faces, "missingSprites": len(missing), "missingSpriteExamples": missing.most_common(20),
            "uvMismatches": len(uv_diff), "uvMismatchExamples": uv_diff.most_common(20)}


def audit_usage(volumes, assets: dict) -> dict:
    blocks = assets["blocks"]
    opaque_cache = {}

    def opaque(vol, block_id):
        key = (id(vol), block_id)
        if key not in opaque_cache:
            name = vol.registry.get(block_id)
            entry = blocks.get(name) if name else None
            opaque_cache[key] = bool(entry and entry["opaque"])
        return opaque_cache[key]

    total, visible = collections.Counter(), collections.Counter()
    for vol in volumes:
        c = vol.core
        for z in range(c.z, c.z + c.depth):
            for x in range(c.x, c.x + c.width):
                col = vol.column_index(x, z)
                if col < 0:
                    continue
                base = col * ov.HEIGHT
                for y in range(ov.HEIGHT):
                    block_id = vol.ids[base + y]
                    if block_id == 0:
                        continue
                    key = (vol.registry.get(block_id, f"#{block_id}"), vol.metas[base + y])
                    total[key] += 1
                    for dx, dy, dz in DIRS:
                        nid, _ = vol.block(x + dx, y + dy, z + dz)
                        if nid <= 0 or not opaque(vol, nid):
                            visible[key] += 1
                            break
    rows = []
    for (name, meta), n in total.most_common():
        entry = blocks.get(name)
        rows.append({"name": name, "meta": meta, "count": n, "visible": visible[(name, meta)],
                     "renderType": entry["renderType"] if entry else None, "opaque": entry["opaque"] if entry else None,
                     "class": entry["class"] if entry else None})
    by_type = collections.Counter()
    for r in rows:
        by_type[str(r["renderType"])] += r["visible"]
    vis = sum(r["visible"] for r in rows)
    return {"blocks": sum(total.values()), "visibleBlocks": vis, "distinctVariants": len(rows),
            "visibleByRenderType": {k: [v, round(100 * v / vis, 2)] for k, v in by_type.most_common()},
            "variants": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", required=True, type=Path)
    ap.add_argument("--layout", required=True, type=Path)
    ap.add_argument("--batches", nargs="+", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    assets = json.loads(a.assets.read_text(encoding="utf-8"))
    layout = json.loads(a.layout.read_text(encoding="utf-8"))
    volumes = [ov.load(p) for p in a.batches]
    world_names = set()
    for vol in volumes:
        world_names.update(vol.registry.values())
    report = {"format": "UIE_OFFLINE_AUDIT1", "assets": audit_assets(assets, layout),
              "registry": {"worldNames": len(world_names), "assetNames": len(assets["blocks"]),
                           "worldNamesWithoutAsset": sorted(world_names - set(assets["blocks"]))[:200],
                           "worldNamesWithoutAssetCount": len(world_names - set(assets["blocks"]))},
              "exportFailures": len(assets.get("failures", [])),
              "usage": audit_usage(volumes, assets)}
    a.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    u = report["usage"]
    print(json.dumps({"assets": {k: v for k, v in report["assets"].items() if "Examples" not in k},
                      "registry": {k: v for k, v in report["registry"].items() if k != "worldNamesWithoutAsset"},
                      "blocks": u["blocks"], "visible": u["visibleBlocks"], "variants": u["distinctVariants"],
                      "visibleByRenderType": dict(list(u["visibleByRenderType"].items())[:15])}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
