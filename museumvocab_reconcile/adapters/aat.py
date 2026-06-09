"""Getty AAT adapter.

Implements the AuthorityAdapter interface against:
  * the OpenRefine reconciliation API  (https://services.getty.edu/vocab/reconcile/)
  * per-concept JSON-LD                 (http://vocab.getty.edu/aat/{id}.json)

EXECUTION NOTE: vocab.getty.edu blocks automated access from the Claude
sandbox (ROBOTS_DISALLOWED), so `lookup` runs on your machine — the split we
kept. The first time you run it, sanity-check one record against
`_parse_concept` (see `tests/`), as Getty's serialization default has shifted.

TWO SERIALIZATIONS
------------------
Getty serves the same concept in two shapes with different keys:

  GVP / SKOS (canonical here)
      Labels carry plain BCP-47 tags: skos:prefLabel/altLabel = [{"@value","@language"}].
      Hierarchy: skos:broader / gvp:broaderPreferred -> {"@id": ...}.
      Scope note: skos:scopeNote -> node with rdf:value/@value.
      This is preferred because the @nb / @nn tags are what the
      Norwegian-altLabel auto-accept signal relies on.

  Linked.Art (now Getty's default for JSON/JSON-LD)
      Labels: identified_by = [{type:"Name", content, language:[{id}]}].
      Languages are AAT URIs, not BCP-47 codes -> needs LANG_URI map below,
      which should be VERIFIED against a live record on first run.

`fetch` auto-detects which shape it received and parses accordingly. To force
the GVP shape deterministically, the adapter requests it with an Accept header
and the documented `.json` URL.
"""
from __future__ import annotations

import json
from typing import Any

import requests

from ..model import Candidate
from .base import AuthorityAdapter

RECONCILE_URL = "https://services.getty.edu/vocab/reconcile/"
CONCEPT_URL = "https://vocab.getty.edu/aat/{id}.json"
# Cap on ancestors visited while climbing to the facet. The climb follows the
# *preferred* parent, so it walks a single lineage; 40 is generous (deepest AAT
# object lineages are ~15 levels) and the node cache makes repeat ancestors free.
MAX_ANCESTOR_DEPTH = 40

# Root of the AAT. By Getty's editorial rules the *facets* sit directly below it
# (https://www.getty.edu/.../3.1/), so the node whose parent is this id IS the
# concept's AAT facet — a reliable signal that does NOT depend on FACET_ROOTS
# being complete. Used to find the facet node for the human-readable `aat_facet`.
AAT_TOP_ID = "300000000"

# AAT id -> internal facet label used in profiles.
#
# What actually matters here are the FACET ROOT ids (the nodes directly under the
# AAT root). `_resolve_hierarchy` climbs the preferred-parent chain to the facet
# and lets the hit CLOSEST TO THE TOP win, so a correct facet root overrides any
# lower, possibly-mislabelled guide term on the same chain. Listing all ~7 facet
# roots makes `facet` resolve for (almost) every term; guide-term ids below are
# only a nearer fallback for chains that don't reach a root within the cap.
# Confirm every id with `python tools/verify_facets.py` (it can reach Getty; the
# sandbox cannot). The reviewer also sees the live `aat_facet` label per term, so
# a missing/wrong mapping here is now visible rather than silent.
FACET_ROOTS: dict[str, str] = {
    "300264092": "work_types",      # Objects Facet (ROOT) — VERIFIED. Do not remove:
                                    # without it, deep object lineages resolve to no facet.
    "300053001": "techniques",      # Processes and Techniques (Activities Facet) [pilot]
    "300054216": "techniques",      # painting techniques [pilot]
    "300053319": "techniques",      # printing/printmaking [pilot]
    "300054196": "techniques",      # textile-making [pilot]
    "300134334": "techniques",      # photographic techniques [pilot]
    "300054686": "techniques",      # publishing
    "300185711": "work_types",      # Object Genres (Objects Facet)
    "300241490": "materials",       # components
    "300015646": "styles_periods",   # styles and periods. 
    "300264091": "materials", #materials facet 
    "300264087": "physical_attributes", #physical_attributes
    "300264086": "subjects", #Associated Concepts Facet sjekk denne etter testing evt
    "300009700": "subjects", #design elements
    "300266038": "formats", #formats
}

