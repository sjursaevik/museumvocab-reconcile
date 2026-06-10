"""Shared builders for the test suite.

Everything here is offline: no network, no Getty, no secrets. Tests inject fake
responses / fixture nodes so the trust logic, Linked-Art parsing and retry
behaviour can be exercised deterministically.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from museumvocab_reconcile.config import Profile
from museumvocab_reconcile.model import Candidate, SourceTerm

# A realistic baseline mirroring profiles/objectnames.aat.yaml. Tests override
# only the keys they care about via `make_profile(overrides)`.
BASE_PROFILE: dict[str, Any] = {
    "profile": "test",
    "authority": "aat",
    "languages": {
        "source": "nb",
        "target": "en",
        "trusted_exact_match_langs": ["nb", "nn"],
    },
    "facets": {
        "accept_all": False,
        "accepted": ["materials", "formats", "work_types", "techniques"],
    },
    "thresholds": {
        "auto_accept": {
            "mode": "full",
            "min_score": 25,
            "min_score_gap": 5,
            "trusted_lang_exact_match": True,
        },
        "review_if": {"cross_facet_ambiguity": True, "broader_only": True},
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def make_profile(overrides: dict[str, Any] | None = None) -> Profile:
    return Profile.from_dict(_deep_merge(BASE_PROFILE, overrides or {}))


def make_candidate(
    *,
    concept_id: str = "300000001",
    score: float = 30.0,
    matched_label: str = "x",
    matched_lang: str = "nb",
    is_exact: bool = False,
    facet: str | None = "work_types",
    pref_label_target: str | None = "thing",
    aat_facet: str | None = None,
    ancestors: list[dict[str, Any]] | None = None,
    query_term: str = "",
    query_lang: str | None = None,   # defaults to matched_lang
) -> Candidate:
    return Candidate(
        authority="aat",
        concept_id=concept_id,
        uri=f"http://vocab.getty.edu/aat/{concept_id}",
        score=score,
        matched_label=matched_label,
        matched_lang=matched_lang,
        query_lang=query_lang if query_lang is not None else matched_lang,
        is_exact=is_exact,
        facet=facet,
        query_term=query_term,
        aat_facet=aat_facet,
        pref_label_target=pref_label_target,
        ancestors=ancestors or [],
    )


def make_term(
    *, term_id: str = "1", nb: str = "ting", en: str = "", level: int = 1,
    target_source: str = "source_data",
    target_alternatives: list[str] | None = None,
    expected_facet: str | None = None,
    expected_hierarchy: str | None = None,
) -> SourceTerm:
    return SourceTerm(
        id=term_id,
        status="Gyldig",
        logical_name=None,
        label=None,
        main_lang_term=nb,
        main_target_term=en,
        main_level=level,
        parents_source=[],
        parents_target=[],
        target_source=target_source,
        target_alternatives=target_alternatives or [],
        expected_facet=expected_facet,
        expected_hierarchy=expected_hierarchy,
    )


@pytest.fixture
def profile() -> Profile:
    return make_profile()
