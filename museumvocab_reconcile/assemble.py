"""Assembly stage: final vocabulary JSON + Linked Art snippets + log.

Merges classified terms with the human decisions (from the review CSV). A term
becomes a final record if it was auto-accepted, or accepted in review. The
Linked Art snippet is derived from the profile's facet -> property map.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Profile
from .model import ClassifiedTerm, Decision


def _linked_art_snippet(uri: str, facet: str | None, label: str, profile: Profile) -> dict | None:
    """Map facet -> Linked Art slot using the profile's linked_art_property."""
    if not uri or not facet:
        return None
    spec = profile.facets.linked_art_property.get(facet)
    if not spec:
        return None
    # Minimal, slot-aware fragment; downstream emission attaches it to the
    # right Linked Art resource (production event vs object).
    return {
        "target": spec["target"],          # "production" | "object" | ...
        "property": spec["prop"],          # "classified_as" | "made_of" | ...
        "value": {"id": uri, "type": "Type", "_label": label},
    }


def build_final_record(ct: ClassifiedTerm, decision: Decision | None, profile: Profile) -> dict[str, Any] | None:
    term = ct.term
    if ct.tier == "auto_accept" and decision is None:
        chosen_id = ct.best.concept_id if ct.best else ""
        chosen_uri = ct.best.uri if ct.best else ""
        chosen_facet = ct.proposed_facet
        chosen_target = ct.proposed_target_term or ""
        source = "auto_accept"
        notes = " | ".join(ct.reasons)
    elif decision and decision.accept:
        chosen_id = decision.chosen_id or (ct.best.concept_id if ct.best else "")
        cand = next((c for c in ct.candidates if c.concept_id == chosen_id), ct.best)
        chosen_uri = cand.uri if cand else ""
        chosen_facet = decision.chosen_facet or ct.proposed_facet
        chosen_target = decision.chosen_target_term or ct.proposed_target_term or ""
        source = "human_review"
        notes = decision.notes or " | ".join(ct.reasons)
    else:
        return None  # rejected or unresolved -> excluded from final output

    src_target = term.main_target_term or ""
    # A translation is "recommended" when it isn't authoritative source data:
    # either it came from the LLM/human (target_source) or the authority match
    # proposes an English the source lacked / differs from it.
    recommended_translation = bool(chosen_target) and (
        term.target_source != "source_data"
        or chosen_target.casefold() != src_target.casefold()
    )
    recommended_authority = bool(chosen_uri)

    return {
        "id": term.id,
        "status": term.status,
        "logical_name": term.logical_name,
        "label": term.label,
        "source_main_term": term.main_lang_term,
        "target_main_term": chosen_target,
        "parents_source": term.parents_source,
        "parents_target": term.parents_target,
        "authority": profile.authority,
        "authority_id": chosen_id,
        "authority_link": chosen_uri,
        "facet": chosen_facet,
        "linked_art": _linked_art_snippet(chosen_uri, chosen_facet, chosen_target, profile),
        "decision_source": source,           # auto_accept | human_review
        "translation_source": term.target_source,  # source_data | llm | human
        "recommendation": recommended_translation or recommended_authority,
        "recommended_translation": recommended_translation,
        "recommended_authority": recommended_authority,
        "notes": notes,
    }


def assemble(
    classified: list[ClassifiedTerm],
    decisions: dict[str, Decision],
    profile: Profile,
    out_json: str | Path,
    out_log: str | Path,
    out_linkedart: str | Path | None = None,
    out_csv: str | Path | None = None,
) -> dict[str, int]:
    final: list[dict[str, Any]] = []
    for ct in classified:
        rec = build_final_record(ct, decisions.get(ct.term.id), profile)
        if rec is not None:
            final.append(rec)

    Path(out_json).write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if out_linkedart:
        la = [{"id": r["id"], **r["linked_art"]} for r in final if r["linked_art"]]
        Path(out_linkedart).write_text(
            json.dumps(la, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if out_csv:
        _write_final_csv(out_csv, final)

    stats = {
        "input_terms": len(classified),
        "final_records": len(final),
        "with_authority": sum(1 for r in final if r["authority_link"]),
        "with_target_term": sum(1 for r in final if r["target_main_term"]),
        "auto_accepted": sum(1 for r in final if r["decision_source"] == "auto_accept"),
        "human_reviewed": sum(1 for r in final if r["decision_source"] == "human_review"),
    }
    _write_log(out_log, classified, final, profile, stats)
    return stats


# Flat, human-readable columns for the CSV (nested fields collapsed to strings).
_CSV_COLUMNS = [
    "id", "status", "logical_name", "label",
    "source_main_term", "target_main_term", "parents_source",
    "authority", "authority_id", "authority_link", "facet",
    "linked_art_target", "linked_art_property",
    "decision_source", "translation_source",
    "recommendation", "recommended_translation",
    "recommended_authority", "notes",
]


def _write_final_csv(path: str | Path, final: list[dict[str, Any]]) -> None:
    """Write a flattened, Excel-friendly CSV (UTF-8 BOM) of the final records."""
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in final:
            la = r.get("linked_art") or {}
            w.writerow(
                {
                    **{k: r.get(k) for k in _CSV_COLUMNS},
                    "parents_source": " > ".join(r.get("parents_source") or []),
                    "linked_art_target": la.get("target", ""),
                    "linked_art_property": la.get("property", ""),
                }
            )


def _write_log(path, classified, final, profile: Profile, stats: dict[str, int]) -> None:
    tiers = Counter(c.tier for c in classified)
    facets = Counter(r["facet"] for r in final if r["facet"])
    lines = [
        "=" * 78,
        f"museumvocab-reconcile  |  profile: {profile.profile}  |  authority: {profile.authority}",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "=" * 78,
        "",
        "CONFIG (decision-relevant)",
        f"  facets.accept_all          {profile.facets.accept_all}",
        f"  facets.preferred           {profile.facets.preferred}",
        f"  auto_accept.mode           {profile.thresholds.auto_accept.mode}",
        f"  auto_accept.min_score      {profile.thresholds.auto_accept.min_score}",
        f"  auto_accept.min_score_gap  {profile.thresholds.auto_accept.min_score_gap}",
        f"  trusted_exact_match_langs  {profile.languages.trusted_exact_match_langs}",
        "",
        "SUMMARY",
    ]
    lines += [f"  {k:<22} {v}" for k, v in stats.items()]
    lines += ["", "TIER DISTRIBUTION"]
    lines += [f"  {t:<14} {n}" for t, n in tiers.most_common()]
    lines += ["", "FACET DISTRIBUTION (final records)"]
    lines += [f"  {f:<22} {n}" for f, n in facets.most_common()]
    lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
