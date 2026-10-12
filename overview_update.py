"""Automatic whole-world overview refresh (run periodically, e.g. hourly, at low priority). Stdlib only.

    python3 overview_update.py --world /home/minecraft/uie/world --state DIR --colors colors.json --store STORE

1. Stage A only for regions whose size/mtime (or skip list) changed since the last run (state/surface).
2. If anything changed (or no build is published yet): rebuild all tiles into a private temp directory
   (about a minute for the whole world), publish them with compare-and-swap (unchanged tiles are hard links to
   the previous build), delete the temp tiles and prune old builds (keep the newest 3, never the current).
3. Write state/status.json (lastRunAt, changed regions, build, elapsed) - also on "nothing changed".

state/status.json (UIE_OVERVIEW_UPDATE1, written atomically during the whole attempt):
  state running | done | error, stage surface | tiles | publish | finished (on error: the stage that failed),
  startedAt / updatedAt (heartbeat, at least every heartbeatSeconds while running) / finishedAt: epoch seconds,
  error (<= 300 chars, only for state error), known counts as soon as they are known (changedRegions,
  failedRegions, surfaceExitCode; levels/tiles after publication). No percentage is invented.
  done: the previous fields (published, build, reason | levels, tiles, pruned, elapsedSeconds) plus complete
  (no failed region and surface exit code 0) and a warning when failedRegions > 0 (partial overview). With no
  changed region but failed regions / a non-zero exit code, reason says so instead of "no region changed".
  currentBuild: the build the store serves - null until read in this attempt, the new build as soon as
  po.publish succeeded (also when cleanup or pruning fail afterwards: the error keeps published, build,
  currentBuild and lastPublication of the new build).
  The same document is mirrored atomically to store/status.json for the public endpoint (state file first;
  the mirror never receives a document the state file does not hold, it may lag after a mirror write error).
  lastSuccess {finishedAt, build, complete}: the last run that reached done (carried over failed attempts).
  lastPublication {publishedAt, build}: the last run that actually published a new build (carried over runs
  that published nothing or failed). Values derived from an older status without "state" keep publishedAt /
  finishedAt null and carry lastRunAt (ISO) instead.

A file lock prevents overlapping runs. The world is only read (stable copies). Never touches the game.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import overview_surface as ovs
import overview_tiles as ovt
import publish_overview as po

FORMAT = "UIE_OVERVIEW_UPDATE1"
HEARTBEAT_SECONDS = 30
HEARTBEAT_THREAD = "overview-heartbeat"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, doc: dict):
    """Readers see either the previous or the new complete file, never a partial one."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(doc, indent=1))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _previous_status(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) and doc.get("format") == FORMAT else {}


def _carried(previous: dict):
    """(lastSuccess, lastPublication) of the previous status; the older writer only wrote successful runs."""
    success, publication = previous.get("lastSuccess"), previous.get("lastPublication")
    legacy = "state" not in previous and previous.get("build") is not None
    if not isinstance(success, dict):
        success = ({"finishedAt": None, "lastRunAt": previous.get("lastRunAt"), "build": previous["build"],
                    "complete": previous.get("failedRegions") == 0} if legacy else None)
    if not isinstance(publication, dict):
        publication = ({"publishedAt": None, "lastRunAt": previous.get("lastRunAt"), "build": previous["build"]}
                       if legacy and previous.get("published") else None)
    return success, publication


class _StatusWriter:
    """Atomic status writes from the run and from one heartbeat thread, serialized by a lock. After the final
    write nothing (no late heartbeat) can overwrite the status.

    Every document goes first to state/status.json and then, unchanged, to the public mirror store/status.json.
    The mirror only ever receives documents the state file already holds: if the state write fails nothing is
    mirrored; if the mirror write fails the mirror keeps its previous document (it may lag, never invent)."""

    def __init__(self, paths, doc: dict, interval: float, clock):
        self.paths, self.doc, self.interval, self.clock = list(paths), doc, interval, clock
        self._lock = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        self._thread = None

    def write(self, final: bool = False, **changes):
        with self._lock:
            if self._closed:
                return
            self.doc.update(changes)
            self.doc["updatedAt"] = self.clock()
            for path in self.paths:          # os.replace replaces a symlink itself, never writes through it
                _atomic_write_json(path, self.doc)
            if final:
                self._closed = True

    def _beat(self):
        while not self._stop.wait(self.interval):
            try:
                self.write()
            except OSError:
                pass                  # one failed heartbeat must not stop the update; the next one retries

    def start(self):
        self._thread = threading.Thread(target=self._beat, name=HEARTBEAT_THREAD, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 10)


