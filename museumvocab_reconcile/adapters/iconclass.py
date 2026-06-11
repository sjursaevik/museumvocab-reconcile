"""Iconclass adapter.

Implements the AuthorityAdapter interface against the Iconclass HTTP service.
Like the AAT adapter, the network calls run on your machine (iconclass.org is
not in the sandbox allowlist); offline stages need no network.

Two structural differences from AAT, absorbed here:

1. HIERARCHY IS IN THE NOTATION, NOT broader URIs.
   Per-concept JSON (confirmed shape):
       GET http://iconclass.org/<notation>.json
       -> {"n": "<notation>",
           "p": ["5","52","52D","52D1"],   # path: ancestors broad->narrow, self last
           "l": [<child notations>],
           "txt": {"en": "...", "de": "...", ...},  # labels by language
           "kw": {"en": [...], ...}}        # keywords
   Ancestors come straight from `p[:-1]` — no chain-walking needed.

2. NO NORWEGIAN, AND NO RECONCILIATION-SPEC SEARCH.
   `txt` languages are en/de/fr/it/pt/jp — not nb/nn — so profiles pivot on
   English. Text search is a FastAPI route (OpenAPI at /openapi.json, Swagger
   at /docs). Rather than hardcode a route that may change, `search` resolves
   it from the live spec once and caches it (picking a GET op that takes a `q`
   query parameter). The "facet" analogue is the top division (first character
   of the notation, 0-9); profiles that don't use divisions set accept_all.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..model import Candidate
from .base import AuthorityAdapter

BASE = "http://iconclass.org"
OPENAPI_URL = f"{BASE}/openapi.json"
CONCEPT_URL = BASE + "/{notation}.json"

TOP_DIVISIONS = {
    "0": "abstract_nonrepresentational",
    "1": "religion_magic",
    "2": "nature",
    "3": "human_being",
    "4": "society_civilization",
    "5": "abstract_ideas_concepts",
    "6": "history",
    "7": "bible",
    "8": "literature",
    "9": "classical_mythology",
}


class IconclassAdapter(AuthorityAdapter):
    name = "iconclass"

    def __init__(self, *args: Any, search_template="/api/search", **kwargs: Any):
        super().__init__(*args, **kwargs)
        # If provided, skips OpenAPI discovery (e.g. "/api/search").
        self._search_tpl: str | None = search_template

    # ---- search -----------------------------------------------------------

    def search(self, label: str, lang: str, limit: int = 5) -> list[Candidate]:
        if not label.strip():
            return []
        tpl = self._resolve_search_route()
        resp = self.request("GET", BASE + tpl, params={"q": label}, accept="application/json")
        notations = _extract_notations(resp.json(), limit)
        q_norm = self.normalise(label)
        out: list[Candidate] = []
        for rank, n in enumerate(notations):
            out.append(
                Candidate(
                    authority=self.name,
                    concept_id=n,
                    uri=f"{BASE}/{n}",
                    # Iconclass search returns a ranking, not numeric scores;
                    # synthesise a descending score so tiering's gap logic works.
                    score=float(max(1, 100 - rank * 5)),
                    matched_label="",          # filled during enrichment
                    matched_lang="en",         # pivot language
                    query_lang="en",
                    is_exact=False,            # recomputed in enrich_candidates
                    facet=None,
                    raw={"query": label, "q_norm": q_norm, "rank": rank},
                )
            )
        return out

    def enrich_candidates(self, candidates: list[Candidate], target_lang: str,
                          prefer_langs: list[str] | None = None) -> list[Candidate]:
        """Extend the base behaviour: also recompute exactness against the
        fetched English label, since search returns only notations."""
        candidates = super().enrich_candidates(candidates, target_lang, prefer_langs)
        for c in candidates:
            q_norm = c.raw.get("q_norm", "")
            label = c.pref_label_target or ""
            c.matched_label = label
            c.is_exact = bool(q_norm) and self.normalise(label) == q_norm
        return candidates

    # ---- fetch ------------------------------------------------------------

    def fetch(self, concept_id: str) -> dict[str, Any]:
        cache_key = f"rec:{concept_id}"
        if self.cache and self.cache.has(cache_key):
            return self.cache.get(cache_key)

        doc = self._fetch_doc(concept_id)
        txt = doc.get("txt", {}) or {}
        path = doc.get("p", []) or []
        # ancestors: path excludes self (last element) -> immediate parent first
        ancestor_ids = [a for a in path if a != concept_id][::-1]
        rec = {
            "id": concept_id,
            "uri": f"{BASE}/{concept_id}",
            "format": "iconclass",
            "pref_labels": {lang.lower(): val for lang, val in txt.items()},
            "alt_labels": {},
            "scope_note": None,
            "ancestors": [{"id": a, "label": None} for a in ancestor_ids],
            "cross_refs": [],
            "facet": TOP_DIVISIONS.get((concept_id or " ")[0]),
        }
        if self.cache:
            self.cache.set(cache_key, rec, flush=False)  # lookup flushes periodically
        return rec

    # ---- internals --------------------------------------------------------

    def _fetch_doc(self, concept_id: str) -> dict[str, Any]:
        doc_key = f"doc:{concept_id}"
        if self.cache and self.cache.has(doc_key):
            return self.cache.get(doc_key)
        # Encode '+' (-> %2B) but keep the key parentheses literal, per the docs.
        encoded = quote(concept_id, safe="()")
        resp = self.request("GET", CONCEPT_URL.format(notation=encoded), accept="application/json")
        doc = resp.json()
        if self.cache:
            self.cache.set(doc_key, doc, flush=False)
        return doc

    def _resolve_search_route(self) -> str:
        """Discover the search path from the live OpenAPI spec, once, cached.

        Picks a GET operation that takes a `q` query parameter and has no path
        parameters. Falls back to a constructor-supplied template."""
        if self._search_tpl:
            return self._search_tpl
        if self.cache and self.cache.has("search_route"):
            self._search_tpl = self.cache.get("search_route")
            return self._search_tpl

        spec = self.request("GET", OPENAPI_URL, accept="application/json").json()
        best: str | None = None
        for path, ops in spec.get("paths", {}).items():
            get = ops.get("get")
            if not get:
                continue
            params = get.get("parameters", [])
            has_q = any(p.get("name") == "q" and p.get("in") == "query" for p in params)
            if has_q and "{" not in path:
                best = path
                break
            if has_q and best is None:
                best = path  # fallback: a parametrised path with q
        if not best:
            raise RuntimeError(
                "Could not find an Iconclass search route in the OpenAPI spec. "
                "Inspect https://iconclass.org/docs and pass search_template=..."
            )
        self._search_tpl = best
        if self.cache:
            self.cache.set("search_route", best)
        return best


def _extract_notations(payload: Any, limit: int) -> list[str]:
    """Normalise the search response into a ranked list of notations.

    Tolerates the common shapes: a bare list of notation strings, a list of
    dicts with an 'n'/'notation' key, or an object wrapping such a list.
    """
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("result", "results", "docs", "items", "data"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            items = []
    else:
        items = []

    notations: list[str] = []
    for it in items:
        if isinstance(it, str):
            n = it
        elif isinstance(it, dict):
            n = it.get("n") or it.get("notation") or it.get("id")
        else:
            n = None
        if n and n not in notations:
            notations.append(n)
        if len(notations) >= limit:
            break
    return notations