# Linked.Art only: AAT language-concept URI -> BCP-47 code. English is well
# known; the Norwegian entries MUST be confirmed against a live record before
# trusting nb/nn exact-match auto-accept on the Linked.Art path.
LANG_URI: dict[str, str] = {
    "300388277": "en",   # English
    "300391418": "nb",   # Norwegian (Bokmaal) — verified
    "300388992": "nn",   # Norwegian (Nynorsk) — verified
}


class AatAdapter(AuthorityAdapter):
    name = "aat"

    # ---- search -----------------------------------------------------------

    def search(self, label: str, lang: str, limit: int = 5) -> list[Candidate]:
        if not label.strip():
            return []
        data = self._reconcile({"q0": {"query": label, "type": "/aat", "limit": limit}})
        hits = _result_hits(data, "q0")
        out: list[Candidate] = []
        for h in hits:
            cid = _clean_id(h.get("id", ""))
            if not cid:
                continue
            out.append(
                Candidate(
                    authority=self.name,
                    concept_id=cid,
                    uri=f"http://vocab.getty.edu/aat/{cid}",
                    score=float(h.get("score", 0.0)),
                    matched_label=h.get("name", ""),
                    matched_lang=lang,
                    query_lang=lang,
                    query_term=label,
                    # Provisional; recomputed against the fetched record's
                    # language-tagged labels in enrich_candidates (the reconcile
                    # `name` is the English display label, not necessarily the
                    # label the query actually matched).
                    is_exact=self.normalise(h.get("name", "")) == self.normalise(label),
                    facet=None,
                    raw=h,
                )
            )
        return out

    _recon_style: str | None = None   # remembered once a style returns results

    def _reconcile(self, queries: dict[str, Any]) -> dict[str, Any]:
        """Call the reconcile endpoint, tolerant of request-format drift.

        Tries the request styles (JSON body, legacy form-encoded), preferring a
        style that has worked before to avoid doubling requests. If the service
        responds (HTTP 200) but with no matches, returns that response (zero
        hits). If EVERY attempt fails at the HTTP level (e.g. persistent rate
        limiting after retries), raises — so the caller records an error and the
        term is retried on resume, rather than silently logging zero matches."""
        styles = ["json", "form"]
        if self._recon_style in styles:
            styles = [self._recon_style] + [s for s in styles if s != self._recon_style]

        last: dict[str, Any] = {}
        last_exc: Exception | None = None
        got_response = False
        for style in styles:
            try:
                if style == "json":
                    resp = self.request("POST", RECONCILE_URL, json={"queries": queries})
                else:
                    resp = self.request(
                        "POST", RECONCILE_URL,
                        data={"queries": json.dumps(queries, ensure_ascii=False)},
                    )
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                continue
            got_response = True
            if isinstance(data, dict):
                last = data
                if any(_result_hits(data, qid) for qid in queries):
                    self._recon_style = style   # stick with what works
                    return data
        if not got_response and last_exc is not None:
            raise last_exc   # don't disguise a service failure as "no matches"
        return last

    # ---- fetch ------------------------------------------------------------

    def fetch(self, concept_id: str) -> dict[str, Any]:
        node = self._node(concept_id)
        ancestors, facet, aat_facet = self._resolve_hierarchy(concept_id, node["broader"])
        return {
            "id": concept_id,
            "uri": f"http://vocab.getty.edu/aat/{concept_id}",
            "format": node["format"],
            "pref_labels": node["pref_labels"],
            "alt_labels": node["alt_labels"],
            "scope_note": node["scope_note"],
            "ancestors": ancestors,
            "cross_refs": node["cross_refs"],
            "facet": facet,            # internal facet label, or None
            "aat_facet": aat_facet,    # live Getty facet "<label> (<id>)", for review
        }

    # ---- internals --------------------------------------------------------

    def _node(self, concept_id: str) -> dict[str, Any]:
        """Return a COMPACT, cached node for one concept.

        Only the fields the pipeline needs are kept (labels, scope note,
        immediate broader ids, cross-refs) — not the full raw JSON-LD, which is
        large and was the main driver of cache bloat. Walking the hierarchy
        reuses these cached nodes, so repeat ancestors cost nothing."""
        key = f"node:{concept_id}"
        if self.cache and self.cache.has(key):
            return self.cache.get(key)
        doc = self._http_doc(concept_id)
        node = _concept_node(doc, concept_id)
        fmt = _detect_format(node)
        if fmt == "linked_art":
            pref, alt, scope, broader = _parse_linked_art(node)
            label = node.get("_label") or pref.get("en")
            # Getty's top-level `_label` is the English preferred descriptor. Some
            # records tag the English preferred Name with no language (it lands
            # under "und") or omit the preferred-term classification, leaving
            # pref["en"] empty — which made review/output fall back to the
            # SOURCE's English term instead of the AAT term. Backfill from _label
            # so the AAT preferred English label is always what surfaces.
            if label and not pref.get("en"):
                pref["en"] = label
        else:
            pref, alt, scope, broader = _parse_gvp(node, doc)
            label = pref.get("en")
        compact = {
            "id": concept_id,
            "format": fmt,
            "label": label,
            "pref_labels": pref,
            "alt_labels": alt,
            "scope_note": scope,
            "broader": broader,
            "cross_refs": _cross_refs(node, fmt),
        }
        if self.cache:
            # Defer the disk write; lookup flushes the cache periodically. This
            # avoids rewriting the whole cache file once per fetched concept.
            self.cache.set(key, compact, flush=False)
        return compact

    def _http_doc(self, concept_id: str) -> dict[str, Any]:
        """Fetch the raw JSON-LD for one concept (not cached; only the compact
        node derived from it is persisted)."""
        resp = self.request(
            "GET", CONCEPT_URL.format(id=concept_id),
            accept="application/ld+json, application/json",
        )
        return resp.json()

    def _resolve_hierarchy(
        self, concept_id: str, broader_ids: list[str]
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """Climb the *preferred*-parent chain to the AAT facet and resolve:

          ancestors  - [{"id","label"}] in climb order (concept's parents, up).
          facet      - internal facet label: the FACET_ROOTS hit CLOSEST TO THE
                       TOP of the chain, so a real facet root overrides a lower,
                       possibly-mislabelled guide term. None if no id matched.
          aat_facet  - the live Getty facet as "<label> (<id>)". The facet is the
                       node sitting directly under the AAT root (AAT_TOP_ID), per
                       Getty's editorial rules, so this is reliable even when that
                       facet's id is absent from FACET_ROOTS — the reviewer always
                       sees the real AAT facet, and a missing mapping is visible.

        Following the preferred (first) parent walks one lineage to the facet
        rather than fanning out across the polyhierarchy, so it reaches the top
        in ~depth fetches (cached, cycle-safe, capped at MAX_ANCESTOR_DEPTH).
        """
        ancestors: list[dict[str, Any]] = []
        visited = {concept_id, AAT_TOP_ID}
        facet: str | None = FACET_ROOTS.get(concept_id)
        facet_node: dict[str, Any] | None = None
        parents = list(broader_ids)
        steps = 0
        while parents and steps < MAX_ANCESTOR_DEPTH:
            cid = parents[0]
            if cid in visited:                      # cycle / already seen
                parents = parents[1:]               # try the next alternative parent
                continue
            visited.add(cid)
            steps += 1
            try:
                n = self._node(cid)
            except requests.RequestException:
                ancestors.append({"id": cid, "label": None})
                break                               # can't climb further; stop cleanly
            label = n.get("label")
            ancestors.append({"id": cid, "label": label})
            if cid in FACET_ROOTS:                   # topmost hit wins (we overwrite climbing up)
                facet = FACET_ROOTS[cid]
            nb = list(n.get("broader", []))
            if AAT_TOP_ID in nb or not nb:           # a node under the root is a Facet
                facet_node = {"id": cid, "label": label}
                break
            parents = nb                             # climb to this node's preferred parent

        aat_facet = None
        if facet_node:
            aat_facet = (
                f"{facet_node['label']} ({facet_node['id']})"
                if facet_node["label"] else facet_node["id"]
            )
        return ancestors, facet, aat_facet


# ---- format detection + parsers -------------------------------------------

def _detect_format(node: dict[str, Any]) -> str:
    if any(k in node for k in ("identified_by", "_label")) and "type" in node:
        return "linked_art"
    return "gvp"


def _parse_gvp(node: dict[str, Any], doc: dict[str, Any]) -> tuple[dict, dict, str | None, list[str]]:
    """Return (pref_labels{lang:str}, alt_labels{lang:[str]}, scope_note, broader_ids)."""
    pref: dict[str, str] = {}
    for entry in _as_list(node.get("skos:prefLabel") or node.get("prefLabel")):
        lang, val = _lit(entry)
        if lang and val and lang not in pref:
            pref[lang] = val

    alt: dict[str, list[str]] = {}
    for entry in _as_list(node.get("skos:altLabel") or node.get("altLabel")):
        lang, val = _lit(entry)
        if lang and val:
            alt.setdefault(lang, []).append(val)

    # Supplement from skosxl Label nodes (where Getty contributors, incl.
    # Nasjonalmuseet, place nb/nn term forms).
    for label_node in _graph_nodes(doc):
        form = label_node.get("skosxl:literalForm") or label_node.get("gvp:term")
        if form is None:
            continue
        lang, val = _lit(form)
        if lang and val and val not in alt.get(lang, []) and pref.get(lang) != val:
            alt.setdefault(lang, []).append(val)

    scope = None
    sn = node.get("skos:scopeNote") or node.get("scopeNote")
    if sn is not None:
        scope = _resolve_note(sn, doc)

    broader: list[str] = []
    for key in ("gvp:broaderPreferred", "skos:broader", "broader"):
        for b in _as_list(node.get(key)):
            cid = _clean_id(b.get("@id", "") if isinstance(b, dict) else str(b))
            if cid and cid not in broader:
                broader.append(cid)
    return pref, alt, scope, broader


def _parse_linked_art(node: dict[str, Any]) -> tuple[dict, dict, str | None, list[str]]:
    """Linked.Art shape. Languages are AAT URIs mapped via LANG_URI."""
    pref: dict[str, str] = {}
    alt: dict[str, list[str]] = {}
    # AAT classification URIs for preferred vs alternate term (suffix match).
    PREF_TYPES = {"300404670"}  # "preferred terms"
    for name in _as_list(node.get("identified_by")):
        if not isinstance(name, dict) or name.get("type") != "Name":
            continue
        content = name.get("content")
        if not content:
            continue
        lang_codes = [
            LANG_URI.get(_clean_id(l.get("id", "")))
            for l in _as_list(name.get("language"))
            if isinstance(l, dict)
        ]
        lang = next((c for c in lang_codes if c), None) or "und"
        is_pref = any(
            _clean_id(c.get("id", "")) in PREF_TYPES
            for c in _as_list(name.get("classified_as")) if isinstance(c, dict)
        )
        if is_pref and lang not in pref:
            pref[lang] = content
        else:
            alt.setdefault(lang, []).append(content)

    scope = None
    for ref in _as_list(node.get("referred_to_by")):
        if isinstance(ref, dict) and ref.get("content"):
            scope = ref["content"]
            break

    broader: list[str] = []
    for key in ("broader", "member_of"):
        for b in _as_list(node.get(key)):
            cid = _clean_id(b.get("id", "") if isinstance(b, dict) else str(b))
            if cid and cid not in broader:
                broader.append(cid)
    return pref, alt, scope, broader


def _cross_refs(node: dict[str, Any], fmt: str) -> list[dict[str, Any]]:
    """Best-effort associative / sibling relations (process<->work-type etc.).

    GVP exposes these as gvp:aatNNNN_* associative properties; Linked.Art as
    `related`. Extract target AAT IDs so the reviewer can spot a process-level
    sibling that reconciliation missed (the pilot's blind spot).
    """
    refs: list[dict[str, Any]] = []
    if fmt == "linked_art":
        for r in _as_list(node.get("related")):
            cid = _clean_id(r.get("id", "")) if isinstance(r, dict) else ""
            if cid:
                refs.append({"id": cid, "relation": "related", "label": r.get("_label")})
    else:
        for key, val in node.items():
            if key.startswith("gvp:aat") and ("_related" in key or "_distinguished" in key):
                for v in _as_list(val):
                    cid = _clean_id(v.get("@id", "")) if isinstance(v, dict) else ""
                    if cid:
                        refs.append({"id": cid, "relation": key, "label": None})
    return refs


# ---- low-level helpers ----------------------------------------------------

def _result_hits(data: dict[str, Any], qid: str) -> list[dict[str, Any]]:
    """Extract the candidate list for a query id across response-shape variants.

    Handles legacy `{qid: {"result": [...]}}`, newer `{qid: {"candidates":
    [...]}}`, and a `{"results": {qid: {...}}}` wrapper. Returns [] for a
    service-manifest response (which has neither)."""
    if not isinstance(data, dict):
        return []
    block = data.get(qid)
    if block is None and isinstance(data.get("results"), dict):
        block = data["results"].get(qid)
    if isinstance(block, dict):
        hits = block.get("result") or block.get("candidates") or []
        return hits if isinstance(hits, list) else []
    return []


def _clean_id(raw: str) -> str:
    raw = (raw or "").strip().split("#")[0]
    # Reconcile returns ids like "aat/300053271"; URIs like ".../aat/300053271";
    # JSON-LD uses the "aat:300053271" prefix; some places give a bare number.
    if raw.startswith("aat/"):
        tail = raw.split("/", 1)[1]
        return tail if tail.isdigit() else ""
    if "/aat/" in raw:
        tail = raw.rsplit("/", 1)[1]
        return tail if tail.isdigit() else ""
    if raw.startswith("aat:"):
        tail = raw.split(":", 1)[1]
        return tail if tail.isdigit() else ""
    return raw if raw.isdigit() else ""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _lit(entry: Any) -> tuple[str | None, str | None]:
    """Extract (language, value) from a JSON-LD literal in any common shape."""
    if isinstance(entry, dict):
        lang = entry.get("@language") or entry.get("language")
        val = entry.get("@value") or entry.get("value")
        return (lang.lower() if isinstance(lang, str) else None), val
    if isinstance(entry, str):
        return None, entry
    return None, None


def _graph_nodes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    g = doc.get("@graph")
    if isinstance(g, list):
        return [n for n in g if isinstance(n, dict)]
    return []


def _concept_node(doc: dict[str, Any], concept_id: str) -> dict[str, Any]:
    """Find the main concept node whether the doc is flat or a @graph."""
    nodes = _graph_nodes(doc)
    if nodes:
        for n in nodes:
            if _clean_id(n.get("@id", "") or n.get("id", "")) == concept_id:
                return n
        return nodes[0]
    return doc


def _resolve_note(sn: Any, doc: dict[str, Any]) -> str | None:
    """scopeNote may be inline (@value/rdf:value) or an @id into the graph."""
    if isinstance(sn, str):
        return sn
    if isinstance(sn, dict):
        if "@value" in sn or "rdf:value" in sn:
            v = sn.get("@value") or sn.get("rdf:value")
            return v.get("@value") if isinstance(v, dict) else v
        ref = _clean_id(sn.get("@id", ""))
        if ref or sn.get("@id"):
            target_id = sn.get("@id")
            for n in _graph_nodes(doc):
                if (n.get("@id") == target_id):
                    v = n.get("rdf:value") or n.get("skos:note") or n.get("@value")
                    _lang, val = _lit(v) if isinstance(v, dict) else (None, v)
                    return val
    return None
