"""AAT concept-record parsing tests.

The 2024 tripwire: Getty serves Linked Art, not SKOS. A SKOS-shaped extractor
returns nothing. These pin the Linked-Art path (language-URI mapping, preferred
vs alternate terms, the English-with-no-language backfill) plus the id/literal
helpers and reconcile-response shape handling.
"""
from __future__ import annotations

from museumvocab_reconcile.adapters import aat
from museumvocab_reconcile.adapters.aat import (
    AatAdapter,
    _clean_id,
    _concept_node,
    _detect_format,
    _lit,
    _parse_linked_art,
    _result_hits,
)


# ---- format detection -----------------------------------------------------

def test_detect_linked_art_vs_gvp():
    assert _detect_format({"type": "Type", "identified_by": []}) == "linked_art"
    assert _detect_format({"type": "Type", "_label": "x"}) == "linked_art"
    # No Linked-Art markers -> treat as GVP/SKOS.
    assert _detect_format({"skos:prefLabel": []}) == "gvp"


# ---- Linked-Art label parsing ---------------------------------------------

def _name(content, lang_uri=None, pref=False):
    n = {"type": "Name", "content": content}
    if lang_uri:
        n["language"] = [{"id": f"http://vocab.getty.edu/aat/{lang_uri}"}]
    if pref:
        n["classified_as"] = [{"id": "http://vocab.getty.edu/aat/300404670"}]
    return n


def test_linked_art_maps_language_uris_and_pref():
    node = {
        "type": "Type",
        "identified_by": [
            _name("bolle", lang_uri="300391418", pref=True),   # nb preferred
            _name("bowl", lang_uri="300388277"),               # en alt
            _name("skål", lang_uri="300388992"),               # nn alt
        ],
        "broader": [{"id": "http://vocab.getty.edu/aat/300264092"}],
    }
    pref, alt, _scope, broader = _parse_linked_art(node)
    assert pref["nb"] == "bolle"
    assert "bowl" in alt["en"]
    assert "skål" in alt["nn"]
    assert broader == ["300264092"]


def test_linked_art_untagged_name_is_english():
    # Getty leaves the English form's Name untagged; it must land under "en",
    # not "und", so a source English term that matches it reports matched_lang en.
    node = {"type": "Type", "identified_by": [_name("oil paint", pref=True)]}
    pref, _alt, _scope, _broader = _parse_linked_art(node)
    assert pref["en"] == "oil paint"
    assert "und" not in pref


def test_linked_art_broader_from_member_of():
    node = {"type": "Type", "identified_by": [],
            "member_of": [{"id": "http://vocab.getty.edu/aat/300111111"}]}
    _pref, _alt, _scope, broader = _parse_linked_art(node)
    assert broader == ["300111111"]


# ---- _node backfill (exercises real _node via a stubbed HTTP doc) ----------

class _DocAdapter(AatAdapter):
    """AatAdapter whose only network call (_http_doc) is replaced by a fixture."""

    def __init__(self, doc):
        super().__init__(cache=None)
        self._doc = doc

    def _http_doc(self, concept_id):
        return self._doc


def test_node_backfills_english_pref_from_label():
    # No Name is classified as preferred, so pref["en"] is empty; the top-level
    # _label must backfill it so review/output show the AAT term, not the
    # source's English. (Untagged Names are now attributed to "en" directly, so
    # this exercises the remaining backfill path: a record with no preferred Name.)
    doc = {
        "id": "http://vocab.getty.edu/aat/300011111",
        "type": "Type",
        "_label": "oil paint",
        "identified_by": [_name("paint, oil", pref=False, lang_uri="300388277")],
        "broader": [{"id": "http://vocab.getty.edu/aat/300264091"}],
    }
    node = _DocAdapter(doc)._node("300011111")
    assert node["format"] == "linked_art"
    assert node["pref_labels"]["en"] == "oil paint"


# ---- id / literal / response-shape helpers --------------------------------

