"""Assemble-stage guard: an off-list chosen_id must not silently inherit the
proposal's URI (the wrong-URI substitution, structural finding #2).

When a reviewer types a chosen_id that is NOT among the term's candidates, the
final record's authority_link must be derived from THAT id, the record flagged,
and an in-list choice must still behave exactly as before.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.assemble import build_final_record
from museumvocab_reconcile.model import ClassifiedTerm, Decision

PROFILE = make_profile()


def _review_term():
    best = make_candidate(concept_id="300111111", score=14, is_exact=False,
                          facet="work_types", pref_label_target="thing")
    return ClassifiedTerm(
        term=make_term(nb="ting", en="thing"), candidates=[best], best=best,
        tier="review", reasons=["below thresholds"],
        proposed_facet="work_types", proposed_target_term="thing",
    )


def test_off_list_chosen_id_derives_uri_and_flags():
    ct = _review_term()
    decision = Decision(id="1", accept=True, chosen_id="300999999",
                        chosen_target_term="correct thing", chosen_facet="work_types",
                        notes="", raw_accept="yes")
    rec = build_final_record(ct, decision, PROFILE)
    assert rec["authority_id"] == "300999999"
    # URI derived from the chosen id, NOT the proposal's 300111111
    assert rec["authority_link"] == "http://vocab.getty.edu/aat/300999999"
    assert "300111111" not in rec["authority_link"]
    assert "off-list" in rec["notes"]


def test_in_list_chosen_id_unchanged():
    ct = _review_term()
    decision = Decision(id="1", accept=True, chosen_id="300111111",
                        chosen_target_term=None, chosen_facet=None,
                        notes="", raw_accept="yes")
    rec = build_final_record(ct, decision, PROFILE)
    assert rec["authority_link"] == "http://vocab.getty.edu/aat/300111111"
    assert "off-list" not in rec["notes"]
