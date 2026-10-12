"""Synthetic-fixture tests for reader.py. Uses in-memory buffers only; writes no files.

Run from the work directory:  python -m unittest discover -s uie-map -p "test_reader.py" -v
"""
import gzip
import io
import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reader as r  # noqa: E402
from reader import FormatError, Limits, UnsupportedFormatError  # noqa: E402

(T_END, T_BYTE, T_SHORT, T_INT, T_LONG, T_FLOAT, T_DOUBLE, T_BARR,
 T_STR, T_LIST, T_COMP, T_IARR, T_LARR) = range(13)


# ---------------------------------------------------------------- test-only NBT encoder

def s(text):
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def payload(tag, value):
    scalars = {T_BYTE: ">b", T_SHORT: ">h", T_INT: ">i", T_LONG: ">q", T_FLOAT: ">f", T_DOUBLE: ">d"}
    if tag in scalars:
        return struct.pack(scalars[tag], value)
    if tag == T_BARR:
        return struct.pack(">i", len(value)) + bytes(value)
    if tag == T_STR:
        return s(value)
    if tag == T_LIST:
        item, items = value
        return struct.pack(">bi", item, len(items)) + b"".join(payload(item, v) for v in items)
    if tag == T_COMP:
        return b"".join(struct.pack(">b", t) + s(k) + payload(t, v) for k, (t, v) in value.items()) + b"\x00"
    if tag == T_IARR:
        return struct.pack(f">i{len(value)}i", len(value), *value)
    if tag == T_LARR:
        return struct.pack(f">i{len(value)}q", len(value), *value)
    raise AssertionError(tag)


def nbt(compound, name=""):
    return b"\x0a" + s(name) + payload(T_COMP, compound)


def raw_compound(body):
    """Unnamed root compound around hand-written child bytes."""
    return b"\x0a" + s("") + body + b"\x00"


# ---------------------------------------------------------------- world fixtures

