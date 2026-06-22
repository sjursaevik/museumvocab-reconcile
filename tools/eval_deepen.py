"""Measure whether `deepen` actually helps, against a small gold set.

Without a ground-truth check, a deeper lookup + an LLM second opinion could be
adding noise rather than signal. This harness reports, over the terms you have
labelled with a correct AAT id:

  candidate_recall   — share of gold terms whose correct id is anywhere in the
                       (post-deepen) candidate set. This is the ceiling: the
                       rule engine and the LLM can only pick what was surfaced,
                       so a low recall means the bottleneck is still DEPTH, not
                       selection.
  rule_accuracy      — share where the rule engine's proposed best == gold.
  llm_accuracy       — share where the LLM recommendation == gold (of the terms
                       that got one).
  llm_vs_rule        — agreement / disagreement / who-was-right on disagreements.

Run it on the pre-deepen 03_classified.json and the post-deepen 03c_deepened.json
to see the lift. Offline; no network.

Gold file: JSON  {"<term_id>": "<aat_id>", ...}
       or  CSV   with columns  id,gold_id  (header required).

    python tools/eval_deepen.py --classified 03c_deepened.json --gold gold.csv
    python tools/eval_deepen.py --classified 03_classified.json --gold gold.csv  # baseline
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def load_gold(path: str) -> dict[str, str]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        return {str(k): str(v) for k, v in json.loads(p.read_text("utf-8")).items()}
    out: dict[str, str] = {}
    with p.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            gid = row.get("gold_id") or row.get("aat_id") or ""
            if row.get("id") and gid:
                out[row["id"]] = gid
    return out


def evaluate(classified: list[dict[str, Any]], gold: dict[str, str]) -> dict[str, Any]:
    by_id = {r["term"]["id"]: r for r in classified}
    n = present = rule_ok = llm_n = llm_ok = 0
    agree = disagree = rule_right_on_disagree = llm_right_on_disagree = 0
    misses: list[str] = []
    for tid, gid in gold.items():
        r = by_id.get(tid)
        if r is None:
            continue
        n += 1
        cand_ids = {c["concept_id"] for c in r.get("candidates", [])}
        if gid in cand_ids:
            present += 1
        elif len(misses) < 25:
            misses.append(tid)
        if r.get("best_id") == gid:
            rule_ok += 1
        llm = r.get("llm_recommended_id")
        if llm:
            llm_n += 1
            if llm == gid:
                llm_ok += 1
            if r.get("best_id") == llm:
                agree += 1
            else:
                disagree += 1
                if r.get("best_id") == gid:
                    rule_right_on_disagree += 1
                if llm == gid:
                    llm_right_on_disagree += 1

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    return {
        "gold_terms_found": n,
        "candidate_recall_pct": pct(present, n),
        "rule_accuracy_pct": pct(rule_ok, n),
        "llm_recommended_n": llm_n,
        "llm_accuracy_pct": pct(llm_ok, llm_n),
        "llm_vs_rule": {
            "agree": agree, "disagree": disagree,
            "rule_right_when_disagree": rule_right_on_disagree,
            "llm_right_when_disagree": llm_right_on_disagree,
        },
        "candidate_recall_misses_sample": misses,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--classified", required=True, help="03_classified.json or 03c_deepened.json")
    ap.add_argument("--gold", required=True, help="gold ids: JSON {id: aat_id} or CSV id,gold_id")
    args = ap.parse_args(argv)
    classified = json.loads(Path(args.classified).read_text("utf-8"))
    gold = load_gold(args.gold)
    report = evaluate(classified, gold)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
