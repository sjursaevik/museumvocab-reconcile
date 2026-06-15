"""Internal data model shared across all pipeline stages.

These dataclasses are authority-agnostic. Adapters normalise their service's
responses into ``Candidate`` objects (including the enriched fields populated by
``enrich_candidates``); everything downstream (tiering, review, assembly) only
ever sees these shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceTerm:
    """One row of the source vocabulary, after prep/normalisation."""

    id: str
    status: str
    logical_name: str | None
    label: str | None
    main_lang_term: str          # main term in the source language (e.g. nb)
    main_target_term: str        # main term in the target language (e.g. en); "" if absent
    main_level: int              # index of the highest non-empty level
    parents_source: list[str]    # parent terms (broader), source language, broad->narrow
    parents_target: list[str]    # parent terms, target language
    raw: dict[str, Any] = field(default_factory=dict)  # original row, for traceability
    target_source: str = "source_data"  # provenance of main_target_term: source_data | llm | human
    # Alternative target-language labels from the translation step (LLM-suggested,
    # possibly pruned by the cataloguer). Used by lookup as FALLBACK queries when
    # the primary queries return nothing useful — same trust level as the LLM
    # main English: lookup query only, never an auto-accept signal.
    target_alternatives: list[str] = field(default_factory=list)
    # LLM-predicted internal facet (e.g. "materials"). ADVISORY ONLY: tiering may
    # use it to break ties among near-tied cross-facet candidates and to annotate
    # review reasons; it never widens the accept gate or auto-accepts anything.
    expected_facet: str | None = None
    # LLM-predicted preferred hierarchy, stored as the CLEANED LABEL (one of the
    # profile's preferred_hierarchies labels; human-editable in the review CSV).
    # Resolved back to its anchor id at classify time. ADVISORY ONLY: steers
    # which candidate among preferred-hierarchy hits is proposed and annotates
    # reasons; never changes the gate, the tier, or a trusted exact pick.
    expected_hierarchy: str | None = None

    @property
    def is_leaf(self) -> bool:
        # Heuristic used by the query-strategy weighting: a term with parents is
        # treated as a leaf, a depth-0 term as a root/umbrella concept.
        return self.main_level > 0


@dataclass
class Candidate:
    """A single ranked authority candidate, normalised across adapters."""

    authority: str               # "aat" | "iconclass" | ...
    concept_id: str
    uri: str
    score: float
    matched_label: str           # the authority label the query matched
    matched_lang: str            # language of matched_label
    query_lang: str              # language the query was issued in
    is_exact: bool               # normalised exact match between query and matched_label
    facet: str | None            # authority-internal category (AAT facet / Iconclass division)
    query_term: str = ""         # the source query string that produced this hit (for match recompute)
    aat_facet: str | None = None # live authority facet label "<name> (<id>)", for human review
    pref_label_target: str | None = None   # preferred label in the target language
    scope_note: str | None = None
    ancestors: list[dict[str, Any]] = field(default_factory=list)   # [{"id":..., "label":...}]
    cross_refs: list[dict[str, Any]] = field(default_factory=list)  # related concepts in other facets
    # SKOS crosswalk links the authority itself asserts to OTHER authorities
    # (e.g. a KulturNav concept's skos:exactMatch -> Getty AAT / Wikidata). Each:
    #   {"relation": "exactMatch"|"closeMatch"|"broadMatch"|"narrowMatch"
    #               |"relatedMatch"|"sameAs",
    #    "uri": str, "authority": "aat"|"wikidata"|... , "id": str|None}
    # These are crowd-/bot-curated in KulturNav, so they are REVIEW-grade hints
    # (a free second-hop to AAT/Wikidata for assembly), never an auto-accept
    # signal. Empty for authorities that don't expose outbound matchings.
    matchings: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassifiedTerm:
    """A source term plus its candidates and the tiering decision."""

    term: SourceTerm
    candidates: list[Candidate]
    best: Candidate | None
    tier: str                    # "auto_accept" | "review" | "no_match"
    reasons: list[str]           # human-readable explanation of the tier decision
    proposed_facet: str | None
    # Structured, aggregatable code for WHY the term landed in its tier (the
    # prose lives in `reasons`). Auto-accept basis: "nb_exact" / "nn_exact" /
    # "source_en_pref_exact" / "score_gap". Review basis (dominant condition):
    # "llm_surfaced" / "ambiguous_cross_facet" / "facet_not_accepted" /
    # "match_lang_untracked" / "below_threshold" / "mode_off" /
    # "mode_exact_only". No candidates: "no_candidates". Feeds the log's
    # distributions and longer-term threshold tuning.
    match_type: str = ""
    proposed_aat_facet: str | None = None    # live AAT facet "<name> (<id>)" of the best candidate
    proposed_hierarchy: str | None = None    # preferred sub-hierarchy "<label> (<id>)" the best sits in
    proposed_target_term: str | None = None  # proposed English/target label


@dataclass
class Decision:
    """A human override read back from the review CSV (one per source ID)."""

    id: str
    accept: bool
    chosen_id: str | None
    chosen_target_term: str | None
    chosen_facet: str | None
    notes: str = ""
    # The raw `accept` cell as read from the CSV, lowercased. Lets assemble tell
    # the machine's "auto" pre-fill (an auto-accepted row exported only for
    # visibility, left untouched) apart from an explicit human "yes" — so an
    # untouched auto-accept keeps its auto_accept provenance instead of being
    # miscounted as human_review.
    raw_accept: str = ""
