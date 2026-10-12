"""Model catalog of the offline renderer: registryName + metadata -> explicit model, textures and rules.

Built from EXISTING exported files only (no Minecraft): assets.json (per-variant face icons, renderType,
isOpaqueCube, class) and the matching atlas-layout.json. Every decision is explicit and carries a tier:

    verified   model chosen by an explicit rule (renderType + class allowlist), e.g. vanilla cube, cross, liquid
    assumed    opaque renderType-0 block of an unlisted class: rendered as a full cube (isOpaqueCube=true is the
               engine's own statement that it is a full opaque cube), but reported as an assumption; also the
               single mod variants of CONFIRMED_CUBES
    fallback   no rule: a visible diagnostic cube (magenta tint) and a report entry, never silently dropped
    invisible  renderType -1 or no textures at all (air-like, technical or tile-entity-only blocks): not drawn,
               reported separately

Model kinds: cube, cross, liquid, slab, stairs, layer (snow/carpet), vine, fallback, none.
Opacity: solid (occludes neighbours), cutout (alpha-tested, does not occlude others), translucent (blended).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

WHITE = 0xFFFFFF
FALLBACK_TINT = (1.0, 0.25, 1.0)
CATALOG_VERSION = "uie-offline-catalog/1"

# renderTypes whose geometry is a full block with (possibly layered) textures: vanilla 0 and 31 (logs), and mod
# renderers confirmed in the audit to draw full cubes with overlays (BoP grass/leaves, chisel CTM, GregTech,
# Thaumcraft and AE2 ores). Unknown mod renderTypes are NOT assumed to be cubes.
CUBE_RENDER_TYPES = {0, 31, 39, 65, 66, 311, 312, 707, 708, 253}   # extend only with measured evidence
# Single mod variants confirmed as full opaque cubes from the production assets.json record and javap bytecode
# (handoff/CLAUDE-CONFIRMED-MODELS.md): (registryName, class, renderType) -> (metas, reason). The renderType number
# alone is NOT a rule (mod renderers need not draw cubes, ids may move); a rule applies only to an opaque block with
# all six faces textured. Tier stays "assumed": the mod renderer itself is not reproduced, only its base icons.
CONFIRMED_CUBES = {
    ("TConstruct:Smeltery", "tconstruct.smeltery.blocks.SmelteryBlock", 387): (
        frozenset({2}), "seared brick (meta 2) as a cube: icon depends on meta only; SmelteryRender not reproduced"),
    ("hbm:tile.deco_steel", "com.hbm.blocks.generic.BlockDecoCT", 202): (
        frozenset({0}), "base icon cube: HBM connected textures (CT fragments) not modelled"),
    # Same class (one blockIcon, no own bounds; bytecode of task 122/123), production records of the live audit
    # qa-20261008/model-rollout-live.json: rt 202, opaque, meta 0 with six identical base icons.
    ("hbm:tile.deco_rusty_steel", "com.hbm.blocks.generic.BlockDecoCT", 202): (
        frozenset({0}), "base icon cube: HBM connected textures (CT fragments) not modelled"),
    ("hbm:tile.deco_tungsten", "com.hbm.blocks.generic.BlockDecoCT", 202): (
        frozenset({0}), "base icon cube: HBM connected textures (CT fragments) not modelled"),
}
CROSS_RENDER_TYPES = {1, 60, 61, 6, 40, 266, 276, 290, 420}   # plants, crops, double plants, mod flowers
LILY_RENDER_TYPES = {23}                                      # flat pad on the water surface
CONNECTED_RENDER_TYPES = {11: "fence", 32: "wall", 41: "pane", 18: "pane"}
# Fixed partial boxes in block units (min, max): chest, cactus, standing torch, bushes.
BOX_RENDER_TYPES = {13: [((1/16, 0, 1/16), (15/16, 1, 15/16))],
                    2: [((7/16, 0, 7/16), (9/16, 10/16, 9/16))], 288: [((1/16, 0, 1/16), (15/16, 14/16, 15/16))],
                    372: [((1/16, 0, 1/16), (15/16, 14/16, 15/16))]}
ORE_NAME = re.compile(r"ore", re.I)
LIQUID_RENDER_TYPES = {4, 42}
VINE_RENDER_TYPES = {20}
STAIR_RENDER_TYPES = {10}
CUTOUT_CLASS = re.compile(r"(Leaves|Leaf|Glass|Pane|Ice|Cloud|Mobspawner|MobSpawner|Web)", re.I)
TRANSLUCENT_CLASS = re.compile(r"(StainedGlass|BlockIce|BlockBreakable|Slime)", re.I)
SLAB_CLASS = re.compile(r"Slab", re.I)
DOUBLE_SLAB_NAME = re.compile(r"double", re.I)
LAYER_CLASS = {"net.minecraft.block.BlockSnow": "snow", "net.minecraft.block.BlockCarpet": "carpet"}
FOLIAGE_NAME = re.compile(r"(leaves|leaf|vine|willow|ivy)", re.I)
GRASS_NAME = re.compile(r"(grass|fern|foliage|plants)", re.I)


@dataclass
class Model:
    kind: str
    tier: str
    opacity: str = "solid"
    faces: list = field(default_factory=list)      # 6 x (u0, v0, u1, v1) or None, MC side order D U N S W E
    tint: str = "none"                              # none | grass | foliage | const
    color: tuple = (1.0, 1.0, 1.0)                  # const tint (from the export) or fallback marker
    shape: dict = field(default_factory=dict)       # slab top/bottom, layer height, stair facing ...
    reason: str = ""

    @property
    def occludes(self) -> bool:
        return self.kind == "cube" and self.opacity == "solid"


class _CompiledBlocks:
    """Read-only view of compiled blocks with the original assets.json shape (decoded lazily per block)."""

    def __init__(self, blocks, uvs, names):
        self._blocks, self._uvs, self._names, self._cache = blocks, uvs, names, {}

    def get(self, name, default=None):
        hit = self._cache.get(name)
        if hit is None:
            raw = self._blocks.get(name)
            if raw is None:
                return default
            rt, opaque, cls_name, variants = raw
            out = []
            for v in variants:
                faces = [None if i < 0 else {"name": self._names[i], "uv": self._uvs[i]} for i in v[2:8]]
                out.append({"meta": v[0], "color": v[1], "faces": faces})
            hit = self._cache[name] = {"renderType": rt, "opaque": opaque, "class": cls_name, "variants": out}
        return hit

    def __contains__(self, name):
        return name in self._blocks


def _rgb(color: int) -> tuple:
    color &= 0xFFFFFF
    return ((color >> 16) & 255) / 255.0, ((color >> 8) & 255) / 255.0, (color & 255) / 255.0


class Catalog:
    def __init__(self, assets: dict, layout: dict):
        self.assets = assets["blocks"]
        self.sprites = dict(layout["registered"])
        missing = layout["missing"]
        self.sprites.setdefault(missing["iconName"], missing)
        self.missing_uv = tuple(missing["uv"])
        self._cache: dict[tuple[str, int], Model] = {}

    COMPILED = "UIE_OFFLINE_CATALOG1"

    @staticmethod
    def compile(assets_path, layout_path, out_path) -> dict:
        """Compact catalog for workers: sprite table (uv only) + per block [renderType, opaque, class, variants]
        with variants [meta, color, 6 sprite indices or -1]. Same models as the source files, ~10x less memory."""
        assets_raw, layout_raw = Path(assets_path).read_bytes(), Path(layout_path).read_bytes()
        assets, layout = json.loads(assets_raw), json.loads(layout_raw)
        sprites = {name: s["uv"] for name, s in layout["registered"].items()}
        missing = layout["missing"]
        sprites.setdefault(missing["iconName"], missing["uv"])
        names, index = [], {}
        # Every layout sprite is kept (uv only), not just the ones assets.json faces use: the mesher also needs
        # sprites no item face exposes, e.g. grass_side_overlay.
        for name, uv in sprites.items():
            index[name] = len(names)
            names.append((name, uv))
        blocks = {}
        for name, block in assets["blocks"].items():
            variants = []
            for v in block["variants"]:
                faces = []
                for face in v["faces"]:
                    if face is None:
                        faces.append(-1)
                        continue
                    key = face["name"] if face["name"] in sprites else "#uv:" + json.dumps(face["uv"])
                    if key not in index:
                        index[key] = len(names)
                        names.append((key, sprites.get(face["name"], face["uv"])))
                    faces.append(index[key])
                variants.append([v["meta"], v.get("color", WHITE), *faces])
            blocks[name] = [block["renderType"], block["opaque"], block["class"], variants]
        identity = hashlib.sha256(CATALOG_VERSION.encode() + b"\0" + hashlib.sha256(assets_raw).digest()
                                  + hashlib.sha256(layout_raw).digest()).hexdigest()
        doc = {"format": Catalog.COMPILED, "identity": identity, "missing": [missing["iconName"], missing["uv"]],
               "sprites": [[k, uv] for k, uv in names], "blocks": blocks}
        Path(out_path).write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
        return {"identity": identity, "sprites": len(names), "blocks": len(blocks)}

    @classmethod
    def load_compiled(cls, path) -> "Catalog":
        doc = json.loads(Path(path).read_bytes())
        if doc.get("format") != cls.COMPILED:
            raise ValueError("not a compiled offline catalog")
        catalog = cls.__new__(cls)
        table = doc["sprites"]
        catalog.sprites = {k: {"uv": uv} for k, uv in table if not k.startswith("#uv:")}
        catalog.sprites.setdefault(doc["missing"][0], {"uv": doc["missing"][1]})
        catalog.missing_uv = tuple(doc["missing"][1])
        uvs = [tuple(uv) for _k, uv in table]
        catalog.assets = _CompiledBlocks(doc["blocks"], uvs, [k for k, _uv in table])
        catalog._cache = {}
        catalog.identity = doc["identity"]
        return catalog

    @classmethod
    def load(cls, assets_path, layout_path) -> "Catalog":
        assets_raw = Path(assets_path).read_bytes()
        layout_raw = Path(layout_path).read_bytes()
        catalog = cls(json.loads(assets_raw), json.loads(layout_raw))
        catalog.identity = hashlib.sha256(CATALOG_VERSION.encode() + b"\0" + hashlib.sha256(assets_raw).digest()
                                          + hashlib.sha256(layout_raw).digest()).hexdigest()
        return catalog

    def _face_uvs(self, variant) -> list:
        uvs = []
        for face in variant["faces"]:
            if face is None:
                uvs.append(None)
                continue
            sprite = self.sprites.get(face["name"])
            # The layout is authoritative for the atlas in use; an unknown sprite keeps the exported UV.
            uvs.append(tuple(sprite["uv"]) if sprite else tuple(face["uv"]))
        return uvs

    def model(self, name: str, meta: int) -> Model:
        key = (name, meta)
        cached = self._cache.get(key)
        if cached is None:
            cached = self._cache[key] = self._resolve(name, meta)
        return cached

    def _resolve(self, name: str, meta: int) -> Model:
        if name == "minecraft:air":
            return Model("none", "invisible", reason="air")
        block = self.assets.get(name)
        if block is None:
            return Model("fallback", "fallback", faces=[self.missing_uv] * 6, color=FALLBACK_TINT,
                         reason="no exported asset")
        variants = block["variants"]
        variant = variants[meta] if meta < len(variants) and variants[meta]["meta"] == meta else \
            next((v for v in variants if v["meta"] == meta), variants[0])
        faces = self._face_uvs(variant)
        rt, cls_name, opaque = block["renderType"], block["class"], block["opaque"]
        short = cls_name.rsplit(".", 1)[-1]
        color = variant.get("color", WHITE)
        if rt == -1 or all(f is None for f in faces):
            return Model("none", "invisible", reason=f"renderType {rt}" if rt == -1 else "no textures")
        complete = all(f is not None for f in faces)
        faces = [f if f is not None else self.missing_uv for f in faces]
        tint, const = self._tint(name, color)
        confirmed = CONFIRMED_CUBES.get((name, cls_name, rt))
        if confirmed and meta in confirmed[0] and variant["meta"] == meta and opaque is True and complete:
            return Model("cube", "assumed", "solid", faces, tint, const, reason=confirmed[1])
        if rt in LIQUID_RENDER_TYPES:
            return Model("liquid", "verified", "translucent", faces, tint, const, {"level": meta & 7},
                         reason=f"liquid renderType {rt}")
        if rt in CROSS_RENDER_TYPES:
            return Model("cross", "verified", "cutout", faces, tint, const, reason=f"cross renderType {rt}")
        if rt in LILY_RENDER_TYPES:
            return Model("lily", "verified", "cutout", faces, tint, const, reason=f"floating pad renderType {rt}")
        if rt in CONNECTED_RENDER_TYPES:
            kind = CONNECTED_RENDER_TYPES[rt]
            return Model(kind, "verified", "cutout" if kind == "pane" else "solid", faces, tint, const,
                         reason=f"{kind} renderType {rt} (connects to same kind and solid blocks)")
        if rt == 22:   # chests: the client capture draws a full planks cube (tile-entity lid not modelled)
            return Model("cube", "verified", "cutout", faces, tint, const, reason="chest as cube (matches capture)")
        if rt in BOX_RENDER_TYPES:
            return Model("box", "verified", "cutout", faces, tint, const, {"boxes": BOX_RENDER_TYPES[rt]},
                         reason=f"fixed box renderType {rt}")
        if rt == 8:
            return Model("ladder", "verified", "cutout", faces, tint, const, {"facing": meta & 7}, reason="ladder")
        if rt == 0 and short == "BlockFarmland":
            return Model("layer", "verified", "solid", faces, tint, const, {"height": 15 / 16}, reason="farmland")
        if rt in VINE_RENDER_TYPES:
            return Model("vine", "verified", "cutout", faces, "foliage" if tint != "none" else tint, const,
                         {"sides": meta & 15}, reason="vine renderType 20")
        if rt in STAIR_RENDER_TYPES:
            return Model("stairs", "verified", "solid", faces, tint, const,
                         {"facing": meta & 3, "upside": bool(meta & 4)}, reason="stairs renderType 10")
        if rt == 0 and cls_name in LAYER_CLASS:
            height = ((meta & 7) + 1) / 8.0 if LAYER_CLASS[cls_name] == "snow" else 1.0 / 16.0
            return Model("layer", "verified", "solid", faces, tint, const, {"height": height},
                         reason=LAYER_CLASS[cls_name])
        if rt == 0 and SLAB_CLASS.search(short) and not DOUBLE_SLAB_NAME.search(name):
            return Model("slab", "verified", "solid", faces, tint, const, {"top": bool(meta & 8)},
                         reason="half slab")
        if rt in CUBE_RENDER_TYPES:
            if CUTOUT_CLASS.search(short) or FOLIAGE_NAME.search(name):
                opacity = "translucent" if TRANSLUCENT_CLASS.search(short) and "Leaves" not in short else "cutout"
                return Model("cube", "verified", opacity, faces, tint, const, reason=f"see-through cube ({short})")
            if opaque:
                tier = "verified" if rt != 0 or cls_name.startswith("net.minecraft.") else "assumed"
                return Model("cube", tier, "solid", faces, tint, const,
                             reason=f"opaque cube renderType {rt}" + ("" if tier == "verified" else " (unlisted class)"))
            if rt == 0 and (ORE_NAME.search(short) or ORE_NAME.search(name.split(":", 1)[-1])):
                # The client does not treat them as opaque either: drawn as a cube that hides no neighbour.
                return Model("cube", "assumed", "cutout", faces, tint, const,
                             reason=f"non-opaque ore-like renderType-0 block ({short}) drawn as a cube")
            return Model("fallback", "fallback", "solid", faces, "const", FALLBACK_TINT,
                         reason=f"non-opaque renderType {rt} block of class {short}: shape unknown")
        return Model("fallback", "fallback", "solid", faces, "const", FALLBACK_TINT,
                     reason=f"no rule for renderType {rt} ({short})")

    @staticmethod
    def _tint(name: str, color: int):
        if color is None or (color & 0xFFFFFF) == WHITE:
            return "none", (1.0, 1.0, 1.0)
        if FOLIAGE_NAME.search(name):
            return "foliage", _rgb(color)
        if GRASS_NAME.search(name):
            return "grass", _rgb(color)
        return "const", _rgb(color)
