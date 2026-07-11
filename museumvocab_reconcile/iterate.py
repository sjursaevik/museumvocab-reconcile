"""Iteration support: re-run unresolved terms with updated settings.

After a review pass, some terms are accepted, some explicitly rejected, and
some left undecided. ``select_for_iteration`` extracts the still-unresolved
subset as a 01-style prepared artifact so the EXISTING pipeline stages
(lookup -> classify [-> deepen] -> review-export -> assemble) can be re-run on
just those terms — typically with an iteration profile that relaxes lookup
settings (and sets ``auto_accept.demote_score_gap_to_review``).

Selection default: tier in {review, no_match} AND the review decision is
undecided (no row, or blank ``accept``). Never selected:

* accepted terms (they are resolved);
* explicitly REJECTED terms (a human ruled "no match exists" — re-offering
  them every iteration would erode the value of that judgement);
* auto-accepted terms (unless a reviewer overrode them, they never reach the
  review queue).

Everything is documented: a manifest records the criteria, sources and the
selected/excluded id sets, and each selected term carries the reviewer's
previous ``notes`` forward as ``SourceTerm.prior_notes`` (a read-only column
in the next review CSV) — the "too broad, look under X" comment is exactly
the context the second pass needs.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .model import ClassifiedTerm, Decision

DEFAULT_TIERS = ("review", "no_match")


def _undecided(d: Decision | None) -> bool:
    return d is None or (not d.accept and not d.rejected)


def select_for_iteration(
    classified: list[ClassifiedTerm],
    decisions: dict[str, Decision],
    *,
    tiers: tuple[str, ...] = DEFAULT_TIERS,
    match_types: tuple[str, ...] = (),
    ids: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (prepared-term dicts for the next iteration, manifest dict).

    ``ids`` is an explicit override: when given, exactly those ids are
    selected (still excluding explicit rejects, which are reported in the
    manifest rather than silently dropped) and ``tiers``/``match_types`` are
    ignored. Unknown ids raise — a typo must not silently shrink the re-run.
    """
    by_id = {ct.term.id: ct for ct in classified}
    if ids:
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            raise ValueError(
                f"iterate-select: {len(unknown)} requested id(s) not present in "
                f"the classified input: {', '.join(unknown[:5])}"
                + (f" (+{len(unknown) - 5} more)" if len(unknown) > 5 else "")
            )

    selected: list[ClassifiedTerm] = []
    skipped: dict[str, list[str]] = {
        "accepted": [], "rejected": [], "tier_not_selected": [],
        "match_type_not_selected": [],
    }
    for ct in classified:
        d = decisions.get(ct.term.id)
        if ids:
            if ct.term.id not in ids:
                continue
            # Explicit rejects are excluded even from an --ids selection: a
            # human ruled these out. Reported in the manifest, never silent.
            if d is not None and d.rejected:
                skipped["rejected"].append(ct.term.id)
                continue
            selected.append(ct)
            continue
        if d is not None and d.accept:
            skipped["accepted"].append(ct.term.id)
            continue
        if d is not None and d.rejected:
            skipped["rejected"].append(ct.term.id)
            continue
        if ct.tier not in tiers:
            skipped["tier_not_selected"].append(ct.term.id)
            continue
        if match_types and ct.match_type not in match_types:
            skipped["match_type_not_selected"].append(ct.term.id)
            continue
        selected.append(ct)

    terms: list[dict[str, Any]] = []
    for ct in selected:
        td = asdict(ct.term)
        d = decisions.get(ct.term.id)
        # Carry the reviewer's context into the next pass (read-only).
        td["prior_notes"] = (d.notes if d else "") or ct.term.prior_notes or ""
        terms.append(td)

    manifest: dict[str, Any] = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "criteria": {
            "tiers": list(tiers) if not ids else None,
            "match_types": list(match_types) if not ids else None,
            "ids": list(ids) or None,
        },
        "input_terms": len(classified),
        "selected": len(terms),
        "selected_ids": [t["id"] for t in terms],
        "skipped": {k: v for k, v in skipped.items() if v},
        "skipped_counts": {k: len(v) for k, v in skipped.items()},
    }
    return terms, manifest
