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
from pathlib import Path

from .model import ClassifiedTerm, Decision

COLUMNS = [
    "id", "tier",                       # context (machine output)
    "source_term", "parents", "english_term", "english_source",  # context
    "proposed_id", "proposed_uri", "proposed_facet", "expected_facet", "proposed_aat_facet", "proposed_hierarchy", "proposed_target_term",
    "matched_term", "matched_lang",    # the AAT label that matched the query (+ its language)
    "best_score", "reasons",            # context
    # ---- editable by the reviewer ----
    "accept", "chosen_id", "chosen_target_term", "chosen_facet", "notes",
]


def _runner_up_note(ct: ClassifiedTerm, n: int = 3) -> str:
    alts = [
        f"{c.concept_id}={c.pref_label_target or c.matched_label}({c.score:.0f})"
        for c in ct.candidates[1 : n + 1]
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
                    "proposed_target_term": ct.proposed_target_term or "",
                    "matched_term": best.matched_label if best else "",
                    "matched_lang": best.matched_lang if best else "",
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


def ingest_review_csv(path: str | Path) -> dict[str, Decision]:
    text = _read_csv_text(path)
    lines = text.splitlines()
    if not lines:
        return {}
    delimiter = _detect_delimiter(lines[0])

    decisions: dict[str, Decision] = {}
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
        accept = accept_raw in {"y", "yes", "true", "1", "auto", "accept"}
        decisions[rid] = Decision(
            id=rid,
            accept=accept,
            chosen_id=(row.get("chosen_id") or "").strip() or None,
            chosen_target_term=(row.get("chosen_target_term") or "").strip() or None,
            chosen_facet=(row.get("chosen_facet") or "").strip() or None,
            notes=(row.get("notes") or "").strip(),
        )
    return decisions
