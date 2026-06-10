"""Optional step 1b: LLM-recommended English for terms missing a target term.

Flow (mirrors the review -> assemble gate):

    translate        01_prepared.json  -> 01b_translations.csv   (review this)
    translate-apply  01_prepared.json + 01b_translations.csv -> 01b_translated.json
    lookup --inp 01b_translated.json

The LLM English is always tagged (`target_source = "llm"`, or `"human"` if a
cataloguer edits it) and is used by lookup only as an *untrusted* query — it can
surface AAT candidates but never auto-accepts, since only nb/nn are trusted.

The provider is behind a small seam (`Translator`); `AnthropicTranslator` is the
default. The API key is read from the ANTHROPIC_API_KEY environment variable and
never stored in a profile or file.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import Profile, TranslationConfig, normalize_hierarchy_label
from .model import SourceTerm
from .review import _detect_delimiter, _read_csv_text  # reuse robust CSV decode


# ---- result + provider seam ----------------------------------------------

PREDICT_SYSTEM_PROMPT = (
    "You are a museum cataloguing assistant. You classify controlled-vocabulary "
    "terms into the facets and sub-hierarchies of art and conservation thesauri "
    "such as the Getty AAT. Each term comes with its existing, authoritative "
    "English label and hierarchical context; you do NOT translate or change any "
    "label. Choose only from the options offered, prefer an empty answer over a "
    "forced fit, and return strictly the JSON requested with no prose."
)


def build_predict_prompt(
    items: list[dict[str, Any]],
    context: str,
    facet_options: list[str] | None = None,
    hierarchy_options: list[str] | None = None,
) -> str:
    """Prediction-only prompt for terms that ALREADY have source-data English.

    Same per-term context as the translation prompt plus the existing English
    label (human-catalogued, so safe — and strong — classification signal). The
    response schema asks only for the advisory predictions: no english, no
    alternatives."""
    lines = []
    if context:
        lines.append(f"Domain context: {context}")
    lines.append(
        "Classify each term below. Read each term within its top-level "
        "collection area (domain) and use its English label and the parent and "
        "sibling terms as context — do not translate anything.\n"
    )
    for it in items:
        parts = [f'- id: {it["id"]}', f'  term: {it["term"]}']
        if it.get("english"):
            parts.append(f'  english label: {it["english"]}')
        if it.get("domain"):
            parts.append(f'  domain (top-level collection area): {it["domain"]}')
        if it.get("parents"):
            parts.append(f'  parents (broad to narrow): {" > ".join(it["parents"])}')
        if it.get("siblings"):
            parts.append(f'  siblings: {", ".join(it["siblings"])}')
        lines.append("\n".join(parts))
    schema = '{"id": "<id>", "confidence": "high|medium|low", "note": "<short note or empty>"'
    suffix, extra = _prediction_schema(facet_options, hierarchy_options)
    schema += suffix
    lines.extend(extra)
    lines.append(
        "\nReturn ONLY a JSON array, no prose, no markdown fences. Each element: "
        + schema
        + "}"
    )
    return "\n".join(lines)


@dataclass
class TranslationResult:
    id: str
    english: str
    alternatives: list[str]
    confidence: str          # "high" | "medium" | "low"
    note: str = ""
    # LLM-predicted internal facet (one of the profile's facet options, or "").
    # Advisory only — see model.SourceTerm.expected_facet.
    expected_facet: str = ""
    # LLM-predicted preferred hierarchy (one of the profile's cleaned
    # preferred_hierarchies labels, or ""). Advisory only — see
    # model.SourceTerm.expected_hierarchy.
    expected_hierarchy: str = ""


class Translator(Protocol):
    def translate_batch(
        self, items: list[dict[str, Any]], context: str, cfg: TranslationConfig
    ) -> list[dict[str, Any]]:
        """Given a list of {id, term, parents, siblings}, return a list of
        {id, english, alternatives, confidence, note} dicts."""
        ...


SYSTEM_PROMPT = (
    "You are a museum cataloguing translation assistant. You translate "
    "controlled-vocabulary terms into the concise English label used in art and "
    "conservation thesauri such as the Getty AAT. Follow these rules:\n"
    "- Return a short noun-phrase label suitable for authority matching, NOT a "
    "sentence or definition.\n"
    "- Prefer the established thesaurus term when one exists; keep a widely used "
    "loanword (e.g. appliqué, étagère, canapé) when that is the accepted English "
    "form.\n"
    "- For object and work types, use the form AAT normally uses, which is "
    "usually plural (e.g. 'drawings', 'brooches', 'side chairs').\n"
    "- When no established English term exists, give the CLOSEST existing AAT "
    "concept rather than a word-by-word literal translation, and set confidence "
    "to low (e.g. a 'Fasanbord' is a kind of games table, not a 'pheasant table').\n"
    "- If a term seems misplaced in its hierarchy, translate the term itself and "
    "note the apparent anomaly.\n"
    "- If unsure, give your best guess and mark confidence accordingly."
)


def _prediction_schema(
    facet_options: list[str] | None, hierarchy_options: list[str] | None
) -> tuple[str, list[str]]:
    """Schema suffix + instruction lines for the advisory predictions, shared by
    the translation and prediction-only prompts so they can never diverge."""
    suffix, lines = "", []
    if facet_options:
        suffix += ', "expected_facet": "<facet or empty>"'
        lines.append(
            "\nAlso predict which facet of the thesaurus each term belongs to, as "
            '"expected_facet": exactly one of '
            + ", ".join(repr(f) for f in facet_options)
            + ', or "" if unsure. This is an advisory hint only.'
        )
    if hierarchy_options:
        suffix += ', "expected_hierarchy": "<hierarchy or empty>"'
        lines.append(
            "\nAlso predict, as \"expected_hierarchy\", which of these vocabulary "
            "sub-hierarchies the term belongs to. Copy the name verbatim from this "
            "closed list: " + ", ".join(repr(h) for h in hierarchy_options)
            + '. The list does NOT cover every term: pick one ONLY when it clearly '
            'applies, and answer "" otherwise — "" is a normal, expected answer, '
            "not a failure. This is an advisory hint only."
        )
    return suffix, lines


def build_user_prompt(
    items: list[dict[str, Any]],
    context: str,
    facet_options: list[str] | None = None,
    hierarchy_options: list[str] | None = None,
) -> str:
    """Build a batched prompt asking for a strict JSON array back.

    ``facet_options`` (the profile's internal facet names) extends only the
    response SCHEMA with an advisory ``expected_facet`` prediction; the per-term
    context (term/domain/parents/siblings) is deliberately unchanged.
    """
    lines = []
    if context:
        lines.append(f"Domain context: {context}")
    lines.append(
        "Translate each term below into its standard English thesaurus label. "
        "Read each term within its top-level collection area (domain) and use the "
        "parent and sibling terms as disambiguating context — translate the TERM "
        "itself.\n"
    )
    for it in items:
        parts = [f'- id: {it["id"]}', f'  term: {it["term"]}']
        if it.get("domain"):
            parts.append(f'  domain (top-level collection area): {it["domain"]}')
        if it.get("parents"):
            parts.append(f'  parents (broad to narrow): {" > ".join(it["parents"])}')
        if it.get("siblings"):
            parts.append(f'  siblings: {", ".join(it["siblings"])}')
        lines.append("\n".join(parts))
    schema = (
        '{"id": "<id>", "english": "<label>", "alternatives": ["<alt>", ...], '
        '"confidence": "high|medium|low", "note": "<short note or empty>"'
    )
    suffix, extra = _prediction_schema(facet_options, hierarchy_options)
    schema += suffix
    lines.extend(extra)
    lines.append(
        "\nReturn ONLY a JSON array, no prose, no markdown fences. Each element: "
        + schema
        + "}"
    )
    return "\n".join(lines)


def parse_response(text: str) -> list[dict[str, Any]]:
    """Parse the model's JSON array, tolerating accidental markdown fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("["), t.rfind("]")
    if start != -1 and end != -1 and end > start:
        t = t[start : end + 1]
    data = json.loads(t)
    return data if isinstance(data, list) else []


class AnthropicTranslator:
    """Default provider using Anthropic's Messages API via the official SDK."""

    def __init__(self, cfg: TranslationConfig):
        try:
            import anthropic  # imported lazily so the core tool needn't have it
        except ImportError as exc:  # pragma: no cover - dependency hint
            raise SystemExit(
                "The 'anthropic' package is required for translation: "
                "pip install anthropic  (and set ANTHROPIC_API_KEY)"
            ) from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        self.cfg = cfg

    def _call(self, system: str, prompt: str, cfg):
        resp = self.client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return parse_response(text)

    def translate_batch(self, items, context, cfg):
        return self._call(
            SYSTEM_PROMPT,
            build_user_prompt(items, context, cfg.facet_options, cfg.hierarchy_options),
            cfg,
        )

    def predict_batch(self, items, context, cfg):
        return self._call(
            PREDICT_SYSTEM_PROMPT,
            build_predict_prompt(items, context, cfg.facet_options, cfg.hierarchy_options),
            cfg,
        )


def get_translator(cfg: TranslationConfig) -> Translator:
    if cfg.provider == "anthropic":
        return AnthropicTranslator(cfg)
    raise ValueError(f"Unknown translation provider: {cfg.provider!r}")


# ---- context assembly -----------------------------------------------------

def _immediate_parent(term: SourceTerm) -> str | None:
    return term.parents_source[-1] if term.parents_source else None


def compute_siblings(terms: list[SourceTerm], max_siblings: int) -> dict[str, list[str]]:
    """Map term id -> sibling term labels (same immediate parent)."""
    by_parent: dict[str, list[SourceTerm]] = {}
    for t in terms:
        p = _immediate_parent(t)
        if p:
            by_parent.setdefault(p, []).append(t)
    out: dict[str, list[str]] = {}
    for t in terms:
        p = _immediate_parent(t)
        sibs = [s.main_lang_term for s in by_parent.get(p, []) if s.id != t.id] if p else []
        out[t.id] = sibs[:max_siblings]
    return out


def _clean_ctx(values: list[str]) -> list[str]:
    """Drop empties and literal NULLs from context lists sent to the LLM."""
    return [v for v in values if v and v.strip() and v.strip().casefold() != "null"]


def term_to_item(
    term: SourceTerm, siblings: list[str], domain_by_root: dict[str, str] | None = None
) -> dict[str, Any]:
    parents = _clean_ctx(term.parents_source)
    root = parents[0] if parents else None   # broadest (top-level) parent
    domain = None
    if root:
        dm = domain_by_root or {}
        domain = dm.get(root.casefold(), root)   # mapped phrase, else the raw root term
    return {
        "id": term.id,
        "term": term.main_lang_term,
        "domain": domain,
        "parents": parents,
        "siblings": _clean_ctx(siblings),
    }


# ---- orchestration --------------------------------------------------------

def missing_target(terms: list[SourceTerm]) -> list[SourceTerm]:
    return [t for t in terms if not t.main_target_term and t.main_lang_term]


def has_target(terms: list[SourceTerm]) -> list[SourceTerm]:
    """Terms with source-data English — the predict-all population."""
    return [t for t in terms if t.main_target_term and t.main_lang_term]


def _validated_predictions(r: dict[str, Any], cfg, profile) -> tuple[str, str]:
    """Validate the advisory fields of one raw LLM element. expected_facet must
    be one of the offered options; expected_hierarchy must map back to a profile
    anchor (stored as the cleaned label). Anything else is dropped, not
    propagated."""
    ef = str(r.get("expected_facet", "")).strip().lower()
    if cfg.facet_options and ef not in {f.lower() for f in cfg.facet_options}:
        ef = ""
    eh = ""
    raw_eh = str(r.get("expected_hierarchy", "")).strip()
    if raw_eh and profile.facets.resolve_hierarchy_label(raw_eh):
        eh = normalize_hierarchy_label(raw_eh)
    return ef, eh


def run_translation(
    terms: list[SourceTerm],
    translator: Translator,
    profile: Profile,
    cache,                       # JsonCache | None
    progress=lambda msg: None,
    max_terms: int = 0,
    only_ids: set[str] | None = None,
    force: bool = False,
) -> dict[str, TranslationResult]:
    """Translate terms missing English, batched, cached, resumable.

    With cfg.predict_all, terms that already have source-data English are also
    processed — for advisory expected_facet/expected_hierarchy predictions only
    (separate prompt, separate `cls:` cache namespace; their English is never
    produced or touched). ``only_ids`` restricts the run to those term ids in
    either population (siblings/context still use the full vocabulary) and
    implicitly enables the predict population for matching ids, so retranslate
    refreshes predict rows without extra flags. ``force`` bypasses the cache
    read so targeted ids are always re-queried. Returns id -> TranslationResult."""
    cfg = profile.translation
    # Facet options the LLM may predict: explicit override, else the profile's
    # accepted facet set. Cached on cfg so providers can read it uniformly.
    if not cfg.facet_options:
        cfg.facet_options = list(profile.facets.accepted)
    if not cfg.hierarchy_options:
        cfg.hierarchy_options = profile.facets.hierarchy_options()
    siblings = compute_siblings(terms, cfg.max_siblings) if cfg.include_siblings else {}
    domain_map = {k.casefold(): v for k, v in (cfg.domain_by_root or {}).items()}
    results: dict[str, TranslationResult] = {}

    def select(population: list[SourceTerm]) -> list[SourceTerm]:
        if only_ids is not None:
            population = [t for t in population if t.id in only_ids]
        if max_terms:
            population = population[:max_terms]
        return population

    def split_cached(targets, stamp):
        pending: list[SourceTerm] = []
        for t in targets:
            cached = None if force else (cache.get(stamp + t.id) if cache else None)
            if cached:
                results[t.id] = TranslationResult(**cached)
            else:
                pending.append(t)
        return pending

    def run_batches(pending, stamp, call, make_items, to_result, label):
        for start in range(0, len(pending), cfg.batch_size):
            batch = pending[start : start + cfg.batch_size]
            try:
                raw = call(make_items(batch), cfg.context, cfg)
            except Exception as exc:  # one bad batch shouldn't kill the run
                progress(f"  {label} batch {start // cfg.batch_size + 1}: ERROR {exc}")
                continue
            by_id = {str(r.get("id")): r for r in raw if isinstance(r, dict)}
            for t in batch:
                res = to_result(t, by_id.get(t.id))
                if res is None:
                    continue
                results[t.id] = res
                if cache:
                    cache.set(stamp + t.id, res.__dict__, flush=False)
            if cache:
                cache.flush()
            progress(f"  {label} {min(start + cfg.batch_size, len(pending))}/{len(pending)}")

    # ---- population 1: terms missing English -> full translation ----------
    stamp = f"tr:{cfg.prompt_version}:{cfg.model}:"
    targets = select(missing_target(terms))
    pending = split_cached(targets, stamp)
    progress(
        f"translate: {len(targets) - len(pending)} cached, {len(pending)} to translate "
        f"in batches of {cfg.batch_size} (model={cfg.model})"
    )

    def translation_result(t, r):
        if not r or not r.get("english"):
            return None
        ef, eh = _validated_predictions(r, cfg, profile)
        return TranslationResult(
            id=t.id,
            english=str(r.get("english", "")).strip(),
            alternatives=[str(a) for a in r.get("alternatives", []) if a],
            confidence=str(r.get("confidence", "")).lower() or "medium",
            note=str(r.get("note", "")).strip(),
            expected_facet=ef,
            expected_hierarchy=eh,
        )

    run_batches(
        pending, stamp, translator.translate_batch,
        lambda batch: [term_to_item(t, siblings.get(t.id, []), domain_map) for t in batch],
        translation_result, "translated",
    )

    # ---- population 2 (opt-in): terms WITH source English -> predictions only
    # Enabled by cfg.predict_all, or implicitly when specific ids are requested
    # (retranslate refreshes rows that already exist in the CSV). The english,
    # target_source and alternatives of these terms are NEVER touched — see the
    # apply_translations predict branch and the tiering provenance guard.
    if cfg.predict_all or only_ids is not None:
        if not (cfg.facet_options or cfg.hierarchy_options):
            if cfg.predict_all:
                progress(
                    "predict: skipped — profile offers no facet or hierarchy options"
                )
        else:
            pstamp = f"cls:{cfg.predict_prompt_version}:{cfg.model}:"
            ptargets = select(has_target(terms))
            ppending = split_cached(ptargets, pstamp)
            if ptargets:
                progress(
                    f"predict: {len(ptargets) - len(ppending)} cached, "
                    f"{len(ppending)} to predict in batches of {cfg.batch_size}"
                )

            def predict_result(t, r):
                if not r:
                    return None
                ef, eh = _validated_predictions(r, cfg, profile)
                return TranslationResult(
                    id=t.id,
                    english="",          # predictions only: english never produced
                    alternatives=[],     # nor LLM lookup queries for trusted terms
                    confidence=str(r.get("confidence", "")).lower() or "medium",
                    note=str(r.get("note", "")).strip(),
                    expected_facet=ef,
                    expected_hierarchy=eh,
                )

            run_batches(
                ppending, pstamp, translator.predict_batch,
                lambda batch: [
                    term_to_item(t, siblings.get(t.id, []), domain_map)
                    | {"english": t.main_target_term}
                    for t in batch
                ],
                predict_result, "predicted",
            )
    return results


# ---- review CSV (the gate before lookup) ----------------------------------

TRANSLATION_COLUMNS = [
    # task: "translate" = term was missing English (approved_english applies);
    # "predict" = term already has trusted source English — only the advisory
    # expected_* fields apply, and approved_english is ignored. (read-only)
    "id", "task", "source_term", "parents", "siblings",        # context (read-only)
    "llm_english", "alternatives", "confidence", "note",
    # ---- editable by the cataloguer ----
    # expected_facet / expected_hierarchy: LLM's advisory predictions — correct
    # or blank them here; alternatives (above) may be pruned too: all flow into
    # lookup/tiering. expected_hierarchy must be one of the profile's
    # preferred_hierarchies labels (unrecognized values are ignored downstream).
    "expected_facet", "expected_hierarchy",
    "accept", "approved_english",
]


def export_translations_csv(
    terms: list[SourceTerm],
    results: dict[str, TranslationResult],
    path: str | Path,
    siblings: dict[str, list[str]] | None = None,
) -> int:
    """Write the LLM translations for review. `accept` is pre-filled "yes" and
    `approved_english` is pre-filled with the LLM suggestion, so leaving a row
    untouched uses the suggestion; editing `approved_english` overrides it; set
    `accept` to no/blank to skip a term."""
    siblings = siblings or {}
    by_id = {t.id: t for t in terms}
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TRANSLATION_COLUMNS, extrasaction="ignore")
        w.writeheader()
        n = 0
        for tid, res in results.items():
            t = by_id.get(tid)
            if not t:
                continue
            predict = bool(t.main_target_term)   # term already has English
            w.writerow({
                "id": tid,
                "task": "predict" if predict else "translate",
                "source_term": t.main_lang_term,
                "parents": " > ".join(t.parents_source),
                "siblings": ", ".join(siblings.get(tid, [])),
                "llm_english": res.english,
                "alternatives": ", ".join(res.alternatives),
                "confidence": res.confidence,
                "note": res.note,
                "expected_facet": res.expected_facet,
                "expected_hierarchy": res.expected_hierarchy,
                "accept": "yes",
                # predict rows carry no English decision; anything typed here is
                # ignored by apply (source English is never overwritten).
                "approved_english": "" if predict else res.english,
            })
            n += 1
    return n


