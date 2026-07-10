"""Pin the resource-export behaviour: nothing silently dropped, dedupe only
collapses identical (uri, target, property), conflicts stay resolvable."""
import json

import pytest

from museumvocab_reconcile.export_resource import (
    build_resource_doc,
    export_resource,
    normalize_label,
)


def _rec(id, nb, uri_id, *, en="x", facet="work_types",
         target="object", prop="classified_as", linked_art=...):
    if linked_art is ...:
        linked_art = {
            "target": target,
            "property": prop,
            "value": {
                "id": f"http://vocab.getty.edu/aat/{uri_id}",
                "type": "Type",
                "_label": en,
            },
        }
    return {
        "id": id,
        "source_main_term": nb,
        "target_main_term": en,
        "authority": "aat",
        "authority_id": uri_id,
        "authority_link": f"http://vocab.getty.edu/aat/{uri_id}",
        "facet": facet,
        "linked_art": linked_art,
        "aat_ancestors": [{"id": "300000001", "label": "parent"}],
        "parents_source": ["Design"],
        "match_type": "nb_exact",
        "decision_source": "auto_accept",
        "matched_lang": "nb",
        "translation_source": "source_data",
    }


def test_normalizer_contract():
    assert normalize_label("  Illustrert   Bok ") == "illustrert bok"
    assert normalize_label("MALERI") == "maleri"
    assert normalize_label("") == ""
    assert normalize_label(None) == ""


def test_identical_rows_collapse_and_merge_ids():
    doc = build_resource_doc(
        [_rec("1", "Album", "300026690"), _rec("2", "Album", "300026690")],
        title="objectnames",
    )
    assert doc["counts"]["entries"] == 1
    (entry,) = doc["entries"].values()
    assert entry["museumplus_ids"] == ["1", "2"]
    # both MuseumPlus ids resolve to the same entry
    eid = doc["by_label"]["album"]
    assert doc["by_museumplus_id"]["1"] == eid
    assert doc["by_museumplus_id"]["2"] == eid
    assert doc["conflicts"] == {}


def test_same_label_different_uri_is_a_conflict_not_a_pick():
    doc = build_resource_doc(
        [_rec("1", "Mappe", "300111111"), _rec("2", "Mappe", "300222222")],
        title="objectnames",
    )
    assert "mappe" not in doc["by_label"]
    assert len(doc["conflicts"]["mappe"]) == 2
    # both entries survive and stay resolvable via uri and via id
    assert doc["counts"]["entries"] == 2
    assert doc["by_museumplus_id"]["1"] != doc["by_museumplus_id"]["2"]
    for uri_id in ("300111111", "300222222"):
        assert doc["by_authority"][f"http://vocab.getty.edu/aat/{uri_id}"]


def test_same_uri_different_slot_is_not_a_duplicate():
    # the Bladgull pattern: one concept, two Linked Art slots
    doc = build_resource_doc(
        [
            _rec("1", "Bladgull", "300264831", facet="materials",
                 target="object", prop="made_of"),
            _rec("2", "Bladgull", "300264831", facet="techniques",
                 target="production", prop="classified_as"),
        ],
        title="objectnames",
    )
    assert doc["counts"]["entries"] == 2
    # the label is ambiguous between the two slots -> conflict, both kept
    assert len(doc["conflicts"]["bladgull"]) == 2
    assert len(doc["by_authority"]["http://vocab.getty.edu/aat/300264831"]) == 2


def test_unmapped_facet_is_listed_not_lost():
    doc = build_resource_doc(
        [_rec("1", "Rokokko", "300021438", facet="styles_periods",
              linked_art=None)],
        title="objectnames",
    )
    assert doc["counts"]["entries"] == 0
    assert doc["skipped_no_snippet"] == [
        {"id": "1", "label": "Rokokko", "facet": "styles_periods"}
    ]


def test_entry_carries_lookup_payload():
    doc = build_resource_doc([_rec("1", "Albarello", "300198823",
                                   en="albarelli")], title="objectnames")
    entry = doc["entries"][doc["by_label"]["albarello"]]
    assert entry["linked_art"]["value"]["_label"] == "albarelli"
    assert entry["aat_ancestors"] and entry["parents_source"]
    assert entry["provenance"]["decision_source"] == "auto_accept"
    assert entry["labels"] == {"nb": "Albarello", "en": "albarelli"}


def test_synonym_labels_collapsing_to_one_entry_stay_visible():
    # Antemensale / Antependium both map to aat 'antependia' -> one entry,
    # two by_label keys, alternate nb label recorded on the entry.
    doc = build_resource_doc(
        [_rec("1", "Antemensale", "300265255", en="antependia"),
         _rec("2", "Antependium", "300265255", en="antependia")],
        title="objectnames",
    )
    assert doc["counts"]["entries"] == 1
    eid = doc["by_label"]["antemensale"]
    assert doc["by_label"]["antependium"] == eid
    assert doc["entries"][eid]["labels"]["nb_alt"] == ["Antependium"]


def test_malformed_record_fails_loudly():
    with pytest.raises(ValueError):
        build_resource_doc([{"id": "", "source_main_term": "x"}], title="t")
    with pytest.raises(ValueError):
        build_resource_doc([{"id": "1", "source_main_term": ""}], title="t")


def test_export_roundtrip(tmp_path):
    inp = tmp_path / "04_final.json"
    inp.write_text(json.dumps([_rec("1", "Maleri", "300033618",
                                    en="paintings (visual works)")]), "utf-8")
    out = tmp_path / "05_resource.json"
    counts = export_resource(inp, out, title="objectnames",
                             profile_name="objectnames.aat.yaml")
    assert counts["entries"] == 1
    doc = json.loads(out.read_text("utf-8"))
    assert doc["title"] == "objectnames"
    assert doc["profile"] == "objectnames.aat.yaml"
    assert doc["normalizer"] == "casefold+collapse-whitespace"
    assert doc["by_label"]["maleri"] in doc["entries"]
