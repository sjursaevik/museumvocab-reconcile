"""KulturNav adapter.

Implements the AuthorityAdapter interface against KulturNav's read API
(https://kulturnav.org/info/api-core). Like the AAT and Iconclass adapters the
network calls run on YOUR machine — kulturnav.org applies bot detection and the
sandbox can't reach it reliably — while the offline stages need no network.

TWO ENDPOINTS, ONE PER OPERATION (the same split as AAT)
-------------------------------------------------------
* search  -> Core API:   GET /api/core/{expr}[/{offset}/{count}][?lang=..]
      `expr` is comma-joined `propertyType:value` clauses combined with AND, e.g.
      `entityType:Concept,entity.dataset:<uuid>,entity.name:akvarell`. Response
      envelope: {"hits": N, "entities": [{"uuid","name",...}]}. There is NO
      per-hit relevance score (Solr ranks but the Core API doesn't expose it), so
      — like Iconclass — we synthesise a descending score from rank. Real trust
      comes from the exact-label recompute in enrich, not from score, so KulturNav
      profiles should run auto_accept.mode = exact_only.

* fetch   -> per-record JSON-LD:  GET /{uuid}  Accept: application/ld+json
      Canonical SKOS with @language-tagged labels (prefLabel/altLabel), broader,
      scopeNote/definition, and the OUTBOUND matchings (skos:exactMatch etc. +
      owl:sameAs) that crosswalk a KulturNav concept to Getty AAT / Wikidata. The
      JSON-LD record is the language-complete source the trusted nb/nn exact
      signal relies on; the Core search `name` is only a provisional display label.

DATASET SCOPING IS A TRUST REQUIREMENT, NOT A NICETY
----------------------------------------------------
KulturNav is multi-tenant: many institutions' vocabularies of varying scope plus
person/place/object name authorities live in one index. An exact `nb` hit in the
WRONG dataset (a homonym in a name list, another museum's idiosyncratic term) is a
real false friend. So `datasets` should always be set in the profile to the
Nasjonalmuseet-curated Concept dataset UUIDs for that vocabulary. The expression
grammar ANDs its clauses (comma) and offers no OR, so multiple datasets are issued
as separate per-dataset searches and merged here. An empty `datasets` list means
UNSCOPED (every Concept) — lower trust; we warn once.

LANGUAGE CODE: Bokmaal is `no` in KulturNav, not `nb`
-----------------------------------------------------
KulturNav's lang param and JSON-LD tags use {no|nn|sv|en|fi|et}; here `no` means
Bokmaal specifically. The engine speaks canonical nb/nn, so we map nb<->no on the
way out (request) and no->nb on the way in (parse). Skipping this would land every
Bokmaal label under the wrong code and silently disable the nb exact-match signal
— the same class of bug as AAT's `/language/` labels collapsing to `und`.

FIRST-RUN VERIFICATION
----------------------
The JSON-LD parser is written defensively but the live shapes have not been pinned
from the sandbox. On the first real run, dump one record with
`python diagnose_kulturnav.py` and confirm: prefLabel/altLabel @language tags
(no/nn/en), broader id form, and how exactMatch/sameAs serialise. Adjust the
small predicate/lang maps below if needed.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..model import Candidate
from .base import AuthorityAdapter

BASE = "https://kulturnav.org"
CORE_URL = BASE + "/api/core/{expr}"
RECORD_URL = BASE + "/{uuid}"

# Engine canonical code -> KulturNav request code, and the inverse for parsing.
_LANG_TO_KN = {"nb": "no"}
_LANG_FROM_KN = {"no": "nb", "nob": "nb", "nor": "nb", "nno": "nn"}


def _to_kn_lang(code: str) -> str:
    return _LANG_TO_KN.get((code or "").lower(), (code or "").lower())


def _from_kn_lang(code: str) -> str:
    c = (code or "").strip().lower()
    # JSON-LD tags can be "no", "no-NO", "nn", "en", ... — take the primary subtag.
    c = c.split("-")[0]
    return _LANG_FROM_KN.get(c, c)


# Host fragments -> (authority short id, id-extractor) for classifying an outbound
# matching URI. Extend as more target authorities appear in the data.
def _classify_match_uri(uri: str) -> tuple[str | None, str | None]:
    u = (uri or "").strip()
    low = u.lower()
    if "vocab.getty.edu/aat/" in low or "/aat/" in low:
        tail = u.rstrip("/").rsplit("/", 1)[-1]
        return "aat", (tail if tail.isdigit() else None)
    if "wikidata.org/" in low:
        tail = u.rstrip("/").rsplit("/", 1)[-1]
        return "wikidata", (tail or None)
    if "vocab.getty.edu/ulan/" in low:
        tail = u.rstrip("/").rsplit("/", 1)[-1]
        return "ulan", (tail if tail.isdigit() else None)
    if "vocab.getty.edu/tgn/" in low:
        tail = u.rstrip("/").rsplit("/", 1)[-1]
        return "tgn", (tail if tail.isdigit() else None)
    if "iconclass.org/" in low:
        return "iconclass", u.rstrip("/").rsplit("/", 1)[-1] or None
    return None, None


class KulturNavAdapter(AuthorityAdapter):
    name = "kulturnav"

    def __init__(
        self,
        *args: Any,
        datasets: list[str] | None = None,
        entity_types: list[str] | None = None,
        search_property: str = "entity.name",
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        # Accept full URIs or bare UUIDs in the profile; store bare UUIDs.
        self.datasets = [_bare_uuid(d) for d in (datasets or []) if _bare_uuid(d)]
        self.entity_types = entity_types or ["Concept"]
        self.search_property = search_property
        self._warned_unscoped = False

    # ---- search -----------------------------------------------------------

    def search(self, label: str, lang: str, limit: int = 5) -> list[Candidate]:
        label = (label or "").strip()
        if not label:
            return []
        klang = _to_kn_lang(lang)
        out: list[Candidate] = []
        seen: set[str] = set()
        # One query per scoped dataset (the expression grammar is AND-only, so
        # datasets can't be OR'd in a single call); merge + dedupe by uuid,
        # preferring the better rank when a concept appears in several queries.
        scopes: list[str | None] = self.datasets or [None]
        if not self.datasets and not self._warned_unscoped:
            self._warned_unscoped = True
            import sys
            print(
                "kulturnav: WARNING running UNSCOPED (no `datasets` in profile.adapter) "
                "— matches across all of KulturNav are low-trust; set dataset UUIDs.",
                file=sys.stderr,
            )
        for ds in scopes:
            for rank, (uuid, name) in enumerate(self._search_one(label, klang, ds, limit)):
                if uuid in seen:
                    continue
                seen.add(uuid)
                out.append(
                    Candidate(
                        authority=self.name,
                        concept_id=uuid,
                        uri=f"{BASE}/{uuid}",
                        # No score from the API; synthesise a descending one so
                        # tiering's gap/ordering logic still functions. Trust is
                        # decided by the exact recompute in enrich, not this.
                        score=float(max(1, 100 - rank * 5)),
                        matched_label=name or "",   # provisional; recomputed in enrich
                        matched_lang=lang,
                        query_lang=lang,
                        query_term=label,
                        is_exact=self.normalise(name or "") == self.normalise(label),
                        facet=None,
                        raw={"dataset": ds, "rank": rank},
                    )
                )
        # Stable re-rank so the merged list is in descending synthetic score.
        out.sort(key=lambda c: c.score, reverse=True)
        # Honour the per-query limit across the merged per-dataset results.
        return out[: limit * max(1, len(self.datasets))]

    def _search_one(
        self, label: str, klang: str, dataset: str | None, limit: int
    ) -> list[tuple[str, str | None]]:
        results: list[tuple[str, str | None]] = []
        # Entity types can't be OR'd in the expression grammar either, so each
        # configured type is its own query (default is just Concept).
        for etype in (self.entity_types or ["Concept"]):
            expr = ",".join(
                [f"entityType:{etype}"]
                + ([f"entity.dataset:{dataset}"] if dataset else [])
                + [f"{self.search_property}:{label}"]
            )
            # `expr` carries ':' and ',' which are grammar; only the label value
            # needs light escaping for spaces. quote() with those safe chars.
            path = quote(f"{expr}/0/{limit}", safe=":,/!<>")
            data = self.request(
                "GET", CORE_URL.format(expr=path),
                accept="application/json",
                params={"lang": klang},
            ).json()
            for ent in _entities(data):
                uuid = _bare_uuid(ent.get("uuid") or ent.get("uri") or "")
                if uuid:
                    results.append((uuid, _caption(ent)))
        return results

    # ---- enrich -----------------------------------------------------------

    def enrich_candidates(
        self, candidates: list[Candidate], target_lang: str,
        prefer_langs: list[str] | None = None,
    ) -> list[Candidate]:
        candidates = super().enrich_candidates(candidates, target_lang, prefer_langs)
        # Fold the fetched record's outbound matchings onto each candidate so a
        # later assemble step can emit the AAT/Wikidata URI. Review-grade only.
        for c in candidates:
            rec = self.fetch(c.concept_id)
            c.matchings = rec.get("matchings", [])
        return candidates

    # ---- fetch ------------------------------------------------------------

    def fetch(self, concept_id: str) -> dict[str, Any]:
        node = self._node(concept_id)
        ancestors = self._walk_ancestors(node["broader"])
        return {
            "id": concept_id,
            "uri": f"{BASE}/{concept_id}",
            "format": node["format"],
            "pref_labels": node["pref_labels"],
            "alt_labels": node["alt_labels"],
            "scope_note": node["scope_note"],
            "ancestors": ancestors,
            "cross_refs": [],
            # KulturNav has no fixed facet vocabulary; the nearest per-concept
            # category is surfaced for human review. Profiles use accept_all, so
            # `facet` doesn't gate — it's informational.
            "facet": node["facet"],
            "aat_facet": node["aat_facet"],
            "matchings": node["matchings"],
        }

    def _node(self, concept_id: str) -> dict[str, Any]:
        """Compact, cached, single-record parse (immediate broader only — no
        hierarchy walk here, so it's safe to reuse while climbing)."""
        cache_key = f"node:{concept_id}"
        if self.cache and self.cache.has(cache_key):
            return self.cache.get(cache_key)

        node = _concept_node(self._http_record(concept_id), concept_id)
        pref, alt = _parse_labels(node)
        category = _parse_category(node)   # (label, id) | (None, None)
        compact = {
            "id": concept_id,
            "format": "kulturnav_jsonld",
            "pref_labels": pref,
            "alt_labels": alt,
            "scope_note": _parse_scope_note(node),
            "broader": _parse_broader(node),
            "facet": (category[0].casefold() if category[0] else None),
            "aat_facet": (
                f"{category[0]} ({category[1]})" if category[0] and category[1]
                else category[0]
            ),
            "matchings": _parse_matchings(node),
        }
        if self.cache:
            self.cache.set(cache_key, compact, flush=False)  # lookup flushes periodically
        return compact

    def _http_record(self, concept_id: str) -> dict[str, Any]:
        resp = self.request(
            "GET", RECORD_URL.format(uuid=concept_id),
            accept="application/ld+json, application/json",
        )
        return resp.json()

    def _walk_ancestors(self, broader_ids: list[str]) -> list[dict[str, Any]]:
        """Climb broader links (narrow->broad) using the compact node cache.

        KulturNav concept hierarchies are typically shallow; follow the first
        broader at each level, dedupe, cap depth defensively, and stop cleanly on
        a network error rather than disguising it as a shorter chain."""
        import requests as _rq
        ancestors: list[dict[str, Any]] = []
        visited: set[str] = set()
        parents = list(broader_ids)
        steps = 0
        while parents and steps < 40:
            cid = parents[0]
            if cid in visited:
                parents = parents[1:]
                continue
            visited.add(cid)
            steps += 1
            try:
                n = self._node(cid)
            except _rq.RequestException:
                ancestors.append({"id": cid, "label": None})
                break
            label = n["pref_labels"].get("nb") or n["pref_labels"].get("en")
            ancestors.append({"id": cid, "label": label})
            parents = list(n["broader"])
        return ancestors


# ---- record parsing (tolerant; verify against a live record on first run) ----

def _entities(data: Any) -> list[dict[str, Any]]:
    """Core API returns {"hits":N,"entities":[...]}; tolerate a bare list too."""
    if isinstance(data, dict):
        ents = data.get("entities")
        return [e for e in ents if isinstance(e, dict)] if isinstance(ents, list) else []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def _caption(ent: dict[str, Any]) -> str | None:
    """Best-effort display label from a Core search hit. The `name` is usually a
    language-keyed dict ({"no":..,"en":..,"*":..}); prefer the native form
    (the `*` default, then Norwegian) over English so the PROVISIONAL label
    matches an nb query — exactness is recomputed from the record in enrich
    regardless, so this only affects pre-enrich display."""
    n = ent.get("name") or ent.get("caption")
    if isinstance(n, str):
        return n
    if isinstance(n, dict):
        for k in ("*", "no", "nb", "nn", "en"):
            if isinstance(n.get(k), str):
                return n[k]
        return next((v for v in n.values() if isinstance(v, str)), None)
    if isinstance(n, list) and n:
        first = n[0]
        return first.get("value") if isinstance(first, dict) else (first if isinstance(first, str) else None)
    return None


def _node_graph(doc: dict[str, Any]) -> list[dict[str, Any]]:
    g = doc.get("@graph")
    if isinstance(g, list):
        return [n for n in g if isinstance(n, dict)]
    return []


def _concept_node(doc: Any, concept_id: str) -> dict[str, Any]:
    """Locate the main concept node whether the JSON-LD is flat or a @graph, or
    whether the Core API envelope ({entities:[...]}) was returned instead.

    KulturNav's JSON-LD @graph also contains sibling fragment nodes — a
    `#about` foaf:Document and `#superconcept.webReference-N` nodes — whose @id
    shares the concept's UUID once the fragment is stripped. Prefer the node
    whose @id has NO fragment (the concept itself); fall back to first match."""
    if isinstance(doc, list):
        doc = doc[0] if doc else {}
    ents = _entities(doc)
    if ents:
        for e in ents:
            if _bare_uuid(e.get("uuid") or e.get("@id") or e.get("id") or "") == concept_id:
                return e
        return ents[0]
    nodes = _node_graph(doc)
    if nodes:
        fallback: dict[str, Any] | None = None
        for n in nodes:
            raw_id = n.get("@id") or n.get("id") or ""
            if _bare_uuid(raw_id) != concept_id:
                continue
            if "#" not in raw_id:        # the concept node, not a #about/#webRef fragment
                return n
            fallback = fallback or n
        if fallback is not None:
            return fallback
        return nodes[0]
    return doc if isinstance(doc, dict) else {}


def _lang_value_pairs(value: Any) -> list[tuple[str, str]]:
    """Normalise a label value into (lang, text) pairs across shapes:
      "text"                                    -> [("", "text")]
      {"@value":"t","@language":"no"}           -> [("nb","t")]
      {"value":"t","lang":"no"}                 -> [("nb","t")]
      {"no":"t","en":"u"}                       -> [("nb","t"),("en","u")]
      [ <any of the above> ...]                 -> flattened
    """
    out: list[tuple[str, str]] = []
    if value is None:
        return out
    if isinstance(value, str):
        return [("", value)]
    if isinstance(value, list):
        for v in value:
            out.extend(_lang_value_pairs(v))
        return out
    if isinstance(value, dict):
        text = value.get("@value") or value.get("value")
        lang = value.get("@language") or value.get("language") or value.get("lang")
        if text is not None:
            return [(_from_kn_lang(lang) if lang else "", text)]
        # language-keyed dict
        for k, v in value.items():
            if k == "*":          # KulturNav's default/unspecified sentinel — skip
                continue
            if isinstance(v, str):
                out.append((_from_kn_lang(k), v))
            elif isinstance(v, dict) and (v.get("value") or v.get("@value")):
                out.append((_from_kn_lang(k), v.get("value") or v.get("@value")))
    return out


# Predicate keys we accept for each logical field (JSON-LD compact or SKOS-prefixed
# or KulturNav core property names). Order doesn't matter; all are merged.
_PREF_KEYS = ("skos:prefLabel", "prefLabel", "entity.name", "name", "caption")
_ALT_KEYS = ("skos:altLabel", "altLabel", "entity.alternativeName", "alternativeName",
             "hiddenLabel", "skos:hiddenLabel")
_SCOPE_KEYS = ("skos:scopeNote", "scopeNote", "concept.scopeNote",
               "skos:definition", "definition", "concept.definition",
               "entity.description", "description")
_BROADER_KEYS = ("skos:broader", "broader", "concept.broader")
_CATEGORY_KEYS = ("concept.category", "category")
_MATCH_KEYS = {
    "exactMatch": ("skos:exactMatch", "exactMatch", "concept.exactMatch",
                   "hasExactExternalAuthority"),
    "closeMatch": ("skos:closeMatch", "closeMatch", "concept.closeMatch"),
    "broadMatch": ("skos:broadMatch", "broadMatch", "concept.broadMatch"),
    "narrowMatch": ("skos:narrowMatch", "narrowMatch", "concept.narrowMatch"),
    "relatedMatch": ("skos:relatedMatch", "relatedMatch", "concept.relatedMatch"),
    "sameAs": ("owl:sameAs", "sameAs", "entity.sameAs", "entity.sameAsUrl"),
}


def _props(node: dict[str, Any]) -> dict[str, Any]:
    """Core API nests fields under "properties"; JSON-LD puts them top-level.
    Return a merged view so the key lookups below work for either shape."""
    merged = dict(node)
    p = node.get("properties")
    if isinstance(p, dict):
        merged.update(p)
    return merged


def _first_present(node: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in node and node[k] not in (None, "", []):
            return node[k]
    return None


def _parse_labels(node: dict[str, Any]) -> tuple[dict[str, str], dict[str, list[str]]]:
    p = _props(node)
    pref: dict[str, str] = {}
    alt: dict[str, list[str]] = {}
    for lang, text in _lang_value_pairs(_first_present(p, _PREF_KEYS)):
        lang = lang or "en"   # untagged label -> treat as the record's display (en)
        pref.setdefault(lang, text)
    for key in _ALT_KEYS:
        for lang, text in _lang_value_pairs(p.get(key)):
            lang = lang or "und"
            if pref.get(lang) != text and text not in alt.setdefault(lang, []):
                alt[lang].append(text)
    return pref, alt


def _parse_scope_note(node: dict[str, Any]) -> str | None:
    pairs = _lang_value_pairs(_first_present(_props(node), _SCOPE_KEYS))
    if not pairs:
        return None
    # Prefer Norwegian, then English, else first.
    by_lang = {l: t for l, t in pairs}
    return by_lang.get("nb") or by_lang.get("en") or pairs[0][1]


def _parse_broader(node: dict[str, Any]) -> list[str]:
    out: list[str] = []
    val = _first_present(_props(node), _BROADER_KEYS)
    for ref in _as_list(val):
        cid = _ref_uuid(ref)
        if cid and cid not in out:
            out.append(cid)
    return out


def _parse_category(node: dict[str, Any]) -> tuple[str | None, str | None]:
    val = _first_present(_props(node), _CATEGORY_KEYS)
    for ref in _as_list(val):
        cid = _ref_uuid(ref)
        label = _ref_caption(ref)
        if cid or label:
            return label, cid
    return None, None


def _parse_matchings(node: dict[str, Any]) -> list[dict[str, Any]]:
    p = _props(node)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for relation, keys in _MATCH_KEYS.items():
        for ref in _as_list(_first_present(p, keys)):
            uri = _ref_uri(ref)
            if not uri:
                continue
            key = (relation, uri)
            if key in seen:
                continue
            seen.add(key)
            authority, ext_id = _classify_match_uri(uri)
            out.append({"relation": relation, "uri": uri,
                        "authority": authority, "id": ext_id})
    return out


# ---- reference helpers ----------------------------------------------------

def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _ref_uuid(ref: Any) -> str:
    """UUID out of an ENTITY_REFERENCE / JSON-LD reference / bare value.

    KulturNav ENTITY_REFERENCE: {"uuid":<prop>,"valueType":"ENTITY_REFERENCE",
    "value":<target uuid>} — follow `value`, NOT `uuid` (which is the property's
    own id). JSON-LD references use {"@id": uri}."""
    if isinstance(ref, dict):
        if ref.get("valueType") == "ENTITY_REFERENCE" and ref.get("value"):
            return _bare_uuid(ref["value"])
        return _bare_uuid(ref.get("@id") or ref.get("id") or ref.get("value") or ref.get("uuid") or "")
    if isinstance(ref, str):
        return _bare_uuid(ref)
    return ""


def _ref_uri(ref: Any) -> str:
    """Absolute URI out of a matching value (URL-typed property or @id ref)."""
    if isinstance(ref, dict):
        return (ref.get("value") or ref.get("@id") or ref.get("id")
                or ref.get("uri") or ref.get("url") or "")
    if isinstance(ref, str):
        return ref
    return ""


def _ref_caption(ref: Any) -> str | None:
    if isinstance(ref, dict):
        cap = ref.get("displayValue") or ref.get("caption") or ref.get("name") or ref.get("_label")
        if isinstance(cap, dict):
            return cap.get("value") or cap.get("@value") or next(iter(cap.values()), None)
        if isinstance(cap, str):
            return cap
    return None


def _bare_uuid(raw: str) -> str:
    """Reduce a KulturNav URI/URN/value to a bare UUID; pass through a bare UUID.
    Tolerant: returns "" if no uuid-like token is found."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.split("#")[0].rstrip("/")
    tail = s.rsplit("/", 1)[-1]
    tail = tail.split(":")[-1]   # strip any urn:/prefix
    return tail