@dataclass
class TranslationDecision:
    id: str
    accept: bool
    approved_english: str
    llm_english: str
    alternatives: list[str] = None  # type: ignore[assignment]  # set in __post_init__
    expected_facet: str = ""
    expected_hierarchy: str = ""

    def __post_init__(self):
        if self.alternatives is None:
            self.alternatives = []

    @property
    def source(self) -> str:
        # "human" if the cataloguer changed the suggestion, else "llm".
        a = (self.approved_english or "").strip().casefold()
        l = (self.llm_english or "").strip().casefold()
        return "human" if a and a != l else "llm"


def _split_alternatives(raw: str) -> list[str]:
    return [a.strip() for a in (raw or "").split(",") if a.strip()]


def ingest_translations_csv(path: str | Path) -> dict[str, TranslationDecision]:
    text = _read_csv_text(path)
    lines = text.splitlines()
    if not lines:
        return {}
    delimiter = _detect_delimiter(lines[0])
    out: dict[str, TranslationDecision] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        row = {(k or "").strip(): v for k, v in row.items()}
        tid = (row.get("id") or "").strip()
        if not tid:
            continue
        accept_raw = (row.get("accept") or "").strip().lower()
        accept = accept_raw in {"y", "yes", "true", "1", "accept"}
        out[tid] = TranslationDecision(
            id=tid,
            accept=accept,
            approved_english=(row.get("approved_english") or "").strip(),
            llm_english=(row.get("llm_english") or "").strip(),
            alternatives=_split_alternatives(row.get("alternatives") or ""),
            expected_facet=(row.get("expected_facet") or "").strip().lower(),
            expected_hierarchy=normalize_hierarchy_label(
                row.get("expected_hierarchy") or ""
            ),
        )
    return out