def update(world: Path, state: Path, colors: Path, store: Path, workers: int = 8, keep: int = 3, *,
           heartbeat_seconds: float = HEARTBEAT_SECONDS, clock=time.time) -> dict:
    state.mkdir(parents=True, exist_ok=True)
    store.mkdir(parents=True, exist_ok=True)
    surface = state / "surface"
    status_path = state / "status.json"
    started = time.monotonic()
    last_success, last_publication = _carried(_previous_status(status_path))
    # currentBuild is unknown (None) until the store has been read inside the attempt: a broken store pointer is
    # reported as this attempt's error instead of leaving the previous success in place.
    writer = _StatusWriter([status_path, store / "status.json"], {
        "format": FORMAT, "state": "running", "stage": "surface", "startedAt": clock(), "updatedAt": None,
        "finishedAt": None, "heartbeatSeconds": heartbeat_seconds, "lastRunAt": _now(), "currentBuild": None,
        "lastSuccess": last_success, "lastPublication": last_publication, "error": None}, heartbeat_seconds, clock)

    def fail(error, stage):
        writer.stop()
        try:
            writer.write(final=True, state="error", stage=stage, error=f"{type(error).__name__}: {error}"[:300],
                         finishedAt=clock(), elapsedSeconds=round(time.monotonic() - started, 1))
        except OSError:
            pass                      # the original error is more important than a failed status write

    try:
        writer.write()
    except OSError as error:          # e.g. the public mirror cannot be written: the state file says why
        fail(error, "surface")
        raise
    writer.start()
    stage = "surface"
    try:
        writer.write(currentBuild=po.current_build(store) if (store / "current.json").exists() else None)
        code = ovs.main(["--world", str(world), "--out", str(surface), "--workers", str(workers), "--changed-only"])
        run = json.loads((surface / "run.json").read_text())
        changed = run["ok"]
        status = {"format": FORMAT, "lastRunAt": writer.doc["lastRunAt"], "changedRegions": changed,
                  "failedRegions": len(run["failed"]), "surfaceExitCode": code, "chunkErrors": run.get("chunkErrors", 0)}
        writer.write(changedRegions=changed, failedRegions=status["failedRegions"], surfaceExitCode=code, chunkErrors=status["chunkErrors"])
        current = po.current_build(store) if (store / "current.json").exists() else None
        if changed == 0 and current is not None:
            if status["failedRegions"] or code or status["chunkErrors"]:
                reason = (f"no region rendered successfully: {status['failedRegions']} regions failed (surface exit "
                          f"code {code}); the previous build is kept")
            else:
                reason = "no region changed"
            status.update({"published": False, "build": current, "reason": reason})
        else:
            stage = "tiles"
            writer.write(stage=stage)
            tiles = state / f"tiles-{int(time.time())}"
            try:
                index = ovt.build(surface, colors, tiles, workers)
                stage = "publish"
                writer.write(stage=stage)
                pointer = po.publish(tiles, store, current)
                # The new build is served from now on: record it before cleanup/prune can fail.
                published = pointer["build"] != current
                if published:
                    last_publication = {"publishedAt": clock(), "build": pointer["build"]}
                writer.write(published=published, build=pointer["build"], currentBuild=pointer["build"],
                             lastPublication=last_publication, levels=index["levels"], tiles=pointer["tiles"])
            finally:
                if tiles.exists():
                    shutil.rmtree(tiles)
            status.update({"published": published, "build": pointer["build"],
                           "levels": index["levels"], "tiles": pointer["tiles"],
                           "pruned": po.prune(store, keep)})
        status["elapsedSeconds"] = round(time.monotonic() - started, 1)
    except BaseException as error:
        fail(error, stage)
        raise
    writer.stop()
    finished = clock()
    complete = status["failedRegions"] == 0 and status["surfaceExitCode"] == 0 and status["chunkErrors"] == 0
    final = {**status, "state": "done", "stage": "finished", "finishedAt": finished, "complete": complete,
             "currentBuild": status["build"], "lastPublication": last_publication, "error": None,
             "lastSuccess": {"finishedAt": finished, "build": status["build"], "complete": complete}}
    if not complete:
        final["warning"] = (f"{status['failedRegions']} regions failed in the surface stage (exit code "
                            f"{status['surfaceExitCode']}), {status['chunkErrors']} chunk read errors; the overview may be outdated or missing")
    writer.write(final=True, **final)
    return dict(writer.doc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True, type=Path)
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--colors", required=True, type=Path)
    ap.add_argument("--store", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    import fcntl   # Linux; update() itself is portable (tests run it on Windows too)
    a.state.mkdir(parents=True, exist_ok=True)
    with open(a.state / "update.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"skipped": "another overview update is running"}))
            return 0
        status = update(a.world.resolve(), a.state.resolve(), a.colors.resolve(), a.store.resolve(), a.workers)
    print(json.dumps(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
