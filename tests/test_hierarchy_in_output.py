"""Hierarchy surfaced into the final output.

Covers the additions in `feat/hierarchy-in-output`:
  * `source_level` (the term's MuseumPlus depth) reaches the final record/CSV,
    independent of whether a match was found;
  * the matched concept's AAT broader chain (`aat_ancestors` / `aat_depth`)
    is read from the CHOSEN candidate;
  * `proposed_hierarchy` reaches the final output;
  * the off-list-override guard: a chosen_id outside the candidate set must NOT
    inherit `best`'s lineage (the wrong-URI substitution class of bug).
"""
from __future__ import annotations

import csv

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.assemble import build_final_record, _write_final_csv
from museumvocab_reconcile.model import ClassifiedTerm, Decision

PROFILE = make_profile()

# climb order (narrow->broad); the last node is intentionally unlabelled to
# exercise the None-drop when building the broad->narrow CSV path.
_ANCESTORS = [
    {"id": "300A", "label": "gilding"},
    {"id": "300B", "label": "surface finishing"},
    {"id": "300C", "label": None},
]


def _auto_term():
    best = make_candidate(
        concept_id="300053789", score=40, is_exact=True, matched_lang="nb",
        facet="techniques", pref_label_target="gilding", ancestors=_ANCESTORS,
    )
    ct = ClassifiedTerm(
        term=make_term(nb="forgylling", en="gilding", level=2),
        candidates=[best], best=best, tier="auto_accept",
        reasons=["nb exact"], match_type="nb_exact",
        proposed_facet="techniques", proposed_target_term="gilding",
        proposed_hierarchy="Processes and Techniques (300053001)",
    )
    return ct


def _review_term():
    best = make_candidate(
        concept_id="300111111", score=14, is_exact=False, facet="work_types",
        pref_label_target="thing", ancestors=[{"id": "300X", "label": "things"}],
    )
    return ClassifiedTerm(
        term=make_term(nb="ting", en="thing", level=1),
        candidates=[best], best=best, tier="review",
        reasons=["below thresholds"], proposed_facet="work_types",
        proposed_target_term="thing",
    )


def test_source_level_and_aat_lineage_on_auto_accept():
    rec = build_final_record(_auto_term(), None, PROFILE)
    assert rec["source_level"] == 2
    assert rec["aat_depth"] == 3
    assert rec["aat_ancestors"] == _ANCESTORS
    assert rec["proposed_hierarchy"] == "Processes and Techniques (300053001)"


def test_in_list_review_reads_chosen_candidate_lineage():
    ct = _review_term()
    decision = Decision(id="1", accept=True, chosen_id="300111111",
                        chosen_target_term=None, chosen_facet=None,
                        notes="", raw_accept="yes")
    rec = build_final_record(ct, decision, PROFILE)
    assert rec["aat_depth"] == 1
    assert rec["aat_ancestors"] == [{"id": "300X", "label": "things"}]
    assert rec["source_level"] == 1


def test_off_list_override_does_not_inherit_best_lineage():
    """The guard: chosen_id outside the candidate set emits NO AAT lineage,
    rather than `best`'s broader chain for a different concept."""
    ct = _review_term()
    decision = Decision(id="1", accept=True, chosen_id="300999999",
                        chosen_target_term="x", chosen_facet="work_types",
                        notes="", raw_accept="yes")
    rec = build_final_record(ct, decision, PROFILE)
    assert rec["aat_ancestors"] == []
    assert rec["aat_depth"] is None
    # source level is intrinsic to the term and still surfaces
    assert rec["source_level"] == 1


def test_csv_columns_and_paths(tmp_path):
    auto = build_final_record(_auto_term(), None, PROFILE)
    # give the auto term source parents so both path columns are exercised
    auto["parents_source"] = ["overflatebehandling", "forgylling"]
    auto["parents_target"] = ["surface finishing", "gilding"]

    out = tmp_path / "final.csv"
    _write_final_csv(out, [auto])
    with out.open(encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))

    for col in ("source_level", "aat_depth", "aat_parents",
                "parents_target", "proposed_hierarchy"):
        assert col in row
    assert row["source_level"] == "2"
    assert row["aat_depth"] == "3"
    # broad->narrow, unlabelled climb-stop node dropped
    assert row["aat_parents"] == "surface finishing > gilding"
    assert row["parents_source"] == "overflatebehandling > forgylling"
    assert row["parents_target"] == "surface finishing > gilding"
    assert row["proposed_hierarchy"] == "Processes and Techniques (300053001)"