def apply_translations(
    terms: list[SourceTerm], decisions: dict[str, TranslationDecision]
) -> tuple[list[SourceTerm], int, int]:
    """Fold accepted review-CSV decisions into the terms.

    Translation branch (term was missing English): approved English becomes
    main_target_term, target_source is tagged llm/human, and alternatives
    (deduped, minus the approved label) plus the advisory expected_* ride along.

    Predict branch (term already has source-data English): ONLY the advisory
    expected_facet/expected_hierarchy are folded in. main_target_term,
    target_source (stays "source_data") and target_alternatives are never
    touched — keeping the invariant that source_data terms carry no
    LLM-generated query strings, which is what the tiering provenance guard
    ("LLM English never auto-accepts") relies on.

    Returns (terms, n_translations_applied, n_predictions_applied)."""
    applied = 0
    predicted = 0
    for t in terms:
        d = decisions.get(t.id)
        if not d or not d.accept:
            continue
        if d.approved_english and not t.main_target_term:
            t.main_target_term = d.approved_english
            t.target_source = d.source
            approved = d.approved_english.strip().casefold()
            seen: set[str] = {approved}
            alts: list[str] = []
            for a in d.alternatives:
                key = a.strip().casefold()
                if key and key not in seen:
                    seen.add(key)
                    alts.append(a.strip())
            t.target_alternatives = alts
            t.expected_facet = d.expected_facet or None
            t.expected_hierarchy = d.expected_hierarchy or None
            applied += 1
        elif t.main_target_term and (d.expected_facet or d.expected_hierarchy):
            t.expected_facet = d.expected_facet or None
            t.expected_hierarchy = d.expected_hierarchy or None
            predicted += 1
    return terms, applied, predicted


