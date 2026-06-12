"""classify must never convert lookup ERRORs into no_match.

lookup records persistent HTTP failures (rate limits, network) as entries with
an ``error`` key and an empty candidate list, and its resume logic retries
them. The gate under test stops `classify` from consuming such entries — which
would tier them ``no_match`` ("no candidates returned") and permanently
mislabel a good term — unless the user explicitly drops them with
--skip-errors (loudly, and only for that run).

Offline: drives the CLI directly with a temp 02_candidates.json and a bundled
profile; classify itself needs no network.
"""
from __future__ import annotations

import json

import pytest

from museumvocab_reconcile.cli import main

PROFILE = "techniques.aat.yaml"  # bundled; resolved by name


def _term(tid: str) -> dict:
    return {
        "id": tid,
        "status": "active",
        "logical_name": None,
        "label": None,
        "main_lang_term": "akvarell",
        "main_target_term": "",
        "main_level": 1,
        "parents_source": [],
        "parents_target": [],
    }


def _candidates_file(tmp_path, entries):
    p = tmp_path / "02_candidates.json"
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return p


GOOD = {"term": _term("100"), "candidates": []}
ERRORED = {"term": _term("200"), "candidates": [], "error": "HTTPError('429')"}


def test_classify_aborts_on_errored_entries(tmp_path):
    inp = _candidates_file(tmp_path, [GOOD, ERRORED])
    out = tmp_path / "03_classified.json"
    with pytest.raises(SystemExit) as exc:
        main(["--profile", PROFILE, "classify", "--inp", str(inp), "--out", str(out)])
    msg = str(exc.value)
    assert "ERROR" in msg and "200" in msg and "re-run lookup" in msg
    assert not out.exists(), "abort must not write a partial classified artifact"


def test_skip_errors_drops_errored_terms_loudly(tmp_path, capsys):
    inp = _candidates_file(tmp_path, [GOOD, ERRORED])
    out = tmp_path / "03_classified.json"
    main([
        "--profile", PROFILE, "classify",
        "--inp", str(inp), "--out", str(out), "--skip-errors",
    ])
    classified = json.loads(out.read_text("utf-8"))
    ids = [c["term"]["id"] for c in classified]
    assert ids == ["100"], "errored term must be dropped, not tiered no_match"
    captured = capsys.readouterr().out
    assert "WARNING" in captured and "200" in captured


def test_clean_input_classifies_normally(tmp_path):
    inp = _candidates_file(tmp_path, [GOOD])
    out = tmp_path / "03_classified.json"
    main(["--profile", PROFILE, "classify", "--inp", str(inp), "--out", str(out)])
    classified = json.loads(out.read_text("utf-8"))
    assert len(classified) == 1
    # genuinely-empty candidates (no error key) is a real no_match
    assert classified[0]["tier"] == "no_match"
