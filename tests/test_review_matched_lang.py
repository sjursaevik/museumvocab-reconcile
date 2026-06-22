"""Review-CSV surface tests.

  * matched_lang / matched_term describe an EXACT match only. For a fuzzy
    proposal the reconcile display name and the query-language echo are NOT
    match evidence, so both cells must be blank rather than mislead a reviewer
    into reading 'nb' as 'matched a Norwegian label' (the 99-row leak).
  * the deepen advisory second-opinion columns are present and populated.
"""
from __future__ import annotations

import csv

from conftest import make_candidate, make_term

from museumvocab_reconcile.model import ClassifiedTerm
from museumvocab_reconcile.review import export_review_csv


def _classified(best, *, tier="review", **kw):
    return ClassifiedTerm(
        term=make_term(nb="Antependium", en="antependium"),
        candidates=[best], best=best, tier=tier, reasons=["below thresholds"],
        proposed_facet=best.facet, proposed_target_term=best.pref_label_target, **kw,
    )


def _read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def test_fuzzy_proposal_blanks_matched_lang(tmp_path):
    # fuzzy best whose provisional matched_lang is just the nb query echo
    fuzzy = make_candidate(concept_id="F", score=17, is_exact=False,
                           matched_label="antependium guards", matched_lang="nb",
                           facet="work_types", pref_label_target="antependium guards")
    out = tmp_path / "r.csv"
    export_review_csv([_classified(fuzzy)], out)
    row = _read(out)[0]
    assert row["matched_lang"] == ""        # not 'nb'
    assert row["matched_term"] == ""


def test_exact_proposal_keeps_matched_lang(tmp_path):
    exact = make_candidate(concept_id="E", score=30, is_exact=True,
                           matched_label="antependia", matched_lang="nb",
                           facet="work_types", pref_label_target="antependia")
    out = tmp_path / "r.csv"
    export_review_csv([_classified(exact)], out)
    row = _read(out)[0]
    assert row["matched_lang"] == "nb"
    assert row["matched_term"] == "antependia"


def test_deepen_columns_present_and_filled(tmp_path):
    fuzzy = make_candidate(concept_id="F", score=14, is_exact=False,
                           facet="work_types", pref_label_target="floor plans")
    ct = _classified(
        fuzzy, deep_used=True, deep_candidates_added=3,
        llm_recommended_id="F", llm_recommended_target_term="floor plans",
        llm_recommendation_confidence="high",
        llm_recommendation_reason="best hierarchy fit", llm_agrees_with_rule=True,
    )
    out = tmp_path / "r.csv"
    export_review_csv([ct], out)
    row = _read(out)[0]
    assert row["deep_used"] == "yes"
    assert row["llm_recommended_id"] == "F"
    assert row["llm_confidence"] == "high"
    assert row["llm_vs_rule"] == "agree"
    assert row["llm_reason"] == "best hierarchy fit"


def test_llm_disagreement_marked(tmp_path):
    best = make_candidate(concept_id="A", score=14, is_exact=False, facet="work_types")
    ct = _classified(best, llm_recommended_id="B", llm_agrees_with_rule=False)
    out = tmp_path / "r.csv"
    export_review_csv([ct], out)
    assert _read(out)[0]["llm_vs_rule"] == "DIFFERS"
