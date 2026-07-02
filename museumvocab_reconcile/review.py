"""Human-in-the-loop review layer.

``export_review_csv`` writes one row per term with the machine proposal
pre-filled and editable columns; a cataloger edits it in a spreadsheet.
``ingest_review_csv`` reads decisions back so ``assemble`` can apply overrides,
keeping machine-proposed and human-chosen values distinct in provenance.

By default only ``review`` and ``no_match`` terms are exported (the queue that
needs a human). Set ``include_auto=True`` to export every term so a reviewer
can also override auto-accepted ones — human-in-the-loop at *every* step.
"""
from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path
from typing import Callable

from .model import ClassifiedTerm, Decision

# Tokens that mean "accept" without triggering a non-standard-marker warning.
# Any OTHER non-empty value in the `accept` column is also treated as an
# accept (e.g. a reviewer's initials in a multi-reviewer setup) but is
# flagged, since it may equally be a typo — see ingest_review_csv.
KNOWN_ACCEPT_TOKENS = {"y", "yes", "true", "1", "auto", "accept"}

# Column order is tuned for the human reviewer working left-to-right in a
# spreadsheet; it carries no logic. Both writer (DictWriter) and reader
# (DictReader) key by name, so this list may be reordered freely without
# affecting ingest, assemble, or the tests.
COLUMNS = [
    "id",
    # ---- readability strip: the four labels to eyeball side by side --------
    # source nb | source en | matched AAT label | proposed AAT en label.
    # matched_lang hugs matched_term: nb/nn here is the trusted signal.
    "source_term", "english_term", "matched_term", "proposed_target_term",
    "matched_lang",
    "tier", "match_type",               # machine verdict + why
    # ---- editable by the reviewer (accept is the primary write target) -----
    "accept", "chosen_id", "chosen_target_term", "chosen_facet", "notes",
    "reasons",                          # runner-up alts, beside the edit zone for overrides
    # ---- deeper context: scroll right only when digging / overriding ------
    "parents",
    "proposed_id", "proposed_uri", "proposed_facet",
    "proposed_aat_facet", "proposed_hierarchy",
    "english_source", "best_score",
    # advisory LLM facet/hierarchy predictions from the translate step
    "expected_facet", "expected_hierarchy",
    # ---- deepen-stage advisory second opinion (blank unless deepen ran) ----
    "deep_used", "deep_candidates_added",
    "llm_recommended_id", "llm_recommended_target_term", "llm_confidence", "llm_vs_rule", "llm_reason",
]


def _runner_up_note(ct: ClassifiedTerm, n: int = 3) -> str:
    """Top-n other candidates, score order. The proposal is excluded by
    IDENTITY, not by position: steering and the match_langs preference can make
    `best` something other than candidates[0], and slicing [1:] then hid the
    true top-scored candidate from the review row while duplicating the
    proposal (the 'Sari' bug)."""
    others = [c for c in ct.candidates if c is not ct.best][:n]
    alts = [
        f"{c.concept_id}={c.pref_label_target or c.matched_label}({c.score:.0f})"
        for c in others
    ]
    return "; ".join(alts)


