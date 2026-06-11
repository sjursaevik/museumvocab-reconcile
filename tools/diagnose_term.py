"""Diagnose lookup + classify for ONE term, end to end, from YOUR machine
(the Getty APIs are not reachable from the sandbox).

Runs the exact pipeline path — gather_candidates (incl. the alternatives
fallback) -> enrich -> classify — and prints the full candidate table with the
fields the proposal logic keys on (score, matched_label, matched_lang, facet,
preferred-hierarchy anchor), plus pool membership and the final tier/reasons.
Optionally dumps the parsed label languages of a specific concept, to check
how Getty tagged a label (e.g. the untagged-prefLabel "und" quirk).

Examples (PowerShell):
  python tools/diagnose_term.py --profile objectnames.aat.yaml --nb Sari --en saris --alternatives sari
  python tools/diagnose_term.py --profile objectnames.aat.yaml --concept 300209968
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from museumvocab_reconcile.adapters import get_adapter
from museumvocab_reconcile.cli import _resolve_profile_path, gather_candidates
from museumvocab_reconcile.config import Profile
from museumvocab_reconcile.model import SourceTerm
from museumvocab_reconcile.tiering import classify


def dump_concept(adapter, cid: str) -> None:
    rec = adapter.fetch(cid)
    print(f"\n== concept {cid} ==")
    print(f"facet: {rec.get('facet')}  aat_facet: {rec.get('aat_facet')}")
    print("pref_labels:")
    for lang, val in (rec.get("pref_labels") or {}).items():
        print(f"  {lang!r}: {val!r}")
    print("alt_labels:")
    for lang, vals in (rec.get("alt_labels") or {}).items():
        for v in vals:
            print(f"  {lang!r}: {v!r}")
    print("ancestors (narrow -> broad):")
    for a in rec.get("ancestors") or []:
        print(f"  {a.get('id')}  {a.get('label')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--nb", help="source-language term (main_lang_term)")
    ap.add_argument("--en", default="", help="target term (main_target_term), if any")
    ap.add_argument("--target-source", default="source_data",
                    choices=["source_data", "llm", "human"],
                    help="provenance of --en (affects the review-only guard)")
    ap.add_argument("--alternatives", default="",
                    help="comma list of LLM alternative labels (fallback queries)")
    ap.add_argument("--expected-facet", default="")
    ap.add_argument("--expected-hierarchy", default="")
    ap.add_argument("--leaf", action="store_true", default=True,
                    help="treat the term as a leaf (default)")
    ap.add_argument("--root", dest="leaf", action="store_false",
                    help="treat the term as a root (query order root)")
    ap.add_argument("--concept", default="",
                    help="just dump one concept's parsed labels/ancestors and exit")
    args = ap.parse_args()

    profile = Profile.load(_resolve_profile_path(args.profile))
    adapter = get_adapter(profile.authority, cache=None)

    if args.concept:
        dump_concept(adapter, args.concept)
        return
    if not args.nb:
        ap.error("--nb is required unless --concept is given")

    term = SourceTerm(
        id="diag", status="Gyldig", logical_name=None, label=None,
        main_lang_term=args.nb, main_target_term=args.en,
        main_level=2 if args.leaf else 0,
        parents_source=["x"] if args.leaf else [], parents_target=[],
        target_source=args.target_source,
        target_alternatives=[a.strip() for a in args.alternatives.split(",") if a.strip()],
        expected_facet=args.expected_facet or None,
        expected_hierarchy=args.expected_hierarchy or None,
    )

    lk = profile.lookup
    cands, used_alts = gather_candidates(
        term, adapter, profile.languages, lk.result_limit,
        min_score=lk.min_candidate_score,
        max_alternative_queries=lk.max_alternative_queries,
        alternatives_trigger_score=lk.alternatives_trigger_score,
    )
    print(f"\n{len(cands)} candidate(s) from queries"
          + (" (alternatives fallback used)" if used_alts else ""))
    ranked = sorted(cands, key=lambda c: c.score, reverse=True)
    if lk.min_candidate_score:
        ranked = [c for c in ranked if c.score >= lk.min_candidate_score]
    if lk.enrich_top_n:
        ranked = ranked[: lk.enrich_top_n]
    enriched = adapter.enrich_candidates(ranked, profile.languages.target)

    ml = profile.languages.match_langs
    print(f"\nmatch_langs: {ml or '-'}   anchors: "
          f"{list(profile.facets.preferred_hierarchies.values()) or '-'}")
    print(f"{'#':<3}{'id':<12}{'score':<7}{'exact':<7}{'q_lang':<7}{'m_lang':<7}"
          f"{'in_ml':<7}{'facet':<16}{'anchor':<12} label / matched")
    for i, c in enumerate(enriched):
        anchor = profile.facets.hierarchy_hit(c) or "-"
        in_ml = "yes" if (not ml or c.matched_lang in ml) else "NO"
        print(f"{i:<3}{c.concept_id:<12}{c.score:<7.0f}{str(c.is_exact):<7}"
              f"{c.query_lang:<7}{c.matched_lang:<7}{in_ml:<7}"
              f"{str(c.facet):<16}{anchor:<12} "
              f"{c.pref_label_target or ''!r} / {c.matched_label!r}")

    ct = classify(term, enriched, profile)
    print(f"\ntier: {ct.tier}")
    print(f"proposed: {ct.best.concept_id if ct.best else '-'} "
          f"{(ct.best.pref_label_target or ct.best.matched_label) if ct.best else ''!r}"
          f"  (hierarchy: {ct.proposed_hierarchy or '-'})")
    for r in ct.reasons:
        print(f"  reason: {r}")


if __name__ == "__main__":
    main()
