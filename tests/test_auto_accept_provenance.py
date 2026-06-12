"""Provenance: an auto-accepted term exported via include_auto_accepted and
passed through review UNTOUCHED must stay decision_source=auto_accept, not be
relabeled human_review.

Reproduces the reported case: classify -> review-export (include_auto) ->
assemble with no edits showed auto_accepted=0, human_reviewed=N. The 'auto'
pre-fill in the accept cell was being read as a human acceptance.

End-to-end through the real review CSV round-trip (offline).
"""
from __future__ import annotations

import json

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.assemble import assemble
from museumvocab_reconcile.review import (
    export_review_csv,
    ingest_review_csv,
)
from museumvocab_reconcile.tiering import classify


def _auto_term():
    profile = make_profile()
    ct = classify(
        make_term(term_id="A", nb="akvarell"),
        [make_candidate(matched_label="akvarell", matched_lang="nb",
                        is_exact=True, score=40, query_term="akvarell")],
        profile,
    )
    assert ct.tier == "auto_accept"
    return profile, ct


def test_untouched_auto_prefill_stays_auto_accept(tmp_path):
    profile, ct = _auto_term()
    review_csv = tmp_path / "03b_review.csv"
    export_review_csv([ct], review_csv, include_auto=True)  # auto row written
    # No edits: ingest the file exactly as exported.
    decisions = ingest_review_csv(review_csv)
    assert decisions["A"].raw_accept == "auto"

    out, log = tmp_path / "04_final.json", tmp_path / "log.txt"
    stats = assemble([ct], decisions, profile, out, log)
    assert stats["auto_accepted"] == 1
    assert stats["human_reviewed"] == 0
    rec = json.loads(out.read_text("utf-8"))[0]
    assert rec["decision_source"] == "auto_accept"


def test_human_edit_of_auto_row_becomes_human_review(tmp_path):
    profile, ct = _auto_term()
    review_csv = tmp_path / "03b_review.csv"
    export_review_csv([ct], review_csv, include_auto=True)
    decisions = ingest_review_csv(review_csv)
    # Reviewer overrides the proposal on the auto row (different concept).
    d = decisions["A"]
    decisions["A"] = type(d)(
        id=d.id, accept=True, chosen_id="300999999",
        chosen_target_term=d.chosen_target_term, chosen_facet=d.chosen_facet,
        notes="corrected", raw_accept="auto",
    )
    out, log = tmp_path / "04_final.json", tmp_path / "log.txt"
    stats = assemble([ct], decisions, profile, out, log)
    assert stats["auto_accepted"] == 0
    assert stats["human_reviewed"] == 1
    rec = json.loads(out.read_text("utf-8"))[0]
    assert rec["decision_source"] == "human_review"
    assert rec["authority_id"] == "300999999"


def test_no_review_file_path_still_auto(tmp_path):
    profile, ct = _auto_term()
    out, log = tmp_path / "04_final.json", tmp_path / "log.txt"
    stats = assemble([ct], {}, profile, out, log)  # decisions empty
    assert stats["auto_accepted"] == 1 and stats["human_reviewed"] == 0
