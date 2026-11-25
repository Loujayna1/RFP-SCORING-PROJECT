"""
Evaluate the system against the technical RFP dataset's ground truth.

Three commands:

  oracle   Check the scoring arithmetic. The dataset files are read with the
           system's own parsers (evaluation grid, compliance matrix, each
           vendor's returned matrix); a perfect verifier returning the true
           level of every requirement is then aggregated by the system's own
           code. The totals must equal expected_scores.csv, both on the
           verified and on the declared levels. No LLM, database or Milvus.

  fetch    Download the system's results for one RFP from the running API
           (GET /compare, then GET /vendor/{id}/score for every vendor) and
           save one JSON file per vendor.

  metrics  Compare fetched results with the ground truth and print the
           tables for the report (Markdown, and LaTeX with --latex):
           confusion matrix, accuracy, macro-F1, Cohen's kappa, detection of
           over-claims and unsupported claims, evidence page accuracy,
           reference checks, and vendor totals / ranking against the
           expected scores.

Examples (from backend/):
  python -m scripts.evaluate_against_ground_truth oracle  --dataset ../technical_rfp_dataset
  python -m scripts.evaluate_against_ground_truth fetch   --api http://localhost:8000 \\
         --token "$TOKEN" --rfp-id <uuid> --out results/RFP-BIL-2026
  python -m scripts.evaluate_against_ground_truth metrics --dataset ../technical_rfp_dataset \\
         --rfp RFP-BIL-2026 --results results/RFP-BIL-2026 --latex
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

LEVELS = ["FullyCompliant", "PartiallyCompliant", "RoadmapCompliant", "NonCompliant", "NotAddressed"]
SHORT = {"FullyCompliant": "FC", "PartiallyCompliant": "PC", "RoadmapCompliant": "RC",
         "NonCompliant": "NC", "NotAddressed": "NA"}
GT_LEVEL = {"FC": "FullyCompliant", "PC": "PartiallyCompliant", "RC": "RoadmapCompliant",
            "NC": "NonCompliant", "NE": "NotAddressed"}
SCALE = [{"label": "Fully Compliant", "value": 5}, {"label": "Partially Compliant", "value": 3},
         {"label": "Roadmap Compliant", "value": 2}, {"label": "Non-Compliant", "value": 1}]
POINTS = {"FullyCompliant": 5, "PartiallyCompliant": 3, "RoadmapCompliant": 2, "NonCompliant": 1, "NotAddressed": 0}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Loading the dataset with the system's own parsers ─────────────────────────

def load_rfp(dataset: Path, rfp: str) -> dict:
    from app.services.evaluation_matrix_parser import parse_evaluation_matrix_xlsx
    from app.services.matrix_processor import extract_requirements_from_xlsx
    from app.services.vendor_matrix import read_vendor_declarations

    base = dataset / rfp
    grid = next((base / "internal").glob("*.xlsx"))
    tree = parse_evaluation_matrix_xlsx(grid.read_bytes(), grid.name)
    criteria: list[dict] = []

    def add(nodes, parent_id=None):        # same flattening as persist_rfp_extraction
        for n in nodes:
            cid = str(uuid.uuid4())
            criteria.append({"criterion_id": cid, "parent_id": parent_id, "label": n.get("label", ""),
                             "section": n.get("section") or n.get("label", ""),
                             "weight": float(n.get("weight") or 0.0),
                             "criterion_type": n.get("criterion_type", "scored"),
                             "evaluation_mode": n.get("evaluation_mode")})
            add(n.get("children") or [], cid)
    add(tree)

    matrix = next((base / "issued").glob("*Compliance_Matrix.xlsx"))
    res = extract_requirements_from_xlsx(matrix.read_bytes(), matrix.name)
    reqs = res.get("requirements", []) if isinstance(res, dict) else res
    requirements = [{**r, "req_id": r["req_code"]} for r in reqs]

    declarations = {}
    for vdir in sorted(p for p in (base / "responses").iterdir() if p.is_dir()):
        f = next(vdir.glob("*.xlsx"))
        decl = read_vendor_declarations(f.read_bytes(), f.name)
        declarations[vdir.name.replace("_", " ")] = {
            d["req_code"]: {"declared_level": d.get("declared_level"), "declared_raw": d.get("declared_raw"),
                            "response_text": d.get("response_text"), "reference": d.get("reference")}
            for d in decl
        }
    return {"criteria": criteria, "requirements": requirements, "declarations": declarations}


# ── oracle ────────────────────────────────────────────────────────────────────

def cmd_oracle(args) -> int:
    from app.services.evaluation import map_requirements_to_criteria
    from app.services.trace import build_trace

    dataset = Path(args.dataset)
    gt = _read_csv(dataset / "ground_truth.csv")
    expected = {(r["rfp"], r["vendor"]): r for r in _read_csv(dataset / "expected_scores.csv")}
    rfps = sorted({r["rfp"] for r in gt}) if not args.rfp else [args.rfp]
    failures = 0
    print(f"{'RFP':<14}{'Vendor':<18}{'reqs':>5}{'verified':>10}{'expected':>10}{'declared':>10}{'expected':>10}  status counts")
    for rfp in rfps:
        data = load_rfp(dataset, rfp)
        mapping = map_requirements_to_criteria(data["requirements"], data["criteria"])
        unmapped = [r["req_code"] for r in data["requirements"] if r["req_id"] not in mapping]
        if unmapped or any(m != "exact" for _, m in mapping.values()):
            print(f"  {rfp}: mapping problem, unmapped={unmapped}")
            failures += 1
        for vendor, decl in data["declarations"].items():
            truth = {g["req_id"]: g for g in gt if g["rfp"] == rfp and g["vendor"] == vendor}
            scores = []
            for r in data["requirements"]:
                lvl = GT_LEVEL[truth[r["req_code"]]["true_level"]]
                page = truth[r["req_code"]]["evidence_page"]
                scores.append({**r, "criterion_id": mapping[r["req_id"]][0], "compliance_level": lvl,
                               "raw_score": POINTS[lvl], "final_score": POINTS[lvl], "human_score": None,
                               "evidence_page": int(page) if page else None})
            t = build_trace(criteria=data["criteria"], scores=scores, declarations=decl, compliance_levels=SCALE)
            exp = expected[(rfp, vendor)]
            ok = (abs(t["total_score"] - float(exp["total_true"])) < 1e-3
                  and abs(t["declared_total"] - float(exp["total_declared"])) < 1e-3)
            failures += 0 if ok else 1
            counts = t["summary"]["verification"]
            print(f"{rfp:<14}{vendor:<18}{len(scores):>5}{t['total_score']:>10.3f}{float(exp['total_true']):>10.3f}"
                  f"{t['declared_total']:>10.3f}{float(exp['total_declared']):>10.3f}  "
                  f"{'OK ' if ok else 'BAD'} {dict(sorted(counts.items()))}")
    print("\nAll totals reproduced." if not failures else f"\n{failures} mismatch(es).")
    return 1 if failures else 0


# ── fetch ─────────────────────────────────────────────────────────────────────

def cmd_fetch(args) -> int:
    import httpx
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(base_url=args.api, headers=headers, timeout=60) as c:
        cmp = c.get(f"/api/v1/rfp/{args.rfp_id}/compare").raise_for_status().json()
        (out / "_compare.json").write_text(json.dumps(cmp, indent=2), encoding="utf-8")
        for v in cmp.get("vendor_details", []):
            data = c.get(f"/api/v1/rfp/{args.rfp_id}/vendor/{v['response_id']}/score").raise_for_status().json()
            (out / f"{v['name'].replace(' ', '_')}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"saved {v['name']}")
    return 0


# ── metrics ───────────────────────────────────────────────────────────────────

def _kappa(pairs: list[tuple[str, str]]) -> float:
    n = len(pairs)
    if not n:
        return float("nan")
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def _macro_f1(pairs: list[tuple[str, str]]) -> float:
    f1s = []
    for lvl in LEVELS:
        tp = sum(1 for t, p in pairs if t == lvl and p == lvl)
        fp = sum(1 for t, p in pairs if t != lvl and p == lvl)
        fn = sum(1 for t, p in pairs if t == lvl and p != lvl)
        if tp + fp + fn == 0:
            continue
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / len(f1s) if f1s else float("nan")


def cmd_metrics(args) -> int:
    dataset, results = Path(args.dataset), Path(args.results)
    gt = [g for g in _read_csv(dataset / "ground_truth.csv") if g["rfp"] == args.rfp]
    expected = {r["vendor"]: r for r in _read_csv(dataset / "expected_scores.csv") if r["rfp"] == args.rfp}
    files = {_norm(p.stem): p for p in results.glob("*.json") if not p.name.startswith("_")}

    pairs, rows, vendor_rows = [], [], []
    for vendor in sorted({g["vendor"] for g in gt}):
        f = files.get(_norm(vendor))
        if not f:
            print(f"! no results for {vendor}", file=sys.stderr)
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        by_code = {r.get("req_code"): r for r in data.get("requirement_scores", [])}
        trace_rows = {}
        for root in (data.get("trace") or {}).get("criteria", []):
            stack = [root]
            while stack:
                n = stack.pop()
                stack.extend(n.get("children", []))
                for r in n.get("requirements", []):
                    trace_rows[r.get("req_code")] = r
        for g in (x for x in gt if x["vendor"] == vendor):
            s = by_code.get(g["req_id"])
            pred = (s or {}).get("compliance_level") or "Missing"
            true = GT_LEVEL[g["true_level"]]
            pairs.append((true, pred))
            tr = trace_rows.get(g["req_id"], {})
            rows.append({"vendor": vendor, "req": g["req_id"], "case": g["case_type"], "true": true, "pred": pred,
                         "status": tr.get("verification"), "gt_page": g["evidence_page"],
                         "sys_page": (s or {}).get("evidence_page"),
                         "ref_ok_gt": g["reference_correct"], "ref_ok_sys": tr.get("reference_matches_evidence")})
        exp = expected[vendor]
        vendor_rows.append({"vendor": vendor, "system": (data.get("vendor_score") or {}).get("total_score"),
                            "expected": float(exp["total_true"]), "declared": float(exp["total_declared"]),
                            "rank_expected": int(exp["rank_true"])})

    n = len(pairs)
    acc = sum(1 for t, p in pairs if t == p) / n if n else float("nan")
    print(f"\n## {args.rfp}: {n} requirement answers\n")
    print(f"Accuracy {acc:.3f} | macro-F1 {_macro_f1(pairs):.3f} | Cohen's kappa {_kappa(pairs):.3f}")
    within_one = sum(1 for t, p in pairs if p in POINTS and abs(LEVELS.index(t) - LEVELS.index(p)) <= 1) / n if n else 0
    print(f"Within one level: {within_one:.3f}")

    print("\nConfusion matrix (rows = true level, columns = system level)\n")
    cols = LEVELS + (["Missing"] if any(p not in LEVELS for _, p in pairs) else []) + \
        (["ScoringFailed"] if any(p == "ScoringFailed" for _, p in pairs) else [])
    cols = list(dict.fromkeys(cols))
    cm = defaultdict(Counter)
    for t, p in pairs:
        cm[t][p] += 1
    print("| true \\ system | " + " | ".join(SHORT.get(c, c) for c in cols) + " |")
    print("|" + "---|" * (len(cols) + 1))
    for t in LEVELS:
        print(f"| {SHORT[t]} | " + " | ".join(str(cm[t][c]) for c in cols) + " |")

    # Detection of false declarations
    bad = [r for r in rows if r["case"] in ("over_claim", "unsupported_claim")]
    flagged = [r for r in rows if r["status"] in ("over_claim", "unsupported")]
    tp = sum(1 for r in flagged if r["case"] in ("over_claim", "unsupported_claim"))
    prec = tp / len(flagged) if flagged else float("nan")
    rec = tp / len(bad) if bad else float("nan")
    print(f"\nFalse declarations detected: recall {rec:.3f} ({tp}/{len(bad)}), precision {prec:.3f} "
          f"({tp}/{len(flagged)} flagged)")
    for case in ("over_claim", "unsupported_claim"):
        sub = [r for r in rows if r["case"] == case]
        hit = sum(1 for r in sub if r["status"] in ("over_claim", "unsupported"))
        print(f"  {case}: {hit}/{len(sub)} flagged")
    by_case = defaultdict(list)
    for r in rows:
        by_case[r["case"]].append(r["true"] == r["pred"])
    print("\nAccuracy by case type")
    for case, v in sorted(by_case.items()):
        print(f"  {case:<26} {sum(v)}/{len(v)} = {sum(v) / len(v):.3f}")

    with_page = [r for r in rows if r["gt_page"] and r["pred"] not in ("NotAddressed", "Missing", "ScoringFailed")]
    page_ok = sum(1 for r in with_page if r["sys_page"] and str(r["sys_page"]) == r["gt_page"])
    print(f"\nEvidence page correct: {page_ok}/{len(with_page)} = {page_ok / len(with_page):.3f}" if with_page else "")

    print("\n| Vendor | System total | Expected (evidence) | Declared | System rank | Expected rank |")
    print("|---|---|---|---|---|---|")
    ranked = sorted(vendor_rows, key=lambda v: -(v["system"] or 0))
    for i, v in enumerate(ranked, 1):
        v["rank_system"] = i
    for v in sorted(vendor_rows, key=lambda v: v["rank_expected"]):
        print(f"| {v['vendor']} | {v['system']:.3f} | {v['expected']:.3f} | {v['declared']:.3f} | "
              f"{v['rank_system']} | {v['rank_expected']} |")
    mae = sum(abs((v["system"] or 0) - v["expected"]) for v in vendor_rows) / len(vendor_rows)
    same_top = ranked[0]["rank_expected"] == 1
    print(f"\nMean absolute error of totals: {mae:.3f}; correct vendor ranked first: {same_top}")

    if args.latex:
        print("\n% LaTeX: confusion matrix")
        print("\\begin{tabular}{l" + "r" * len(cols) + "}\\toprule")
        print("True $\\backslash$ System & " + " & ".join(SHORT.get(c, c) for c in cols) + " \\\\ \\midrule")
        for t in LEVELS:
            print(f"{SHORT[t]} & " + " & ".join(str(cm[t][c]) for c in cols) + " \\\\")
        print("\\bottomrule\\end{tabular}")
        print("\n% LaTeX: vendor totals")
        print("\\begin{tabular}{lrrrrr}\\toprule")
        print("Vendor & System & Expected & Declared & Rank (system) & Rank (expected) \\\\ \\midrule")
        for v in sorted(vendor_rows, key=lambda v: v["rank_expected"]):
            print(f"{v['vendor']} & {v['system']:.2f} & {v['expected']:.2f} & {v['declared']:.2f} & "
                  f"{v['rank_system']} & {v['rank_expected']} \\\\")
        print("\\bottomrule\\end{tabular}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nPer-requirement comparison written to {args.csv}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("oracle")
    o.add_argument("--dataset", required=True)
    o.add_argument("--rfp")
    f = sub.add_parser("fetch")
    f.add_argument("--api", required=True)
    f.add_argument("--token", required=True)
    f.add_argument("--rfp-id", required=True)
    f.add_argument("--out", required=True)
    m = sub.add_parser("metrics")
    m.add_argument("--dataset", required=True)
    m.add_argument("--rfp", required=True)
    m.add_argument("--results", required=True)
    m.add_argument("--latex", action="store_true")
    m.add_argument("--csv")
    args = ap.parse_args()
    return {"oracle": cmd_oracle, "fetch": cmd_fetch, "metrics": cmd_metrics}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
