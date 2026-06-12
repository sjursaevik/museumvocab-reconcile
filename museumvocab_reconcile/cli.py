"""Command-line entry point: re-entrant stages over versioned artifacts.

    prep            source.json            -> 01_prepared.json
    translate       01_prepared.json       -> 01b_translations.csv   (optional, needs API key)
    translate-apply + 01b_translations.csv -> 01b_translated.json
    retranslate     refresh a subset of 01b_translations.csv in place
    flag-anomalies  01b_translations.csv   -> 01b_anomalies.csv
    lookup          01_prepared.json       -> 02_candidates.json   (network; runs on your machine)
    classify        02_candidates.json     -> 03_classified.json
    review-export   03_classified.json     -> 03b_review.csv        (edit this by hand)
    assemble        03_classified.json + 03b_review.csv -> 04_final.json (+ .csv, _linkedart.json, log.txt)

Each stage reads the previous artifact and writes the next, so you can stop,
inspect or hand-edit any artifact, and resume.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .adapters import get_adapter
from .assemble import assemble
from .cache import JsonCache
from .config import Profile
from .loader import load_source
from .model import Candidate, ClassifiedTerm, SourceTerm
from .review import export_review_csv, ingest_review_csv
from .tiering import classify
from .translate import (
    apply_results_to_csv,
    apply_translations,
    compute_siblings,
    export_translations_csv,
    flag_anomalies,
    get_translator,
    ingest_translations_csv,
    has_target,
    missing_target,
    run_translation,
    select_retranslate_ids,
)


def _resolve_profile_path(path: str) -> str:
    """Accept either a real path or a bare profile name.

    If `path` exists (absolute or relative to CWD), use it. Otherwise look for
    a profile of that name bundled inside the installed package's profiles/
    directory, so `--profile techniques.aat.yaml` works from any folder.
    """
    p = Path(path)
    if p.exists():
        return str(p)
    bundled = Path(__file__).parent / "profiles" / p.name
    if bundled.exists():
        return str(bundled)
    available = sorted(q.name for q in (Path(__file__).parent / "profiles").glob("*.yaml"))
    raise FileNotFoundError(
        f"Profile not found: {path!r}. Looked in the current folder and in the "
        f"bundled profiles. Bundled profiles you can pass by name: {available}"
    )


def _load_profile(path: str) -> Profile:
    profile = Profile.load(_resolve_profile_path(path))
    for w in profile.validate():
        print(f"[profile warning] {w}")
    return profile


def cmd_prep(args):
    profile = _load_profile(args.profile)
    terms = load_source(args.source, profile)
    Path(args.out).write_text(
        json.dumps([asdict(t) for t in terms], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"prep: {len(terms)} terms -> {args.out}")


def cmd_translate(args):
    profile = _load_profile(args.profile)
    if getattr(args, "predict_all", False):
        profile.translation.predict_all = True
    terms = [SourceTerm(**d) for d in json.loads(Path(args.inp).read_text("utf-8"))]
    targets = missing_target(terms)
    n_target = len(targets if not args.max_terms else targets[: args.max_terms])
    n_predict = 0
    if profile.translation.predict_all:
        predict_pop = has_target(terms)
        n_predict = len(predict_pop if not args.max_terms else predict_pop[: args.max_terms])
    bs = profile.translation.batch_size
    print(
        f"translate: {len(terms)} terms, {len(targets)} missing English"
        + (f"; will translate up to {args.max_terms}" if args.max_terms else f"; will translate {len(targets)}")
        + (f"; will predict facet/hierarchy for {n_predict} terms with existing English"
           if profile.translation.predict_all else "")
        + f". Estimated API calls ~{-(-n_target // bs) + -(-n_predict // bs)} "
        f"(batch_size={bs})."
    )
    if args.dry_run:
        print("translate: --dry-run set, no API calls made.")
        return

    cache = JsonCache(args.cache)
    translator = get_translator(profile.translation)
    results = run_translation(
        terms, translator, profile, cache, progress=print, max_terms=args.max_terms
    )
    sibs = (
        compute_siblings(terms, profile.translation.max_siblings)
        if profile.translation.include_siblings else {}
    )
    n = export_translations_csv(terms, results, args.out, siblings=sibs)
    print(
        f"translate: {n} translations -> {args.out}  "
        "(review 'accept'/'approved_english', then run translate-apply)"
    )


def cmd_retranslate(args):
    profile = _load_profile(args.profile)
    terms = [SourceTerm(**d) for d in json.loads(Path(args.inp).read_text("utf-8"))]
    confidences = [c for c in (args.confidence or "").split(",") if c.strip()]
    ids = [i for i in (args.ids or "").split(",") if i.strip()]
    target_ids = select_retranslate_ids(args.translations, confidences, ids)
    if not target_ids:
        print("retranslate: no terms matched the selection; nothing to do.")
        return
    print(
        f"retranslate: re-querying {len(target_ids)} term(s) "
        f"(confidence={confidences or '-'}, ids={len(ids)}); model={profile.translation.model}"
    )
    if args.dry_run:
        print("retranslate: --dry-run set, no API calls made.")
        return
    cache = JsonCache(args.cache)
    translator = get_translator(profile.translation)
    results = run_translation(
        terms, translator, profile, cache, progress=print,
        only_ids=target_ids, force=True, max_terms=args.max_terms,
    )
    n = apply_results_to_csv(args.translations, results, args.out)
    print(f"retranslate: updated {n} row(s) -> {args.out} (other rows preserved)")


def cmd_flag_anomalies(args):
    n = flag_anomalies(args.inp, args.out)
    print(f"flag-anomalies: {n} flagged rows -> {args.out}")


def cmd_translate_apply(args):
    _load_profile(args.profile)  # validate profile early
    terms = [SourceTerm(**d) for d in json.loads(Path(args.inp).read_text("utf-8"))]
    decisions = ingest_translations_csv(args.translations)
    terms, applied, predicted = apply_translations(terms, decisions)
    Path(args.out).write_text(
        json.dumps([asdict(t) for t in terms], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    n_llm = sum(1 for t in terms if t.target_source == "llm")
    n_human = sum(1 for t in terms if t.target_source == "human")
    print(
        f"translate-apply: applied {applied} translations "
        f"({n_llm} llm, {n_human} human-edited)"
        + (f" and {predicted} facet/hierarchy predictions for terms with existing English"
           if predicted else "")
        + f" -> {args.out}  (now run: lookup --inp {args.out})"
    )


def gather_candidates(
    term: SourceTerm,
    adapter,
    languages,
    result_limit: int,
    min_score: float = 0.0,
    max_alternative_queries: int = 3,
    alternatives_trigger_score: float = 60.0,
) -> tuple[list[Candidate], bool]:
    """Run the primary (source/target) reconcile queries for one term, falling
    back to the term's LLM alternative labels when the primaries found nothing
    CONVINCING — no candidate at or above ``alternatives_trigger_score``.

    The trigger is deliberately independent of ``min_score`` (the pre-enrich
    junk filter): with min_score 0, a single low-scored fuzzy hit used to
    suppress the fallback entirely (real case: 'Sari' -> only 'Sari (Samanid
    pottery style)' @33 blocked the 'sari' alternative query that finds the
    correct 'saris (garments)'). A weak hit is not evidence the primary
    queries succeeded. Set alternatives_trigger_score to 0 to restore the
    strict behaviour (fallback only when the primaries return nothing usable).

    Alternatives only ADD candidates; ranking stays score-based, so a strong
    primary result is never displaced by the extra queries.

    Returns (candidates, used_alternatives). Alternative-surfaced candidates are
    ordinary lookup results — provenance stays visible via Candidate.query_lang/
    query_term plus term.target_source, and tiering routes LLM-English-surfaced
    matches to review (never auto-accept).
    """
    order = languages.query_order_by_depth.get(
        "leaf" if term.is_leaf else "root", [languages.source, languages.target]
    )
    cands: dict[str, Candidate] = {}

    def add(c: Candidate) -> None:
        prev = cands.get(c.concept_id)
        # Keep the occurrence whose QUERY was in the source language: with
        # root-order [en, nb] an en-found duplicate would otherwise mask the
        # fact that the trusted nb query also surfaced the same concept.
        if prev is None or (
            prev.query_lang != languages.source and c.query_lang == languages.source
        ):
            cands[c.concept_id] = c

    for lang in order:
        label = term.main_lang_term if lang == languages.source else term.main_target_term
        if not label:
            continue
        for c in adapter.search(label, lang, limit=result_limit):
            add(c)

    used_alternatives = False
    trigger = max(min_score, alternatives_trigger_score)
    convincing = (
        [c for c in cands.values() if c.score >= trigger]
        if trigger else cands.values()
    )
    if not convincing and term.target_alternatives and max_alternative_queries > 0:
        used_alternatives = True
        for alt in term.target_alternatives[:max_alternative_queries]:
            for c in adapter.search(alt, languages.target, limit=result_limit):
                add(c)

    return list(cands.values()), used_alternatives


def cmd_lookup(args):
    import time

    profile = _load_profile(args.profile)
    terms = [SourceTerm(**d) for d in json.loads(Path(args.inp).read_text("utf-8"))]
    cache = JsonCache(args.cache)
    adapter = get_adapter(
        profile.authority, cache=cache,
        max_retries=args.max_retries, backoff=args.retry_backoff,
        request_delay=args.request_delay,
    )
    lang_order = profile.languages
    lk = profile.lookup
    # CLI flags override profile values when provided.
    result_limit = args.limit if args.limit is not None else lk.result_limit
    enrich_top_n = args.enrich_top_n if args.enrich_top_n is not None else lk.enrich_top_n
    min_score = args.min_score if args.min_score is not None else lk.min_candidate_score

    out_path = Path(args.out)
    # Resume: load any results already written, skip those term IDs.
    results: list[dict] = []
    done: set[str] = set()
    if out_path.exists():
        try:
            loaded = json.loads(out_path.read_text("utf-8"))
            # Drop previously-errored terms so resume retries them (a transient
            # rate-limit or network blip shouldn't become a permanent zero).
            results = [r for r in loaded if not r.get("error")]
            done = {r["term"]["id"] for r in results}
        except (json.JSONDecodeError, KeyError, OSError):
            results, done = [], set()

    pending = [t for t in terms if t.id not in done]
    if args.max_terms:
        pending = pending[: args.max_terms]
    total = len(pending)
    print(
        f"lookup: {len(done)} already done, {total} to process"
        + (f" (capped by --max-terms {args.max_terms})" if args.max_terms else "")
        + f"; authority={profile.authority}, cache={args.cache}"
    )

    def flush():
        out_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cache.flush()   # persist concepts fetched since the last flush

    try:
        for i, t in enumerate(pending, 1):
            try:
                cand_list, used_alts = gather_candidates(
                    t, adapter, lang_order, result_limit,
                    min_score=min_score,
                    max_alternative_queries=lk.max_alternative_queries,
                    alternatives_trigger_score=lk.alternatives_trigger_score,
                )
                # Score filter + top-N cap BEFORE enrichment: each enrichment
                # fetches a concept and walks its hierarchy, so this bounds both
                # runtime and cache growth.
                ranked = sorted(cand_list, key=lambda c: c.score, reverse=True)
                if min_score:
                    ranked = [c for c in ranked if c.score >= min_score]
                if enrich_top_n:
                    ranked = ranked[:enrich_top_n]
                # Attribution preference for _refine_match: trusted langs
                # first, then the other tracked match languages (deduped).
                pl = list(dict.fromkeys(
                    lang_order.trusted_exact_match_langs + lang_order.match_langs
                ))
                enriched = adapter.enrich_candidates(
                    ranked, lang_order.target, prefer_langs=pl
                )
                results.append({"term": asdict(t), "candidates": [asdict(c) for c in enriched]})
                via = " (via LLM alternatives)" if used_alts and enriched else ""
                print(f"  [{i}/{total}] {t.id} {t.main_lang_term!r} -> {len(enriched)} candidates{via}")
            except Exception as exc:  # network or parse error: record and continue
                results.append({"term": asdict(t), "candidates": [], "error": repr(exc)})
                print(f"  [{i}/{total}] {t.id} {t.main_lang_term!r} -> ERROR: {exc}")

            if i % args.flush_every == 0:
                flush()
            if args.sleep:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("\ninterrupted — saving progress so far (re-run to resume)")
    finally:
        flush()

    n_err = sum(1 for r in results if r.get("error"))
    print(f"lookup: wrote {len(results)} terms -> {args.out}" + (f" ({n_err} errors)" if n_err else ""))


def cmd_classify(args):
    profile = _load_profile(args.profile)
    raw = json.loads(Path(args.inp).read_text("utf-8"))
    # ---- error gate -------------------------------------------------------
    # lookup records persistent HTTP failures as {"error": ...} entries with an
    # empty candidate list, and its resume logic retries them. If classify ran
    # over such entries it would tier them "no_match" ("no candidates
    # returned") — silently converting a rate-limit blip into a permanent miss,
    # the exact failure mode the lookup error handling exists to prevent.
    errored = [e for e in raw if e.get("error")]
    if errored:
        ids = ", ".join(e["term"]["id"] for e in errored[:5])
        more = f" (+{len(errored) - 5} more)" if len(errored) > 5 else ""
        if not args.skip_errors:
            raise SystemExit(
                f"classify: {len(errored)} term(s) in {args.inp} carry a lookup "
                f"ERROR (e.g. ids {ids}{more}). These are transient failures, "
                f"not no-matches — re-run lookup to retry them (resume skips "
                f"completed terms), then classify again. To classify anyway "
                f"WITHOUT these terms, pass --skip-errors."
            )
        print(
            f"classify: WARNING --skip-errors set; dropping {len(errored)} "
            f"errored term(s) (ids {ids}{more}) from the output. They will be "
            f"absent from review and assembly until lookup retries them."
        )
        raw = [e for e in raw if not e.get("error")]
    out = []
    for entry in raw:
        term = SourceTerm(**entry["term"])
        cands = [Candidate(**c) for c in entry["candidates"]]
        ct = classify(term, cands, profile)
        out.append(_classified_to_dict(ct))
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tiers = {}
    for o in out:
        tiers[o["tier"]] = tiers.get(o["tier"], 0) + 1
    print(f"classify: {len(out)} terms -> {args.out}  tiers={tiers}")


def cmd_review_export(args):
    profile = _load_profile(args.profile)
    classified = [_dict_to_classified(d) for d in json.loads(Path(args.inp).read_text("utf-8"))]
    include_auto = args.include_auto or profile.review.include_auto_accepted
    n = export_review_csv(classified, args.out, include_auto=include_auto)
    extra = " (incl. auto-accepted)" if include_auto else ""
    print(f"review-export: {n} rows{extra} -> {args.out}  (edit 'accept'/'chosen_*'/'notes')")


def cmd_assemble(args):
    profile = _load_profile(args.profile)
    classified = [_dict_to_classified(d) for d in json.loads(Path(args.inp).read_text("utf-8"))]
    review_exists = bool(args.review and Path(args.review).exists())
    decisions = ingest_review_csv(args.review) if review_exists else {}
    n_review_tier = sum(1 for ct in classified if ct.tier in ("review", "no_match"))
    if n_review_tier and not review_exists:
        print(
            f"assemble: WARNING no review file at {args.review!r}; {n_review_tier} "
            "review/no-match term(s) will be EXCLUDED (only auto-accepted terms are "
            "kept). Run review-export, edit it, then re-run assemble."
        )
    elif n_review_tier and not decisions:
        print(
            f"assemble: WARNING review file {args.review!r} yielded no decisions; "
            f"{n_review_tier} review/no-match term(s) will be excluded."
        )
    stats = assemble(
        classified, decisions, profile, args.out, args.log, args.linkedart, out_csv=args.csv
    )
    print(f"assemble: {stats} -> {args.out}")


# ---- (de)serialisation helpers for the classified artifact ----------------

def _classified_to_dict(ct: ClassifiedTerm) -> dict:
    return {
        "term": asdict(ct.term),
        "candidates": [asdict(c) for c in ct.candidates],
        "best_id": ct.best.concept_id if ct.best else None,
        "tier": ct.tier,
        "reasons": ct.reasons,
        "proposed_facet": ct.proposed_facet,
        "proposed_aat_facet": ct.proposed_aat_facet,
        "proposed_hierarchy": ct.proposed_hierarchy,
        "proposed_target_term": ct.proposed_target_term,
    }


def _dict_to_classified(d: dict) -> ClassifiedTerm:
    cands = [Candidate(**c) for c in d["candidates"]]
    best = next((c for c in cands if c.concept_id == d.get("best_id")), cands[0] if cands else None)
    return ClassifiedTerm(
        term=SourceTerm(**d["term"]), candidates=cands, best=best,
        tier=d["tier"], reasons=d["reasons"], proposed_facet=d.get("proposed_facet"),
        proposed_aat_facet=d.get("proposed_aat_facet"),
        proposed_hierarchy=d.get("proposed_hierarchy"),
        proposed_target_term=d.get("proposed_target_term"),
    )


def main(argv=None):
    p = argparse.ArgumentParser(prog="museumvocab-reconcile")
    p.add_argument("--profile", required=True, help="path to a profile YAML")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("prep"); s.add_argument("source"); s.add_argument("--out", default="01_prepared.json"); s.set_defaults(func=cmd_prep)
    s = sub.add_parser("translate"); s.add_argument("--inp", default="01_prepared.json"); s.add_argument("--out", default="01b_translations.csv"); s.add_argument("--cache", default="translation_cache.json"); s.add_argument("--max-terms", type=int, default=0, help="translate at most N terms (smoke test)"); s.add_argument("--dry-run", action="store_true", help="report how many terms would be translated, make no API calls"); s.add_argument("--predict-all", action="store_true", help="also run terms that ALREADY have English through the LLM, for advisory expected_facet/expected_hierarchy predictions only (their English is never touched)"); s.set_defaults(func=cmd_translate)
    s = sub.add_parser("translate-apply"); s.add_argument("--inp", default="01_prepared.json"); s.add_argument("--translations", default="01b_translations.csv"); s.add_argument("--out", default="01b_translated.json"); s.set_defaults(func=cmd_translate_apply)
    s = sub.add_parser("flag-anomalies"); s.add_argument("--inp", default="01b_translations.csv"); s.add_argument("--out", default="01b_anomalies.csv"); s.set_defaults(func=cmd_flag_anomalies)
    s = sub.add_parser("retranslate"); s.add_argument("--inp", default="01_prepared.json", help="prepared terms (for context)"); s.add_argument("--translations", default="01b_translations.csv", help="existing translations to refresh"); s.add_argument("--out", default="01b_translations.csv", help="merged output (defaults to overwriting --translations)"); s.add_argument("--confidence", default="low,medium", help="comma list of confidences to re-translate"); s.add_argument("--ids", default="", help="comma list of specific term ids to re-translate"); s.add_argument("--cache", default="translation_cache.json"); s.add_argument("--max-terms", type=int, default=0); s.add_argument("--dry-run", action="store_true", help="report selection, make no API calls"); s.set_defaults(func=cmd_retranslate)
    s = sub.add_parser("lookup"); s.add_argument("--inp", default="01_prepared.json"); s.add_argument("--out", default="02_candidates.json"); s.add_argument("--cache", default="cache.json"); s.add_argument("--limit", type=int, default=None, help="candidates per reconcile query (overrides profile lookup.result_limit)"); s.add_argument("--enrich-top-n", type=int, default=None, help="enrich at most N candidates per term (overrides profile lookup.enrich_top_n)"); s.add_argument("--min-score", type=float, default=None, help="drop candidates below this score before enriching (overrides profile lookup.min_candidate_score)"); s.add_argument("--max-terms", type=int, default=0, help="process at most N terms (0 = all); useful for a quick smoke test"); s.add_argument("--sleep", type=float, default=0.2, help="seconds to wait between terms (politeness)"); s.add_argument("--flush-every", type=int, default=10, help="write the output file every N terms"); s.add_argument("--max-retries", type=int, default=4, help="retry attempts for transient HTTP errors (429/499/5xx)"); s.add_argument("--retry-backoff", type=float, default=1.5, help="exponential backoff base for retries"); s.add_argument("--request-delay", type=float, default=0.0, help="seconds to pause before every HTTP request (throttle if the server rate-limits)"); s.set_defaults(func=cmd_lookup)
    s = sub.add_parser("classify"); s.add_argument("--inp", default="02_candidates.json"); s.add_argument("--out", default="03_classified.json"); s.add_argument("--skip-errors", action="store_true", help="drop terms whose lookup errored instead of aborting (they stay un-classified until lookup retries them)"); s.set_defaults(func=cmd_classify)
    s = sub.add_parser("review-export"); s.add_argument("--inp", default="03_classified.json"); s.add_argument("--out", default="03b_review.csv"); s.add_argument("--include-auto", action="store_true", help="also export auto-accepted terms (also settable via profile review.include_auto_accepted)"); s.set_defaults(func=cmd_review_export)
    s = sub.add_parser("assemble"); s.add_argument("--inp", default="03_classified.json"); s.add_argument("--review", default="03b_review.csv"); s.add_argument("--out", default="04_final.json"); s.add_argument("--linkedart", default="04_linkedart.json"); s.add_argument("--csv", default="04_final.csv", help="human-readable CSV of the final records"); s.add_argument("--log", default="log.txt"); s.set_defaults(func=cmd_assemble)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
