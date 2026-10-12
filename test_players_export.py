"""Tests for players_export.py using synthetic gzipped NBT player files (stdlib only)."""
from __future__ import annotations

import gzip
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import players_export as pe  # noqa: E402

U1 = "77ecff94-3a90-49f5-b73e-8533a26d6629"
U2 = "2e2af13e-c8e3-4be2-9849-3a3956d49c87"
U3 = "e5c57d78-b912-47b5-af7b-38eadf52371c"


# ---- tiny NBT encoder: values are (tag, payload) tuples
def _s(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _payload(tag: int, value) -> bytes:
    if tag == 3:
        return struct.pack(">i", value)
    if tag == 5:
        return struct.pack(">f", value)
    if tag == 6:
        return struct.pack(">d", value)
    if tag == 8:
        return _s(value)
    if tag == 9:
        item, items = value
        return struct.pack(">bi", item, len(items)) + b"".join(_payload(item, v) for v in items)
    if tag == 10:
        return b"".join(struct.pack(">b", t) + _s(k) + _payload(t, v) for k, (t, v) in value.items()) + b"\x00"
    raise ValueError(tag)


def nbt_root(fields: dict) -> bytes:
    return b"\x0a" + _s("") + _payload(10, fields)


def player_dat(x=1.5, y=64.0, z=-20.25, yaw=90.0, dim=0, extra=None) -> bytes:
    fields = {
        "Pos": (9, (6, [x, y, z])),
        "Rotation": (9, (5, [yaw, 10.0])),
        "Dimension": (3, dim),
        "SomethingElse": (8, "x"),
    }
    if extra:
        fields.update(extra)
    return gzip.compress(nbt_root(fields))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.world = os.path.join(self.root, "world")
        self.pd = os.path.join(self.world, "playerdata")
        os.makedirs(self.pd)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name: str, data: bytes, mtime: float | None = None):
        path = os.path.join(self.pd, name)
        with open(path, "wb") as fh:
            fh.write(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class ParseTests(Base):
    def test_parse_fields(self):
        got = pe.parse_player_nbt(player_dat(x=-100.75, y=70.5, z=33.0, yaw=-45.0, dim=-1))
        self.assertEqual(got, {"dimension": -1, "x": -100.75, "y": 70.5, "z": 33.0, "yaw": -45.0})

    def test_missing_pos_rejected(self):
        raw = gzip.compress(nbt_root({"Dimension": (3, 0)}))
        with self.assertRaises(pe.PlayerParseError):
            pe.parse_player_nbt(raw)

    def test_not_gzip_rejected(self):
        with self.assertRaises(pe.PlayerParseError):
            pe.parse_player_nbt(b"not gzip at all")

    def test_nan_rejected(self):
        with self.assertRaises(pe.PlayerParseError):
            pe.parse_player_nbt(player_dat(x=float("nan")))


class CollectTests(Base):
    def test_collect_names_mtime_and_skip(self):
        self.put(U1 + ".dat", player_dat(x=243.9, y=112.4, z=641.5, dim=7), mtime=1_790_000_000)
        self.put(U2 + ".dat", b"\x1f\x8b broken")
        self.put(U3 + ".dat", player_dat(dim=0))
        self.put(U1 + ".dat.zaloha-20260820", player_dat(x=0.0))  # backup: ignored
        self.put("Dejw_Proste.baub", b"junk")  # other mod files: ignored
        with open(os.path.join(self.root, "usercache.json"), "w", encoding="utf-8") as fh:
            json.dump([{"name": "eXibionista2", "uuid": U1.upper(), "expiresOn": "x"}], fh)
        with open(os.path.join(self.root, "usernamecache.json"), "w", encoding="utf-8") as fh:
            json.dump({U1: "ForgeName"}, fh)

        players, skipped = pe.collect_players(self.world, self.root)
        self.assertEqual([p["uuid"] for p in players], sorted([U1, U3]))
        by = {p["uuid"]: p for p in players}
        p1 = by[U1]
        self.assertEqual(p1["name"], "ForgeName")  # Forge cache wins over vanilla
        self.assertEqual(p1["dimension"], 7)
        self.assertAlmostEqual(p1["x"], 243.9)
        self.assertEqual(p1["lastSeen"], "2026-09-21T14:13:20Z")
        self.assertIsNone(p1["online"])
        self.assertIsNone(by[U3]["name"])
        self.assertEqual([s[0] for s in skipped], [U2 + ".dat"])

    def test_vanilla_cache_used_when_no_forge_cache(self):
        self.put(U1 + ".dat", player_dat())
        with open(os.path.join(self.root, "usercache.json"), "w", encoding="utf-8") as fh:
            json.dump([{"name": "eXibionista2", "uuid": U1}], fh)
        players, _ = pe.collect_players(self.world, self.root)
        self.assertEqual(players[0]["name"], "eXibionista2")

    def test_online_from_log(self):
        self.put(U1 + ".dat", player_dat())
        self.put(U2 + ".dat", player_dat())
        self.put(U3 + ".dat", player_dat())
        with open(os.path.join(self.root, "usernamecache.json"), "w", encoding="utf-8") as fh:
            json.dump({U1: "Alpha", U2: "Beta", U3: "Gamma"}, fh)
        log = os.path.join(self.root, "latest.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("[01:00:00] [Server thread/INFO]: Gamma joined the game\n"
                     "[02:00:00] [Server thread/INFO]: Starting minecraft server version 1.7.10\n"
                     "[03:00:00] [Server thread/INFO]: Alpha joined the game\n"
                     "[03:00:01] [Server thread/INFO]: Beta joined the game\n"
                     "[03:10:00] [Server thread/INFO]: Beta lost connection: TranslatableComponent{...}\n"
                     "[03:10:00] [Server thread/INFO]: Beta left the game\n")
        players, _ = pe.collect_players(self.world, self.root, log)
        self.assertEqual({p["name"]: p["online"] for p in players}, {"Alpha": True, "Beta": False, "Gamma": False})

    def test_source_is_copied_before_parsing(self):
        src = self.put(U1 + ".dat", player_dat())
        seen = []
        real = pe.parse_player_nbt

        def spy(raw):
            seen.append(raw)
            return real(raw)

        opened = []
        real_open = open

        def tracking_open(path, mode="r", *a, **kw):
            opened.append((os.path.abspath(path), mode))
            return real_open(path, mode, *a, **kw)

        with mock.patch.object(pe, "parse_player_nbt", spy), mock.patch("builtins.open", tracking_open):
            pe.collect_players(self.world, None)
        self.assertEqual(len(seen), 1)
        # the source is only opened read-only, and the parsed bytes came from a different (temp) path
        self.assertTrue(all("w" not in m and "a" not in m for p, m in opened if p == os.path.abspath(src)))
        self.assertTrue(any(p != os.path.abspath(src) and p.endswith("player.dat") for p, _ in opened))


class OutputTests(Base):
    def test_cli_writes_document(self):
        self.put(U1 + ".dat", player_dat(dim=-1))
        out = os.path.join(self.root, "out", "players.json")
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = pe.main(["--world", self.world, "--server-dir", self.root, "--out", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["format"], "UIE_PLAYERS1")
        self.assertIsNone(doc["onlineSource"])
        self.assertRegex(doc["generated"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(len(doc["players"]), 1)
        self.assertEqual(doc["players"][0]["dimension"], -1)
        self.assertEqual([f for f in os.listdir(os.path.dirname(out))], ["players.json"])

    def test_atomic_write_keeps_old_file_on_failure(self):
        out = os.path.join(self.root, "players.json")
        pe.write_atomic(out, {"format": "UIE_PLAYERS1", "players": []})
        with open(out, encoding="utf-8") as fh:
            before = fh.read()
        with mock.patch.object(pe.json, "dump", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                pe.write_atomic(out, {"format": "UIE_PLAYERS1", "players": [1]})
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        self.assertFalse([f for f in os.listdir(self.root) if f.endswith(".tmp")])

    def test_missing_playerdata_returns_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = pe.main(["--world", os.path.join(self.root, "nope"), "--out", os.path.join(self.root, "p.json")])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(self.root, "p.json")))


if __name__ == "__main__":
    unittest.main()