def test_clean_id_variants():
    assert _clean_id("aat/300053271") == "300053271"
    assert _clean_id("http://vocab.getty.edu/aat/300053271") == "300053271"
    assert _clean_id("aat:300053271") == "300053271"
    assert _clean_id("300053271") == "300053271"
    assert _clean_id("http://vocab.getty.edu/aat/300053271#this") == "300053271"
    assert _clean_id("not-an-id") == ""
    assert _clean_id("") == ""


def test_lit_handles_jsonld_and_plain_shapes():
    assert _lit({"@value": "v", "@language": "NB"}) == ("nb", "v")
    assert _lit({"value": "v", "language": "en"}) == ("en", "v")
    assert _lit("bare") == (None, "bare")
    assert _lit(123) == (None, None)


def test_result_hits_across_response_shapes():
    assert _result_hits({"q0": {"result": [{"id": "1"}]}}, "q0") == [{"id": "1"}]
    assert _result_hits({"q0": {"candidates": [{"id": "2"}]}}, "q0") == [{"id": "2"}]
    assert _result_hits({"results": {"q0": {"result": [{"id": "3"}]}}}, "q0") == [{"id": "3"}]
    # A service-manifest response (no result block) -> no hits, not an error.
    assert _result_hits({"name": "Getty reconcile"}, "q0") == []


def test_concept_node_finds_target_in_graph():
    doc = {"@graph": [
        {"@id": "http://vocab.getty.edu/aat/300000001", "_label": "other"},
        {"@id": "http://vocab.getty.edu/aat/300000002", "_label": "target"},
    ]}
    assert _concept_node(doc, "300000002")["_label"] == "target"
    # Flat doc: returned as-is.
    assert _concept_node({"_label": "flat"}, "300000009")["_label"] == "flat"


def test_lang_uri_map_covers_norwegian():
    # The nb/nn auto-accept signal depends on these being mapped on the LA path.
    assert aat.LANG_URI["300391418"] == "nb"
    assert aat.LANG_URI["300388992"] == "nn"


def _lname(content, code, pref=False, alternative=None):
    n = {"type": "Name", "content": content,
         "language": [{"id": f"http://vocab.getty.edu/language/{code}", "_label": code}]}
    if alternative:
        n["alternative"] = [{"type": "Name", "content": alternative,
                             "language": [{"id": f"http://vocab.getty.edu/language/{code}"}]}]
    n["classified_as"] = [{"id": "http://vocab.getty.edu/aat/300404670"}] if pref else []
    return n


def test_linked_art_resolves_language_slash_uris_and_harvests_alternatives():
    # Real Getty shape (record 300027760): languages are /language/<code> URIs,
    # and the qualified parenthetical descriptor lives in `alternative`.
    node = {"type": "Type", "_label": "registrations (licenses)", "identified_by": [
        _lname("registrations", "en", pref=True, alternative="registrations (licenses)"),
        _lname("agrément", "fr", alternative="agrément"),
        _lname("registration", "en", alternative="registration (license)"),
        _lname("registratiebewijzen", "nl"),
    ]}
    pref, alt, _scope, _broader = _parse_linked_art(node)
    assert pref == {"en": "registrations (licenses)"}      # qualified form, not "und"
    assert alt["fr"] == ["agrément"]                        # was collapsing to "und"
    assert "registration (license)" in alt["en"]            # alternative harvested
    assert "registrations" in alt["en"]                     # base kept for matching
    assert alt["nl"] == ["registratiebewijzen"]


def test_resolve_lang_handles_both_uri_forms_and_norwegian_aliases():
    from museumvocab_reconcile.adapters.aat import _resolve_lang
    assert _resolve_lang({"language": [{"id": "http://vocab.getty.edu/language/fr"}]}) == "fr"
    assert _resolve_lang({"language": [{"id": "http://vocab.getty.edu/aat/300391418"}]}) == "nb"
    assert _resolve_lang({"language": [{"id": "http://vocab.getty.edu/language/no"}]}) == "nb"
    assert _resolve_lang({"language": [{"id": "x", "_label": "nn"}]}) == "nn"
    assert _resolve_lang({}) == ""
