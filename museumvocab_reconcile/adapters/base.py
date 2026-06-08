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

    def enrich_candidates(
        self, candidates: list[Candidate], target_lang: str
    ) -> list[Candidate]:
        """Fetch each candidate and fold facet/hierarchy/labels onto it."""
        for c in candidates:
            rec = self.fetch(c.concept_id)
            c.facet = rec.get("facet")
            c.scope_note = rec.get("scope_note")
            c.ancestors = rec.get("ancestors", [])
            c.cross_refs = rec.get("cross_refs", [])
            c.pref_label_target = rec.get("pref_labels", {}).get(target_lang)
        return candidates

    @staticmethod
    def normalise(text: str) -> str:
        return " ".join(text.strip().casefold().split())
