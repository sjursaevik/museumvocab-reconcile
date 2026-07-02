"""Non-standard 'accept' markers (e.g. reviewer initials).

Real-world failure: a reviewer used 's' as their accept marker across ~1300
rows. The old parser only recognised a fixed token set (y/yes/true/1/auto/
accept), so every 's' silently fell through as "not accepted" — no warning,
no count, just ~1300 terms missing from the final output. Per the project's
"silent failures are critical bugs" principle, any non-empty accept value
must count as an accept (supporting per-reviewer initials in multi-reviewer
setups), and non-standard tokens must be surfaced via a progress callback so
they show up in the CLI/log rather than disappearing quietly.
"""
from __future__ import annotations

import csv

from museumvocab_reconcile.review import KNOWN_ACCEPT_TOKENS, ingest_review_csv


def _write_csv(path, rows):
    fieldnames = ["id", "accept", "chosen_id", "chosen_target_term", "chosen_facet", "notes"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({**{k: "" for k in fieldnames}, **row})


def test_nonstandard_nonempty_token_is_accepted(tmp_path):
    path = tmp_path / "review.csv"
    _write_csv(path, [{"id": "1", "accept": "s"}, {"id": "2", "accept": "kh"}])
    decisions = ingest_review_csv(path)
    assert decisions["1"].accept is True
    assert decisions["2"].accept is True
    assert decisions["1"].raw_accept == "s"


def test_blank_is_still_not_accepted(tmp_path):
    path = tmp_path / "review.csv"
    _write_csv(path, [{"id": "1", "accept": ""}])
    decisions = ingest_review_csv(path)
    assert decisions["1"].accept is False


def test_known_tokens_still_recognized_without_warning(tmp_path):
    path = tmp_path / "review.csv"
    _write_csv(path, [{"id": str(i), "accept": tok} for i, tok in enumerate(KNOWN_ACCEPT_TOKENS)])
    warnings = []
    decisions = ingest_review_csv(path, progress=warnings.append)
    assert all(d.accept for d in decisions.values())
    assert warnings == []  # no non-standard markers among known tokens


def test_nonstandard_marker_triggers_one_grouped_warning(tmp_path):
    path = tmp_path / "review.csv"
    rows = [{"id": str(i), "accept": "s"} for i in range(5)] + [{"id": "x", "accept": "kh"}]
    _write_csv(path, rows)
    warnings = []
    ingest_review_csv(path, progress=warnings.append)
    assert len(warnings) == 1  # grouped into a single summary, not one per row
    assert "'s'=5" in warnings[0]
    assert "'kh'=1" in warnings[0]
