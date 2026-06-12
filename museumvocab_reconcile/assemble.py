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

from . import __version__
from .config import Profile, normalize_hierarchy_label
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
        matched_lang = ct.best.matched_lang if ct.best else ""
    elif decision and decision.accept:
        chosen_id = decision.chosen_id or (ct.best.concept_id if ct.best else "")
        cand = next((c for c in ct.candidates if c.concept_id == chosen_id), ct.best)
        chosen_uri = cand.uri if cand else ""
        chosen_facet = decision.chosen_facet or ct.proposed_facet
        chosen_target = decision.chosen_target_term or ct.proposed_target_term or ""
        source = "human_review"
        notes = decision.notes or " | ".join(ct.reasons)
        matched_lang = cand.matched_lang if cand else ""
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
        "matched_lang": matched_lang,        # language of the matched authority label
        "match_type": ct.match_type,         # structured tier basis (see model.ClassifiedTerm)
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
    run_info: dict[str, str] | None = None,
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
    _write_log(out_log, classified, final, profile, stats, decisions, run_info)
    return stats


# Flat, human-readable columns for the CSV (nested fields collapsed to strings).
_CSV_COLUMNS = [
    "id", "status", "logical_name", "label",
    "source_main_term", "target_main_term", "parents_source",
    "authority", "authority_id", "authority_link", "facet",
    "matched_lang", "match_type",
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


def _write_log(
    path,
    classified,
    final,
    profile: Profile,
    stats: dict[str, int],
    decisions: dict[str, Decision] | None = None,
    run_info: dict[str, str] | None = None,
) -> None:
    """Human-readable run report. Everything here is derived from the classified
    artifact + review decisions; nothing is recomputed from the network."""
    decisions = decisions or {}
    by_id = {ct.term.id: ct for ct in classified}
    final_ids = {r["id"] for r in final}

    def pct(n: int, d: int) -> str:
        return f"{n} ({n / d:.0%})" if d else "0"

    tiers = Counter(c.tier for c in classified)
    facets = Counter(r["facet"] for r in final if r["facet"])
    hierarchies = Counter(
        ct.proposed_hierarchy for ct in classified
        if ct.term.id in final_ids and ct.proposed_hierarchy
    )
    match_types_all = Counter(c.match_type or "(unset)" for c in classified)
    accept_basis = Counter(
        c.match_type or "(unset)" for c in classified if c.tier == "auto_accept"
    )
    matched_langs = Counter(r["matched_lang"] or "(none)" for r in final)

    # ---- review outcomes ---------------------------------------------------
    review_tier = [ct for ct in classified if ct.tier in ("review", "no_match")]
    rev_accepted = rev_rejected = rev_overridden = 0
    for ct in review_tier:
        d = decisions.get(ct.term.id)
        if d is None:
            continue
        if d.accept:
            rev_accepted += 1
            proposed = ct.best.concept_id if ct.best else ""
            if d.chosen_id and d.chosen_id != proposed:
                rev_overridden += 1
        else:
            rev_rejected += 1
    rev_undecided = len(review_tier) - rev_accepted - rev_rejected

    # ---- translation provenance + LLM advisory agreement -------------------
    translation_src = Counter(r["translation_source"] for r in final)
    n_rec_translation = sum(1 for r in final if r["recommended_translation"])
    n_rec_authority = sum(1 for r in final if r["recommended_authority"])
    facet_pred = [ct for ct in classified if ct.term.expected_facet]
    facet_agree = sum(
        1 for ct in facet_pred if ct.proposed_facet == ct.term.expected_facet
    )
    hier_pred = [ct for ct in classified if ct.term.expected_hierarchy]
    hier_agree = sum(
        1 for ct in hier_pred
        if ct.proposed_hierarchy
        and normalize_hierarchy_label(ct.proposed_hierarchy)
        == normalize_hierarchy_label(ct.term.expected_hierarchy)
    )

    no_match_sample = [
        f"{ct.term.id} {ct.term.main_lang_term!r}"
        for ct in classified if ct.tier == "no_match"
    ]

    lk = profile.lookup
    anchors = profile.facets.preferred_hierarchies
    lines = [
        "=" * 78,
        f"museumvocab-reconcile {__version__}  |  profile: {profile.profile}  |  authority: {profile.authority}",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "=" * 78,
        "",
        "CONFIG (decision-relevant)",
        f"  facets.accept_all          {profile.facets.accept_all}",
        f"  facets.accepted            {profile.facets.accepted}",
        f"  facets.hierarchy_mode      {profile.facets.hierarchy_mode}"
        + (f"  ({len(anchors)} preferred hierarchies: "
           f"{', '.join(sorted(anchors))})" if anchors else ""),
        f"  auto_accept.mode           {profile.thresholds.auto_accept.mode}",
        f"  auto_accept.min_score      {profile.thresholds.auto_accept.min_score}",
        f"  auto_accept.min_score_gap  {profile.thresholds.auto_accept.min_score_gap}",
        f"  trusted_exact_match_langs  {profile.languages.trusted_exact_match_langs}",
        f"  trusted_target_pref_exact  {profile.languages.trusted_target_pref_exact}",
        f"  match_langs                {profile.languages.match_langs}",
        f"  query_order_by_depth       {profile.languages.query_order_by_depth}",
        "  lookup (profile values; CLI flags at lookup time are not recorded here)",
        f"    result_limit             {lk.result_limit}",
        f"    enrich_top_n             {lk.enrich_top_n}",
        f"    min_candidate_score      {lk.min_candidate_score}",
        f"    max_alternative_queries  {lk.max_alternative_queries}",
        f"    alternatives_trigger     {lk.alternatives_trigger_score}",
        "",
        "SUMMARY",
    ]
    lines += [f"  {k:<22} {v}" for k, v in stats.items()]
    n_in = stats.get("input_terms", 0)
    lines += [
        f"  {'authority coverage':<22} {pct(stats.get('with_authority', 0), n_in)} of input terms",
        "",
        "TIER DISTRIBUTION",
    ]
    lines += [f"  {t:<14} {pct(n, n_in)}" for t, n in tiers.most_common()]
    lines += ["", "AUTO-ACCEPT BASIS (match_type of auto-accepted terms)"]
    lines += [f"  {t:<26} {n}" for t, n in accept_basis.most_common()] or ["  (none)"]
    lines += ["", "MATCH TYPE (all classified terms)"]
    lines += [f"  {t:<26} {n}" for t, n in match_types_all.most_common()]
    lines += ["", "MATCHED LANGUAGE (final records)"]
    lines += [f"  {l:<14} {pct(n, len(final))}" for l, n in matched_langs.most_common()]
    lines += [
        "",
        "REVIEW OUTCOMES",
        f"  review/no_match terms      {len(review_tier)}",
        f"  accepted in review         {rev_accepted}",
        f"    with proposal overridden {rev_overridden} (reviewer chose a different concept)",
        f"  rejected in review         {rev_rejected}",
        f"  undecided (excluded)       {rev_undecided}",
        "",
        "TRANSLATION PROVENANCE (final records)",
    ]
    lines += [f"  {k:<14} {n}" for k, n in translation_src.most_common()]
    lines += [
        f"  recommended_translation    {n_rec_translation}",
        f"  recommended_authority      {n_rec_authority}",
    ]
    if facet_pred or hier_pred:
        lines += ["", "LLM ADVISORY AGREEMENT (prediction vs proposed candidate)"]
        if facet_pred:
            lines.append(f"  expected_facet             {pct(facet_agree, len(facet_pred))} of {len(facet_pred)} predictions agree")
        if hier_pred:
            lines.append(f"  expected_hierarchy         {pct(hier_agree, len(hier_pred))} of {len(hier_pred)} predictions agree")
    lines += ["", "FACET DISTRIBUTION (final records)"]
    lines += [f"  {f:<22} {n}" for f, n in facets.most_common()]
    if hierarchies:
        lines += ["", "PREFERRED HIERARCHY DISTRIBUTION (final records)"]
        lines += [f"  {h:<40} {n}" for h, n in hierarchies.most_common()]
    if no_match_sample:
        shown = no_match_sample[:20]
        lines += ["", f"NO-MATCH TERMS ({len(no_match_sample)} total"
                  + (f", first {len(shown)} shown" if len(no_match_sample) > len(shown) else "") + ")"]
        lines += [f"  {t}" for t in shown]
    if run_info:
        lines += ["", "RUN INFO"]
        lines += [f"  {k:<14} {v}" for k, v in run_info.items()]
    lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
