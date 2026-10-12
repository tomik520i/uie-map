"""Export player positions of a Forge 1.7.10 world to players.json (UIE_PLAYERS1).

Read-only towards the game: every <world>/playerdata/<uuid>.dat is first copied
to a private temporary file (the server rewrites these files on save) and then
parsed with the NBT reader from reader.py. Files that fail to parse are skipped
and reported on stderr; the output is written atomically (temp + os.replace).

Names come from <server-dir>/usercache.json (vanilla list) and
<server-dir>/usernamecache.json (Forge uuid -> name map). Online status is
optional: with --log (normally <server-dir>/logs/latest.log) join/leave lines of
the current server run decide it, otherwise "online" is null. IP addresses in
the log are never read into the output.

Usage:
  python players_export.py --world WORLD --server-dir SERVER --out players.json [--log LOG]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reader import COMPRESSION_GZIP, FormatError, decompress, parse_nbt  # noqa: E402

FORMAT = "UIE_PLAYERS1"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_DAT_BYTES = 8 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
# Vanilla 1.7.10 server log lines; "logged in with entity id" carries the IP, so it is not used.
_LOG_START = re.compile(r"Starting minecraft server version")
_LOG_JOIN = re.compile(r"\]: (\S+) joined the game\s*$")
_LOG_LEAVE = re.compile(r"\]: (\S+) (?:left the game|lost connection:)")


class PlayerParseError(ValueError):
    pass


def iso_utc(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finite(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlayerParseError(f"{what} is not a number")
    value = float(value)
    if not math.isfinite(value):
        raise PlayerParseError(f"{what} is not finite")
    return value


def parse_player_nbt(raw: bytes) -> dict:
    """Decode gzipped player NBT bytes into {dimension, x, y, z, yaw}."""
    try:
        _, root = parse_nbt(decompress(raw, COMPRESSION_GZIP))
    except FormatError as exc:
        raise PlayerParseError(str(exc)) from None
    pos = root.get("Pos")
    if not isinstance(pos, list) or len(pos) != 3:
        raise PlayerParseError("missing Pos list of 3 doubles")
    x, y, z = (_finite(v, "Pos") for v in pos)
    rot = root.get("Rotation")
    yaw = 0.0
    if isinstance(rot, list) and len(rot) >= 1:
        yaw = _finite(rot[0], "Rotation")
    dim = root.get("Dimension", 0)
    if isinstance(dim, bool) or not isinstance(dim, int):
        raise PlayerParseError("Dimension is not an int")
    return {"dimension": dim, "x": x, "y": y, "z": z, "yaw": yaw}


def _read_json(path: str):
    try:
        if os.path.getsize(path) > MAX_CACHE_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_names(server_dir: str | None) -> dict:
    """uuid (lowercase) -> name from usercache.json and usernamecache.json; missing files are fine."""
    names: dict[str, str] = {}
    if not server_dir:
        return names
    forge = _read_json(os.path.join(server_dir, "usernamecache.json"))
    if isinstance(forge, dict):
        for uuid, name in forge.items():
            if isinstance(uuid, str) and isinstance(name, str) and name:
                names[uuid.lower()] = name
    vanilla = _read_json(os.path.join(server_dir, "usercache.json"))
    if isinstance(vanilla, list):
        for entry in vanilla:
            if isinstance(entry, dict) and isinstance(entry.get("uuid"), str) and isinstance(entry.get("name"), str):
                names.setdefault(entry["uuid"].lower(), entry["name"])
    return names


def online_names_from_log(log_path: str) -> set:
    """Names online at the end of the log (current server run only)."""
    online: set[str] = set()
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if _LOG_START.search(line):
                online.clear()
                continue
            m = _LOG_JOIN.search(line)
            if m:
                online.add(m.group(1))
                continue
            m = _LOG_LEAVE.search(line)
            if m:
                online.discard(m.group(1))
    return online


def _copy_private(src: str, tmpdir: str) -> str:
    dst = os.path.join(tmpdir, "player.dat")
    with open(src, "rb") as fin:
        data = fin.read(MAX_DAT_BYTES + 1)
    if len(data) > MAX_DAT_BYTES:
        raise PlayerParseError(f"file larger than {MAX_DAT_BYTES} bytes")
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fout:
        fout.write(data)
    return dst


def collect_players(world: str, server_dir: str | None = None, log_path: str | None = None):
    """Return (players, skipped) where skipped is a list of (filename, reason)."""
    playerdata = os.path.join(world, "playerdata")
    names = load_names(server_dir)
    online = None
    if log_path:
        online = online_names_from_log(log_path)
    players, skipped = [], []
    entries = sorted(os.listdir(playerdata))
    with tempfile.TemporaryDirectory(prefix="uie-players-") as tmpdir:
        for fname in entries:
            # Only exact <uuid>.dat; backups such as <uuid>.dat.zaloha-* or .dat_old are ignored.
            if not fname.endswith(".dat") or not UUID_RE.match(fname[:-4]):
                continue
            src = os.path.join(playerdata, fname)
            try:
                mtime = os.stat(src).st_mtime
                copy = _copy_private(src, tmpdir)
                with open(copy, "rb") as fh:
                    data = parse_player_nbt(fh.read())
            except (OSError, PlayerParseError) as exc:
                skipped.append((fname, str(exc)))
                continue
            uuid = fname[:-4].lower()
            name = names.get(uuid)
            player = {"uuid": uuid, "name": name, **data, "lastSeen": iso_utc(mtime)}
            player["online"] = None if online is None else bool(name and name in online)
            players.append(player)
    return players, skipped


def build_document(players: list, generated: float | None = None, online_source: str | None = None) -> dict:
    return {
        "format": FORMAT,
        "generated": iso_utc(time.time() if generated is None else generated),
        "onlineSource": online_source,
        "players": players,
    }


def write_atomic(path: str, doc: dict) -> None:
    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".players-", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--world", required=True, help="world directory containing playerdata/")
    ap.add_argument("--server-dir", help="server directory with usercache.json / usernamecache.json")
    ap.add_argument("--log", help="server log (logs/latest.log) for online status; omitted -> online is null")
    ap.add_argument("--out", required=True, help="output players.json")
    args = ap.parse_args(argv)
    try:
        players, skipped = collect_players(args.world, args.server_dir, args.log)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    write_atomic(args.out, build_document(players, online_source="log" if args.log else None))
    for fname, reason in skipped:
        print(f"skipped {fname}: {reason}", file=sys.stderr)
    print(f"players: {len(players)} written, {len(skipped)} skipped -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
