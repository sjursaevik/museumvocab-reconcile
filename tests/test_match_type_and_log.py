"""match_type: structured, aggregatable tier basis + the expanded log report.

`reasons` stays the human-readable explanation; `match_type` is its machine
counterpart so the assemble log (and longer-term threshold tuning) can count
*why* terms landed where they did without parsing prose. The log test runs
assemble end-to-end (offline) and asserts the report sections exist and the
counts derive correctly.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.assemble import assemble
from museumvocab_reconcile.model import Decision
from museumvocab_reconcile.tiering import classify


def test_nb_exact_basis(profile):
    ct = classify(
        make_term(nb="akvarell"),
        [make_candidate(matched_label="akvarell", matched_lang="nb", is_exact=True,
                        query_term="akvarell", score=40)],
        profile,
    )
    assert ct.tier == "auto_accept" and ct.match_type == "nb_exact"


def test_score_gap_basis(profile):
    ct = classify(
        make_term(),
        [make_candidate(concept_id="1", score=40, matched_lang="nb"),
         make_candidate(concept_id="2", score=10, matched_lang="nb")],
        profile,
    )
    assert ct.tier == "auto_accept" and ct.match_type == "score_gap"


def test_source_en_pref_exact_basis():
    profile = make_profile({"languages": {"trusted_target_pref_exact": True}})
    ct = classify(
        make_term(en="watercolor", target_source="source_data"),
        [make_candidate(matched_label="watercolor", matched_lang="en",
                        query_lang="en", is_exact=True,
                        pref_label_target="watercolor", score=40)],
        profile,
    )
    assert ct.tier == "auto_accept" and ct.match_type == "source_en_pref_exact"


def test_llm_surfaced_basis(profile):
    ct = classify(
        make_term(en="watercolor", target_source="llm"),
        [make_candidate(matched_label="watercolor", matched_lang="en",
                        query_lang="en", is_exact=True, score=99,
                        query_term="watercolor")],
        profile,
    )
    assert ct.tier == "review" and ct.match_type == "llm_surfaced"


def test_facet_not_accepted_basis(profile):
    ct = classify(
        make_term(),
        [make_candidate(facet="agents", score=40, matched_lang="nb")],
        profile,
    )
    assert ct.tier == "review" and ct.match_type == "facet_not_accepted"


def test_below_threshold_and_no_candidates(profile):
    weak = classify(
        make_term(),
        [make_candidate(concept_id="1", score=12, matched_lang="nb"),
         make_candidate(concept_id="2", score=11, matched_lang="nb")],
        profile,
    )
    assert weak.tier == "review" and weak.match_type == "below_threshold"
    none = classify(make_term(), [], profile)
    assert none.tier == "no_match" and none.match_type == "no_candidates"


def test_log_report_sections_and_counts(tmp_path, profile):
    auto = classify(
        make_term(term_id="A", nb="akvarell"),
        [make_candidate(concept_id="10", matched_label="akvarell",
                        matched_lang="nb", is_exact=True, score=40,
                        query_term="akvarell")],
        profile,
    )
    reviewed = classify(
        make_term(term_id="B", nb="ting"),
        [make_candidate(concept_id="20", score=12, matched_lang="nb"),
         make_candidate(concept_id="21", score=11, matched_lang="nb")],
        profile,
    )
    missing = classify(make_term(term_id="C", nb="ukjent"), [], profile)
    # reviewer accepts B but overrides the proposal to the runner-up
    decisions = {"B": Decision(id="B", accept=True, chosen_id="21",
                               chosen_target_term=None, chosen_facet=None)}
    out, log = tmp_path / "04_final.json", tmp_path / "log.txt"
    stats = assemble(
        [auto, reviewed, missing], decisions, profile, out, log,
        run_info={"profile": "test.yaml", "classified": "03_classified.json"},
    )
    assert stats["final_records"] == 2
    text = log.read_text("utf-8")
    for section in (
        "AUTO-ACCEPT BASIS", "MATCH TYPE", "MATCHED LANGUAGE",
        "REVIEW OUTCOMES", "TRANSLATION PROVENANCE", "NO-MATCH TERMS",
        "RUN INFO",
    ):
        assert section in text, f"missing log section {section}"
    assert "nb_exact" in text and "no_candidates" in text
    assert "with proposal overridden 1" in text
    assert "undecided (excluded)       1" in text  # C had no decision
    assert "facets.preferred " not in text  # deprecated knob no longer logged
    assert "ukjent" in text  # no-match sample is actionable


def test_final_record_carries_matched_lang_and_match_type(tmp_path, profile):
    auto = classify(
        make_term(term_id="A", nb="akvarell"),
        [make_candidate(matched_label="akvarell", matched_lang="nb",
                        is_exact=True, score=40, query_term="akvarell")],
        profile,
    )
    out, log = tmp_path / "04_final.json", tmp_path / "log.txt"
    assemble([auto], {}, profile, out, log)
    import json
    rec = json.loads(out.read_text("utf-8"))[0]
    assert rec["matched_lang"] == "nb" and rec["match_type"] == "nb_exact"
