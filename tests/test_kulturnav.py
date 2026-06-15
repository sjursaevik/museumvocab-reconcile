"""KulturNav adapter tests — all offline (no network, no secrets).

Pins the non-obvious decisions: Bokmaal's `no` code mapping to canonical nb, the
two serialization shapes the record parser tolerates (compact JSON-LD and the
Core API `properties` envelope), the outbound-matching crosswalk extraction, and
the search->enrich flow (per-dataset scoping, synthetic scores, and the
exact-match recompute that the nb/nn trust gate relies on).
"""
from __future__ import annotations

import json

import pytest

from museumvocab_reconcile.adapters import get_adapter
from museumvocab_reconcile.adapters.kulturnav import (
    KulturNavAdapter,
    _bare_uuid,
    _caption,
    _classify_match_uri,
    _concept_node,
    _from_kn_lang,
    _parse_broader,
    _parse_category,
    _parse_labels,
    _parse_matchings,
    _parse_scope_note,
    _to_kn_lang,
)


# ---- language code mapping (the Bokmaal `no` tripwire) --------------------

def test_bokmaal_code_maps_no_to_nb_both_ways():
    assert _to_kn_lang("nb") == "no"
    assert _from_kn_lang("no") == "nb"
    assert _from_kn_lang("no-NO") == "nb"   # primary subtag only
    assert _from_kn_lang("nn") == "nn"
    assert _from_kn_lang("en") == "en"


# ---- id / reference normalisation ----------------------------------------

def test_bare_uuid_from_uri_and_value():
    assert _bare_uuid("https://kulturnav.org/abc-123") == "abc-123"
    assert _bare_uuid("abc-123") == "abc-123"
    assert _bare_uuid("urn:uuid:abc-123") == "abc-123"
    assert _bare_uuid("") == ""


def test_classify_match_uri():
    assert _classify_match_uri("http://vocab.getty.edu/aat/300078925") == ("aat", "300078925")
    assert _classify_match_uri("https://www.wikidata.org/wiki/Q123") == ("wikidata", "Q123")
    assert _classify_match_uri("https://example.org/x") == (None, None)


# ---- label parsing across both shapes ------------------------------------

def test_parse_labels_jsonld_language_tags():
    node = {
        "skos:prefLabel": [
            {"@value": "akvarell", "@language": "no"},
            {"@value": "akvarell", "@language": "nn"},
            {"@value": "watercolor", "@language": "en"},
        ],
        "skos:altLabel": [{"@value": "vannfarge", "@language": "no"}],
    }
    pref, alt = _parse_labels(node)
    assert pref == {"nb": "akvarell", "nn": "akvarell", "en": "watercolor"}
    assert alt == {"nb": ["vannfarge"]}


def test_parse_labels_core_properties_envelope():
    node = {"properties": {"entity.name": [
        {"value": "akvarell", "lang": "no"},
        {"value": "watercolor", "lang": "en"},
    ]}}
    pref, _alt = _parse_labels(node)
    assert pref == {"nb": "akvarell", "en": "watercolor"}


def test_untagged_label_treated_as_english():
    pref, _ = _parse_labels({"skos:prefLabel": "watercolor"})
    assert pref == {"en": "watercolor"}


# ---- scope note, broader, category ---------------------------------------

def test_scope_note_prefers_norwegian():
    node = {"skos:scopeNote": [
        {"@value": "english note", "@language": "en"},
        {"@value": "norsk note", "@language": "no"},
    ]}
    assert _parse_scope_note(node) == "norsk note"


def test_broader_and_category_entity_reference():
    node = {
        "skos:broader": [{"@id": "https://kulturnav.org/parent-uuid"}],
        "concept.category": {
            "valueType": "ENTITY_REFERENCE", "uuid": "propid",
            "value": "cat-uuid", "displayValue": "Teknikk",
        },
    }
    assert _parse_broader(node) == ["parent-uuid"]
    assert _parse_category(node) == ("Teknikk", "cat-uuid")


# ---- outbound matchings (the free AAT/Wikidata crosswalk) ----------------

