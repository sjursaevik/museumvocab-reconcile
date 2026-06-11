"""Tests for translate --predict-all.

The contract being pinned:
  * terms that ALREADY have source-data English form a second, opt-in
    population that gets a prediction-only prompt (existing English shown as
    context; no english/alternatives requested) and a separate `cls:` cache
    namespace — so the same id never collides across modes, and prediction
    prompt changes never invalidate cached translations;
  * predict rows ride the same review CSV (task column, blank approved_english)
    and the same accept gate;
  * apply folds ONLY expected_facet/expected_hierarchy for these terms —
    main_target_term, target_source (stays source_data) and target_alternatives
    are never touched, preserving the invariant the tiering provenance guard
    ("LLM English never auto-accepts") relies on;
  * the advisory signals now touching auto-accept-capable terms stays safe:
    a trusted nb exact still auto-accepts against a contrary prediction.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.tiering import classify
from museumvocab_reconcile.translate import (
    apply_translations,
    build_predict_prompt,
    export_translations_csv,
    has_target,
    ingest_translations_csv,
    run_translation,
)

ANCHORS = {"300037335": "Furnishings (hierarchy name)", "300045611": "Containers (hierarchy name)"}


def predict_profile():
    p = make_profile({"facets": {"preferred_hierarchies": dict(ANCHORS)}})
    p.translation.predict_all = True
    return p


class FakeTranslator:
    """Returns canned elements; records which method served which ids."""

    def __init__(self, translate_payload=None, predict_payload=None):
        self.translate_payload = translate_payload or []
        self.predict_payload = predict_payload or []
        self.translate_ids: list[str] = []
        self.predict_ids: list[str] = []
        self.predict_items: list[dict] = []

    def translate_batch(self, items, context, cfg):
        self.translate_ids += [i["id"] for i in items]
        return self.translate_payload

    def predict_batch(self, items, context, cfg):
        self.predict_ids += [i["id"] for i in items]
        self.predict_items += items
        return self.predict_payload


class FakeCache:
    def __init__(self):
        self.data: dict[str, dict] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, flush=False):
        self.data[key] = value

    def flush(self):
        pass


# ---- prompt ----------------------------------------------------------------

def test_predict_prompt_shows_english_and_requests_no_english_field():
    items = [{"id": "1", "term": "skap", "english": "cupboards",
              "domain": None, "parents": [], "siblings": []}]
    p = build_predict_prompt(items, "ctx", ["work_types"], ["furnishings"])
    assert "english label: cupboards" in p
    assert '"english"' not in p and '"alternatives"' not in p
    assert "expected_facet" in p and "expected_hierarchy" in p and '""' in p


# ---- populations, results, cache namespace ----------------------------------

def make_pair():
    # one term missing English (translate), one with source English (predict)
    return [
        make_term(term_id="1", nb="forgylling"),
        make_term(term_id="2", nb="skap", en="cupboards"),
    ]


def test_predict_all_runs_both_populations_with_right_methods():
    profile = predict_profile()
    tr = FakeTranslator(
        translate_payload=[{"id": "1", "english": "gilding", "alternatives": [],
                            "confidence": "high", "note": ""}],
        predict_payload=[{"id": "2", "confidence": "high", "note": "",
                          "expected_facet": "work_types",
                          "expected_hierarchy": "furnishings"}],
    )
    cache = FakeCache()
    results = run_translation(make_pair(), tr, profile, cache)
    assert tr.translate_ids == ["1"] and tr.predict_ids == ["2"]
    # predict result never carries English or alternatives
    assert results["2"].english == "" and results["2"].alternatives == []
    assert results["2"].expected_hierarchy == "furnishings"
    # the existing English was offered as context to the prediction prompt
    assert tr.predict_items[0]["english"] == "cupboards"
    # namespaced cache keys: tr: for translation, cls: for prediction
    assert any(k.startswith("tr:") and k.endswith(":1") for k in cache.data)
    assert any(k.startswith("cls:") and k.endswith(":2") for k in cache.data)
    assert not any(k.startswith("tr:") and k.endswith(":2") for k in cache.data)


def test_predict_population_skipped_by_default():
    profile = make_profile({"facets": {"preferred_hierarchies": dict(ANCHORS)}})
    tr = FakeTranslator(
        translate_payload=[{"id": "1", "english": "gilding", "alternatives": [],
                            "confidence": "high", "note": ""}],
    )
    results = run_translation(make_pair(), tr, profile, cache=None)
    assert tr.predict_ids == [] and "2" not in results


def test_only_ids_implicitly_refreshes_predict_rows():
    # retranslate path: targeted ids in the predict population are re-queried
    # without any extra flag (predict_all stays False).
    profile = make_profile({"facets": {"preferred_hierarchies": dict(ANCHORS)}})
    tr = FakeTranslator(
        predict_payload=[{"id": "2", "confidence": "high", "note": "",
                          "expected_hierarchy": "containers"}],
    )
    results = run_translation(make_pair(), tr, profile, cache=None, only_ids={"2"})
    assert tr.predict_ids == ["2"] and tr.translate_ids == []
    assert results["2"].expected_hierarchy == "containers"


# ---- CSV + apply: the trust invariants ----------------------------------------

def roundtrip(tmp_path):
    profile = predict_profile()
    tr = FakeTranslator(
        translate_payload=[{"id": "1", "english": "gilding",
                            "alternatives": ["gilt"], "confidence": "high",
                            "note": "", "expected_facet": "techniques"}],
        predict_payload=[{"id": "2", "confidence": "high", "note": "",
                          "expected_facet": "work_types",
                          "expected_hierarchy": "furnishings"}],
    )
    terms = make_pair()
    results = run_translation(terms, tr, profile, cache=None)
    csv_path = tmp_path / "tr.csv"
    export_translations_csv(terms, results, csv_path)
    return terms, csv_path


def test_csv_marks_task_and_blanks_predict_english_decision(tmp_path):
    _, csv_path = roundtrip(tmp_path)
    text = csv_path.read_text(encoding="utf-8-sig")
    header, row1, row2 = text.splitlines()[:3]
    cols = header.split(",")
    r1 = dict(zip(cols, row1.split(",")))
    r2 = dict(zip(cols, row2.split(",")))
    assert r1["task"] == "translate" and r1["approved_english"] == "gilding"
    assert r2["task"] == "predict" and r2["approved_english"] == ""
    assert r2["accept"] == "yes"


def test_apply_predict_branch_touches_only_advisory_fields(tmp_path):
    terms, csv_path = roundtrip(tmp_path)
    decisions = ingest_translations_csv(csv_path)
    terms, applied, predicted = apply_translations(terms, decisions)
    assert applied == 1 and predicted == 1
    t2 = terms[1]
    assert t2.main_target_term == "cupboards"        # untouched
    assert t2.target_source == "source_data"          # untouched
    assert t2.target_alternatives == []               # invariant: no LLM queries
    assert t2.expected_facet == "work_types"
    assert t2.expected_hierarchy == "furnishings"


def test_apply_ignores_edited_english_on_predict_rows(tmp_path):
    terms, csv_path = roundtrip(tmp_path)
    # cataloguer (mistakenly) types an English override on the predict row
    decisions = ingest_translations_csv(csv_path)
    decisions["2"].approved_english = "wardrobes"
    terms, applied, predicted = apply_translations(terms, decisions)
    assert terms[1].main_target_term == "cupboards"   # source English wins
    assert predicted == 1


def test_apply_respects_accept_no_on_predict_rows(tmp_path):
    terms, csv_path = roundtrip(tmp_path)
    decisions = ingest_translations_csv(csv_path)
    decisions["2"].accept = False
    terms, applied, predicted = apply_translations(terms, decisions)
    assert predicted == 0
    assert terms[1].expected_facet is None and terms[1].expected_hierarchy is None


# ---- tiering safety on the auto-accept-capable population ----------------------

def test_trusted_exact_still_auto_accepts_against_contrary_prediction():
    profile = make_profile({"facets": {
        "preferred_hierarchies": dict(ANCHORS), "hierarchy_mode": "prefer",
    }})
    exact = make_candidate(
        concept_id="C", score=40, facet="work_types", matched_lang="nb",
        is_exact=True, ancestors=[{"id": "300045611", "label": "Containers"}],
    )
    term = make_term(nb="skap", en="cupboards",                # source_data English
                     expected_facet="materials",               # contrary advisory
                     expected_hierarchy="furnishings")
    out = classify(term, [exact], profile)
    assert out.tier == "auto_accept" and out.best.concept_id == "C"


def test_has_target_selects_terms_with_english():
    assert [t.id for t in has_target(make_pair())] == ["2"]
