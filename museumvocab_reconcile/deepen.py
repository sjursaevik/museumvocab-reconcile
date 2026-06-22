"""Optional `deepen` stage: a deeper, Norwegian-first second pass for the hard
terms, plus an advisory LLM recommendation.

Where it sits:

    classify -> [deepen] -> review-export

Why it exists
-------------
The primary lookup caps candidates by RAW reconcile score before exactness is
knowable, and Getty scores English-label matches higher — so the Norwegian
candidates that carry the *trusted* auto-accept signal are the ones most often
truncated. The deepen stage targets the low-confidence subset and:

  1. RE-QUERIES the authority with widened, Norwegian-first parameters (higher
     limit, more alternative queries, optional cross-facet sibling harvest), and
     MERGES the result with the term's original (already-enriched) candidates;
  2. RE-CLASSIFIES the merged set with the SAME rule engine — so a deeper lookup
     that finally surfaces a trusted nb/nn exact can legitimately promote the
     term to auto_accept *via the rule*, never via the LLM;
  3. optionally asks an LLM to recommend ONE candidate FROM THE CANDIDATE SET as
     a parallel second opinion for the reviewer.

Trust invariants (do not weaken)
--------------------------------
* The rule engine owns tier/best/match_type. The LLM recommendation is ADVISORY
  metadata only and can never auto-accept anything (it is never fed back into
  ``classify``).
* The LLM may only pick an id that is present in the candidate set. An off-list
  id is REJECTED and flagged, never silently substituted (the same failure mode
  the assemble-stage chosen_id guard exists to prevent).
* The recommendation is cached under a version-stamped key that also hashes the
  candidate set, so a changed prompt, model, or candidate set re-derives rather
  than serving a stale opinion (temperature 0 keeps picks reproducible).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Callable, Protocol

from .config import DeepenConfig, Profile
from .model import Candidate, ClassifiedTerm, SourceTerm
from .tiering import _cross_facet_ambiguity, classify

# A `gather_candidates`-shaped callable, injected by the CLI to avoid a circular
# import (cli imports deepen). Signature mirrors cli.gather_candidates.
GatherFn = Callable[..., tuple[list[Candidate], bool]]


# ===========================================================================
# selection: which classified terms get the deep pass
# ===========================================================================

def select_for_deepen(ct: ClassifiedTerm, profile: Profile) -> bool:
    """True when this term is in the hard subset worth a deeper look.

    Only review/no-match terms are eligible — an auto-accepted term already
    cleared the trust gate and must not be disturbed. Within those, any one of
    the configured triggers selects it.
    """
    if ct.tier not in ("review", "no_match"):
        return False
    d = profile.deepen

    if ct.tier == "no_match" or not ct.candidates:
        return d.select_no_match

    if d.select_below_score and ct.best is not None and ct.best.score < d.select_below_score:
        return True
    if d.select_if_no_exact and not any(c.is_exact for c in ct.candidates):
        return True
    if d.select_if_cross_facet_ambiguity and ct.best is not None:
        # Re-derive rather than trust match_type (older artifacts may lack it).
        if _cross_facet_ambiguity(
            ct.candidates, ct.best, profile.thresholds.auto_accept.min_score_gap
        ):
            return True
    return False


# ===========================================================================
# widened, Norwegian-first re-lookup  (network)
# ===========================================================================

def _sibling_candidates(
    original: list[Candidate],
    adapter: Any,
    term: SourceTerm,
    source_lang: str,
    max_siblings: int,
) -> list[Candidate]:
    """Build candidate stubs from the cross-facet siblings (`cross_refs`) and any
    authority-asserted crosswalk `matchings` of the strongest original
    candidates — a free in-graph hop (no SPARQL) toward a process<->work-type or
    material counterpart the reconcile query may have missed.

    Stubs carry score 0 and the source term as their query, so enrichment can
    detect an honest nb/nn exact on them; they are enriched UNCONDITIONALLY by
    the caller (never subject to the score cap that would hide their exactness).
    """
    seen = {c.concept_id for c in original}
    out: list[Candidate] = []
    uri_template = original[0].uri.replace(original[0].concept_id, "{id}") if original else ""
    for c in sorted(original, key=lambda x: x.score, reverse=True):
        for ref in list(c.cross_refs) + [
            m for m in c.matchings if m.get("authority") == c.authority
        ]:
            sid = ref.get("id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(
                Candidate(
                    authority=c.authority,
                    concept_id=sid,
                    uri=uri_template.format(id=sid) if uri_template else "",
                    score=0.0,
                    matched_label=ref.get("label") or "",
                    matched_lang=source_lang,
                    query_lang=source_lang,
                    is_exact=False,
                    facet=None,
                    query_term=term.main_lang_term,
                    raw={"sibling_of": c.concept_id, "relation": ref.get("relation")},
                )
            )
            if len(out) >= max_siblings:
                return out
    return out


def widen_candidates(
    term: SourceTerm,
    original: list[Candidate],
    adapter: Any,
    profile: Profile,
    gather_fn: GatherFn,
) -> tuple[list[Candidate], int]:
    """Re-query wide + Norwegian-first, merge with the original enriched
    candidates, enrich the union, and return (candidates, n_new).

    The original (already-enriched) occurrence wins the dedup so we never lose
    facet/ancestors. Reconcile candidates are capped by score for enrichment
    cost; harvested siblings bypass that cap (few, and only useful if exact).
    """
    d = profile.deepen
    langs = profile.languages
    fresh, _used = gather_fn(
        term, adapter, langs, d.result_limit,
        min_score=0.0,
        max_alternative_queries=d.max_alternative_queries,
        alternatives_trigger_score=d.alternatives_trigger_score,
    )
    by_id: dict[str, Candidate] = {}
    for c in fresh:
        by_id.setdefault(c.concept_id, c)
    orig_ids = {c.concept_id for c in original}
    for c in original:                       # enriched original wins the dedup
        by_id[c.concept_id] = c

    reconcile = sorted(by_id.values(), key=lambda c: c.score, reverse=True)
    ranked = reconcile[: d.enrich_top_n]

    siblings: list[Candidate] = []
    if d.include_sibling_candidates and original:
        ranked_ids = {c.concept_id for c in ranked}
        siblings = [
            s for s in _sibling_candidates(
                original, adapter, term, langs.source, d.max_sibling_candidates
            )
            if s.concept_id not in ranked_ids
        ]

    final = ranked + siblings
    prefer = list(dict.fromkeys(langs.trusted_exact_match_langs + langs.match_langs))
    # Enrich the whole set (cache-backed, idempotent): fills facet/ancestors and
    # recomputes is_exact/matched_lang against the fetched language-tagged labels
    # — including for the siblings, so a genuine nb/nn-exact sibling is detected.
    adapter.enrich_candidates(final, langs.target, prefer_langs=prefer)
    n_new = len({c.concept_id for c in final} - orig_ids)
    return final, n_new


# ===========================================================================
# LLM recommendation  (advisory)  — provider seam mirrors translate.py
# ===========================================================================

REC_SYSTEM_PROMPT = (
    "You are a museum cataloguing assistant. Your task is SELECTION, not "
    "translation: from a CLOSED list of Getty AAT candidates, identify the one "
    "concept that is CORRECT for a Norwegian controlled-vocabulary term — or say "
    "none is.\n"
    "\n"
    "Hard rules:\n"
    "- Choose ONLY from the candidate ids provided. Never invent, guess, or "
    "return an id that is not in the list.\n"
    "- Returning an empty recommended_id is the CORRECT answer whenever no "
    "candidate genuinely fits. The list often does NOT contain the right concept; "
    "abstaining is expected and far better than a plausible-looking wrong pick. "
    "Do not stretch to fit.\n"
    "\n"
    "Weigh the evidence in THIS ORDER (earlier evidence dominates later):\n"
    "  1. An nb or nn altLabel (or matched label) equal to the source term. "
    "These Norwegian labels were contributed to AAT by the museum itself, so such "
    "a match is near-decisive EVIDENCE OF CORRECTNESS — not a stylistic "
    "preference. (Norwegian labels matter only as this evidence; do not favour an "
    "nb/nn-labelled candidate that the scope note and hierarchy show is wrong.)\n"
    "  2. The scope note: does the concept's definition actually describe this "
    "term?\n"
    "  3. Facet and parent hierarchy fit.\n"
    "  4. Same specificity: prefer a concept at the term's own level over a "
    "merely broader ancestor.\n"
    "  5. The reconcile score is WEAK evidence only; never let it override 1-4.\n"
    "\n"
    "Confidence: 'high' only when an nb/nn label matches or the scope note "
    "clearly fits; 'medium' for a good facet/hierarchy fit without those; 'low' "
    "when you are guessing. Keep the reason to one sentence and cite the specific "
    "evidence (which rule above) that decided it."
)


class Recommender(Protocol):
    def recommend(
        self, payload: dict[str, Any], context: str, cfg: DeepenConfig
    ) -> dict[str, Any]:
        """Given {term, parents, expected_*, candidates:[...]}, return
        {recommended_id, confidence, reason}."""
        ...


def _candidate_evidence(c: Candidate) -> dict[str, Any]:
    """Compact, decision-relevant evidence for one candidate (keeps the prompt
    small and the nb/nn signal explicit)."""
    alt = c.alt_labels or {}
    return {
        "id": c.concept_id,
        "label": c.pref_label_target or c.matched_label,
        # surface the trusted signal explicitly: a museum-curated nb/nn alt equal
        # to the source term is near-decisive evidence of correctness
        "nb_altLabels": alt.get("nb", []),
        "nn_altLabels": alt.get("nn", []),
        "exact_match": c.is_exact,
        "matched_lang": c.matched_lang if c.is_exact else "",  # honest: blank when fuzzy
        "facet": c.facet,
        "aat_facet": c.aat_facet,
        "scope_note": (c.scope_note or "")[:300],
        "parents": [a.get("label") for a in (c.ancestors or [])[:6] if a.get("label")],
        "reconcile_score": round(c.score, 1),
        "sibling_of": (c.raw or {}).get("sibling_of") if isinstance(c.raw, dict) else None,
    }


def build_recommend_payload(ct: ClassifiedTerm) -> dict[str, Any]:
    return {
        "id": ct.term.id,
        "source_term": ct.term.main_lang_term,
        "parents": ct.term.parents_source,
        "existing_english": ct.term.main_target_term or "",
        "expected_facet": ct.term.expected_facet or "",
        "expected_hierarchy": ct.term.expected_hierarchy or "",
        "candidates": [_candidate_evidence(c) for c in ct.candidates],
    }


def build_recommend_prompt(payload: dict[str, Any], context: str) -> str:
    lines: list[str] = []
    if context:
        lines.append(f"Domain context: {context}\n")
    lines.append(f"Norwegian source term: {payload['source_term']}")
    if payload.get("parents"):
        lines.append("Hierarchy (broad to narrow): " + " > ".join(payload["parents"]))
    if payload.get("existing_english"):
        lines.append(f"Existing English (advisory): {payload['existing_english']}")
    if payload.get("expected_facet"):
        lines.append(f"LLM-predicted facet (advisory): {payload['expected_facet']}")
    if payload.get("expected_hierarchy"):
        lines.append(f"LLM-predicted hierarchy (advisory): {payload['expected_hierarchy']}")
    lines.append("\nCandidates (choose the best id, or none):")
    for c in payload["candidates"]:
        lines.append(json.dumps(c, ensure_ascii=False))
    valid_ids = ", ".join(c["id"] for c in payload["candidates"]) or "(none)"
    lines.append(
        "\nReturn ONLY a JSON object, no prose, no markdown fences:\n"
        '{"recommended_id": "<one of: ' + valid_ids + ' or empty>", '
        '"confidence": "high|medium|low", "reason": "<one sentence>"}'
    )
    return "\n".join(lines)


def parse_recommendation(text: str) -> dict[str, Any]:
    """Parse the model's JSON object, tolerating accidental markdown fences."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start : end + 1]
    try:
        data = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class AnthropicRecommender:
    """Default provider using Anthropic's Messages API via the official SDK."""

    def __init__(self, cfg: DeepenConfig):
        try:
            import anthropic  # lazy: the core tool needn't depend on it
        except ImportError as exc:  # pragma: no cover - dependency hint
            raise SystemExit(
                "The 'anthropic' package is required for deepen --use-llm: "
                "pip install anthropic  (and set ANTHROPIC_API_KEY)"
            ) from exc
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        self.cfg = cfg

    def recommend(self, payload, context, cfg):
        resp = self.client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            system=REC_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_recommend_prompt(payload, context)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return parse_recommendation(text)


