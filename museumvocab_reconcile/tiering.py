"""Classification / confidence tiering.

Assigns each source term a tier — ``auto_accept`` | ``review`` | ``no_match`` —
and a proposed facet, using only signals the technique pilot found trustworthy:

  * trusted-language exact label match (NOT the authority's own match flag),
  * reconciliation score plus the gap to the next candidate,
  * whether the candidate's facet is accepted.

All thresholds come from the profile. The two overrides added in review are
honored here:

  facets.accept_all            - if True, every facet is accepted.
  thresholds.auto_accept.mode  - "off" sends everything to review;
                                 "exact_only" auto-accepts only on a trusted
                                 exact match; "full" also allows score/gap.
"""
from __future__ import annotations

from .config import Profile
from .model import Candidate, ClassifiedTerm, SourceTerm


def _norm(text: str | None) -> str:
    return " ".join((text or "").strip().casefold().split())


def _trusted_exact(c: Candidate, profile: Profile, term: SourceTerm) -> bool:
    langs = profile.languages
    if c.is_exact and c.matched_lang in langs.trusted_exact_match_langs:
        return True
    # Source-data English descriptor rule: an exact match on the TARGET-language
    # PREFERRED label, where the query is the term's human-catalogued English
    # (target_source == "source_data"), is trusted. LLM/edited English never
    # qualifies (caught earlier by the review-only guard as well), and an exact
    # hit on a mere alt label stays review-tier — alt labels include used-for
    # and variant terms that can be broader than the concept.
    return (
        langs.trusted_target_pref_exact
        and term.target_source == "source_data"
        and c.is_exact
        and c.query_lang == langs.target
        and c.matched_lang == langs.target
        and bool(c.pref_label_target)
        and _norm(c.matched_label) == _norm(c.pref_label_target)
    )


def _match_band(c: Candidate, match_langs: list[str]) -> int:
    """Proposal-ordering band: 0 = exact match on a tracked-language label,
    1 = exact match outside match_langs, 2 = fuzzy. Reconcile score only ranks
    WITHIN a band — its absolute values are noise at the low end (real case:
    the exact-en descriptor hit for 'Dollhouse' scored 9.5 below three fuzzy
    relatives at 11.8-17.5), and a fuzzy candidate's matched_lang is just an
    echo of the query language, so it carries no language evidence at all."""
    if c.is_exact:
        return 0 if (not match_langs or c.matched_lang in match_langs) else 1
    return 2


def _cross_facet_ambiguity(candidates: list[Candidate], top: Candidate, gap: float) -> bool:
    """Two near-tied top candidates sitting in different facets (within `gap`)."""
    rivals = [c for c in candidates if c is not top and abs(c.score - top.score) <= gap]
    return any(c.facet != top.facet for c in rivals)


