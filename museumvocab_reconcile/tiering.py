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


def _score_gap(candidates: list[Candidate]) -> float:
    if len(candidates) < 2:
        return float("inf")
    s = sorted((c.score for c in candidates), reverse=True)
    return s[0] - s[1]


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
    best = ranked[0]
    gap = _score_gap(ranked)
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

    proposed_target = best.pref_label_target or (
        term.main_target_term if term.main_target_term else None
    )
    return ClassifiedTerm(
        term=term, candidates=ranked, best=best, tier=tier, reasons=reasons,
        proposed_facet=best.facet, proposed_target_term=proposed_target,
    )
