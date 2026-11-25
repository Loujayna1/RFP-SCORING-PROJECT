"""
Chapter 5 tables from the system's results, using the levels declared in the
vendors' compliance matrices as reference labels.

For the generated vendor responses, each requirement was assigned a level
(FC, PC, RC, NC) when the response was written, and this level is the one
declared in the vendor's matrix. The system stores those declarations and
never shows them to the language model, so comparing the verified level with
the declared level measures how well the system recovers the reference label.

Input: one folder per RFP, filled by
    python -m scripts.evaluate_against_ground_truth fetch ... --out results/<RFP>
(one JSON file per vendor, as returned by GET /vendor/{id}/score).

Usage (from backend/):
    python -m scripts.agreement_report \\
        --rfp "RFP A=results/A" --rfp "RFP B=results/B" \\
        --rfp "RFP C=results/C" --rfp "RFP D=results/D" --rfp "RFP E=results/E" \\
        --group "Company RFPs=RFP A,RFP B" --group "Company-style RFPs=RFP C,RFP D,RFP E" \\
        --consistency "results/C=results/C_run2" \\
        --latex > results/chapter5_tables.txt

--consistency compares two executions of the same RFP (fetched into two
folders) and reports the share of requirements that received the same level.
Processing times are computed from the timestamps returned by the API:
scoring time = span of the requirement scores of a vendor; end-to-end time =
from the upload of the response to the computation of its score (first runs).
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REF = ["FullyCompliant", "PartiallyCompliant", "RoadmapCompliant", "NonCompliant"]
PRED = REF + ["NotAddressed", "ScoringFailed"]
SHORT = {"FullyCompliant": "FC", "PartiallyCompliant": "PC", "RoadmapCompliant": "RC",
         "NonCompliant": "NC", "NotAddressed": "NA", "ScoringFailed": "Failed"}


def load_rfp(folder: Path) -> list[dict]:
    vendors = []
    for f in sorted(folder.glob("*.json")):
        if f.name.startswith("_"):
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        trace = data.get("trace") or {}
        rows = []
        stack = list(trace.get("criteria", []))
        while stack:
            n = stack.pop()
            stack.extend(n.get("children", []))
            rows.extend(n.get("requirements", []))
        rows.extend(trace.get("unmapped_requirements", []))
        times = [r.get("scored_at") for r in data.get("requirement_scores", []) if r.get("scored_at")]
        vendors.append({
            "vendor": f.stem.replace("_", " "),
            "rows": rows,
            "scored_at": sorted(times),
            "computed_at": (data.get("vendor_score") or {}).get("computed_at"),
            "levels": {r.get("req_code"): r.get("compliance_level") for r in data.get("requirement_scores", [])},
            "total": (data.get("vendor_score") or {}).get("total_score"),
            "declared_total": trace.get("declared_total"),
            "evidence_rate": (trace.get("summary") or {}).get("evidence_rate"),
        })
    cmp_file = folder / "_compare.json"
    if cmp_file.exists():
        uploaded = {v["name"]: v.get("uploaded_at") for v in json.loads(cmp_file.read_text(encoding="utf-8")).get("vendor_details", [])}
        for v in vendors:
            v["uploaded_at"] = uploaded.get(v["vendor"])
    return vendors


def _ts(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", ""))
    except (TypeError, ValueError):
        return None


def pairs_of(vendors):
    return [(r["declared_level"], r.get("compliance_level") or "NotAddressed")
            for v in vendors for r in v["rows"] if r.get("declared_level") in REF]


def kappa(pairs):
    n = len(pairs)
    if not n:
        return float("nan")
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def per_class(pairs):
    out = {}
    for lvl in REF:
        tp = sum(t == lvl and p == lvl for t, p in pairs)
        fp = sum(t != lvl and p == lvl for t, p in pairs)
        fn = sum(t == lvl and p != lvl for t, p in pairs)
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
        out[lvl] = {"support": tp + fn, "precision": pr, "recall": rc, "f1": f1}
    return out


def overall(pairs):
    pc = per_class(pairs)
    present = [v["f1"] for v in pc.values() if v["support"]]
    return {
        "n": len(pairs),
        "acc": sum(t == p for t, p in pairs) / len(pairs) if pairs else float("nan"),
        "macro_f1": sum(present) / len(present) if present else float("nan"),
        "kappa": kappa(pairs),
        "na": sum(p == "NotAddressed" for _, p in pairs) / len(pairs) if pairs else 0,
        "failed": sum(p == "ScoringFailed" for _, p in pairs) / len(pairs) if pairs else 0,
    }


def pct(x):
    return f"{100 * x:.1f}\\%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rfp", action="append", required=True, help='"Name=folder"')
    ap.add_argument("--group", action="append", default=[], help='"Group=RFP1,RFP2"')
    ap.add_argument("--latex", action="store_true")
    ap.add_argument("--consistency", action="append", default=[], help='"run1_folder=run2_folder"')
    a = ap.parse_args()

    rfps = {}
    for spec in a.rfp:
        name, folder = spec.split("=", 1)
        rfps[name.strip()] = load_rfp(Path(folder.strip()))
    groups = {}
    for spec in a.group:
        name, members = spec.split("=", 1)
        groups[name.strip()] = [m.strip() for m in members.split(",")]
    if not groups:
        groups = {n: [n] for n in rfps}

    def gp(names):
        return [v for n in names for v in rfps[n]]

    print("% ===== Table: size of the evaluation data")
    for n, vs in rfps.items():
        reqs = max((len(v["rows"]) for v in vs), default=0)
        print(f"{n} & requirements {reqs} & vendors {len(vs)} & pairs {len(pairs_of(vs))} \\\\")
    print(f"Total pairs & {len(pairs_of(gp(list(rfps))))} \\\\")

    print("\n% ===== Table: distribution of the reference labels (declared levels)")
    for g, names in list(groups.items()) + [("All", list(rfps))]:
        c = Counter(t for t, _ in pairs_of(gp(names)))
        tot = sum(c.values()) or 1
        print(f"{g} & " + " & ".join(f"{c[l]} ({100*c[l]/tot:.1f}\\%)" for l in REF) + f" & {sum(c.values())} \\\\")

    print("\n% ===== Table rq2-overall: Pairs & Accuracy & Macro-F1 & kappa")
    for g, names in list(groups.items()) + [("All", list(rfps))]:
        o = overall(pairs_of(gp(names)))
        print(f"{g} & {o['n']} & {pct(o['acc'])} & {pct(o['macro_f1'])} & {o['kappa']:.2f} \\\\")
    for n, vs in rfps.items():
        o = overall(pairs_of(vs))
        print(f"%   per RFP {n}: n={o['n']} acc={o['acc']:.3f} macroF1={o['macro_f1']:.3f} "
              f"kappa={o['kappa']:.2f} notaddressed={o['na']:.3f} failed={o['failed']:.4f}")

    allp = pairs_of(gp(list(rfps)))
    print("\n% ===== Table rq2-perclass: Level & Support & Precision & Recall & F1")
    for lvl, v in per_class(allp).items():
        print(f"{lvl} & {v['support']} & {pct(v['precision'])} & {pct(v['recall'])} & {pct(v['f1'])} \\\\")

    print("\n% ===== Table rq2-confusion: rows = reference (declared), columns = system")
    print("% columns: " + " & ".join(SHORT[p] for p in PRED))
    cm = defaultdict(Counter)
    for t, p in allp:
        cm[t][p] += 1
    for t in REF:
        print(f"{SHORT[t]} & " + " & ".join(str(cm[t][p]) for p in PRED) + " \\\\")
    o = overall(allp)
    print(f"% Not Addressed share: {pct(o['na'])}; scoring failures: {pct(o['failed'])}")

    print("\n% ===== Flags and evidence")
    rows = [r for v in gp(list(rfps)) for r in v["rows"]]
    flagged = sum(1 for r in rows if r.get("confidence_flag"))
    print(f"% requirement scores flagged for review: {flagged}/{len(rows)} = {pct(flagged/len(rows)) if rows else '-'}")
    er = [v["evidence_rate"] for v in gp(list(rfps)) if v["evidence_rate"] is not None]
    if er:
        print(f"% evidence found in the proposal (mean over vendors): {pct(statistics.mean(er))}")
    st = Counter(r.get("verification") for r in rows)
    print(f"% declared vs verified outcomes: {dict(st)}")

    print("\n% ===== Table rq3-ranking: RFP & first by system & first by reference & top-1 agreement & rank correlation")
    for n, vs in rfps.items():
        vs2 = [v for v in vs if v["total"] is not None and v["declared_total"] is not None]
        if len(vs2) < 2:
            continue
        sys_rank = sorted(vs2, key=lambda v: -v["total"])
        ref_rank = sorted(vs2, key=lambda v: -v["declared_total"])
        rs = {v["vendor"]: i for i, v in enumerate(sys_rank)}
        rr = {v["vendor"]: i for i, v in enumerate(ref_rank)}
        k = len(vs2)
        rho = 1 - 6 * sum((rs[x] - rr[x]) ** 2 for x in rs) / (k * (k * k - 1))
        gap = sys_rank[0]["total"] - sys_rank[1]["total"]
        conf = "low" if gap < 0.1 else "moderate" if gap < 0.3 else "high"
        print(f"{n} & {sys_rank[0]['vendor']} ({sys_rank[0]['total']:.2f}) & {ref_rank[0]['vendor']} "
              f"({ref_rank[0]['declared_total']:.2f}) & {'yes' if sys_rank[0] is ref_rank[0] else 'no'} & "
              f"{rho:.2f} \\\\  % decision confidence {conf} (gap {gap:.2f})")

    print("\n% ===== Processing time")
    scoring, e2e, per_req = [], [], []
    for v in gp(list(rfps)):
        t = [_ts(x) for x in v["scored_at"]]
        t = [x for x in t if x]
        if len(t) >= 2:
            secs = (max(t) - min(t)).total_seconds()
            scoring.append(secs / 60)
            per_req.append(secs / len(t))
        up, done = _ts(v.get("uploaded_at")), _ts(v.get("computed_at"))
        if up and done and done > up:
            e2e.append((done - up).total_seconds() / 60)
    if scoring:
        print(f"% scoring phase per vendor response: mean {statistics.mean(scoring):.1f} min "
              f"(min {min(scoring):.1f}, max {max(scoring):.1f}, n={len(scoring)}); "
              f"mean {statistics.mean(per_req):.1f} s per requirement")
    if e2e:
        print(f"% upload to final score per vendor response: mean {statistics.mean(e2e):.1f} min "
              f"(min {min(e2e):.1f}, max {max(e2e):.1f}, n={len(e2e)})")

    for spec in a.consistency:
        f1, f2 = spec.split("=", 1)
        r1 = {v["vendor"]: v for v in load_rfp(Path(f1.strip()))}
        r2 = {v["vendor"]: v for v in load_rfp(Path(f2.strip()))}
        same = total = 0
        for name in set(r1) & set(r2):
            for code, lvl in r1[name]["levels"].items():
                if code in r2[name]["levels"]:
                    total += 1
                    same += lvl == r2[name]["levels"][code]
        print(f"\n% ===== Consistency {f1.strip()} vs {f2.strip()}: same level for {same}/{total} requirements "
              f"= {pct(same / total) if total else '-'} ({len(set(r1) & set(r2))} vendors)")

    print("\n% ===== Per-vendor totals (system vs reference)")
    for n, vs in rfps.items():
        for v in vs:
            print(f"% {n} | {v['vendor']} | system {v['total']} | reference {v['declared_total']}")


if __name__ == "__main__":
    main()