def classify(term: SourceTerm, candidates: list[Candidate], profile: Profile) -> ClassifiedTerm:
    aa = profile.thresholds.auto_accept
    review_if = profile.thresholds.review_if
    facets = profile.facets

    if not candidates:
        return ClassifiedTerm(
            term=term, candidates=[], best=None, tier="no_match",
            reasons=["no candidates returned"], match_type="no_candidates",
            proposed_facet=None, proposed_target_term=None,
        )

    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    # When a specific facet set is required (accept_all=False), prefer the
    # strongest candidate whose facet is accepted. This stops an out-of-facet top
    # hit (e.g. an Associated-Concepts match in an object-name vocab) from
    # hijacking the proposal or masking an in-facet alternative. If nothing is
    # accepted, fall back to the overall top — it gets flagged and routed to
    # review below, never auto-accepted.
    accepted_cands = [c for c in ranked if facets.is_accepted(c.facet)]
    # Proposal ordering: exactness first, score within band (_match_band). An
    # exact label match in a tracked language beats any fuzzy hit regardless of
    # reconcile score; an exact match in an untracked language still beats fuzzy
    # (it is usually the right concept — loanwords) but trips the match_langs
    # review flag below. Trust/tier semantics are decided separately.
    match_langs = profile.languages.match_langs
    pool = sorted(
        accepted_cands,
        key=lambda c: (_match_band(c, match_langs), -c.score),
    )
    # Transparency for the reviewer: if banding pushed the HIGHEST-scORED
    # accepted candidate out of the proposal slot, disclose it with the reason.
    band_demoted: Candidate | None = (
        accepted_cands[0]
        if pool and accepted_cands and pool[0] is not accepted_cands[0]
        else None
    )
    best = pool[0] if pool else ranked[0]
    # `prefer` mode: a facet is too coarse to rank, so within the pool favour a
    # candidate that also sits in a preferred sub-hierarchy. This steers WHICH
    # candidate is proposed; the accepted-facet gate is unchanged. Two guards keep
    # it safe: (1) never demote a trusted-language exact top pick — that signal is
    # sacrosanct (see skill: nb/nn exact ≈ certain); (2) the pool keeps score
    # order, so in_hier[0] is the strongest in-hierarchy candidate. If steering
    # picks a lower-scored candidate the gap below shrinks accordingly, so an
    # uncertain result routes to review rather than auto-accepting an
    # out-of-hierarchy rival — the conservative direction.
    # Resolve the LLM's advisory hierarchy prediction (a cleaned label, possibly
    # human-edited in the CSV) back to its anchor id; None if it matches no anchor.
    expected_anchor = (
        facets.resolve_hierarchy_label(term.expected_hierarchy)
        if term.expected_hierarchy else None
    )
    hier_steered = False
    if (
        facets.hierarchy_mode == "prefer"
        and facets.preferred_hierarchies
        and pool
        and not _trusted_exact(pool[0], profile, term)
    ):
        in_hier = [c for c in pool if facets.hierarchy_hit(c)]
        if in_hier:
            # Two-tier preference: the strongest candidate in the LLM-EXPECTED
            # anchor wins over the strongest in any other anchor; without (or
            # outside) a prediction, behaviour is unchanged. Advisory only —
            # the gap-shrink mechanic below still routes uncertainty to review.
            in_expected = (
                [c for c in in_hier if facets.in_hierarchy(c, expected_anchor)]
                if expected_anchor else []
            )
            best = (in_expected or in_hier)[0]
            hier_steered = True
    # Advisory expected-facet tie-break (LLM prediction from the translate step):
    # among near-tied pool candidates, prefer one matching the predicted facet.
    # Strictly a proposal-steering signal with the same guards as hierarchy
    # steering — never overrides a trusted exact, never beats a human-curated
    # hierarchy hit, and never changes the accept gate. Picking a lower-scored
    # candidate shrinks the gap below, so uncertainty still routes to review.
    expected_facet_agrees: bool | None = None
    if term.expected_facet:
        if (
            pool
            and not hier_steered
            and not _trusted_exact(pool[0], profile, term)
            and best.facet != term.expected_facet
        ):
            near = [
                c for c in pool
                if abs(c.score - pool[0].score) <= aa.min_score_gap
                and c.facet == term.expected_facet
            ]
            if near:
                best = near[0]
        expected_facet_agrees = best.facet == term.expected_facet
    # Gap is measured from `best` to its nearest rival of ANY facet, so a higher-
    # scoring non-accepted rival shrinks (or negates) the gap and blocks auto-accept.
    rival_scores = [c.score for c in ranked if c is not best]
    gap = (best.score - max(rival_scores)) if rival_scores else float("inf")
    facet_ok = facets.is_accepted(best.facet)
    exact = _trusted_exact(best, profile, term) and aa.trusted_lang_exact_match
    reasons: list[str] = []
    # Structured counterpart of `reasons`: the first (dominant) hard review
    # condition to fire wins; auto-accept branches set their basis below.
    match_type = ""

    def code(value: str) -> None:
        nonlocal match_type
        if not match_type:
            match_type = value

    # ---- hard review conditions (independent of auto-accept mode) ----------
    if review_if.cross_facet_ambiguity and _cross_facet_ambiguity(ranked, best, aa.min_score_gap):
        reasons.append("cross-facet ambiguity among near-tied candidates")
        code("ambiguous_cross_facet")
    if not facet_ok:
        reasons.append(
            f"best candidate facet {best.facet!r} not in accepted set"
            + ("" if facets.accept_all else f" {facets.accepted}")
        )
        code("facet_not_accepted")
    if match_langs and best.is_exact and best.matched_lang not in match_langs:
        # `und`/foreign = the query exactly matched a label in a language we
        # don't track (e.g. a French prefLabel an English query coincided
        # with). Propose it — it is often the right concept — but never trust
        # it past review. Fuzzy candidates carry no language evidence (their
        # matched_lang is just the query language), so they are not flagged.
        reasons.append(
            f"best candidate matched via language {best.matched_lang!r}, "
            f"not in match_langs {match_langs}"
        )
        code("match_lang_untracked")
    # Ancestor-promoted candidates are synthesised OUTSIDE the reconcile
    # results (broad-term rescue: the concept sat on a child's parent chain and
    # its label exactly matched the query). The label match is real — often a
    # museum-authored nb/nn altLabel — but the surfacing route is new and
    # unaudited, so route to review first; the trusted-exact gate can be opened
    # for these once a run has been hand-audited (mirrors the deepen-promotion
    # posture).
    if getattr(best, "promoted_from", None):
        reasons.append(
            f"promoted from ancestor walk of candidate {best.promoted_from} "
            f"(not a reconcile hit) — review to confirm"
        )
        code("ancestor_promoted")
    # LLM English is a LOOKUP QUERY ONLY, never a trust signal: if the best
    # candidate was surfaced by a target-language query whose label did not come
    # from the source data (target_source llm/human — i.e. the translate step,
    # including its alternatives), the term must go to review regardless of
    # score, gap, or even an exact label match. Only nb/nn queries (always
    # human-catalogued) or source-data English can support auto-accept.
    if (
        best.query_lang == profile.languages.target
        and term.target_source != "source_data"
    ):
        reasons.append(
            f"surfaced via {term.target_source} {profile.languages.target!r} query "
            f"{best.query_term!r} — review only, never auto-accept"
        )
        # Dominant over the other flags: it is the trust rule, not a symptom.
        match_type = "llm_surfaced"
        exact = False  # an exact hit on a generated label is coincidence, not trust

    # ---- auto-accept decision, gated by mode -------------------------------
    if aa.mode == "off":
        tier = "review"
        reasons.insert(0, "auto_accept.mode=off: all terms routed to review")
        code("mode_off")
    elif reasons:  # a hard review condition already fired
        tier = "review"
        code("review_other")  # safety net; the conditions above set their own code
    elif exact and facet_ok:
        tier = "auto_accept"
        reasons.append(f"exact match in trusted language ({best.matched_lang})")
        # Basis: trusted-language altLabel exact (nb_exact / nn_exact) vs the
        # source-data English prefLabel descriptor rule.
        code(
            f"{best.matched_lang}_exact"
            if best.matched_lang in profile.languages.trusted_exact_match_langs
            else f"source_{profile.languages.target}_pref_exact"
        )
    elif aa.mode == "full" and facet_ok and best.score >= aa.min_score and gap >= aa.min_score_gap:
        tier = "auto_accept"
        reasons.append(
            f"score {best.score:.1f} >= {aa.min_score} and gap {gap:.1f} >= {aa.min_score_gap}"
        )
        code("score_gap")
    else:
        tier = "review"
        if aa.mode == "exact_only":
            reasons.append("auto_accept.mode=exact_only and no trusted exact match")
            code("mode_exact_only")
        else:
            reasons.append(
                f"below thresholds (score {best.score:.1f}, gap "
                f"{'inf' if gap == float('inf') else f'{gap:.1f}'})"
            )
            code("below_threshold")

    # broader-only flag is advisory metadata for the reviewer
    if review_if.broader_only and best.facet and not exact and best.score < aa.min_score:
        reasons.append("possible broader-only match — verify specificity")

    # Surface the preferred sub-hierarchy the proposed candidate sits in (advisory;
    # appended after the tier decision so it never forces a review).
    hier_anchor = facets.hierarchy_hit(best)
    proposed_hierarchy = (
        f"{facets.preferred_hierarchies[hier_anchor]} ({hier_anchor})"
        if hier_anchor else None
    )
    # Disclose when banding demoted the highest-scored candidate (see above).
    if band_demoted is not None and band_demoted is not best:
        why = (
            f"exact match via {band_demoted.matched_lang!r}, outside "
            f"match_langs {match_langs}"
            if band_demoted.is_exact
            else "fuzzy match, outranked by an exact label match"
        )
        reasons.append(
            f"higher-scored candidate {band_demoted.concept_id} "
            f"{(band_demoted.pref_label_target or band_demoted.matched_label)!r} "
            f"(score {band_demoted.score:.0f}) not proposed: {why}"
        )
    if proposed_hierarchy:
        reasons.append(f"in preferred hierarchy {proposed_hierarchy}")
    # Advisory note for the reviewer: does the LLM's facet prediction agree with
    # the proposed candidate? Appended after the tier decision — informational only.
    if expected_facet_agrees is not None:
        reasons.append(
            f"LLM expected facet {term.expected_facet!r} "
            + ("agrees with proposal" if expected_facet_agrees
               else f"differs from proposed facet {best.facet!r}")
        )
    # Same advisory annotation for the hierarchy prediction. An edited/stale
    # label that maps to no profile anchor is reported, not silently dropped.
    if term.expected_hierarchy:
        if expected_anchor is None:
            reasons.append(
                f"LLM expected hierarchy {term.expected_hierarchy!r} matches no "
                f"profile anchor — ignored"
            )
        elif facets.in_hierarchy(best, expected_anchor):
            reasons.append(
                f"LLM expected hierarchy {term.expected_hierarchy!r} agrees with proposal"
            )
        else:
            reasons.append(
                f"LLM expected hierarchy {term.expected_hierarchy!r} differs from "
                f"proposal" + (f" (in {proposed_hierarchy})" if proposed_hierarchy else
                               " (proposal in no preferred hierarchy)")
            )

    proposed_target = best.pref_label_target or (
        term.main_target_term if term.main_target_term else None
    )
    return ClassifiedTerm(
        term=term, candidates=ranked, best=best, tier=tier, reasons=reasons,
        match_type=match_type, proposed_facet=best.facet, proposed_aat_facet=best.aat_facet,
        proposed_hierarchy=proposed_hierarchy,
        proposed_target_term=proposed_target,
    )
