"""Export stage: 04_final.json -> a MongoDB `resources` collection document.

Produces one JSON document per vocabulary, shaped for O(1) lookup inside the
LinkedArtConversion MongoDB Atlas trigger (which loads config docs via
``resources.findOne({title: ...})``).

Document shape
--------------
{
  "title": "<resource title>",            # trigger lookup key
  "profile": "<profile file name>",
  "generated_at": "<ISO timestamp>",
  "normalizer": "casefold+collapse-whitespace",
  "counts": {...},
  "entries": { "<entry_id>": {...}, ... },     # canonical store
  "by_label":          { "<normalized nb label>": "<entry_id>" },
  "by_authority":      { "<uri>": ["<entry_id>", ...] },
  "by_museumplus_id":  { "<id>": "<entry_id>" },
  "conflicts":         { "<normalized label>": ["<entry_id>", ...] },
  "skipped_no_snippet": [ {"id","label","facet"}, ... ],
}

Design rules (see project conventions):
- Vocabulary entries are NEVER silently dropped. Rows without a Linked Art
  snippet (unmapped facet) are listed in ``skipped_no_snippet``; labels that
  map to multiple distinct (uri, slot) pairs go to ``conflicts`` — they stay
  resolvable via ``by_authority`` / ``by_museumplus_id``, just not via the
  ambiguous label.
- Deduplication collapses only rows identical on (uri, target, property);
  their MuseumPlus ids are merged so provenance is preserved.
- Redundant-broader-term collapse is deliberately NOT done here: "Krukke" and
  "Beholder" are both legitimate lookup entries for *different* objects. That
  collapse belongs at object level, inside the trigger.

The label normalizer here is the contract the trigger's JavaScript must
mirror exactly: ``" ".join(s.split()).casefold()``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def normalize_label(s: str) -> str:
    """Casefold + trim + collapse internal whitespace.

    This is the shared label-join contract between the Python exporter and the
    JavaScript trigger. Change it in both places or not at all.
    """
    return " ".join((s or "").split()).casefold()


def _entry_id(rec: dict[str, Any]) -> str:
    la = rec["linked_art"]
    return f"{rec['authority_id']}:{la['target']}.{la['property']}"


def _build_entry(rec: dict[str, Any]) -> dict[str, Any]:
    la = rec["linked_art"]
    return {
        "uri": rec["authority_link"],
        "authority": rec["authority"],
        "authority_id": rec["authority_id"],
        "facet": rec["facet"],
        "linked_art": la,
        "labels": {
            "nb": rec["source_main_term"],
            "en": rec["target_main_term"],
        },
        "aat_ancestors": rec.get("aat_ancestors") or [],
        "parents_source": rec.get("parents_source") or [],
        "museumplus_ids": [rec["id"]],
        "provenance": {
            "match_type": rec.get("match_type", ""),
            "decision_source": rec.get("decision_source", ""),
            "matched_lang": rec.get("matched_lang", ""),
            "translation_source": rec.get("translation_source", ""),
        },
    }


def build_resource_doc(
    final_records: list[dict[str, Any]],
    *,
    title: str,
    profile_name: str = "",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Pure transform: assembled final records -> resource document.

    Raises ValueError on records missing the fields the document is keyed by
    (id / source_main_term) — a malformed input must fail loudly, not produce
    a silently incomplete lookup table.
    """
    entries: dict[str, dict[str, Any]] = {}
    by_label: dict[str, str] = {}
    by_authority: dict[str, list[str]] = {}
    by_museumplus_id: dict[str, str] = {}
    label_to_eids: dict[str, list[str]] = {}
    skipped: list[dict[str, Any]] = []

    for rec in final_records:
        if not rec.get("id") or not rec.get("source_main_term"):
            raise ValueError(
                f"record missing id/source_main_term: {json.dumps(rec)[:200]}"
            )
        if not rec.get("linked_art"):
            # Facet had no linked_art_property mapping in the profile. Keep it
            # visible: it is not part of the lookup table, but it is not lost.
            skipped.append(
                {"id": rec["id"], "label": rec["source_main_term"],
                 "facet": rec.get("facet", "")}
            )
            continue

        eid = _entry_id(rec)
        if eid in entries:
            # identical (uri, target, property): collapse, merge provenance ids
            if rec["id"] not in entries[eid]["museumplus_ids"]:
                entries[eid]["museumplus_ids"].append(rec["id"])
            # a DIFFERENT nb label collapsing into the same entry is a synonym
            # (e.g. Antemensale / Antependium -> antependia): keep it visible.
            labels = entries[eid]["labels"]
            if normalize_label(rec["source_main_term"]) != normalize_label(
                labels["nb"]
            ):
                alts = labels.setdefault("nb_alt", [])
                if rec["source_main_term"] not in alts:
                    alts.append(rec["source_main_term"])
        else:
            entries[eid] = _build_entry(rec)
            by_authority.setdefault(rec["authority_link"], []).append(eid)

        by_museumplus_id[rec["id"]] = eid

        norm = normalize_label(rec["source_main_term"])
        eids = label_to_eids.setdefault(norm, [])
        if eid not in eids:
            eids.append(eid)

    conflicts: dict[str, list[str]] = {}
    for norm, eids in label_to_eids.items():
        if len(eids) == 1:
            by_label[norm] = eids[0]
        else:
            # same label, different (uri, slot): a label lookup cannot decide.
            # Explicit conflict — resolvable via authority uri / museumplus id.
            conflicts[norm] = eids

    return {
        "title": title,
        "profile": profile_name,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "normalizer": "casefold+collapse-whitespace",
        "counts": {
            "input_records": len(final_records),
            "entries": len(entries),
            "labels": len(by_label),
            "conflicts": len(conflicts),
            "skipped_no_snippet": len(skipped),
        },
        "entries": entries,
        "by_label": by_label,
        "by_authority": by_authority,
        "by_museumplus_id": by_museumplus_id,
        "conflicts": conflicts,
        "skipped_no_snippet": skipped,
    }


def export_resource(
    inp: str | Path,
    out: str | Path,
    *,
    title: str,
    profile_name: str = "",
) -> dict[str, Any]:
    """Read 04_final.json, write the resource document, return its counts."""
    final_records = json.loads(Path(inp).read_text("utf-8"))
    doc = build_resource_doc(
        final_records, title=title, profile_name=profile_name
    )
    Path(out).write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), "utf-8"
    )
    return doc["counts"]