# ---- anomaly helper -------------------------------------------------------

# Substrings in a translation note that suggest a source-hierarchy / cataloguing
# problem the reviewer may want to feed back into MuseumPlus cleanup.
ANOMALY_KEYWORDS = ("anomal", "unusual", "misplac", "odd placement", "does not fit")

ANOMALY_COLUMNS = ["id", "source_term", "parents", "llm_english", "confidence", "note"]


def flag_anomalies(
    in_csv: str | Path, out_csv: str | Path, keywords: tuple[str, ...] = ANOMALY_KEYWORDS
) -> int:
    """Scan a translations CSV and write rows whose note flags a likely source
    hierarchy/cataloguing anomaly to their own CSV. Returns the count."""
    text = _read_csv_text(in_csv)
    lines = text.splitlines()
    if not lines:
        return 0
    delimiter = _detect_delimiter(lines[0])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fields = [(f or "").strip() for f in (reader.fieldnames or [])]
    cols = [c for c in ANOMALY_COLUMNS if c in fields] or fields

    hits = []
    for row in reader:
        row = {(k or "").strip(): v for k, v in row.items()}
        note = (row.get("note") or "").casefold()
        if any(k in note for k in keywords):
            hits.append(row)

    with Path(out_csv).open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in hits:
            w.writerow(r)
    return len(hits)


