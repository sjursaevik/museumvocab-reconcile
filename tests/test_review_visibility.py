"""Regression tests for the 'Sari' review artifact.

Real-world failure (objectnames, term 'Sari'): the top-scored candidate —
almost certainly the correct 'saris (garments)' — was exact-matched via a
language outside match_langs (Getty's untagged-label 'und' quirk), so the
match_langs preference demoted it from the proposal pool and a low-scored
'Sari (Samanid pottery style)' was proposed instead. Two compounding defects
made this invisible to the reviewer:

  1. the review CSV's runner-up note sliced candidates[1:], assuming the
     proposal is candidates[0] — when a preference or steering moves `best`
     down the ranking, the true top candidate vanished from the row and the
     proposal was listed as its own alternative;
  2. tiering demoted the higher-scored candidate silently — no reason named
     it, so nothing in the row hinted a stronger rival existed.

These tests pin the fixes: the runner-up note excludes the proposal by
IDENTITY, and a match_langs demotion of a higher-scored candidate is disclosed
in reasons. The demotion behaviour itself (a deliberate design) is unchanged.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.review import _runner_up_note
from museumvocab_reconcile.tiering import classify


def sari_profile():
    return make_profile({
        "facets": {"accept_all": True, "accepted": []},
        "languages": {"match_langs": ["nb", "nn", "en"]},
    })


def sari_candidates():
    garments = make_candidate(
        concept_id="300209968", score=100, facet="work_types",
        matched_lang="und", is_exact=True, query_lang="en", query_term="saris",
        matched_label="saris (garments)",
    )
    samanid = make_candidate(
        concept_id="300021733", score=33, facet="styles_periods",
        matched_lang="en", is_exact=False, query_lang="nb", query_term="Sari",
        matched_label="Sari (Samanid pottery style)",
    )
    return garments, samanid


def test_match_langs_demotion_is_disclosed_in_reasons():
    garments, samanid = sari_candidates()
    term = make_term(nb="Sari", en="saris", target_source="llm")
    out = classify(term, [garments, samanid], sari_profile())
    assert out.best.concept_id == "300021733"   # demotion behaviour unchanged
    assert out.tier == "review"
    assert any(
        "higher-scored candidate 300209968" in r and "outside match_langs" in r
        for r in out.reasons
    )


def test_runner_up_note_never_hides_the_top_candidate():
    garments, samanid = sari_candidates()
    term = make_term(nb="Sari", en="saris", target_source="llm")
    out = classify(term, [garments, samanid], sari_profile())
    note = _runner_up_note(out)
    # the hidden-top bug: proposal used to be listed as its own alternative
    assert "300209968" in note          # the true top candidate is visible
    assert "300021733" not in note      # the proposal is not its own runner-up


def test_runner_up_note_unchanged_when_proposal_is_top():
    top = make_candidate(concept_id="A", score=90, facet="work_types")
    second = make_candidate(concept_id="B", score=50, facet="work_types")
    out = classify(make_term(nb="x"), [top, second], make_profile())
    note = _runner_up_note(out)
    assert "B" in note and "A=" not in note


def test_no_demotion_reason_when_nothing_demoted():
    top = make_candidate(concept_id="A", score=90, facet="work_types",
                         matched_lang="en")
    out = classify(make_term(nb="x"), [top], sari_profile())
    assert not any("outside match_langs" in r for r in out.reasons)
