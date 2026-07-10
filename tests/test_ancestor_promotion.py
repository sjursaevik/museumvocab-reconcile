"""Ancestor promotion (broad-term rescue) — lookup + tiering contract.

The failure mode: a broad term ('Fotografi') reconciles to its narrower
children only, the broad concept itself pushed outside result_limit — yet it
sits on every child's parent chain, which enrichment already walked and
cached. Pins:

  * an ancestor whose nb altLabel exactly matches the query is promoted;
  * an ancestor already present as a candidate is NOT duplicated;
  * non-matching ancestors are never promoted (no flooding);
  * promoted candidates carry score 0.0, promoted_from provenance, and an
    honestly recomputed matched_lang/is_exact;
  * tiering routes a promoted best to review with match_type
    `ancestor_promoted`, even on an nb exact label match (review-first
    posture until a run is hand-audited);
  * a promoted rival never blocks a genuine trusted-exact reconcile hit.

All offline: FakeConceptAdapter serves canned concept records.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.adapters.base import AuthorityAdapter
from museumvocab_reconcile.cli import promote_matching_ancestors
from museumvocab_reconcile.tiering import classify

LANGS = make_profile().languages
PREFER = ["nb", "nn", "en"]


class FakeConceptAdapter:
    """Serves canned concept records; counts fetches vs. label peeks."""

    name = "aat"
    normalise = staticmethod(AuthorityAdapter.normalise)
    # borrow the real refine logic so matched_lang attribution is exercised
    _refine_match = AuthorityAdapter._refine_match
    candidate_from_concept = AuthorityAdapter.candidate_from_concept

    def __init__(self, records: dict[str, dict]):
        self.records = records
        self.fetched: list[str] = []
        self.peeked: list[str] = []

    def fetch(self, concept_id):
        self.fetched.append(concept_id)
        return self.records[concept_id]

    def peek_labels(self, concept_id):
        self.peeked.append(concept_id)
        rec = self.records[concept_id]
        return rec.get("pref_labels", {}) or {}, rec.get("alt_labels", {}) or {}


def _rec(cid, *, en, nb_alts=(), facet="techniques", ancestors=()):
    return {
        "id": cid,
        "uri": f"http://vocab.getty.edu/aat/{cid}",
        "pref_labels": {"en": en},
        "alt_labels": {"nb": list(nb_alts)},
        "facet": facet,
        "aat_facet": "Activities (300264090)",
        "scope_note": None,
        "ancestors": list(ancestors),
        "cross_refs": [],
    }


# PHOTO is the broad concept; the reconcile results are its children only.
PHOTO = "300054225"
RECORDS = {
    PHOTO: _rec(PHOTO, en="photography", nb_alts=["fotografi"]),
    "AERIAL": _rec("AERIAL", en="aerial photography"),
    "PROCESS": _rec("PROCESS", en="processes and techniques"),  # no label match
}


def _children():
    """Two enriched child candidates sharing PHOTO on their parent chains."""
    a = make_candidate(
        concept_id="AERIAL", score=80, matched_label="aerial photography",
        matched_lang="en", facet="techniques", query_term="fotografi",
        ancestors=[{"id": PHOTO, "label": "photography"},
                   {"id": "PROCESS", "label": "processes and techniques"}],
    )
    b = make_candidate(
        concept_id="DIGITAL", score=70, matched_label="digital photography",
        matched_lang="en", facet="techniques", query_term="fotografi",
        ancestors=[{"id": PHOTO, "label": "photography"}],
    )
    return [a, b]


def test_matching_ancestor_is_promoted_once_with_provenance():
    adapter = FakeConceptAdapter(RECORDS)
    term = make_term(nb="Fotografi", en="")
    out = promote_matching_ancestors(term, _children(), adapter, LANGS, PREFER)
    assert [c.concept_id for c in out] == [PHOTO]  # once, despite two children
    c = out[0]
    assert c.score == 0.0
    assert c.promoted_from == "AERIAL"          # first surfacing child
    assert c.is_exact and c.matched_lang == "nb"  # recomputed, not assumed
    assert c.matched_label == "fotografi"
    assert c.query_lang == "nb" and c.query_term == "Fotografi"
    # PROCESS was peeked (cache hit in production) but never fetched/promoted
    assert "PROCESS" in adapter.peeked and "PROCESS" not in adapter.fetched


def test_ancestor_already_a_candidate_is_not_duplicated():
    adapter = FakeConceptAdapter(RECORDS)
    cands = _children() + [make_candidate(
        concept_id=PHOTO, score=60, matched_label="fotografi",
        matched_lang="nb", is_exact=True, facet="techniques",
    )]
    out = promote_matching_ancestors(make_term(nb="Fotografi"), cands, adapter, LANGS, PREFER)
    assert out == []


def test_non_matching_ancestors_never_promoted():
    adapter = FakeConceptAdapter(RECORDS)
    term = make_term(nb="Flyfotografi")  # matches no ancestor label
    assert promote_matching_ancestors(term, _children(), adapter, LANGS, PREFER) == []
    assert adapter.fetched == []  # peeks only; nothing synthesised


def test_target_label_match_promotes_with_target_provenance():
    # The English query string can also trigger promotion; query_lang records
    # it honestly so the llm_surfaced trust rule stays effective downstream.
    adapter = FakeConceptAdapter(RECORDS)
    term = make_term(nb="", en="photography", target_source="llm")
    out = promote_matching_ancestors(term, _children(), adapter, LANGS, PREFER)
    assert [c.concept_id for c in out] == [PHOTO]
    assert out[0].query_lang == "en" and out[0].matched_lang == "en"


def test_tiering_routes_promoted_best_to_review():
    # nb exact + accepted facet would normally auto-accept — the promotion
    # provenance must force review with the dedicated match_type.
    adapter = FakeConceptAdapter(RECORDS)
    term = make_term(nb="Fotografi")
    promoted = promote_matching_ancestors(term, _children(), adapter, LANGS, PREFER)
    ct = classify(term, _children() + promoted, make_profile())
    assert ct.best.concept_id == PHOTO          # exact beats fuzzy children
    assert ct.tier == "review"
    assert ct.match_type == "ancestor_promoted"
    assert any("ancestor walk" in r for r in ct.reasons)


def test_llm_surfaced_dominates_ancestor_promoted():
    adapter = FakeConceptAdapter(RECORDS)
    term = make_term(nb="", en="photography", target_source="llm")
    promoted = promote_matching_ancestors(term, _children(), adapter, LANGS, PREFER)
    ct = classify(term, _children() + promoted, make_profile())
    assert ct.tier == "review"
    assert ct.match_type == "llm_surfaced"      # the trust rule wins


def test_promoted_rival_does_not_block_genuine_trusted_exact():
    # A real reconcile hit with an nb exact match must still auto-accept even
    # when a promoted ancestor sits in the candidate list (score 0 rival can
    # never shrink the gap or hijack the proposal).
    real = make_candidate(
        concept_id="REAL", score=90, matched_label="fotografi",
        matched_lang="nb", is_exact=True, facet="techniques",
        query_term="Fotografi", query_lang="nb",
    )
    adapter = FakeConceptAdapter(RECORDS)
    term = make_term(nb="Fotografi")
    promoted = promote_matching_ancestors(term, [real] + _children(), adapter, LANGS, PREFER)
    ct = classify(term, [real] + _children() + promoted, make_profile())
    assert ct.best.concept_id == "REAL"
    assert ct.tier == "auto_accept"
    assert ct.match_type == "nb_exact"
