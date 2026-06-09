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


def _trusted_exact(c: Candidate, profile: Profile) -> bool:
    return (
        c.is_exact
        and c.matched_lang in profile.languages.trusted_exact_match_langs
    )


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
            reasons=["no candidates returned"], proposed_facet=None,
            proposed_target_term=None,
        )

    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    # When a specific facet set is required (accept_all=False), prefer the
    # strongest candidate whose facet is accepted. This stops an out-of-facet top
    # hit (e.g. an Associated-Concepts match in an object-name vocab) from
    # hijacking the proposal or masking an in-facet alternative. If nothing is
    # accepted, fall back to the overall top — it gets flagged and routed to
    # review below, never auto-accepted.
    accepted_cands = [c for c in ranked if facets.is_accepted(c.facet)]
    best = accepted_cands[0] if accepted_cands else ranked[0]
    # `prefer` mode: a facet is too coarse to rank, so among accepted candidates
    # favour one that also sits in a preferred sub-hierarchy. This steers WHICH
    # candidate is proposed; the accepted-facet gate is unchanged. Two guards keep
    # it safe: (1) never demote a trusted-language exact top pick — that signal is
    # sacrosanct (see skill: nb/nn exact ≈ certain); (2) accepted_cands keeps score
    # order, so in_hier[0] is the strongest in-hierarchy candidate. If steering
    # picks a lower-scored candidate the gap below shrinks accordingly, so an
    # uncertain result routes to review rather than auto-accepting an
    # out-of-hierarchy rival — the conservative direction.
    if (
        facets.hierarchy_mode == "prefer"
        and facets.preferred_hierarchies
        and accepted_cands
        and not _trusted_exact(accepted_cands[0], profile)
    ):
        in_hier = [c for c in accepted_cands if facets.hierarchy_hit(c)]
        if in_hier:
            best = in_hier[0]
    # Gap is measured from `best` to its nearest rival of ANY facet, so a higher-
    # scoring non-accepted rival shrinks (or negates) the gap and blocks auto-accept.
    rival_scores = [c.score for c in ranked if c is not best]
    gap = (best.score - max(rival_scores)) if rival_scores else float("inf")
    facet_ok = facets.is_accepted(best.facet)
    exact = _trusted_exact(best, profile) and aa.trusted_lang_exact_match
    reasons: list[str] = []

    # ---- hard review conditions (independent of auto-accept mode) ----------
    if review_if.cross_facet_ambiguity and _cross_facet_ambiguity(ranked, best, aa.min_score_gap):
        reasons.append("cross-facet ambiguity among near-tied candidates")
    if not facet_ok:
        reasons.append(
            f"best candidate facet {best.facet!r} not in accepted set"
            + ("" if facets.accept_all else f" {facets.accepted}")
        )
    match_langs = profile.languages.match_langs
    if match_langs and best.matched_lang not in match_langs:
        # `und` = matched a label in a language we don't track (e.g. a French
        # prefLabel an English query coincided with). Don't trust it on score.
        reasons.append(
            f"best candidate matched via language {best.matched_lang!r}, "
            f"not in match_langs {match_langs}"
        )

    # ---- auto-accept decision, gated by mode -------------------------------
    if aa.mode == "off":
        tier = "review"
        reasons.insert(0, "auto_accept.mode=off: all terms routed to review")
    elif reasons:  # a hard review condition already fired
        tier = "review"
    elif exact and facet_ok:
        tier = "auto_accept"
        reasons.append(f"exact match in trusted language ({best.matched_lang})")
    elif aa.mode == "full" and facet_ok and best.score >= aa.min_score and gap >= aa.min_score_gap:
        tier = "auto_accept"
        reasons.append(
            f"score {best.score:.1f} >= {aa.min_score} and gap {gap:.1f} >= {aa.min_score_gap}"
        )
    else:
        tier = "review"
        if aa.mode == "exact_only":
            reasons.append("auto_accept.mode=exact_only and no trusted exact match")
        else:
            reasons.append(
                f"below thresholds (score {best.score:.1f}, gap "
                f"{'inf' if gap == float('inf') else f'{gap:.1f}'})"
            )

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
    if proposed_hierarchy:
        reasons.append(f"in preferred hierarchy {proposed_hierarchy}")

    proposed_target = best.pref_label_target or (
        term.main_target_term if term.main_target_term else None
    )
    return ClassifiedTerm(
        term=term, candidates=ranked, best=best, tier=tier, reasons=reasons,
        proposed_facet=best.facet, proposed_aat_facet=best.aat_facet,
        proposed_hierarchy=proposed_hierarchy,
        proposed_target_term=proposed_target,
    )
