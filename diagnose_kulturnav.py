"""Quick standalone check of the KulturNav read API from YOUR machine.

Run:  python diagnose_kulturnav.py [label] [dataset-uuid]
e.g.  python diagnose_kulturnav.py akvarell 64ac501d-7595-45fb-b1d9-07b74660b824

The sandbox can't reach kulturnav.org (bot detection), and the JSON-LD record
shapes weren't pinnable from there, so the adapter's parser is written defensively
and VERIFIED HERE. This prints:

  1. a scoped Core API search response (the envelope `search()` reads), and
  2. one record fetched two ways — the per-UUID JSON-LD (`fetch()`'s source) and
     the Core API single-record view — so you can confirm:
       * prefLabel/altLabel language tags use `no` (Bokmaal) / `nn` / `en`
       * broader / concept.category reference shape (ENTITY_REFERENCE vs @id)
       * how exactMatch / sameAs serialise (the AAT/Wikidata crosswalk)

If anything differs from the maps in adapters/kulturnav.py, adjust the small
predicate/lang tables there (and add a regression case to tests/test_kulturnav.py).
"""
import json
import sys

import requests

BASE = "https://kulturnav.org"
UA = "museumvocab-reconcile/0.1 (Nasjonalmuseet; vocabulary reconciliation)"

label = sys.argv[1] if len(sys.argv) > 1 else "akvarell"
dataset = sys.argv[2] if len(sys.argv) > 2 else "64ac501d-7595-45fb-b1d9-07b74660b824"


def show(title, method, url, **kwargs):
    print(f"\n===== {title} =====\n{url}")
    try:
        r = requests.request(method, url, timeout=30,
                             headers={"User-Agent": UA, **kwargs.pop("headers", {})}, **kwargs)
        print("status:", r.status_code, "| content-type:", r.headers.get("content-type"))
        try:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:2500])
        except ValueError:
            print(r.text[:2000])
        return r
    except Exception as exc:  # noqa: BLE001
        print("error:", repr(exc))
        return None


# 1. Scoped Core API search (Concept + dataset + entity.name).
expr = f"entityType:Concept,entity.dataset:{dataset},entity.name:{label}"
sr = show("Core search (scoped)", "GET", f"{BASE}/api/core/{expr}/0/5",
          params={"lang": "no"}, headers={"Accept": "application/json"})

# Pull the first uuid to fetch a full record.
uuid = None
try:
    ents = (sr.json() or {}).get("entities") if sr else None
    if ents:
        first = ents[0]
        uuid = (first.get("uuid") or "").rstrip("/").rsplit("/", 1)[-1]
except Exception:  # noqa: BLE001
    pass

if uuid:
    # 2a. Per-UUID JSON-LD — the shape fetch() parses.
    show("Record JSON-LD", "GET", f"{BASE}/{uuid}",
         headers={"Accept": "application/ld+json"})
    # 2b. Core API single-record with the related/matching properties inlined.
    props = ("entity.name,entity.alternativeName,concept.scopeNote,concept.definition,"
             "concept.category,concept.broader,concept.exactMatch,concept.closeMatch,"
             "entity.sameAs,related,caption")
    show("Record Core API", "GET", f"{BASE}/api/core/{uuid}",
         params={"properties": props, "displayValues": "true", "labels": "true"},
         headers={"Accept": "application/json"})
else:
    print("\n(no uuid found in search results — try a different label/dataset)")

print(
    "\nWhat to confirm: language tags are `no`/`nn`/`en` (Bokmaal=`no`); broader and "
    "concept.category are followable to a UUID; exactMatch/sameAs are absolute URIs "
    "(vocab.getty.edu/aat/<id>, wikidata.org/wiki/<Q>). Mismatch -> edit the maps in "
    "adapters/kulturnav.py."
)
