"""Translate-step metadata tests: alternatives + expected_facet.

Pins two pipeline decisions:
  * `alternatives` survive the translate-apply gate onto the SourceTerm so
    lookup can use them as FALLBACK queries (they were previously dropped);
  * `expected_facet` is requested in the response SCHEMA only (per-term prompt
    context unchanged), validated against the profile's facet options, carried
    through the review CSV, and lands on the term as an ADVISORY signal.
"""
from __future__ import annotations

from conftest import make_profile, make_term

from museumvocab_reconcile.translate import (
    TRANSLATION_COLUMNS,
    TranslationResult,
    apply_translations,
    build_user_prompt,
    export_translations_csv,
    ingest_translations_csv,
    run_translation,
)

ITEMS = [{"id": "1", "term": "forgylling", "domain": None, "parents": [], "siblings": []}]


# ---- prompt schema ---------------------------------------------------------

def test_prompt_without_facet_options_has_no_expected_facet():
    p = build_user_prompt(ITEMS, "ctx")
    assert "expected_facet" not in p


def test_prompt_with_facet_options_extends_schema_only():
    p_base = build_user_prompt(ITEMS, "ctx")
    p = build_user_prompt(ITEMS, "ctx", ["techniques", "materials"])
    assert "expected_facet" in p and "'techniques'" in p and "'materials'" in p
    # per-term context block unchanged: same term lines in both prompts
    term_line = "  term: forgylling"
    assert term_line in p_base and term_line in p


# ---- run_translation validates expected_facet ------------------------------

class FakeTranslator:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def translate_batch(self, items, context, cfg):
        self.calls += 1
        return self.payload


def _run(payload, accepted=("techniques", "materials")):
    profile = make_profile({"facets": {"accepted": list(accepted)}})
    terms = [make_term(term_id="1", nb="forgylling")]
    return run_translation(terms, FakeTranslator(payload), profile, cache=None)


def test_valid_expected_facet_kept_and_normalised():
    res = _run([{"id": "1", "english": "gilding", "alternatives": ["gilt"],
                 "confidence": "high", "note": "", "expected_facet": "Techniques"}])
    assert res["1"].expected_facet == "techniques"


def test_invalid_expected_facet_dropped():
    res = _run([{"id": "1", "english": "gilding", "alternatives": [],
                 "confidence": "high", "note": "", "expected_facet": "made-up-facet"}])
    assert res["1"].expected_facet == ""


def test_missing_expected_facet_tolerated():
    res = _run([{"id": "1", "english": "gilding", "alternatives": [],
                 "confidence": "high", "note": ""}])
    assert res["1"].expected_facet == ""


# ---- CSV round-trip and apply ----------------------------------------------

def test_csv_roundtrip_carries_alternatives_and_expected_facet(tmp_path):
    terms = [make_term(term_id="1", nb="forgylling")]
    results = {"1": TranslationResult(
        id="1", english="gilding", alternatives=["gilt", "gold plating"],
        confidence="high", note="", expected_facet="techniques",
    )}
    csv_path = tmp_path / "tr.csv"
    assert export_translations_csv(terms, results, csv_path) == 1
    assert "expected_facet" in TRANSLATION_COLUMNS

    decisions = ingest_translations_csv(csv_path)
    d = decisions["1"]
    assert d.alternatives == ["gilt", "gold plating"]
    assert d.expected_facet == "techniques"

    applied_terms, n, n_pred = apply_translations(terms, decisions)
    assert n == 1 and n_pred == 0
    t = applied_terms[0]
    assert t.main_target_term == "gilding"
    assert t.target_source == "llm"
    assert t.target_alternatives == ["gilt", "gold plating"]
    assert t.expected_facet == "techniques"


def test_apply_dedupes_alternatives_against_approved_english(tmp_path):
    terms = [make_term(term_id="1", nb="forgylling")]
    results = {"1": TranslationResult(
        id="1", english="gilding", alternatives=["Gilding", "gilt", "gilt"],
        confidence="high", note="",
    )}
    csv_path = tmp_path / "tr.csv"
    export_translations_csv(terms, results, csv_path)
    applied_terms, _, _ = apply_translations(terms, ingest_translations_csv(csv_path))
    # "Gilding" duplicates the approved label; the repeated "gilt" is deduped.
    assert applied_terms[0].target_alternatives == ["gilt"]


def test_old_csv_without_new_columns_still_ingests(tmp_path):
    # A v2-era CSV (no alternatives/expected_facet columns) must not break.
    csv_path = tmp_path / "old.csv"
    csv_path.write_text(
        "id,llm_english,accept,approved_english\n1,gilding,yes,gilding\n",
        encoding="utf-8",
    )
    d = ingest_translations_csv(csv_path)["1"]
    assert d.accept and d.alternatives == [] and d.expected_facet == ""