class SectionBuilder:
    """Writes Blocks/Add/Data following the 1.7.10 spec independently of reader.py."""

    def __init__(self, y, with_add=True):
        self.y = y
        self.blocks = bytearray(4096)
        self.data = bytearray(2048)
        self.add = bytearray(2048) if with_add else None

    @staticmethod
    def _nib(arr, i, v):
        b = arr[i // 2]
        arr[i // 2] = (b & 0x0F) | (v << 4) if i % 2 else (b & 0xF0) | v

    def set(self, x, y, z, block_id, meta=0):
        i = y * 256 + z * 16 + x
        self.blocks[i] = block_id & 0xFF
        if self.add is None:
            assert block_id < 256
        else:
            self._nib(self.add, i, block_id >> 8)
        self._nib(self.data, i, meta)

    def tag(self):
        comp = {"Y": (T_BYTE, self.y), "Blocks": (T_BARR, self.blocks), "Data": (T_BARR, self.data),
                "BlockLight": (T_BARR, bytes(2048)), "SkyLight": (T_BARR, bytes(2048))}
        if self.add is not None:
            comp["Add"] = (T_BARR, self.add)
        return comp


def chunk_nbt(cx, cz, sections=(), extra_level=None, extra_root=None):
    level = {"xPos": (T_INT, cx), "zPos": (T_INT, cz),
             "Sections": (T_LIST, (T_COMP, [sb.tag() if isinstance(sb, SectionBuilder) else sb
                                            for sb in sections])),
             "Biomes": (T_BARR, bytes(256)), "HeightMap": (T_IARR, [0] * 256)}
    level.update(extra_level or {})
    root = {"Level": (T_COMP, level)}
    root.update(extra_root or {})
    return nbt(root)


def record(nbt_bytes, compression=2):
    comp = gzip.compress(nbt_bytes) if compression == 1 else zlib.compress(nbt_bytes)
    return struct.pack(">iB", len(comp) + 1, compression) + comp


def build_region(records):
    """records: {(local_x, local_z): record bytes}; sectors allocated sequentially and padded."""
    header, body, sector = bytearray(8192), bytearray(), 2
    for (lx, lz), rec in records.items():
        count = max(1, -(-len(rec) // 4096))
        i = lx + lz * 32
        struct.pack_into(">I", header, i * 4, (sector << 8) | count)
        struct.pack_into(">i", header, 4096 + i * 4, 1_600_000_000 + i)
        body += rec + bytes(count * 4096 - len(rec))
        sector += count
    return bytes(header + body)


def raw_region(entries, body=b""):
    """entries: {(local_x, local_z): (sector_offset, sector_count)} with an arbitrary body."""
    header = bytearray(8192)
    for (lx, lz), (off, cnt) in entries.items():
        struct.pack_into(">I", header, (lx + lz * 32) * 4, (off << 8) | cnt)
    return bytes(header) + body


def open_region(data, rx=0, rz=0, limits=r.DEFAULT_LIMITS):
    return r.RegionFile(io.BytesIO(data), rx, rz, limits)


def level_dat(entries=(), data_extra=None, fml=True):
    data = {"version": (T_INT, 19133), "LevelName": (T_STR, "uie")}
    data.update(data_extra or {})
    root = {"Data": (T_COMP, data)}
    if fml:
        items = [{"K": (T_STR, k), "V": (T_INT, v)} for k, v in entries]
        root["FML"] = (T_COMP, {"ItemData": (T_LIST, (T_COMP, items))})
    return gzip.compress(nbt(root))


# ---------------------------------------------------------------- NBT

class NbtTests(unittest.TestCase):
    SAMPLE = {
        "b": (T_BYTE, -5), "s": (T_SHORT, -300), "i": (T_INT, -70000), "l": (T_LONG, -2 ** 40),
        "f": (T_FLOAT, 1.5), "d": (T_DOUBLE, -2.25), "ba": (T_BARR, b"\x00\xff\x7f"),
        "str": (T_STR, "žluťoučký kůň"), "list": (T_LIST, (T_COMP, [{"n": (T_INT, 1)}, {}])),
        "empty": (T_LIST, (T_END, [])), "ia": (T_IARR, [1, -1, 2 ** 31 - 1]),
        "la": (T_LARR, [2 ** 63 - 1, -2 ** 63]),
        "nested": (T_COMP, {"x": (T_LIST, (T_LIST, [(T_BYTE, [1, -2]), (T_STR, [])]))}),
    }

    def test_all_tag_types(self):
        name, root = r.parse_nbt(nbt(self.SAMPLE, "root"))
        self.assertEqual(name, "root")
        self.assertEqual(root["b"], -5)
        self.assertEqual(root["s"], -300)
        self.assertEqual(root["i"], -70000)
        self.assertEqual(root["l"], -2 ** 40)
        self.assertEqual(root["f"], 1.5)
        self.assertEqual(root["d"], -2.25)
        self.assertEqual(root["ba"], b"\x00\xff\x7f")
        self.assertIsInstance(root["ba"], bytes)
        self.assertEqual(root["str"], "žluťoučký kůň")
        self.assertEqual(root["list"], [{"n": 1}, {}])
        self.assertEqual(root["empty"], [])
        self.assertEqual(root["ia"], [1, -1, 2 ** 31 - 1])
        self.assertEqual(root["la"], [2 ** 63 - 1, -2 ** 63])
        self.assertEqual(root["nested"], {"x": [[1, -2], []]})

    def test_modified_utf8(self):
        raw = b"a\xc0\x80b" + "😀".encode("utf-8", "surrogatepass")
        data = raw_compound(b"\x08" + s("k") + struct.pack(">H", len(raw)) + raw)
        self.assertEqual(r.parse_nbt(data)[1]["k"], "a\x00b\U0001F600")

    def test_invalid_utf8(self):
        data = raw_compound(b"\x08" + s("k") + struct.pack(">H", 1) + b"\xff")
        with self.assertRaises(FormatError):
            r.parse_nbt(data)

    def test_every_truncation_is_rejected(self):
        data = nbt(self.SAMPLE, "root")
        for n in range(len(data)):
            with self.subTest(length=n), self.assertRaises(FormatError):
                r.parse_nbt(data[:n])

    def test_trailing_bytes(self):
        with self.assertRaises(FormatError):
            r.parse_nbt(nbt({}) + b"\x00")

    def test_root_must_be_compound(self):
        with self.assertRaises(FormatError):
            r.parse_nbt(b"\x08" + s("") + s("x"))

    def test_invalid_tag_type(self):
        with self.assertRaises(FormatError):
            r.parse_nbt(raw_compound(b"\x0d" + s("x")))
        with self.assertRaises(FormatError):
            r.parse_nbt(raw_compound(b"\x09" + s("x") + struct.pack(">bi", 13, 0)))

    def test_negative_lengths(self):
        for tag in (T_BARR, T_IARR, T_LARR):
            with self.subTest(tag=tag), self.assertRaises(FormatError):
                r.parse_nbt(raw_compound(bytes([tag]) + s("a") + struct.pack(">i", -1)))
        with self.assertRaises(FormatError):
            r.parse_nbt(raw_compound(b"\x09" + s("a") + struct.pack(">bi", T_BYTE, -1)))

    def test_huge_declared_array_rejected_by_limit(self):
        data = raw_compound(b"\x0c" + s("a") + struct.pack(">i", 2 ** 31 - 1))
        with self.assertRaisesRegex(FormatError, "limit"):
            r.parse_nbt(data)

    def test_array_byte_limit(self):
        limits = Limits(max_array_bytes=8)
        self.assertEqual(r.parse_nbt(nbt({"a": (T_BARR, bytes(8))}), limits)[1]["a"], bytes(8))
        with self.assertRaises(FormatError):
            r.parse_nbt(nbt({"a": (T_BARR, bytes(9))}), limits)
        with self.assertRaises(FormatError):
            r.parse_nbt(nbt({"a": (T_LARR, [1, 2])}), limits)

    def test_list_limits(self):
        with self.assertRaises(FormatError):
            r.parse_nbt(raw_compound(b"\x09" + s("a") + struct.pack(">bi", T_END, 3)))
        with self.assertRaises(FormatError):
            r.parse_nbt(nbt({"a": (T_LIST, (T_BYTE, [1, 2, 3]))}), Limits(max_list_len=2))

    def test_depth_limit_without_recursion_error(self):
        inner = struct.pack(">bi", T_END, 0)
        for _ in range(5000):
            inner = struct.pack(">bi", T_LIST, 1) + inner
        with self.assertRaisesRegex(FormatError, "nesting"):
            r.parse_nbt(raw_compound(b"\x09" + s("x") + inner))
        shallow = struct.pack(">bi", T_LIST, 1) + struct.pack(">bi", T_END, 0)
        self.assertEqual(r.parse_nbt(raw_compound(b"\x09" + s("x") + shallow), Limits(max_depth=2))[1],
                         {"x": [[]]})
        with self.assertRaises(FormatError):
            r.parse_nbt(raw_compound(b"\x09" + s("x") + shallow), Limits(max_depth=1))

    def test_node_limit(self):
        with self.assertRaisesRegex(FormatError, "node"):
            r.parse_nbt(nbt({"a": (T_LIST, (T_BYTE, list(range(20))))}), Limits(max_nodes=10))


# ---------------------------------------------------------------- decompression

class DecompressTests(unittest.TestCase):
    PAYLOAD = bytes(range(256)) * 64

    def test_gzip_and_zlib(self):
        self.assertEqual(r.decompress(gzip.compress(self.PAYLOAD), 1), self.PAYLOAD)
        self.assertEqual(r.decompress(zlib.compress(self.PAYLOAD), 2), self.PAYLOAD)

    def test_bomb_rejected(self):
        limits = Limits(max_decompressed=1_000_000)
        for comp, kind in ((zlib.compress(bytes(10_000_000)), 2), (gzip.compress(bytes(10_000_000)), 1)):
            with self.subTest(kind=kind), self.assertRaisesRegex(FormatError, "exceeds"):
                r.decompress(comp, kind, limits)

    def test_exact_limit_allowed(self):
        self.assertEqual(r.decompress(zlib.compress(bytes(1000)), 2, Limits(max_decompressed=1000)), bytes(1000))

    def test_compressed_input_limit(self):
        with self.assertRaises(FormatError):
            r.decompress(zlib.compress(self.PAYLOAD), 2, Limits(max_compressed=10))

    def test_truncated_garbage_trailing_unknown(self):
        comp = zlib.compress(self.PAYLOAD)
        cases = {"truncated": (comp[: len(comp) // 2], 2), "garbage": (b"not compressed", 2),
                 "trailing": (gzip.compress(self.PAYLOAD) + b"junk", 1), "unknown": (comp, 5),
                 "wrong kind": (comp, 1)}
        for label, (data, kind) in cases.items():
            with self.subTest(label), self.assertRaises(FormatError):
                r.decompress(data, kind)


# ---------------------------------------------------------------- level.dat / FML

class LevelDatTests(unittest.TestCase):
    ENTRIES = [("\x01minecraft:stone", 1), ("\x02minecraft:stone", 1), ("\x01mod:big", 300),
               ("\x02mod:item", 4200), ("\x03weird:entry", 5), ("\x01minecraft:air", 0)]

    def test_legacy_level_and_registry_filtering(self):
        root = r.parse_level_dat(level_dat(self.ENTRIES))
        self.assertEqual(root["Data"]["LevelName"], "uie")
        reg = r.parse_fml_registry(root)
        self.assertEqual(dict(reg.blocks), {0: "minecraft:air", 1: "minecraft:stone", 300: "mod:big"})
        self.assertEqual(dict(reg.items), {1: "minecraft:stone", 4200: "mod:item"})
        self.assertEqual(reg.skipped_entries, 1)
        self.assertEqual(reg.block_name(300), "mod:big")
        self.assertIsNone(reg.block_name(4200))

    def test_newer_level_rejected(self):
        with self.assertRaises(UnsupportedFormatError):
            r.parse_level_dat(level_dat(data_extra={"DataVersion": (T_INT, 1343)}))
        with self.assertRaises(UnsupportedFormatError):
            r.parse_level_dat(level_dat(data_extra={"version": (T_INT, 19132)}))

    def test_not_gzip(self):
        with self.assertRaises(FormatError):
            r.parse_level_dat(nbt({"Data": (T_COMP, {"version": (T_INT, 19133)})}))

    def test_neid_16bit_block_ids_accepted(self):
        reg = r.parse_fml_registry(r.parse_level_dat(level_dat(
            [("\x01hbm:tile.hadron_coil_gold", 12965), ("\x01mod:top", 15983), ("\x01mod:max", 65535)])))
        self.assertEqual(reg.block_name(12965), "hbm:tile.hadron_coil_gold")
        self.assertEqual(reg.block_name(15983), "mod:top")
        self.assertEqual(reg.block_name(65535), "mod:max")

    def test_block_ids_outside_16bit_rejected(self):
        for value in (65536, -1):
            with self.subTest(value=value), self.assertRaises(UnsupportedFormatError):
                r.parse_fml_registry(r.parse_level_dat(level_dat([("\x01mod:x", value)])))

    def test_item_ids_above_block_range_allowed(self):
        reg = r.parse_fml_registry(r.parse_level_dat(level_dat([("\x02mod:x", 31999)])))
        self.assertEqual(dict(reg.items), {31999: "mod:x"})
        self.assertEqual(dict(reg.blocks), {})

    def test_conflicts_and_malformed(self):
        with self.assertRaises(FormatError):
            r.parse_fml_registry(r.parse_level_dat(level_dat([("\x01a:a", 5), ("\x01b:b", 5)])))
        same = r.parse_fml_registry(r.parse_level_dat(level_dat([("\x01a:a", 5), ("\x01a:a", 5)])))
        self.assertEqual(dict(same.blocks), {5: "a:a"})
        for bad in ({"FML": {"ItemData": [{"K": "\x01a:a"}]}}, {"FML": {"ItemData": [{"K": "\x01", "V": 1}]}},
                    {"FML": {"ItemData": ["x"]}}, {"FML": {"ItemData": {}}}, {"FML": {}}, {}):
            with self.subTest(bad=bad), self.assertRaises(FormatError):
                r.parse_fml_registry(bad)

    def test_forge_18_registries_rejected(self):
        with self.assertRaises(UnsupportedFormatError):
            r.parse_fml_registry({"FML": {"Registries": {}}})


# ---------------------------------------------------------------- sections / chunks

def section_dict(y=0, blocks=None, data=None, add=None, **extra):
    raw = {"Y": y, "Blocks": bytes(4096) if blocks is None else bytes(blocks),
           "Data": bytes(2048) if data is None else bytes(data)}
    if add is not None:
        raw["Add"] = bytes(add)
    raw.update(extra)
    return raw


def chunk_dict(sections, cx=0, cz=0):
    return {"Level": {"xPos": cx, "zPos": cz, "Sections": sections}}


class ChunkTests(unittest.TestCase):
    def test_nibble_layout_both_halves(self):
        # Hand-written bytes: index 0 uses the low nibble, index 1 the high nibble.
        blocks, data, add = bytearray(4096), bytearray(2048), bytearray(2048)
        blocks[0], blocks[1] = 0xAB, 0xCD
        data[0] = 0x5A   # meta(idx0)=0xA, meta(idx1)=0x5
        add[0] = 0x52    # add(idx0)=0x2, add(idx1)=0x5
        chunk = r.decode_chunk(chunk_dict([section_dict(0, blocks, data, add)]))
        self.assertEqual(chunk.block(0, 0, 0), (0x2AB, 0xA))
        self.assertEqual(chunk.block(1, 0, 0), (0x5CD, 0x5))
        self.assertEqual(chunk.block(2, 0, 0), (0, 0))

    def test_builder_matches_spec(self):
        sb = SectionBuilder(0)
        sb.set(0, 0, 0, 0x2AB, 0xA)
        sb.set(1, 0, 0, 0x5CD, 0x5)
        self.assertEqual((sb.data[0], sb.add[0], sb.blocks[0], sb.blocks[1]), (0x5A, 0x52, 0xAB, 0xCD))

    def test_yzx_index_order_and_ids_above_255(self):
        sb = SectionBuilder(3)
        sb.set(3, 5, 7, 256, 1)
        sb.set(15, 15, 15, 4095, 15)
        sb.set(14, 15, 15, 255, 14)
        _, root = r.parse_nbt(chunk_nbt(0, 0, [sb]))
        chunk = r.decode_chunk(root)
        self.assertEqual(chunk.sections[3].blocks[5 * 256 + 7 * 16 + 3], 0)  # low byte of 256
        self.assertEqual(chunk.block(3, 48 + 5, 7), (256, 1))
        self.assertEqual(chunk.block(7, 48 + 5, 3), (0, 0))
        self.assertEqual(chunk.block(15, 63, 15), (4095, 15))
        self.assertEqual(chunk.block(14, 63, 15), (255, 14))
        self.assertEqual(chunk.sections[3].block_id(15, 15, 15), 4095)
        self.assertEqual(chunk.sections[3].meta(15, 15, 15), 15)

    def test_without_add_every_meta(self):
        sb = SectionBuilder(15, with_add=False)
        for meta in range(16):
            sb.set(meta, 15, 0, 200, meta)
        chunk = r.decode_chunk(r.parse_nbt(chunk_nbt(0, 0, [sb]))[1])
        self.assertIsNone(chunk.sections[15].add)
        for meta in range(16):
            self.assertEqual(chunk.block(meta, 255, 0), (200, meta))

    def test_missing_section_is_air_and_bounds(self):
        chunk = r.decode_chunk(chunk_dict([]))
        self.assertEqual(chunk.block(0, 100, 0), (0, 0))
        for coords in ((16, 0, 0), (0, 256, 0), (0, -1, 0), (0, 0, -1)):
            with self.subTest(coords=coords), self.assertRaises(IndexError):
                chunk.block(*coords)

    def test_unsupported_formats(self):
        cases = {
            "Data16": chunk_dict([section_dict(Data16=bytes(4096))]),
            "Data16 with Blocks16": chunk_dict([section_dict(Blocks16=bytes(8192), Data16=bytes(4096))]),
            "Palette": chunk_dict([section_dict(Palette=[])]),
            "Y16": chunk_dict([section_dict(16)]),
            "Y-1": chunk_dict([section_dict(-1)]),
            "DataVersion": {"DataVersion": 1343, **chunk_dict([])},
            "int biomes": {"Level": {"xPos": 0, "zPos": 0, "Biomes": [0] * 256}},
        }
        for label, root in cases.items():
            with self.subTest(label), self.assertRaises(UnsupportedFormatError):
                r.decode_chunk(root)

    def test_corrupt_chunks(self):
        cases = {
            "short Blocks": chunk_dict([section_dict(blocks=bytes(4095))]),
            "missing Data": chunk_dict([{"Y": 0, "Blocks": bytes(4096)}]),
            "bad Add": chunk_dict([section_dict(add=bytes(100))]),
            "list Blocks": chunk_dict([section_dict() | {"Blocks": [0] * 4096}]),
            "dup Y": chunk_dict([section_dict(1), section_dict(1)]),
            "no Level": {},
            "no xPos": {"Level": {"zPos": 0}},
            "bad biomes": {"Level": {"xPos": 0, "zPos": 0, "Biomes": bytes(10)}},
        }
        for label, root in cases.items():
            with self.subTest(label), self.assertRaises(FormatError):
                r.decode_chunk(root)


class NeidTests(unittest.TestCase):
    """Blocks16 = 4096 big-endian u16 IDs (NotEnoughIDs 1.4.3.4, ByteBuffer default order)."""

    @staticmethod
    def neid_section(**extra):
        blocks16, data = bytearray(8192), bytearray(2048)
        blocks16[0:2] = b"\x32\xa5"          # index 0 (0,0,0)      -> 12965
        blocks16[2:4] = b"\x00\x01"          # index 1 (1,0,0)      -> 1
        blocks16[8190:8192] = b"\x3e\x6f"    # index 4095 (15,15,15) -> 15983
        data[0] = 0x73                       # meta idx0 = 3, idx1 = 7
        data[2047] = 0xC0                    # meta idx4095 (odd, high nibble) = 12
        blocks, add = bytearray(4096), bytearray(2048)
        blocks[0], add[0] = 0x05, 0x01       # legacy idx0 would read 261
        blocks[4095] = 0x09                  # legacy idx4095 would read 9
        raw = {"Y": 2, "Blocks16": bytes(blocks16), "Blocks": bytes(blocks), "Add": bytes(add),
               "Data": bytes(data)}
        raw.update(extra)
        return raw

    def test_hardcoded_ids_and_meta(self):
        chunk = r.decode_chunk(chunk_dict([self.neid_section()]))
        self.assertEqual(chunk.block(0, 32, 0), (12965, 3))
        self.assertEqual(chunk.block(1, 32, 0), (1, 7))
        self.assertEqual(chunk.block(15, 47, 15), (15983, 12))
        self.assertEqual(chunk.sections[2].block_id(15, 15, 15), 0x3E6F)
        self.assertEqual(chunk.block(2, 32, 0), (0, 0))

    def test_blocks16_has_priority_over_legacy(self):
        chunk = r.decode_chunk(chunk_dict([self.neid_section()]))
        legacy = r.decode_chunk(chunk_dict([{k: v for k, v in self.neid_section().items() if k != "Blocks16"}]))
        self.assertEqual(legacy.block(0, 32, 0), (261, 3))
        self.assertEqual(legacy.block(15, 47, 15), (9, 12))
        self.assertEqual(chunk.block(0, 32, 0), (12965, 3))
        self.assertEqual(chunk.block(15, 47, 15), (15983, 12))

    def test_blocks16_without_legacy_arrays(self):
        raw = {k: v for k, v in self.neid_section().items() if k not in ("Blocks", "Add")}
        self.assertEqual(r.decode_chunk(chunk_dict([raw])).block(0, 32, 0), (12965, 3))

    def test_blocks16_through_nbt_and_surface(self):
        raw = self.neid_section()
        section = {k: (T_BYTE, v) if k == "Y" else (T_BARR, v) for k, v in raw.items()}
        chunk = r.decode_chunk(r.parse_nbt(chunk_nbt(0, 0, [section]))[1])
        surface = chunk.surface()
        self.assertEqual(surface.at(0, 0), (32, 12965, 3))
        self.assertEqual(surface.at(15, 15), (47, 15983, 12))
        self.assertIsNone(surface.at(2, 0))

    def test_bad_blocks16_length(self):
        for size in (0, 4096, 8191, 8193):
            with self.subTest(size=size), self.assertRaises(FormatError):
                r.decode_chunk(chunk_dict([self.neid_section(Blocks16=bytes(size))]))
        with self.assertRaises(FormatError):
            r.decode_chunk(chunk_dict([self.neid_section(Blocks16=[0] * 4096)]))

    def test_corrupt_legacy_arrays_still_rejected(self):
        with self.assertRaises(FormatError):
            r.decode_chunk(chunk_dict([self.neid_section(Blocks=bytes(10))]))
        with self.assertRaises(FormatError):
            r.decode_chunk(chunk_dict([{k: v for k, v in self.neid_section().items() if k != "Data"}]))


class SurfaceTests(unittest.TestCase):
    def test_surface_ids_and_meta(self):
        low, mid, top = SectionBuilder(0), SectionBuilder(1), SectionBuilder(15)
        low.set(0, 5, 0, 1, 0)
        mid.set(0, 4, 0, 300, 7)       # y=20 above the stone at y=5
        low.set(1, 3, 0, 256, 9)       # Blocks byte 0 but Add=1: not air
        top.set(15, 15, 15, 4095, 15)  # y=255
        for meta in range(16):
            low.set(meta, 0, 5, 42, meta)
        chunk = r.decode_chunk(r.parse_nbt(chunk_nbt(-3, 7, [low, mid, top]))[1])
        surface = chunk.surface()
        self.assertEqual((surface.chunk_x, surface.chunk_z), (-3, 7))
        self.assertEqual(surface.at(0, 0), (20, 300, 7))
        self.assertEqual(surface.at(1, 0), (3, 256, 9))
        self.assertEqual(surface.at(15, 15), (255, 4095, 15))
        self.assertIsNone(surface.at(2, 2))
        self.assertEqual(surface.heights[2 * 16 + 2], -1)
        for meta in range(16):
            self.assertEqual(surface.at(meta, 5), (0, 42, meta))

    def test_empty_chunk_surface(self):
        surface = r.decode_chunk(chunk_dict([])).surface()
        self.assertEqual(surface.heights, (-1,) * 256)
        self.assertEqual(len(surface.ids), 256)
        self.assertEqual(len(surface.metas), 256)


# ---------------------------------------------------------------- regions

def stone_chunk(cx, cz):
    sb = SectionBuilder(0)
    sb.set(0, 0, 0, 1, 2)
    return chunk_nbt(cx, cz, [sb])


class RegionTests(unittest.TestCase):
    def test_empty_regions(self):
        for data in (b"", bytes(8192)):
            with self.subTest(size=len(data)):
                with open_region(data) as region:
                    self.assertEqual(region.locations(), [])
                    self.assertIsNone(region.read_chunk(0, 0))
                    self.assertIsNone(region.read_chunk(31, 31))

    def test_truncated_header(self):
        with self.assertRaises(FormatError):
            open_region(bytes(100))

    def test_gzip_and_zlib_chunks_and_timestamps(self):
        data = build_region({(0, 0): record(stone_chunk(0, 0), 2), (5, 3): record(stone_chunk(5, 3), 1)})
        region = open_region(data)
        self.assertEqual([(l.local_x, l.local_z) for l in region.locations()], [(0, 0), (5, 3)])
        self.assertEqual(region.locations()[1].timestamp, 1_600_000_000 + 5 + 3 * 32)
        for lx, lz in ((0, 0), (5, 3)):
            chunk = region.read_chunk(lx, lz)
            self.assertEqual((chunk.x, chunk.z), (lx, lz))
            self.assertEqual(chunk.block(0, 0, 0), (1, 2))
        self.assertIsNone(region.read_chunk(1, 0))

    def test_negative_coordinates(self):
        self.assertEqual(r.chunk_to_region(-1, -1), (-1, -1, 31, 31))
        self.assertEqual(r.chunk_to_region(-32, -33), (-1, -2, 0, 31))
        self.assertEqual(r.chunk_to_region(-64, 63), (-2, 1, 0, 31))
        self.assertEqual(r.chunk_to_region(31, 32), (0, 1, 31, 0))
        data = build_region({(31, 31): record(stone_chunk(-1, -1)), (0, 31): record(stone_chunk(-32, -1))})
        region = open_region(data, -1, -1)
        self.assertEqual((region.read_chunk(31, 31).x, region.read_chunk(31, 31).z), (-1, -1))
        self.assertEqual(region.read_chunk(0, 31).x, -32)
        other = open_region(build_region({(0, 31): record(stone_chunk(-64, 63))}), -2, 1)
        self.assertEqual((other.read_chunk(0, 31).x, other.read_chunk(0, 31).z), (-64, 63))

    def test_position_mismatch(self):
        region = open_region(build_region({(1, 1): record(stone_chunk(0, 0))}))
        with self.assertRaisesRegex(FormatError, "claims position"):
            region.read_chunk(1, 1)

    def test_local_index_bounds(self):
        with self.assertRaises(IndexError):
            open_region(b"").read_chunk(32, 0)

    def test_region_name(self):
        self.assertEqual(r.region_coords_from_name("world/region/r.-1.2.mca"), (-1, 2))
        for bad in ("r.1.mca", "r.a.b.mca", "r.1.2.mcr"):
            with self.subTest(bad), self.assertRaises(FormatError):
                r.region_coords_from_name(bad)
        with self.assertRaises(ValueError):
            r.RegionFile(io.BytesIO(b""))

    def test_corrupt_records(self):
        pad = lambda b: b + bytes(4096 - len(b))  # noqa: E731
        good = record(stone_chunk(0, 0))
        cases = {
            "offset in header": (raw_region({(0, 0): (1, 1)}, pad(good)), FormatError),
            "zero sectors": (raw_region({(0, 0): (2, 0)}, pad(good)), FormatError),
            "beyond eof": (raw_region({(0, 0): (50, 1)}), FormatError),
            "zero length": (raw_region({(0, 0): (2, 1)}, pad(struct.pack(">iB", 0, 2))), FormatError),
            "length > sectors": (raw_region({(0, 0): (2, 1)}, pad(struct.pack(">iB", 5000, 2))), FormatError),
            "truncated payload": (raw_region({(0, 0): (2, 1)}, struct.pack(">iB", 100, 2) + b"x" * 10),
                                  FormatError),
            "truncated file": (build_region({(0, 0): good})[: 8192 + len(good) - 3], FormatError),
            "uncompressed": (raw_region({(0, 0): (2, 1)}, pad(good[:4] + b"\x03" + good[5:])),
                             UnsupportedFormatError),
            "external mcc": (raw_region({(0, 0): (2, 1)}, pad(good[:4] + b"\x82" + good[5:])),
                             UnsupportedFormatError),
            "unknown compression": (raw_region({(0, 0): (2, 1)}, pad(good[:4] + b"\x09" + good[5:])),
                                    FormatError),
            "corrupt zlib": (build_region({(0, 0): struct.pack(">iB", 9, 2) + b"\x78\x9cgarbag"}), FormatError),
            "corrupt nbt": (build_region({(0, 0): record(stone_chunk(0, 0)[:-7])}), FormatError),
        }
        for label, (data, error) in cases.items():
            with self.subTest(label), self.assertRaises(error):
                open_region(data).read_chunk(0, 0)

    def test_bomb_in_region(self):
        region = open_region(build_region({(0, 0): record(bytes(5_000_000))}), limits=Limits(max_decompressed=100_000))
        with self.assertRaisesRegex(FormatError, "exceeds"):
            region.read_chunk(0, 0)

    def test_corrupt_chunk_does_not_hide_others(self):
        data = build_region({(0, 0): struct.pack(">iB", 5, 2) + b"junk", (1, 0): record(stone_chunk(1, 0))})
        region = open_region(data)
        with self.assertRaises(FormatError):
            region.read_chunk(0, 0)
        self.assertEqual(region.read_chunk(1, 0).block(0, 0, 0), (1, 2))


if __name__ == "__main__":
    unittest.main()