def test_matchings_extracted_and_classified():
    node = {
        "skos:exactMatch": [{"@id": "http://vocab.getty.edu/aat/300078925"}],
        "owl:sameAs": "https://www.wikidata.org/wiki/Q22915256",
        "concept.closeMatch": [{"value": "http://vocab.getty.edu/aat/300015050"}],
    }
    m = _parse_matchings(node)
    by_rel = {(x["relation"], x["authority"], x["id"]) for x in m}
    assert ("exactMatch", "aat", "300078925") in by_rel
    assert ("sameAs", "wikidata", "Q22915256") in by_rel
    assert ("closeMatch", "aat", "300015050") in by_rel


# ---- search + enrich integration (fake session, no network) --------------

class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeSession:
    """Routes Core search vs per-record fetch by URL; records calls for asserts."""

    def __init__(self, search_by_dataset, records):
        self.headers = {}
        self.search_by_dataset = search_by_dataset   # dataset uuid -> envelope
        self.records = records                        # uuid -> jsonld record
        self.calls = []

    def request(self, method, url, timeout=None, headers=None, **kwargs):
        self.calls.append(url)
        if "/api/core/" in url:
            params = kwargs.get("params", {})
            # which dataset is being scoped (the expr is in the path)
            ds = next((d for d in self.search_by_dataset if d and d in url), None)
            return _Resp(self.search_by_dataset.get(ds, {"hits": 0, "entities": []}))
        uuid = url.rstrip("/").rsplit("/", 1)[-1]
        return _Resp(self.records.get(uuid, {}))


def _record(uuid, nb, en=None, exact_aat=None):
    pref = [{"@value": nb, "@language": "no"}]
    if en:
        pref.append({"@value": en, "@language": "en"})
    rec = {"@id": f"https://kulturnav.org/{uuid}", "skos:prefLabel": pref}
    if exact_aat:
        rec["skos:exactMatch"] = [{"@id": f"http://vocab.getty.edu/aat/{exact_aat}"}]
    return rec


def test_search_scopes_each_dataset_and_dedupes():
    ds1, ds2 = "dataset-one", "dataset-two"
    session = _FakeSession(
        search_by_dataset={
            ds1: {"hits": 1, "entities": [{"uuid": "u-akvarell", "name": "akvarell"}]},
            ds2: {"hits": 1, "entities": [{"uuid": "u-akvarell", "name": "akvarell"}]},
        },
        records={"u-akvarell": _record("u-akvarell", "akvarell", "watercolor", exact_aat="300078925")},
    )
    ad = KulturNavAdapter(datasets=[ds1, ds2], session=session)
    cands = ad.search("akvarell", "nb", limit=5)
    # one query per dataset
    core_calls = [c for c in session.calls if "/api/core/" in c]
    assert len(core_calls) == 2
    # deduped to a single candidate despite appearing in both datasets
    assert len(cands) == 1 and cands[0].concept_id == "u-akvarell"


def test_enrich_recomputes_nb_exact_and_attaches_matchings():
    ds = "dataset-one"
    session = _FakeSession(
        search_by_dataset={ds: {"hits": 1, "entities": [
            {"uuid": "u-akvarell", "name": "akvarell"}]}},
        records={"u-akvarell": _record("u-akvarell", "akvarell", "watercolor", exact_aat="300078925")},
    )
    ad = KulturNavAdapter(datasets=[ds], session=session)
    cands = ad.search("akvarell", "nb", limit=5)
    enriched = ad.enrich_candidates(cands, target_lang="en", prefer_langs=["nb", "nn", "en"])
    c = enriched[0]
    assert c.is_exact is True
    assert c.matched_lang == "nb"          # matched the Bokmaal label, not display English
    assert c.pref_label_target == "watercolor"
    assert {"relation": "exactMatch", "uri": "http://vocab.getty.edu/aat/300078925",
            "authority": "aat", "id": "300078925"} in c.matchings


def test_unscoped_search_warns_once(capsys):
    session = _FakeSession(
        search_by_dataset={None: {"hits": 0, "entities": []}},
        records={},
    )
    ad = KulturNavAdapter(datasets=[], session=session)
    ad.search("noe", "nb", limit=5)
    ad.search("annet", "nb", limit=5)
    err = capsys.readouterr().err
    assert err.count("UNSCOPED") == 1   # warned once, not per call


def test_registry_exposes_kulturnav():
    ad = get_adapter("kulturnav", datasets=["d"])
    assert isinstance(ad, KulturNavAdapter)