def get_recommender(cfg: DeepenConfig) -> Recommender:
    if cfg.provider == "anthropic":
        return AnthropicRecommender(cfg)
    raise ValueError(f"Unknown deepen recommender provider: {cfg.provider!r}")


# ===========================================================================
# orchestration
# ===========================================================================

def _candset_hash(candidates: list[Candidate]) -> str:
    """Stable hash of the candidate set (ids + rounded scores), so a changed set
    invalidates the cached recommendation. Uses sha1 (Python's hash() is salted
    per-process and would not be reproducible across runs)."""
    key = json.dumps(
        sorted((c.concept_id, round(c.score, 1)) for c in candidates),
        ensure_ascii=False,
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def recommend_for_term(
    ct: ClassifiedTerm,
    recommender: Recommender,
    cfg: DeepenConfig,
    cache: Any = None,
    *,
    force: bool = False,
) -> None:
    """Attach the validated LLM recommendation onto ``ct`` in place.

    Closed-set validation is strict: a recommended id not present in the
    candidate set is dropped and the off-list id is recorded in the reason, so a
    hallucinated id can never become the recommendation or feed assembly.
    """
    if not ct.candidates:
        return
    cand_ids = {c.concept_id for c in ct.candidates}
    stamp = f"rec:{cfg.prompt_version}:{cfg.model}:"
    key = f"{stamp}{ct.term.id}:{_candset_hash(ct.candidates)}"
    data: dict[str, Any] | None = None
    if not force and cache is not None and cache.has(key):
        data = cache.get(key)
    if data is None:
        data = recommender.recommend(
            build_recommend_payload(ct), cfg.context, cfg
        ) or {}
        if cache is not None:
            cache.set(key, data, flush=False)

    rec_id = (data.get("recommended_id") or "").strip() or None
    conf = (data.get("confidence") or "").strip().lower()
    reason = (data.get("reason") or "").strip()

    ct.llm_recommendation_source = f"llm_deep:{cfg.model}:{cfg.prompt_version}"
    ct.llm_recommendation_confidence = conf if conf in {"high", "medium", "low"} else ""

    if rec_id and rec_id in cand_ids:
        chosen = next(c for c in ct.candidates if c.concept_id == rec_id)
        ct.llm_recommended_id = rec_id
        ct.llm_recommended_target_term = chosen.pref_label_target or chosen.matched_label
        ct.llm_recommendation_reason = reason
        ct.llm_agrees_with_rule = bool(ct.best and rec_id == ct.best.concept_id)
    else:
        # No pick, or an off-list/hallucinated id: keep it visible, never adopt it.
        ct.llm_recommended_id = None
        ct.llm_recommended_target_term = None
        ct.llm_agrees_with_rule = None
        if rec_id:
            ct.llm_recommendation_reason = (
                f"[ignored off-list id {rec_id!r}] " + reason
            ).strip()
        else:
            ct.llm_recommendation_reason = reason or "no candidate recommended"


def run_deepen(
    classified: list[ClassifiedTerm],
    adapter: Any,
    recommender: Recommender | None,
    profile: Profile,
    cache: Any = None,
    *,
    gather_fn: GatherFn,
    progress: Callable[[str], None] = lambda _m: None,
    max_terms: int = 0,
    force: bool = False,
    flush_every: int = 10,
) -> tuple[list[ClassifiedTerm], dict[str, int]]:
    """Run the deep pass over the selected hard subset; return (terms, stats).

    Non-selected terms pass through untouched (``deep_used`` stays False). The
    expensive work (authority fetches, LLM calls) is cached, so an interrupted
    run resumes cheaply on re-run.
    """
    out: list[ClassifiedTerm] = []
    selected_idx = [i for i, ct in enumerate(classified) if select_for_deepen(ct, profile)]
    if max_terms:
        selected_idx = selected_idx[:max_terms]
    selected = set(selected_idx)
    stats = {
        "selected": len(selected), "deepened": 0, "candidates_added": 0,
        "promoted_to_auto_accept": 0, "llm_recommended": 0,
        "llm_disagrees": 0, "llm_off_list": 0, "errors": 0,
    }
    progress(f"deepen: {len(selected)} of {len(classified)} terms selected for the deep pass")

    for i, ct in enumerate(classified):
        if i not in selected:
            out.append(ct)
            continue
        try:
            widened, n_new = widen_candidates(
                ct.term, ct.candidates, adapter, profile, gather_fn
            )
            new_ct = classify(ct.term, widened, profile)   # rule engine owns the tier
            new_ct.deep_used = True
            new_ct.deep_candidates_added = n_new
            stats["deepened"] += 1
            stats["candidates_added"] += n_new
            if ct.tier != "auto_accept" and new_ct.tier == "auto_accept":
                stats["promoted_to_auto_accept"] += 1

            if profile.deepen.use_llm and recommender is not None and new_ct.candidates:
                recommend_for_term(new_ct, recommender, profile.deepen, cache, force=force)
                if new_ct.llm_recommended_id:
                    stats["llm_recommended"] += 1
                    if new_ct.llm_agrees_with_rule is False:
                        stats["llm_disagrees"] += 1
                elif "off-list" in new_ct.llm_recommendation_reason:
                    stats["llm_off_list"] += 1
            out.append(new_ct)
            promoted = " -> auto_accept" if ct.tier != new_ct.tier == "auto_accept" else ""
            rec = (
                f" | llm={new_ct.llm_recommended_id}"
                f"{'' if new_ct.llm_agrees_with_rule is not False else ' (DISAGREES)'}"
                if new_ct.llm_recommended_id else ""
            )
            progress(
                f"  [{i + 1}] {ct.term.id} {ct.term.main_lang_term!r}: "
                f"+{n_new} cand, tier={new_ct.tier}{promoted}{rec}"
            )
        except Exception as exc:  # network/LLM error: keep the original, count it
            stats["errors"] += 1
            ct.llm_recommendation_reason = f"deepen error: {exc!r}"
            out.append(ct)
            progress(f"  [{i + 1}] {ct.term.id} -> ERROR: {exc}")
        if cache is not None and (i + 1) % flush_every == 0:
            cache.flush()
    if cache is not None:
        cache.flush()
    return out, stats
