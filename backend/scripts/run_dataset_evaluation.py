"""
Run the whole evaluation on the technical RFP dataset, unattended.

For every RFP of the dataset it:
  1. uploads the RFP package (RFP + compliance matrix + evaluation matrix),
  2. waits for ingestion, then locks the rubric (the evaluation grid),
  3. uploads the five vendor responses and waits until all are scored,
  4. downloads the results (same as `evaluate_against_ground_truth fetch`),
  5. computes the metrics (same as `evaluate_against_ground_truth metrics`)
     and saves them as metrics.md, metrics.tex and metrics.csv.

Progress is saved in <out>/<tag>/state.json, so if the run stops (network,
rate limit, laptop sleeps) you can launch the same command again and it
continues where it stopped instead of uploading everything again.

Usage (from backend/, with the stack running):

  export RFP_EMAIL=admin@...  RFP_PASSWORD=...
  python -m scripts.run_dataset_evaluation run --dataset ../technical_rfp_dataset --tag run1

  # only some RFPs
  python -m scripts.run_dataset_evaluation run --dataset ../technical_rfp_dataset --tag run1 \
         --rfps RFP-BIL-2026 RFP-CLD-2026

  # consistency: run one RFP a second time, then compare the two runs
  python -m scripts.run_dataset_evaluation run --dataset ../technical_rfp_dataset --tag run2 \
         --rfps RFP-BIL-2026 --vendors Docuspring "Folio Systems"
  python -m scripts.run_dataset_evaluation consistency \
         --a results/run1/RFP-BIL-2026 --b results/run2/RFP-BIL-2026

  # summary of all RFPs of a run (overall accuracy, kappa, timings)
  python -m scripts.run_dataset_evaluation summary --dataset ../technical_rfp_dataset --tag run1
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from argparse import Namespace
from datetime import datetime
from pathlib import Path

import httpx

from scripts.evaluate_against_ground_truth import (
    GT_LEVEL, LEVELS, _kappa, _macro_f1, _norm, _read_csv, cmd_fetch, cmd_metrics,
)

ALL_RFPS = ["RFP-CLD-2026", "RFP-SOC-2026", "RFP-BIL-2026", "RFP-IOT-2026", "RFP-CCS-2026"]
LEVEL_SCALE = [{"label": "Fully Compliant", "value": 5}, {"label": "Partially Compliant", "value": 3},
               {"label": "Roadmap Compliant", "value": 2}, {"label": "Non-Compliant", "value": 1}]
FINISHED = {"done", "error", "failed"}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── API client with re-login and rate-limit handling ──────────────────────────

class Api:
    def __init__(self, base: str, email: str, password: str):
        self.base, self.email, self.password = base.rstrip("/"), email, password
        self.c = httpx.Client(base_url=self.base, timeout=300)
        self.token = ""
        self.login()

    def login(self) -> None:
        r = self.c.post("/api/v1/auth/login", json={"email": self.email, "password": self.password})
        r.raise_for_status()
        self.token = r.json()["access_token"]
        self.c.headers["Authorization"] = f"Bearer {self.token}"

    def call(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(8):
            files = kw.pop("_files_factory", None)
            if files:
                kw["files"] = files()
                kw["_files_factory"] = files
            try:
                r = self.c.request(method, url, **{k: v for k, v in kw.items() if k != "_files_factory"})
            except httpx.TransportError as e:
                log(f"  network error ({e}); retrying in 15 s")
                time.sleep(15)
                continue
            if r.status_code == 401:
                self.login()
                continue
            if r.status_code == 429:
                wait = 20 + 10 * attempt
                log(f"  rate limited; waiting {wait} s")
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 3:
                log(f"  server error {r.status_code}; retrying in 20 s")
                time.sleep(20)
                continue
            return r
        raise RuntimeError(f"{method} {url} kept failing")

    def wait_task(self, task_id: str, label: str, timeout_s: int) -> dict:
        start, last = time.time(), None
        while True:
            t = self.call("GET", f"/api/v1/tasks/{task_id}").json()
            if (t.get("status"), t.get("progress")) != last:
                last = (t.get("status"), t.get("progress"))
                log(f"  {label}: {t.get('status')} {t.get('progress') or 0}%")
            if t.get("status") in FINISHED:
                return t
            if time.time() - start > timeout_s:
                log(f"  {label}: timeout after {timeout_s} s")
                return t
            time.sleep(10)


# ── state ─────────────────────────────────────────────────────────────────────

def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def vendor_zips(rfp_dir: Path) -> list[tuple[str, Path]]:
    out = []
    for z in sorted((rfp_dir / "responses").glob("*_response.zip")):
        out.append((z.name[: -len("_response.zip")].replace("_", " "), z))
    return out


# ── one RFP ───────────────────────────────────────────────────────────────────

def run_rfp(api: Api, dataset: Path, rfp: str, run_dir: Path, state: dict, state_path: Path, args) -> None:
    rfp_dir = dataset / rfp
    st = state.setdefault(rfp, {})

    # 1-2. RFP package
    if not st.get("rfp_id"):
        pkg = rfp_dir / f"{rfp}_package.zip"
        log(f"{rfp}: uploading package")
        st["rfp_uploaded_at"] = time.time()
        r = api.call("POST", "/api/v1/rfp",
                     data={"title": f"{rfp} Technical RFP", "issuing_company": "The Client",
                           "compliance_levels": json.dumps(LEVEL_SCALE)},
                     _files_factory=lambda: {"file": (pkg.name, pkg.read_bytes(), "application/zip")})
        r.raise_for_status()
        st.update(rfp_id=r.json()["rfp_id"], rfp_task=r.json()["task_id"])
        save_state(state_path, state)
    rfp_id = st["rfp_id"]

    if not st.get("rfp_ready"):
        t = api.wait_task(st["rfp_task"], f"{rfp} ingestion", args.timeout)
        if t.get("status") != "done":
            raise RuntimeError(f"{rfp}: ingestion ended with {t.get('status')}: {t.get('error')}")
        info = api.call("GET", f"/api/v1/rfp/{rfp_id}").json()
        log(f"{rfp}: ready, {info.get('requirement_count')} requirements")
        if not info.get("rubric_locked"):
            lk = api.call("POST", f"/api/v1/rfp/{rfp_id}/rubric/lock", json={})
            log(f"{rfp}: rubric lock -> {lk.status_code}")
        st.update(rfp_ready=True, rfp_ready_at=time.time(), requirement_count=info.get("requirement_count"))
        save_state(state_path, state)

    # 3. vendors
    vendors = st.setdefault("vendors", {})
    zips = dict(vendor_zips(rfp_dir))
    if args.vendors:
        wanted = {_norm(v) for v in args.vendors}
        zips = {n: z for n, z in zips.items() if _norm(n) in wanted}

    def wait_vendor(name: str) -> None:
        v = vendors[name]
        t = api.wait_task(v["task_id"], f"{rfp} / {name}", args.timeout)
        v.update(status=t.get("status"), error=t.get("error"), finished_at=time.time(),
                 seconds=round(time.time() - v["uploaded_at"]))
        save_state(state_path, state)
        if t.get("status") != "done":
            log(f"  ! {name} ended with {t.get('status')}: {t.get('error')}")
            return
        # A vendor can finish "done" while some requirements ended as
        # ScoringFailed (e.g. every key at its daily limit). Treat that as a
        # failure so the vendor is re-graded, instead of keeping failed answers.
        try:
            sc = api.call("GET", f"/api/v1/rfp/{rfp_id}/vendor/{v['response_id']}/score").json()
            failed = sum(1 for r in sc.get("requirement_scores", [])
                         if r.get("compliance_level") == "ScoringFailed")
        except Exception as e:
            log(f"  could not check ScoringFailed for {name}: {e}")
            failed = 0
        v["scoring_failed"] = failed
        if failed:
            v.update(status="error", error=f"{failed} requirements ended as ScoringFailed")
            log(f"  ! {name}: {failed} requirements ScoringFailed -> will be re-graded")
        save_state(state_path, state)

    # vendors that ended in error are re-graded on the same response (no new upload)
    for name, v in vendors.items():
        if v.get("status") in ("error", "failed") and v.get("retries", 0) < args.max_retries:
            log(f"{rfp}: retrying {name} (previous error: {str(v.get('error'))[:120]})")
            z = zips[name]
            r = api.call("POST", f"/api/v1/rfp/{rfp_id}/vendor/{v['response_id']}/retry",
                         data={"acknowledge_unlocked_rubric": "true"},
                         _files_factory=lambda z=z: {"file": (z.name, z.read_bytes(), "application/zip")})
            if r.status_code >= 400:
                log(f"  retry refused ({r.status_code}): {r.text[:200]}")
                continue
            v.update(task_id=r.json()["task_id"], status=None, error=None, uploaded_at=time.time(),
                     retries=v.get("retries", 0) + 1)
            save_state(state_path, state)
            if args.sequential:
                wait_vendor(name)
                time.sleep(args.upload_gap)

    for name, z in zips.items():
        if name in vendors:
            continue
        log(f"{rfp}: uploading {name}")
        r = api.call("POST", f"/api/v1/rfp/{rfp_id}/vendor",
                     data={"vendor_name": name, "acknowledge_unlocked_rubric": "true"},
                     _files_factory=lambda z=z: {"file": (z.name, z.read_bytes(), "application/zip")})
        r.raise_for_status()
        vendors[name] = {"response_id": r.json()["response_id"], "task_id": r.json()["task_id"],
                         "uploaded_at": time.time()}
        save_state(state_path, state)
        if args.sequential:
            wait_vendor(name)
        time.sleep(args.upload_gap)

    for name, v in vendors.items():
        if v.get("status") is None:
            wait_vendor(name)

    bad = [n for n, v in vendors.items() if v.get("status") != "done"]
    if bad:
        raise RuntimeError(f"vendors not scored: {bad}. Run the same command again to retry them.")

    # 4-5. fetch + metrics
    res_dir = run_dir / rfp
    log(f"{rfp}: fetching results")
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_fetch(Namespace(api=api.base, token=api.token, rfp_id=rfp_id, out=str(res_dir)))
    for latex, fname in ((False, "metrics.md"), (True, "metrics.tex")):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_metrics(Namespace(dataset=str(dataset), rfp=rfp, results=str(res_dir), latex=latex,
                                  csv=str(res_dir / "metrics.csv")))
        (res_dir / fname).write_text(buf.getvalue(), encoding="utf-8")
    st["done"] = True
    save_state(state_path, state)
    log(f"{rfp}: finished -> {res_dir}/metrics.md")
    print((res_dir / "metrics.md").read_text(encoding="utf-8")[:1500])


def cmd_run(args) -> int:
    email = args.email or os.environ.get("RFP_EMAIL")
    password = args.password or os.environ.get("RFP_PASSWORD")
    if not email or not password:
        print("Set RFP_EMAIL and RFP_PASSWORD (or pass --email/--password).", file=sys.stderr)
        return 2
    dataset, run_dir = Path(args.dataset), Path(args.out) / args.tag
    state_path = run_dir / "state.json"
    state = load_state(state_path)
    api = Api(args.api, email, password)
    failed = []
    for rfp in args.rfps:
        if state.get(rfp, {}).get("done") and not args.refetch:
            log(f"{rfp}: already done (use --refetch to recompute metrics)")
            continue
        if args.refetch and state.get(rfp, {}).get("done"):
            state[rfp]["done"] = False
        try:
            run_rfp(api, dataset, rfp, run_dir, state, state_path, args)
        except Exception as e:  # keep going with the other RFPs
            log(f"{rfp}: FAILED: {e}")
            failed.append(rfp)
    log("all done" if not failed else f"finished with failures: {failed} (run the same command again to resume)")
    return 1 if failed else 0


# ── summary over a run ────────────────────────────────────────────────────────

def _pairs(dataset: Path, rfp: str, res_dir: Path):
    gt = [g for g in _read_csv(dataset / "ground_truth.csv") if g["rfp"] == rfp]
    files = {_norm(p.stem): p for p in res_dir.glob("*.json") if not p.name.startswith("_")}
    pairs = []
    for g in gt:
        f = files.get(_norm(g["vendor"]))
        if not f:
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        by = {r.get("req_code"): r for r in data.get("requirement_scores", [])}
        pairs.append((GT_LEVEL[g["true_level"]], (by.get(g["req_id"]) or {}).get("compliance_level") or "Missing"))
    return pairs


def cmd_summary(args) -> int:
    dataset, run_dir = Path(args.dataset), Path(args.out) / args.tag
    state = load_state(run_dir / "state.json")
    allp = []
    print(f"| RFP | answers | accuracy | macro-F1 | kappa | mean time per vendor (min) |\n|---|---|---|---|---|---|")
    for rfp in ALL_RFPS:
        d = run_dir / rfp
        if not d.exists():
            continue
        p = _pairs(dataset, rfp, d)
        allp += p
        secs = [v.get("seconds") for v in state.get(rfp, {}).get("vendors", {}).values() if v.get("seconds")]
        t = f"{sum(secs) / len(secs) / 60:.1f}" if secs else "-"
        acc = sum(a == b for a, b in p) / len(p) if p else float("nan")
        print(f"| {rfp} | {len(p)} | {acc:.3f} | {_macro_f1(p):.3f} | {_kappa(p):.3f} | {t} |")
    if allp:
        acc = sum(a == b for a, b in allp) / len(allp)
        print(f"| **All** | {len(allp)} | {acc:.3f} | {_macro_f1(allp):.3f} | {_kappa(allp):.3f} | |")
    return 0


# ── consistency between two runs ──────────────────────────────────────────────

def _levels(res_dir: Path) -> dict:
    out = {}
    for f in res_dir.glob("*.json"):
        if f.name.startswith("_"):
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        for r in data.get("requirement_scores", []):
            out[(_norm(f.stem), r.get("req_code"))] = r.get("compliance_level")
        out[(_norm(f.stem), "__total__")] = (data.get("vendor_score") or {}).get("total_score")
    return out


def cmd_consistency(args) -> int:
    a, b = _levels(Path(args.a)), _levels(Path(args.b))
    keys = [k for k in a if k in b and k[1] != "__total__"]
    pairs = [(a[k], b[k]) for k in keys]
    same = sum(x == y for x, y in pairs)
    print(f"Requirements compared: {len(pairs)}")
    print(f"Identical level in both runs: {same} ({same / len(pairs):.1%})" if pairs else "nothing to compare")
    print(f"Cohen's kappa between runs: {_kappa(pairs):.3f}")
    print("\n| Vendor | total run A | total run B | difference |\n|---|---|---|---|")
    for k in sorted(k for k in a if k[1] == "__total__" and k in b):
        ta, tb = a[k], b[k]
        diff = f"{abs(ta - tb):.2f}" if ta is not None and tb is not None else "-"
        print(f"| {k[0]} | {ta} | {tb} | {diff} |")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--api", default="http://localhost:8000")
    r.add_argument("--email")
    r.add_argument("--password")
    r.add_argument("--dataset", required=True)
    r.add_argument("--rfps", nargs="+", default=ALL_RFPS)
    r.add_argument("--tag", default="run1")
    r.add_argument("--out", default="results")
    r.add_argument("--timeout", type=int, default=3600, help="max seconds to wait for one task")
    r.add_argument("--upload-gap", type=float, default=7, help="seconds between vendor uploads (rate limit)")
    r.add_argument("--vendors", nargs="+", help="only these vendors (e.g. for a cheaper consistency run)")
    r.add_argument("--sequential", action="store_true",
                   help="grade one vendor at a time (needed with low LLM rate limits)")
    r.add_argument("--max-retries", type=int, default=2, help="re-grade a failed vendor at most this many times")
    r.add_argument("--refetch", action="store_true", help="re-download results and recompute metrics")
    s = sub.add_parser("summary")
    s.add_argument("--dataset", required=True)
    s.add_argument("--tag", default="run1")
    s.add_argument("--out", default="results")
    c = sub.add_parser("consistency")
    c.add_argument("--a", required=True)
    c.add_argument("--b", required=True)
    args = ap.parse_args()
    return {"run": cmd_run, "summary": cmd_summary, "consistency": cmd_consistency}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