# ---- targeted re-translation ---------------------------------------------

def select_retranslate_ids(
    translations_csv: str | Path,
    confidences: list[str] | None = None,
    ids: list[str] | None = None,
) -> set[str]:
    """Pick term ids to re-translate: those with a matching confidence in the
    existing translations CSV, plus any explicit ids."""
    want_conf = {c.strip().casefold() for c in (confidences or []) if c.strip()}
    out = {i.strip() for i in (ids or []) if i.strip()}
    if want_conf:
        text = _read_csv_text(translations_csv)
        lines = text.splitlines()
        if lines:
            delim = _detect_delimiter(lines[0])
            for row in csv.DictReader(io.StringIO(text), delimiter=delim):
                row = {(k or "").strip(): v for k, v in row.items()}
                if (row.get("confidence") or "").strip().casefold() in want_conf:
                    out.add((row.get("id") or "").strip())
    out.discard("")
    return out


def apply_results_to_csv(
    existing_csv: str | Path, results: dict[str, TranslationResult], out_csv: str | Path
) -> int:
    """Overlay fresh results onto an existing translations CSV, leaving rows not
    in `results` untouched (preserving any review edits there). For updated rows
    the suggestion columns are refreshed and approved_english/accept are reset to
    the new suggestion. Returns the number of rows updated."""
    text = _read_csv_text(existing_csv)
    lines = text.splitlines()
    if not lines:
        return 0
    delim = _detect_delimiter(lines[0])
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    fields = [(f or "").strip() for f in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): v for k, v in r.items()} for r in reader]

    updated = 0
    for row in rows:
        res = results.get((row.get("id") or "").strip())
        if not res:
            continue
        row["llm_english"] = res.english
        row["alternatives"] = ", ".join(res.alternatives)
        row["confidence"] = res.confidence
        row["note"] = res.note
        row["expected_facet"] = res.expected_facet
        row["expected_hierarchy"] = res.expected_hierarchy
        row["approved_english"] = res.english
        row["accept"] = "yes"
        updated += 1

    cols = fields or TRANSLATION_COLUMNS
    with Path(out_csv).open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return updated
