"""Tests for the advisory LLM expected_hierarchy prediction.

Design contract being pinned:
  * the LLM chooses from the profile's preferred_hierarchies labels (closed
    set, cleaned of '(hierarchy name)' / guide-term noise) or "" — free-text
    hierarchy names never survive validation;
  * the prediction refines the EXISTING hierarchy steering (expected anchor
    first, then any anchor) with the same guards: never past a trusted nb/nn
    exact, never a gate or tier change;
  * agreement/disagreement — and unresolvable edited labels — are annotated
    for the reviewer, and the label is surfaced in the review CSV.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.config import normalize_hierarchy_label
from museumvocab_reconcile.tiering import classify
from museumvocab_reconcile.translate import (
    TRANSLATION_COLUMNS,
    TranslationResult,
    apply_translations,
    build_user_prompt,
    export_translations_csv,
    ingest_translations_csv,
    run_translation,
)

ANCHORS = {
    "300037335": "Furnishings (hierarchy name)",
    "300045611": "Containers (hierarchy name)",
    "300053003": "<processes and techniques by specific type>",
}


def hier_profile(extra=None):
    over = {"facets": {"preferred_hierarchies": dict(ANCHORS), "hierarchy_mode": "prefer"}}
    if extra:
        over["facets"].update(extra)
    return make_profile(over)


ITEMS = [{"id": "1", "term": "skap", "domain": None, "parents": [], "siblings": []}]


# ---- label normalization ----------------------------------------------------

def test_normalize_strips_display_noise():
    assert normalize_hierarchy_label("Furnishings (hierarchy name)") == "furnishings"
    assert (normalize_hierarchy_label("<processes and techniques by specific type>")
            == "processes and techniques by specific type")
    assert normalize_hierarchy_label("  Object   Groupings ") == "object groupings"
    assert normalize_hierarchy_label("") == ""


def test_hierarchy_options_and_label_resolution():
    f = hier_profile().facets
    assert f.hierarchy_options() == [
        "furnishings", "containers", "processes and techniques by specific type",
    ]
    # resolution tolerates the original noisy form, the cleaned form, and case
    assert f.resolve_hierarchy_label("Furnishings (hierarchy name)") == "300037335"
    assert f.resolve_hierarchy_label("furnishings") == "300037335"
    assert f.resolve_hierarchy_label("CONTAINERS") == "300045611"
    assert f.resolve_hierarchy_label("made-up") is None
    assert f.resolve_hierarchy_label("") is None


# ---- prompt schema -----------------------------------------------------------

def test_prompt_offers_cleaned_closed_list_and_empty_guidance():
    p = build_user_prompt(ITEMS, "ctx", ["work_types"], ["furnishings", "containers"])
    assert "expected_hierarchy" in p and "'furnishings'" in p and "'containers'" in p
    assert '""' in p  # empty is presented as a normal answer
    assert "(hierarchy name)" not in p


def test_prompt_without_anchors_omits_expected_hierarchy():
    assert "expected_hierarchy" not in build_user_prompt(ITEMS, "ctx", ["work_types"])


# ---- run_translation validation ---------------------------------------------

class FakeTranslator:
    def __init__(self, payload):
        self.payload = payload

    def translate_batch(self, items, context, cfg):
        return self.payload


def _run(payload):
    profile = hier_profile()
    terms = [make_term(term_id="1", nb="skap")]
    return run_translation(terms, FakeTranslator(payload), profile, cache=None)


def test_valid_hierarchy_label_kept_normalized():
    res = _run([{"id": "1", "english": "cabinets", "alternatives": [],
                 "confidence": "high", "note": "",
                 "expected_hierarchy": "Furnishings (hierarchy name)"}])
    assert res["1"].expected_hierarchy == "furnishings"


def test_free_text_hierarchy_dropped():
    res = _run([{"id": "1", "english": "cabinets", "alternatives": [],
                 "confidence": "high", "note": "",
                 "expected_hierarchy": "storage furniture and casework"}])
    assert res["1"].expected_hierarchy == ""


# ---- CSV round-trip and apply -------------------------------------------------

def test_csv_roundtrip_and_apply_carry_expected_hierarchy(tmp_path):
    assert "expected_hierarchy" in TRANSLATION_COLUMNS
    terms = [make_term(term_id="1", nb="skap")]
    results = {"1": TranslationResult(
        id="1", english="cabinets", alternatives=[], confidence="high",
        note="", expected_hierarchy="furnishings",
    )}
    csv_path = tmp_path / "tr.csv"
    export_translations_csv(terms, results, csv_path)
    decisions = ingest_translations_csv(csv_path)
    # ingest normalizes, so a cataloguer pasting the noisy AAT form also works
    assert decisions["1"].expected_hierarchy == "furnishings"
    applied, _ = apply_translations(terms, decisions)
    assert applied[0].expected_hierarchy == "furnishings"


# ---- tiering: two-tier steering ------------------------------------------------

def in_anchor(anchor_id, **kw):
    return make_candidate(ancestors=[{"id": anchor_id, "label": "x"}], **kw)


def test_expected_anchor_beats_stronger_other_anchor_candidate():
    profile = hier_profile()
    other = in_anchor("300045611", concept_id="C", score=40, facet="work_types")
    expected = in_anchor("300037335", concept_id="F", score=30, facet="work_types")
    term = make_term(nb="skap", expected_hierarchy="furnishings")
    out = classify(term, [other, expected], profile)
    assert out.best.concept_id == "F"
    assert any("'furnishings' agrees" in r for r in out.reasons)


def test_no_candidate_in_expected_anchor_falls_back_to_any_anchor():
    profile = hier_profile()
    other = in_anchor("300045611", concept_id="C", score=40, facet="work_types")
    outside = make_candidate(concept_id="O", score=45, facet="work_types")
    term = make_term(nb="skap", expected_hierarchy="furnishings")
    out = classify(term, [outside, other], profile)
    assert out.best.concept_id == "C"  # unchanged single-tier behaviour
    assert any("'furnishings' differs" in r for r in out.reasons)


def test_expected_hierarchy_never_overrides_trusted_exact():
    profile = hier_profile()
    exact = in_anchor("300045611", concept_id="C", score=30, facet="work_types",
                      matched_lang="nb", is_exact=True)
    expected = in_anchor("300037335", concept_id="F", score=29, facet="work_types")
    term = make_term(nb="skap", expected_hierarchy="furnishings")
    out = classify(term, [exact, expected], profile)
    assert out.best.concept_id == "C"
    assert out.tier == "auto_accept"


def test_unrecognized_edited_label_is_reported_not_silent():
    profile = hier_profile()
    cand = in_anchor("300045611", concept_id="C", score=40, facet="work_types")
    term = make_term(nb="skap", expected_hierarchy="storage thingies")
    out = classify(term, [cand], profile)
    assert out.best.concept_id == "C"
    assert any("matches no profile anchor" in r for r in out.reasons)


def test_no_prediction_changes_nothing():
    profile = hier_profile()
    other = in_anchor("300045611", concept_id="C", score=40, facet="work_types")
    expected = in_anchor("300037335", concept_id="F", score=30, facet="work_types")
    out = classify(make_term(nb="skap"), [other, expected], profile)
    assert out.best.concept_id == "C"
    assert not any("expected hierarchy" in r for r in out.reasons)


# ---- profile validation warnings ------------------------------------------------

def test_validate_warns_on_colliding_normalized_labels():
    profile = hier_profile({"preferred_hierarchies": {
        "300037335": "Furnishings (hierarchy name)",
        "300999999": "furnishings",
    }})
    assert any("normalize to the same name" in w for w in profile.validate())


def test_validate_warns_on_deprecated_preferred():
    profile = make_profile({"facets": {"preferred": "work_types"}})
    assert any("deprecated" in w for w in profile.validate())


def test_clean_profile_validates_quietly():
    assert hier_profile().validate() == []
