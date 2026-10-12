"""Offline replacement of the client's WarmBatch: render every job of a prepared plan without Minecraft.

    python3 offline_batch.py --plan PLAN_DIR --catalog-dir CATALOG_DIR --reports OUT_DIR [--caves]

For each job of plan.json (same order as the client): render inputs/<id>/snapshot.bin with offline_render into
results/<id>/geometry/ and only then write results/<id>/receipt.json (UIE_WARM_RESULT1 contract the worker and
verify_rendered_plan check: state captured, id, snapshotSha256, manifestSha256, elapsedSeconds) plus the renderer
identity. Per-job reports (timings, fallbacks with coordinates, peak RSS) go to OUT_DIR, never into the plan's
result directory (the verifier allows only receipt.json and geometry/ there). A failing job writes
worker-status.json {state: fatal} and exits non-zero, so the worker records a transient failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from offline_render import RENDERER, Renderer


def _write_atomic(path: Path, raw: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def run(plan: Path, catalog_dir: Path, reports: Path, caves: bool) -> int:
    plan_doc = json.loads((plan / "plan.json").read_bytes())
    jobs = plan_doc["jobs"]
    renderer = Renderer(catalog_dir, caves=caves)
    reports.mkdir(parents=True, exist_ok=True)
    total = len(jobs)
    for n, job in enumerate(jobs):
        started = time.perf_counter()
        result = plan / "results" / job["id"]
        geometry = result / "geometry"
        try:
            report = renderer.render(plan / job["input"], geometry, write_report=False)
            if report["snapshotSha256"] != job["snapshotSha256"]:
                raise RuntimeError("rendered input does not match the job's snapshot hash")
        except Exception as exc:   # report and stop: the worker sees a fatal status and retries in a new attempt
            _write_atomic(plan / "worker-status.json",
                          json.dumps({"state": "fatal", "job": job["id"], "error": str(exc)[:500]}).encode())
            print(f"offline_batch: job {job['id']} failed: {exc}", file=sys.stderr, flush=True)
            return 2
        manifest_sha = hashlib.sha256((geometry / "manifest.json").read_bytes()).hexdigest()
        elapsed = round(time.perf_counter() - started, 3)
        (reports / f"{job['id']}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        receipt = {"format": "UIE_WARM_RESULT1", "id": job["id"], "snapshotSha256": job["snapshotSha256"],
                   "state": "captured", "completed": n + 1, "total": total, "manifestSha256": manifest_sha,
                   "elapsedSeconds": elapsed, "renderer": {"name": RENDERER, "identity": renderer.identity}}
        _write_atomic(result / "receipt.json", json.dumps(receipt, separators=(",", ":")).encode())
        print(f"offline_batch: {n + 1}/{total} {job['id'][:12]} {report['chunks']} chunks in {elapsed}s", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--catalog-dir", required=True, type=Path)
    ap.add_argument("--reports", required=True, type=Path)
    ap.add_argument("--caves", action="store_true")
    a = ap.parse_args(argv)
    return run(a.plan.resolve(), a.catalog_dir.resolve(), a.reports.resolve(), a.caves)


if __name__ == "__main__":
    sys.exit(main())
