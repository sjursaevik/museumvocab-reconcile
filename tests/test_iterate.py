"""Iteration support: iterate-select, three-state review decisions, merged
assembly with iteration stamping, and the iteration-profile score_gap demotion.

All offline. Covers the design decisions:

* the `accept` column carries THREE states — accept / explicit reject
  (n/no/false/0/reject/rejected) / blank = undecided — where rejects are final
  (never re-selected) and undecided terms are the iterate-select pool;
* selection carries the reviewer's previous notes forward as prior_notes;
* multi-input assemble merges iterations last-wins per id, pairing each
  classification with ITS OWN iteration's decision, and stamps `iteration`;
* `auto_accept.demote_score_gap_to_review` routes score_gap promotions to
  review under relaxed iteration settings while trusted exacts still
  auto-accept.
"""
from __future__ import annotations

import csv
import json

import pytest

from museumvocab_reconcile.assemble import assemble
from museumvocab_reconcile.iterate import select_for_iteration
from museumvocab_reconcile.model import ClassifiedTerm, Decision
from museumvocab_reconcile.review import (
    KNOWN_REJECT_TOKENS,
    export_review_csv,
    ingest_review_csv,
)
from museumvocab_reconcile.tiering import classify

from conftest import make_candidate, make_profile, make_term


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_csv(path, rows):
    fieldnames = ["id", "accept", "chosen_id", "chosen_target_term", "chosen_facet", "notes"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({**{k: "" for k in fieldnames}, **row})


def _ct(term_id: str, tier: str, *, match_type: str = "", prior_notes: str = "") -> ClassifiedTerm:
    term = make_term(term_id=term_id, nb=f"term{term_id}")
    term.prior_notes = prior_notes
    cand = make_candidate(concept_id=f"30000000{term_id}") if tier != "no_match" else None
    return ClassifiedTerm(
        term=term,
        candidates=[cand] if cand else [],
        best=cand,
        tier=tier,
        reasons=[],
        match_type=match_type or ("no_candidates" if tier == "no_match" else "below_threshold"),
        proposed_facet=cand.facet if cand else None,
        proposed_target_term=cand.pref_label_target if cand else None,
    )


def _decision(term_id: str, *, accept=False, rejected=False, notes="", raw="") -> Decision:
    return Decision(
        id=term_id, accept=accept, rejected=rejected,
        chosen_id=None, chosen_target_term=None, chosen_facet=None,
        notes=notes, raw_accept=raw,
    )


# ---------------------------------------------------------------------------
# three-state ingest
# ---------------------------------------------------------------------------

def test_reject_tokens_are_rejects_not_accepts(tmp_path):
    """BEHAVIOUR CHANGE: 'no' etc. used to count as a (non-standard) accept."""
    path = tmp_path / "review.csv"
    _write_csv(path, [{"id": str(i), "accept": tok} for i, tok in enumerate(sorted(KNOWN_REJECT_TOKENS))])
    decisions = ingest_review_csv(path)
    assert all(d.rejected for d in decisions.values())
    assert not any(d.accept for d in decisions.values())


def test_three_states_coexist(tmp_path):
    path = tmp_path / "review.csv"
    _write_csv(path, [
        {"id": "1", "accept": "yes"},
        {"id": "2", "accept": "no"},
        {"id": "3", "accept": ""},
    ])
    d = ingest_review_csv(path)
    assert d["1"].accept and not d["1"].rejected
    assert d["2"].rejected and not d["2"].accept
    assert not d["3"].accept and not d["3"].rejected  # undecided


def test_rejects_are_reported_via_progress(tmp_path):
    path = tmp_path / "review.csv"
    _write_csv(path, [{"id": "1", "accept": "no"}, {"id": "2", "accept": "reject"}])
    msgs = []
    ingest_review_csv(path, progress=msgs.append)
    assert any("2 row(s) explicitly REJECTED" in m for m in msgs)


def test_reject_tokens_do_not_trip_nonstandard_marker_warning(tmp_path):
    path = tmp_path / "review.csv"
    _write_csv(path, [{"id": "1", "accept": "no"}])
    msgs = []
    ingest_review_csv(path, progress=msgs.append)
    assert not any("non-standard" in m for m in msgs)


# ---------------------------------------------------------------------------
# iterate-select
# ---------------------------------------------------------------------------

def test_default_selection_is_undecided_review_tier():
    classified = [
        _ct("1", "auto_accept", match_type="nb_exact"),  # never in review pool
        _ct("2", "review"),                              # accepted -> out
        _ct("3", "review"),                              # rejected -> out (final)
        _ct("4", "review"),                              # undecided -> selected
        _ct("5", "no_match"),                            # no decision row -> selected
    ]
    decisions = {
        "2": _decision("2", accept=True, raw="y"),
        "3": _decision("3", rejected=True, raw="no"),
        "4": _decision("4"),
    }
    terms, manifest = select_for_iteration(classified, decisions)
    assert [t["id"] for t in terms] == ["4", "5"]
    assert manifest["skipped_counts"]["accepted"] == 1
    assert manifest["skipped_counts"]["rejected"] == 1
    assert manifest["selected_ids"] == ["4", "5"]


def test_reviewer_notes_carried_forward_as_prior_notes():
    classified = [_ct("4", "review")]
    decisions = {"4": _decision("4", notes="too broad - look under textiles")}
    terms, _ = select_for_iteration(classified, decisions)
    assert terms[0]["prior_notes"] == "too broad - look under textiles"


def test_prior_notes_column_in_next_review_export(tmp_path):
    ct = _ct("4", "review", prior_notes="too broad - look under textiles")
    out = tmp_path / "review.csv"
    export_review_csv([ct], out)
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["prior_notes"] == "too broad - look under textiles"
    assert rows[0]["notes"] == ""  # prior notes never pre-fill the new decision


def test_explicit_ids_selection_still_excludes_rejects():
    classified = [_ct("1", "review"), _ct("2", "review")]
    decisions = {"2": _decision("2", rejected=True, raw="no")}
    terms, manifest = select_for_iteration(classified, decisions, ids=("1", "2"))
    assert [t["id"] for t in terms] == ["1"]
    assert manifest["skipped"]["rejected"] == ["2"]


def test_unknown_id_raises_instead_of_silently_shrinking():
    classified = [_ct("1", "review")]
    with pytest.raises(ValueError, match="not present"):
        select_for_iteration(classified, {}, ids=("1", "999"))


def test_match_type_filter_narrows_selection():
    a = _ct("1", "review", match_type="broader_only")
    b = _ct("2", "review", match_type="below_threshold")
    terms, manifest = select_for_iteration([a, b], {}, match_types=("broader_only",))
    assert [t["id"] for t in terms] == ["1"]
    assert manifest["skipped"]["match_type_not_selected"] == ["2"]


def test_selected_terms_are_valid_prepared_input():
    """The subset must round-trip through SourceTerm(**d) like 01_prepared."""
    from museumvocab_reconcile.model import SourceTerm

    terms, _ = select_for_iteration([_ct("4", "review")], {"4": _decision("4", notes="hint")})
    st = SourceTerm(**json.loads(json.dumps(terms[0])))
    assert st.id == "4" and st.prior_notes == "hint"


# ---------------------------------------------------------------------------
# assemble: rejects excluded + counted, iteration stamping
# ---------------------------------------------------------------------------

def test_assemble_excludes_rejects_and_counts_them(tmp_path, profile):
    classified = [_ct("1", "review"), _ct("2", "review")]
    decisions = {
        "1": _decision("1", accept=True, raw="y"),
        "2": _decision("2", rejected=True, raw="no", notes="not an AAT concept"),
    }
    stats = assemble(
        classified, decisions, profile,
        tmp_path / "final.json", tmp_path / "log.txt",
    )
    final = json.loads((tmp_path / "final.json").read_text("utf-8"))
    assert [r["id"] for r in final] == ["1"]
    assert stats["human_rejected"] == 1
    log = (tmp_path / "log.txt").read_text("utf-8")
    assert "rejected in review         1" in log
    assert "not an AAT concept" in log  # the judgement is documented, not dropped


def test_assemble_stamps_iteration_and_logs_history(tmp_path, profile):
    classified = [_ct("1", "review"), _ct("2", "review")]
    decisions = {
        "1": _decision("1", accept=True, raw="y"),
        "2": _decision("2", accept=True, raw="y"),
    }
    stats = assemble(
        classified, decisions, profile,
        tmp_path / "final.json", tmp_path / "log.txt",
        iteration_of={"1": 1, "2": 2},
    )
    final = {r["id"]: r for r in json.loads((tmp_path / "final.json").read_text("utf-8"))}
    assert final["1"]["iteration"] == 1
    assert final["2"]["iteration"] == 2
    assert stats["final_records"] == 2
    log = (tmp_path / "log.txt").read_text("utf-8")
    assert "ITERATIONS (2 passes merged" in log
    assert "iteration 2: 1 terms classified, 1 in final output" in log


def test_assemble_default_iteration_is_one(tmp_path, profile):
    classified = [_ct("1", "review")]
    decisions = {"1": _decision("1", accept=True, raw="y")}
    assemble(classified, decisions, profile, tmp_path / "final.json", tmp_path / "log.txt")
    final = json.loads((tmp_path / "final.json").read_text("utf-8"))
    assert final[0]["iteration"] == 1


# ---------------------------------------------------------------------------
# CLI merge: last iteration wins, decisions pair with their own iteration
# ---------------------------------------------------------------------------

def _classified_dict(ct: ClassifiedTerm) -> dict:
    from museumvocab_reconcile.cli import _classified_to_dict

    return _classified_to_dict(ct)


def test_cli_assemble_merges_iterations_last_wins(tmp_path, profile, monkeypatch):
    from museumvocab_reconcile import cli

    # Iteration 1: term 1 auto-accepted; term 2 review, left UNDECIDED.
    it1 = [_ct("1", "auto_accept", match_type="nb_exact"), _ct("2", "review")]
    # Iteration 2: term 2 re-classified (still review) and ACCEPTED there.
    it2 = [_ct("2", "review")]
    (tmp_path / "03_it1.json").write_text(
        json.dumps([_classified_dict(c) for c in it1]), encoding="utf-8")
    (tmp_path / "03_it2.json").write_text(
        json.dumps([_classified_dict(c) for c in it2]), encoding="utf-8")
    _write_csv(tmp_path / "rev_it1.csv", [{"id": "2", "accept": ""}])
    _write_csv(tmp_path / "rev_it2.csv", [{"id": "2", "accept": "yes"}])

    monkeypatch.setattr(cli, "_load_profile", lambda _p: profile)
    args = type("A", (), {
        "profile": "test.yaml",
        "inp": [str(tmp_path / "03_it1.json"), str(tmp_path / "03_it2.json")],
        "review": [str(tmp_path / "rev_it1.csv"), str(tmp_path / "rev_it2.csv")],
        "out": str(tmp_path / "final.json"),
        "log": str(tmp_path / "log.txt"),
        "linkedart": str(tmp_path / "la.json"),
        "csv": str(tmp_path / "final.csv"),
    })()
    cli.cmd_assemble(args)

    final = {r["id"]: r for r in json.loads((tmp_path / "final.json").read_text("utf-8"))}
    assert set(final) == {"1", "2"}
    assert final["1"]["iteration"] == 1
    assert final["2"]["iteration"] == 2
    assert final["2"]["decision_source"] == "human_review"


def test_cli_assemble_stale_earlier_decision_never_applies(tmp_path, profile, monkeypatch):
    """A term accepted in iteration 1's CSV but RE-CLASSIFIED in iteration 2
    with no iteration-2 decision must NOT inherit the stale iteration-1 accept:
    the human approved a different candidate set."""
    from museumvocab_reconcile import cli

    it1 = [_ct("2", "review")]
    it2 = [_ct("2", "review")]
    (tmp_path / "03_it1.json").write_text(
        json.dumps([_classified_dict(c) for c in it1]), encoding="utf-8")
    (tmp_path / "03_it2.json").write_text(
        json.dumps([_classified_dict(c) for c in it2]), encoding="utf-8")
    _write_csv(tmp_path / "rev_it1.csv", [{"id": "2", "accept": "yes"}])
    _write_csv(tmp_path / "rev_it2.csv", [{"id": "2", "accept": ""}])

    monkeypatch.setattr(cli, "_load_profile", lambda _p: profile)
    args = type("A", (), {
        "profile": "test.yaml",
        "inp": [str(tmp_path / "03_it1.json"), str(tmp_path / "03_it2.json")],
        "review": [str(tmp_path / "rev_it1.csv"), str(tmp_path / "rev_it2.csv")],
        "out": str(tmp_path / "final.json"),
        "log": str(tmp_path / "log.txt"),
        "linkedart": str(tmp_path / "la.json"),
        "csv": str(tmp_path / "final.csv"),
    })()
    cli.cmd_assemble(args)
    final = json.loads((tmp_path / "final.json").read_text("utf-8"))
    assert final == []  # undecided in the winning iteration -> excluded


def test_cli_assemble_mismatched_pairs_abort(tmp_path, profile, monkeypatch):
    from museumvocab_reconcile import cli

    monkeypatch.setattr(cli, "_load_profile", lambda _p: profile)
    args = type("A", (), {
        "profile": "test.yaml",
        "inp": ["a.json", "b.json"],
        "review": ["r.csv"],
        "out": "f.json", "log": "l.txt", "linkedart": "la.json", "csv": "f.csv",
    })()
    with pytest.raises(SystemExit, match="one --review per --inp"):
        cli.cmd_assemble(args)


# ---------------------------------------------------------------------------
# iteration profile: demote score_gap auto-accepts to review
# ---------------------------------------------------------------------------

def test_demote_score_gap_routes_to_review():
    profile = make_profile(
        {"thresholds": {"auto_accept": {"demote_score_gap_to_review": True}}}
    )
    term = make_term()
    # Clear score_gap winner, no exact match: would auto-accept without the flag.
    cands = [
        make_candidate(concept_id="1", score=40, is_exact=False, matched_lang="en", query_lang="nb"),
        make_candidate(concept_id="2", score=20, is_exact=False, matched_lang="en", query_lang="nb"),
    ]
    ct = classify(term, cands, profile)
    assert ct.tier == "review"
    assert ct.match_type == "score_gap_demoted"
    assert any("demote_score_gap_to_review" in r for r in ct.reasons)


def test_demote_flag_never_touches_trusted_exact():
    profile = make_profile(
        {"thresholds": {"auto_accept": {"demote_score_gap_to_review": True}}}
    )
    term = make_term(nb="gobeleng")
    cands = [make_candidate(matched_label="gobeleng", matched_lang="nb", is_exact=True, score=30)]
    ct = classify(term, cands, profile)
    assert ct.tier == "auto_accept"
    assert ct.match_type == "nb_exact"


def test_flag_off_keeps_score_gap_auto_accept():
    profile = make_profile()
    term = make_term()
    cands = [
        make_candidate(concept_id="1", score=40, is_exact=False, matched_lang="en", query_lang="nb"),
        make_candidate(concept_id="2", score=20, is_exact=False, matched_lang="en", query_lang="nb"),
    ]
    ct = classify(term, cands, profile)
    assert ct.tier == "auto_accept"
    assert ct.match_type == "score_gap"
