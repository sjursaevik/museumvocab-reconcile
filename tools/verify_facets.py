"""Verify the AAT id -> facet mappings in adapters/aat.py against live Getty.

Why this exists
---------------
`FACET_ROOTS` is hand-maintained and a few entries had drifted from the live
records (e.g. 300264092 was labelled "materials" but is the Objects facet, and
300015646 was labelled a design-element but is the Styles & Periods hierarchy).
The Claude sandbox cannot reach vocab.getty.edu (ROBOTS_DISALLOWED), so this
check is meant to be run on YOUR machine, where Getty is reachable.

What it does
------------
For every id currently in FACET_ROOTS (and any extra ids you pass), it fetches
the concept, prints its live ``_label`` and walks the *full* broader chain to the
top to report the actual facet root. It then flags:
  * a wrong facet  — top-of-chain facet doesn't match the mapped internal label;
  * a non-root id  — the id is below the facet (a hierarchy/guide/concept), which
                     is allowed as a "catch" but is worth knowing;
  * an out-of-set facet — the term sits in a facet that's not in the accepted set.

It also resolves the candidate facet-root ids (Materials / Physical Attributes /
Associated Concepts) so you can paste the confirmed ones into FACET_ROOTS.

Usage (PowerShell, from the repo root)::

    python tools\verify_facets.py
    python tools\verify_facets.py 300264091 300264087 300264086   # check candidates
    python tools\verify_facets.py --no-cache                      # bypass the disk cache

Note: this walks the broader chain WITHOUT the MAX_ANCESTOR_DEPTH cap that the
adapter uses, so it always reaches the true facet even in deep polyhierarchies.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script (python tools/verify_facets.py) without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from museumvocab_reconcile.adapters.aat import FACET_ROOTS, AatAdapter  # noqa: E402
from museumvocab_reconcile.cache import JsonCache  # noqa: E402

# Internal facet labels that may auto-accept (mirrors the profile `accepted` set).
ACCEPTED = {"techniques", "work_types", "materials", "formats", "design_motifs"}

# Map an AAT facet *label* (as it appears at the top of the broader chain) to the
# internal facet label the pipeline uses. Confirm/extend the left-hand strings
# against what this script prints for known roots.
AAT_FACET_TO_INTERNAL = {
    "Activities Facet": "techniques",
    "Objects Facet": "work_types",
    "Materials Facet": "materials",
    "Physical Attributes Facet": "formats",
    "Associated Concepts Facet": "design_motifs",
    # Facets NOT in the accepted set (presence => the mapping is suspect):
    "Styles and Periods Facet": None,
    "Agents Facet": None,
    "Brand Names Facet": None,
}


def full_broader_walk(adapter: AatAdapter, concept_id: str) -> list[dict]:
    """Walk broader links to the very top, returning [self, ...ancestors],
    each as {"id", "label"}. No depth cap; cycle-safe."""
    chain: list[dict] = []
    seen: set[str] = set()
    frontier = [concept_id]
    while frontier:
        cid = frontier.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        try:
            node = adapter._node(cid)
        except Exception as exc:  # network/parse error — report, keep going
            chain.append({"id": cid, "label": f"<error: {exc}>"})
            continue
        chain.append({"id": cid, "label": node.get("label")})
        # Prefer a single primary parent for a clean "top", but keep all so a
        # polyhierarchy still resolves a facet.
        frontier = list(node.get("broader", [])) + frontier
    return chain


def facet_root_of(chain: list[dict]) -> dict | None:
    """The last node in the walk is the highest ancestor reached = the facet."""
    return chain[-1] if chain else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ids", nargs="*", help="extra AAT ids to resolve (beyond FACET_ROOTS)")
    ap.add_argument("--no-cache", action="store_true", help="bypass the on-disk node cache")
    ap.add_argument("--cache", default=".cache/aat_nodes.json", help="cache file path")
    ap.add_argument("--request-delay", type=float, default=0.2,
                    help="seconds between requests (be polite to Getty)")
    args = ap.parse_args()

    cache = None if args.no_cache else JsonCache(args.cache)
    adapter = AatAdapter(cache=cache, request_delay=args.request_delay)

    ids = list(FACET_ROOTS) + [i for i in args.ids if i not in FACET_ROOTS]
    problems = 0

    print(f"{'id':<12} {'mapped':<13} {'live _label':<34} {'-> facet root':<28} flags")
    print("-" * 110)
    for cid in ids:
        mapped = FACET_ROOTS.get(cid, "—")
        chain = full_broader_walk(adapter, cid)
        self_label = chain[0]["label"] if chain else "<none>"
        root = facet_root_of(chain)
        root_label = root["label"] if root else "<none>"

        flags = []
        is_root = len(chain) <= 1  # nothing broader => this id *is* a facet/top
        if not is_root:
            flags.append("not-a-root(catch)")

        expected_internal = AAT_FACET_TO_INTERNAL.get(root_label or "", "??")
        if expected_internal is None:
            flags.append(f"facet '{root_label}' NOT in accepted set")
            problems += 1
        elif expected_internal == "??":
            flags.append(f"unknown facet '{root_label}' (add to AAT_FACET_TO_INTERNAL)")
            problems += 1
        elif mapped not in ("—",) and mapped != expected_internal:
            flags.append(f"MISMATCH: mapped={mapped} but facet={expected_internal}")
            problems += 1

        if mapped != "—" and mapped not in ACCEPTED:
            flags.append(f"mapped facet '{mapped}' not in accepted set")
            problems += 1

        print(f"{cid:<12} {mapped:<13} {str(self_label)[:33]:<34} "
              f"{str(root_label)[:27]:<28} {'; '.join(flags)}")

    print("-" * 110)
    if problems:
        print(f"{problems} issue(s) found. Fix FACET_ROOTS in adapters/aat.py and re-run.")
        return 1
    print("All mappings consistent with live Getty facet roots.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
