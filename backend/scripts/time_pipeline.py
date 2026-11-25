"""
Performance test: time each stage of the pipeline for one RFP and a few vendors.

It uploads a fresh copy of one RFP package, then the chosen vendor responses one
at a time (nothing else should be running), and polls the task every second to
record when each stage starts:

  RFP:     upload -> ready (parsing + indexing of the RFP package)
  vendor:  upload -> started -> parsing/indexing (5-75%) -> scoring (80%) -> done

Usage (from backend/, stack running, RFP_EMAIL / RFP_PASSWORD set):

  python -m scripts.time_pipeline --dataset "<v2 path>" --rfp RFP-IOT-2026 \
         --vendors Nodexa "Quantel Connect" Simvera

Results are printed as a table and saved to results/performance/<rfp>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from scripts.run_dataset_evaluation import Api, LEVEL_SCALE, vendor_zips
from scripts.evaluate_against_ground_truth import _norm


def poll(api: Api, task_id: str, timeout: int = 3600) -> dict:
    """Poll a task every second; return timestamps of stage changes."""
    t0 = time.time()
    marks = {"uploaded": t0}
    while True:
        t = api.call("GET", f"/api/v1/tasks/{task_id}").json()
        now = time.time()
        status, prog = t.get("status"), t.get("progress") or 0
        stage = ((t.get("result") or {}).get("stage") or "") if isinstance(t.get("result"), dict) else ""
        if status == "running" and "started" not in marks:
            marks["started"] = now
        if stage.startswith("parsing") and "parsing" not in marks:
            marks["parsing"] = now
        if (stage == "scoring" or prog >= 80) and "scoring" not in marks and status == "running":
            marks["scoring"] = now
        if status in ("done", "error", "failed"):
            marks["done"] = now
            marks["status"] = status
            marks["error"] = t.get("error")
            return marks
        if now - t0 > timeout:
            marks["status"] = "timeout"
            return marks
        time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--rfp", default="RFP-IOT-2026")
    ap.add_argument("--vendors", nargs="+", help="vendor names (default: first 3)")
    ap.add_argument("--out", default="results/performance")
    args = ap.parse_args()

    email, password = os.environ.get("RFP_EMAIL"), os.environ.get("RFP_PASSWORD")
    if not email or not password:
        print("Set RFP_EMAIL and RFP_PASSWORD.", file=sys.stderr)
        return 2
    api = Api(args.api, email, password)
    rfp_dir = Path(args.dataset) / args.rfp
    pkg = rfp_dir / f"{args.rfp}_package.zip"

    # RFP package
    r = api.call("POST", "/api/v1/rfp",
                 data={"title": f"{args.rfp} performance test", "issuing_company": "The Client",
                       "compliance_levels": json.dumps(LEVEL_SCALE)},
                 _files_factory=lambda: {"file": (pkg.name, pkg.read_bytes(), "application/zip")})
    r.raise_for_status()
    rfp_id = r.json()["rfp_id"]
    m = poll(api, r.json()["task_id"])
    rfp_secs = m["done"] - m["uploaded"]
    print(f"RFP package {args.rfp}: {rfp_secs:.1f} s ({m['status']})")
    api.call("POST", f"/api/v1/rfp/{rfp_id}/rubric/lock", json={})

    zips = dict(vendor_zips(rfp_dir))
    names = args.vendors or list(zips)[:3]
    wanted = {_norm(n) for n in names}
    rows = []
    for name, z in zips.items():
        if _norm(name) not in wanted:
            continue
        size_mb = z.stat().st_size / 1e6
        r = api.call("POST", f"/api/v1/rfp/{rfp_id}/vendor",
                     data={"vendor_name": name, "acknowledge_unlocked_rubric": "true"},
                     _files_factory=lambda z=z: {"file": (z.name, z.read_bytes(), "application/zip")})
        r.raise_for_status()
        m = poll(api, r.json()["task_id"])
        start = m.get("started", m["uploaded"])
        scoring = m.get("scoring", m["done"])
        row = {
            "vendor": name, "zip_mb": round(size_mb, 2), "status": m["status"],
            "queue_s": round(start - m["uploaded"], 1),
            "parse_index_s": round(scoring - start, 1),
            "scoring_s": round(m["done"] - scoring, 1),
            "total_s": round(m["done"] - m["uploaded"], 1),
        }
        rows.append(row)
        print(f"{name}: {row}")
        time.sleep(5)

    print(f"\n| Vendor | Zip (MB) | Queue (s) | Parsing + indexing (s) | Scoring (s) | Total (s) |")
    print("|---|---|---|---|---|---|")
    for x in rows:
        print(f"| {x['vendor']} | {x['zip_mb']} | {x['queue_s']} | {x['parse_index_s']} | {x['scoring_s']} | {x['total_s']} |")
    ok = [x for x in rows if x["status"] == "done"]
    if ok:
        avg = lambda k: sum(x[k] for x in ok) / len(ok)
        print(f"| **Mean** | | {avg('queue_s'):.1f} | {avg('parse_index_s'):.1f} | {avg('scoring_s'):.1f} | {avg('total_s'):.1f} |")
    print(f"\nRFP package ingestion: {rfp_secs:.1f} s")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.rfp}.json").write_text(json.dumps(
        {"rfp": args.rfp, "rfp_id": rfp_id, "rfp_ingestion_s": round(rfp_secs, 1),
         "vendors": rows, "at": datetime.now().isoformat()}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
