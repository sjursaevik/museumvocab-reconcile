"""The gold-set eval harness computes the metrics it claims, including the
recall ceiling and the rule-vs-LLM-on-disagreement breakdown."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from eval_deepen import evaluate, load_gold  # noqa: E402


def _term(tid, best_id, cand_ids, llm_id=None):
    return {
        "term": {"id": tid},
        "best_id": best_id,
        "candidates": [{"concept_id": c} for c in cand_ids],
        "llm_recommended_id": llm_id,
    }


def test_metrics_and_disagreement_attribution():
    classified = [
        # gold in cands, rule right, llm agrees + right
        _term("1", "A", ["A", "B"], llm_id="A"),
        # gold in cands, rule wrong, llm right (disagree -> llm right)
        _term("2", "X", ["G", "X"], llm_id="G"),
        # gold NOT in cands (recall miss), rule wrong, no llm pick
        _term("3", "Q", ["Q", "R"]),
        # gold in cands, rule right, llm wrong (disagree -> rule right)
        _term("4", "M", ["M", "N"], llm_id="N"),
    ]
    gold = {"1": "A", "2": "G", "3": "Z", "4": "M"}
    rep = evaluate(classified, gold)
    assert rep["gold_terms_found"] == 4
    assert rep["candidate_recall_pct"] == 75.0       # 3 of 4 gold ids surfaced
    assert rep["rule_accuracy_pct"] == 50.0          # terms 1, 4
    assert rep["llm_recommended_n"] == 3
    assert rep["llm_accuracy_pct"] == round(100 * 2 / 3, 1)   # 1, 2 right; 4 wrong
    assert rep["llm_vs_rule"]["agree"] == 1
    assert rep["llm_vs_rule"]["disagree"] == 2
    assert rep["llm_vs_rule"]["llm_right_when_disagree"] == 1   # term 2
    assert rep["llm_vs_rule"]["rule_right_when_disagree"] == 1  # term 4
    assert "3" in rep["candidate_recall_misses_sample"]


def test_load_gold_csv(tmp_path):
    p = tmp_path / "gold.csv"
    p.write_text("id,gold_id\n10,300001\n11,300002\n", encoding="utf-8")
    assert load_gold(str(p)) == {"10": "300001", "11": "300002"}
