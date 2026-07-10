"""The authority-adapter seam.

Every authority (AAT, Iconclass, later ULAN/TGN/Wikidata/LCSH) implements this
interface. The engine only ever talks to ``AuthorityAdapter``; it never knows
which service is behind it. Two operations are required:

    search(label, lang, limit) -> list[Candidate]
        Query a label, get back ranked, normalised candidates.

    fetch(concept_id) -> dict   (a "concept record")
        Retrieve one concept's labels (by language), authority-internal
        category/facet, scope note, ancestor chain, and cross-facet relations.

``enrich_candidates`` ties them together: search, then fetch each hit so the
returned Candidates carry facet + hierarchy used by tiering.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

import requests

from ..cache import JsonCache
from ..model import Candidate

#: A descriptive User-Agent. Many vocabulary servers (Getty included) reject or
#: drop the default "python-requests/x.y" agent — a real UA avoids 403/499s.
DEFAULT_USER_AGENT = (
    "museumvocab-reconcile/0.1 (Nasjonalmuseet; Linked Art vocabulary reconciliation)"
)
#: Transient HTTP statuses worth retrying. 499 = client-closed/blocked (often
#: WAF or rate limiting); 429 = explicit rate limit; 5xx = server-side.
RETRY_STATUSES = (429, 499, 500, 502, 503, 504)


class AuthorityAdapter(ABC):
    #: short id used in profiles ("aat", "iconclass", ...)
    name: str = "base"

    def __init__(
        self,
        cache: JsonCache | None = None,
        *,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        max_retries: int = 4,
        backoff: float = 1.5,
        request_delay: float = 0.0,
        session: requests.Session | None = None,
    ):
        self.cache = cache
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.request_delay = request_delay
        self.session = session or requests.Session()
        # Force our UA: a fresh Session already carries "python-requests/x.y",
        # which is exactly the default many servers block — so override, not setdefault.
        self.session.headers["User-Agent"] = user_agent

    @abstractmethod
    def search(self, label: str, lang: str, limit: int = 5) -> list[Candidate]:
        """Return ranked candidates for a label. Facet/ancestors may be empty
        here and filled in by :meth:`fetch` / :meth:`enrich_candidates`."""

    @abstractmethod
    def fetch(self, concept_id: str) -> dict[str, Any]:
        """Return a concept record:
        {
          "id": str, "uri": str,
          "pref_labels": {lang: str}, "alt_labels": {lang: [str, ...]},
          "facet": str | None,                # authority-internal category
          "scope_note": str | None,
          "ancestors": [{"id":..., "label":...}, ...],
          "cross_refs": [{"id":..., "label":..., "facet":...}, ...],
        }
        Implementations should use ``self.cache`` keyed by concept_id."""

    # ---- HTTP with retry/backoff -----------------------------------------

    def request(
        self, method: str, url: str, *, accept: str | None = None, **kwargs: Any
    ) -> requests.Response:
        """Issue an HTTP request through the shared session, retrying transient
        failures (RETRY_STATUSES and connection errors) with exponential
        backoff, honouring Retry-After when present."""
        headers = dict(kwargs.pop("headers", {}))
        if accept:
            headers["Accept"] = accept
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.request_delay:
                time.sleep(self.request_delay)
            try:
                resp = self.session.request(
                    method, url, timeout=self.timeout, headers=headers, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self._wait(None, attempt))
                    continue
                raise
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                time.sleep(self._wait(resp, attempt))
                continue
            resp.raise_for_status()
            return resp
        if last_exc:  # pragma: no cover - defensive
            raise last_exc
        raise RuntimeError(f"request to {url} failed after retries")

    def _wait(self, resp: requests.Response | None, attempt: int) -> float:
        if resp is not None:
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                return min(float(ra), 30.0)
        return min(self.backoff ** attempt, 30.0)

    # ---- shared helpers ---------------------------------------------------

    def peek_labels(self, concept_id: str) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Return (pref_labels, alt_labels) for a concept as cheaply as the
        adapter can manage. Default: a full :meth:`fetch` (cached). Adapters
        with a lighter cached representation (AAT's compact nodes) override
        this so callers can inspect labels WITHOUT triggering hierarchy
        resolution."""
        rec = self.fetch(concept_id)
        return rec.get("pref_labels", {}) or {}, rec.get("alt_labels", {}) or {}

    def candidate_from_concept(
        self,
        concept_id: str,
        *,
        query_term: str,
        query_lang: str,
        target_lang: str,
        prefer_langs: list[str] | None = None,
        promoted_from: str | None = None,
    ) -> Candidate:
        """Build a fully-enriched Candidate directly from a concept record —
        for candidates synthesised OUTSIDE the reconcile results (ancestor
        promotion). Score is 0.0: the concept has no reconcile score and a
        fabricated one would silently participate in the score/gap gate.
        matched_label/matched_lang/is_exact are recomputed honestly by
        _refine_match against the record's language-tagged labels."""
        rec = self.fetch(concept_id)
        alt = rec.get("alt_labels", {}) or {}
        if prefer_langs:
            alt = {k: v for k, v in alt.items() if k in prefer_langs}
        c = Candidate(
            authority=self.name,
            concept_id=concept_id,
            uri=rec.get("uri", ""),
            score=0.0,
            matched_label=query_term,   # provisional; recomputed below
            matched_lang=query_lang,
            query_lang=query_lang,
            query_term=query_term,
            is_exact=False,             # provisional; recomputed below
            facet=rec.get("facet"),
            aat_facet=rec.get("aat_facet"),
            scope_note=rec.get("scope_note"),
            ancestors=rec.get("ancestors", []),
            cross_refs=rec.get("cross_refs", []),
            pref_label_target=(rec.get("pref_labels", {}) or {}).get(target_lang),
            alt_labels=alt,
            promoted_from=promoted_from,
        )
        self._refine_match(c, rec, prefer_langs)
        return c

    def enrich_candidates(
        self,
        candidates: list[Candidate],
        target_lang: str,
        prefer_langs: list[str] | None = None,
    ) -> list[Candidate]:
        """Fetch each candidate and fold facet/hierarchy/labels onto it.

        ``prefer_langs`` (typically trusted_exact_match_langs + match_langs, in
        that order) biases the matched-language ATTRIBUTION when the same label
        exists in several languages — see _refine_match."""
        for c in candidates:
            rec = self.fetch(c.concept_id)
            c.facet = rec.get("facet")
            c.aat_facet = rec.get("aat_facet")
            c.scope_note = rec.get("scope_note")
            c.ancestors = rec.get("ancestors", [])
            c.cross_refs = rec.get("cross_refs", [])
            c.pref_label_target = rec.get("pref_labels", {}).get(target_lang)
            # Retain alt labels for downstream reasoning (the deepen recommender
            # surfaces the nb/nn ones). Filter to prefer_langs when given so the
            # candidates artifact doesn't carry every language's alt labels;
            # _refine_match below still sees the FULL record for exact detection.
            alt = rec.get("alt_labels", {}) or {}
            if prefer_langs:
                alt = {k: v for k, v in alt.items() if k in prefer_langs}
            c.alt_labels = alt
            if c.query_term:
                self._refine_match(c, rec, prefer_langs)
        return candidates

    def _refine_match(
        self, c: Candidate, rec: dict[str, Any], prefer_langs: list[str] | None = None
    ) -> None:
        """Recompute matched_label / matched_lang / is_exact against the fetched
        record's language-tagged labels, rather than trusting the reconcile
        display name (always the English label) and the query language.

        A query can exactly match a label in a language OTHER than the one it was
        issued in — e.g. an English term that coincides with a French prefLabel
        (tagged "und" when its language URI isn't mapped), or an nb/nn query that
        Getty matched via a Norwegian altLabel while the display name stays
        English. Recording the true matched language is what lets the
        trusted-exact gate (nb/nn) and the match-language filter behave correctly.
        Only EXACT matches are reassigned; a fuzzy hit keeps its provisional
        display values and is simply marked not-exact."""
        q = self.normalise(c.query_term)
        if not q:
            return
        pairs: list[tuple[str, str]] = list((rec.get("pref_labels") or {}).items())
        for lang, vals in (rec.get("alt_labels") or {}).items():
            pairs.extend((lang, v) for v in vals)
        exact = [(lang, val) for lang, val in pairs if self.normalise(val) == q]
        if exact:
            # Attribution tie-break when the same surface form exists in several
            # languages (e.g. 'sari' in en/es/it/fr/nl + 'Sari' in de): prefer the
            # query language, then the caller's preferred languages IN ORDER
            # (trusted nb/nn first, then the other match_langs), then a stable
            # alphabetical order. The old query-lang-then-alphabetical rule
            # attributed 'Sari' to 'de', tripping the match_langs demotion for a
            # label that is equally an English one.
            pl = prefer_langs or []
            rank = {lang: i for i, lang in enumerate(pl)}

            def key(lv: tuple[str, str]):
                lang = lv[0]
                return (
                    0 if lang == c.query_lang else 1,
                    rank.get(lang, len(pl)),
                    lang,
                )

            exact.sort(key=key)
            lang, val = exact[0]
            c.matched_lang, c.matched_label, c.is_exact = lang, val, True
        else:
            c.is_exact = False

    @staticmethod
    def normalise(text: str) -> str:
        return " ".join(text.strip().casefold().split())