def export_review_csv(
    classified: list[ClassifiedTerm], path: str | Path, *, include_auto: bool = False
) -> int:
    rows = [c for c in classified if include_auto or c.tier != "auto_accept"]
    # utf-8-sig writes a BOM so Excel opens it as UTF-8 (correct Norwegian chars)
    # rather than guessing a legacy code page.
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for ct in rows:
            best = ct.best
            w.writerow(
                {
                    "id": ct.term.id,
                    "tier": ct.tier,
                    "match_type": ct.match_type,
                    "source_term": ct.term.main_lang_term,
                    "parents": " > ".join(ct.term.parents_source),
                    # the English term, whatever its origin; english_source says where from
                    "english_term": ct.term.main_target_term,
                    "english_source": ct.term.target_source,
                    "proposed_id": best.concept_id if best else "",
                    "proposed_uri": best.uri if best else "",
                    "proposed_facet": ct.proposed_facet or "",
                    # advisory LLM facet prediction from the translate step
                    "expected_facet": ct.term.expected_facet or "",
                    "proposed_aat_facet": ct.proposed_aat_facet or "",
                    "proposed_hierarchy": ct.proposed_hierarchy or "",
                    # advisory LLM hierarchy prediction from the translate step
                    "expected_hierarchy": ct.term.expected_hierarchy or "",
                    "proposed_target_term": ct.proposed_target_term or "",
                    # matched_term/matched_lang describe an EXACT label match. For
                    # a fuzzy proposal the reconcile display name and the query
                    # language carry no match evidence (matched_lang is just the
                    # query echo), so blank them rather than mislead the reviewer.
                    "matched_term": (best.matched_label if best and best.is_exact else ""),
                    "matched_lang": (best.matched_lang if best and best.is_exact else ""),
                    "deep_used": "yes" if ct.deep_used else "",
                    "deep_candidates_added": ct.deep_candidates_added or "",
                    "llm_recommended_id": ct.llm_recommended_id or "",
                    "llm_recommended_target_term": ct.llm_recommended_target_term or "",
                    "llm_confidence": ct.llm_recommendation_confidence or "",
                    # quick scan column: do the two opinions agree?
                    "llm_vs_rule": (
                        "" if ct.llm_agrees_with_rule is None
                        else ("agree" if ct.llm_agrees_with_rule else "DIFFERS")
                    ),
                    "llm_reason": ct.llm_recommendation_reason or "",
                    "best_score": f"{best.score:.1f}" if best else "",
                    "reasons": " | ".join(ct.reasons)
                    + (f" || alts: {_runner_up_note(ct)}" if ct.candidates else ""),
                    # editable cells pre-filled with the proposal so "accept"
                    # with no edits = take the machine suggestion.
                    "accept": "auto" if ct.tier == "auto_accept" else "",
                    "chosen_id": best.concept_id if best else "",
                    "chosen_target_term": ct.proposed_target_term or "",
                    "chosen_facet": ct.proposed_facet or "",
                    "notes": "",
                }
            )
    return len(rows)


def _read_csv_text(path: str | Path) -> str:
    """Decode a CSV that may have been re-saved by Excel.

    Tries UTF-8 (with/without BOM) first, then Windows-1252, then Latin-1 as a
    last resort that never fails — so a code-page mismatch can't crash the run.
    """
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _detect_delimiter(header_line: str) -> str:
    """Norwegian/European Excel often uses ';'. Pick whatever the header uses."""
    counts = {d: header_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def ingest_review_csv(
    path: str | Path, *, progress: Callable[[str], None] | None = None
) -> dict[str, Decision]:
    """Read reviewer decisions back from the review CSV.

    Any NON-EMPTY value in `accept` counts as an accept — not just the known
    tokens (y/yes/true/1/auto/accept). This lets multiple reviewers write
    their initials in `accept` as a lightweight "who accepted this" signal
    (kept verbatim in Decision.raw_accept). Tokens outside the known set are
    still flagged via `progress`, since a non-standard token is equally
    consistent with a typo — silently dropping ~1000+ rows because of one
    unrecognized token is exactly the kind of silent failure this project
    treats as a bug, so we warn instead of guessing.
    """
    text = _read_csv_text(path)
    lines = text.splitlines()
    if not lines:
        return {}
    delimiter = _detect_delimiter(lines[0])

    decisions: dict[str, Decision] = {}
    nonstandard_markers: Counter[str] = Counter()
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None or "id" not in [(f or "").strip() for f in reader.fieldnames]:
        raise ValueError(
            f"Could not find an 'id' column in {path}. Detected delimiter "
            f"{delimiter!r}; header was: {lines[0]!r}. If you edited in Excel, "
            "save as 'CSV UTF-8 (Comma delimited)'."
        )
    # Normalise header whitespace just in case.
    for row in reader:
        row = {(k or "").strip(): v for k, v in row.items()}
        rid = (row.get("id") or "").strip()
        if not rid:
            continue
        accept_raw = (row.get("accept") or "").strip().lower()
        accept = bool(accept_raw)
        if accept and accept_raw not in KNOWN_ACCEPT_TOKENS:
            nonstandard_markers[accept_raw] += 1
        decisions[rid] = Decision(
            id=rid,
            accept=accept,
            chosen_id=(row.get("chosen_id") or "").strip() or None,
            chosen_target_term=(row.get("chosen_target_term") or "").strip() or None,
            chosen_facet=(row.get("chosen_facet") or "").strip() or None,
            notes=(row.get("notes") or "").strip(),
            raw_accept=accept_raw,
        )
    if progress and nonstandard_markers:
        total = sum(nonstandard_markers.values())
        breakdown = ", ".join(
            f"{tok!r}={n}" for tok, n in nonstandard_markers.most_common()
        )
        progress(
            f"ingest_review_csv: {total} row(s) accepted via non-standard "
            f"'accept' marker(s) — treated as accept (e.g. reviewer initials), "
            f"but verify these are intentional: {breakdown}"
        )
    return decisions
