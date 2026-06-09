r"""Show which AAT *hierarchies* your reconciled terms actually land in.

Why this exists
---------------
`facets.accepted` gates auto-accept, but a facet (Objects, Materials, ...) is too
coarse to *rank* candidates: many unrelated terms share one facet. The planned
`facets.preferred_hierarchies` knob (mode ``prefer``) refines candidate selection
by anchoring on AAT *sub-hierarchies* — guide-term nodes a level or two below the
facet. This tool tells you, empirically, which sub-hierarchies your own vocabulary
falls into, so you choose anchors from real data instead of guessing.

It is the offline counterpart of `verify_facets.py`: where that one resolves a few
ids against live Getty, this one reads an existing pipeline artifact and needs no
network. Every enriched candidate already carries its `ancestors` chain (the
preferred-parent climb, facet node last) and `aat_facet`, so the just-below-facet
node is read straight off the JSON.

What it reads
-------------
`02_candidates.json` (from `lookup`) or `03_classified.json` (from `classify`).
Both serialise each candidate as ``asdict(Candidate)`` including ``ancestors`` and
``aat_facet``; the classified file additionally has ``best_id``/``tier``.

What it does
------------
For each in-scope candidate it locates the facet node in the ancestor chain and
reports the node sitting ``--depth-below-facet`` levels under it (depth 1 = the top
sub-hierarchy; larger = finer). It tallies these per facet, counting how many
distinct source terms each sub-hierarchy would cover, with example terms, and can
emit a paste-ready ``preferred_hierarchies:`` block.

Scopes (``--scope``):
  * ``accepted`` (default) — only candidates whose facet is in the accepted set.
    This mirrors how `preferred_hierarchies` will be applied (facet is the gate),
    so it is the distribution that actually matters for picking anchors.
  * ``best`` — only the candidate the classifier proposed per term (classified
    file: its ``best_id``; candidates file: strongest accepted-facet, else top).
  * ``all``  — every enriched candidate (noisiest).

Usage (PowerShell, from the repo root)::

    python tools\profile_hierarchies.py 02_candidates.json
    python tools\profile_hierarchies.py 03_classified.json --scope best
    python tools\profile_hierarchies.py 02_candidates.json --profile museumvocab_reconcile\profiles\techniques.aat.yaml
    python tools\profile_hierarchies.py 02_candidates.json --depth-below-facet 2 --min-count 3
    python tools\profile_hierarchies.py 02_candidates.json --emit-yaml --min-count 3

NOTE: do NOT add ``preferred_hierarchies:`` to a live profile yet — ``FacetConfig``
does not accept the key, so it would break profile loading until that field lands.
This tool reads profiles leniently (raw YAML) precisely so it works in the meantime.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Default accepted facets (mirrors the profile `accepted` set). Overridden by
# --profile when supplied.
DEFAULT_ACCEPTED = {"techniques", "work_types", "materials", "formats", "design_motifs"}

_FACET_ID_RE = re.compile(r"\((\d+)\)\s*$")  # "Objects Facet (300264092)" -> 300264092

# Synthetic bucket keys for candidates whose just-below-facet node can't be read.
_UNRESOLVED = ("(facet unresolved — climb capped/errored)", None)
_DIRECT = ("(directly under facet — no sub-hierarchy)", None)
_NO_ANC = ("(no ancestors — candidate not enriched)", None)


def _facet_id_name(aat_facet: str | None) -> tuple[str | None, str | None]:
    """Split ``"Objects Facet (300264092)"`` into (id, name). Tolerates a bare id."""
    if not aat_facet:
        return None, None
    m = _FACET_ID_RE.search(aat_facet)
    if m:
        return m.group(1), aat_facet[: m.start()].strip()
    if aat_facet.isdigit():
        return aat_facet, None
    return None, aat_facet


def _below_facet(cand: dict, depth: int) -> tuple[str, str | None, str | None]:
    """Return (hierarchy_label, hierarchy_id, hierarchy_label_for_yaml) for the node
    sitting ``depth`` levels below the facet in this candidate's ancestor chain.

    Falls back to synthetic buckets when the chain can't place a node there.
    Ancestors are climb order (narrow->broad); the facet node is last when the
    climb resolved (``aat_facet`` is present).
    """
    ancestors: list[dict[str, Any]] = cand.get("ancestors") or []
    if not ancestors:
        return _NO_ANC[0], _NO_ANC[1], None

    ids = [a.get("id") for a in ancestors]
    facet_id, _ = _facet_id_name(cand.get("aat_facet"))

    if facet_id and facet_id in ids:
        fidx = ids.index(facet_id)
    elif cand.get("aat_facet"):
        fidx = len(ids) - 1          # resolved but id not matched: trust last-is-facet
    else:
        return _UNRESOLVED[0], _UNRESOLVED[1], None

    if fidx == 0:                     # facet is the immediate parent: nothing below it
        return _DIRECT[0], _DIRECT[1], None

    target = fidx - depth
    shallow = target < 0
    if shallow:
        target = 0                    # deepest-below-facet node this chain offers

    node = ancestors[target]
    hid = node.get("id")
    label = node.get("label") or ""
    shown = f"{label} ({hid})" if label else str(hid)
    if shallow:
        shown += f"  [shallow: only {fidx} level(s) below facet]"
    return shown, hid, (label or None)


def _load_accepted_and_anchored(profile_path: str | None) -> tuple[set[str], dict[str, str]]:
    """From a profile YAML (read leniently): the accepted facet set and any already
    declared preferred_hierarchies (id -> label). Empty/default if not supplied."""
    if not profile_path:
        return set(DEFAULT_ACCEPTED), {}
    try:
        import yaml  # pyyaml is already a project dependency (profiles are YAML)
    except ImportError:
        print("  (note: PyYAML not importable; --profile ignored)", file=sys.stderr)
        return set(DEFAULT_ACCEPTED), {}
    data = yaml.safe_load(Path(profile_path).read_text("utf-8")) or {}
    facets = data.get("facets", {}) or {}
    accepted = set(facets.get("accepted", []) or DEFAULT_ACCEPTED)
    if facets.get("accept_all"):
        accepted = set()  # sentinel: empty means "accept everything" below
    anchored = dict(facets.get("preferred_hierarchies", {}) or {})
    return accepted, {str(k): v for k, v in anchored.items()}


def _select(entry: dict, scope: str, accepted: set[str]) -> list[dict]:
    """Pick the in-scope candidates from one term entry."""
    cands: list[dict] = entry.get("candidates") or []
    if not cands:
        return []

    def is_accepted(c: dict) -> bool:
        return not accepted or (c.get("facet") in accepted)  # empty set == accept_all

    if scope == "all":
        return cands
    if scope == "accepted":
        return [c for c in cands if is_accepted(c)]
    if scope == "best":
        best_id = entry.get("best_id")
        if best_id:  # classified file
            return [c for c in cands if c.get("concept_id") == best_id]
        # candidates file: reproduce classify()'s pick — strongest accepted, else top
        ranked = sorted(cands, key=lambda c: c.get("score") or 0.0, reverse=True)
        acc = [c for c in ranked if is_accepted(c)]
        return [acc[0]] if acc else [ranked[0]]
    raise ValueError(f"unknown scope {scope!r}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input", nargs="?", default="02_candidates.json",
                    help="02_candidates.json or 03_classified.json (default: 02_candidates.json)")
    ap.add_argument("--profile", default=None,
                    help="profile YAML: read accepted facets + already-anchored hierarchies")
    ap.add_argument("--scope", choices=["accepted", "best", "all"], default="accepted",
                    help="which candidates to tally (default: accepted)")
    ap.add_argument("--depth-below-facet", type=int, default=1, metavar="N",
                    help="node N levels below the facet to tally (1=top sub-hierarchy; larger=finer)")
    ap.add_argument("--top", type=int, default=0, metavar="K",
                    help="show only the top K hierarchies per facet (0 = all)")
    ap.add_argument("--examples", type=int, default=3, metavar="M",
                    help="example source terms to show per hierarchy (default: 3)")
    ap.add_argument("--min-count", type=int, default=1, metavar="C",
                    help="hide hierarchies covering fewer than C distinct terms (default: 1)")
    ap.add_argument("--emit-yaml", action="store_true",
                    help="print a paste-ready preferred_hierarchies: block instead of the report")
    args = ap.parse_args()

    if args.depth_below_facet < 1:
        ap.error("--depth-below-facet must be >= 1")

    accepted, anchored = _load_accepted_and_anchored(args.profile)
    entries = json.loads(Path(args.input).read_text("utf-8"))

    # facet_name -> hierarchy_key -> stats
    # hierarchy_key = (shown_label, hierarchy_id, yaml_label)
    by_facet: dict[str, dict[tuple, dict[str, Any]]] = defaultdict(lambda: defaultdict(
        lambda: {"terms": set(), "cands": 0, "examples": {}}
    ))
    facet_terms: dict[str, set[str]] = defaultdict(set)
    total_terms_with_cands = 0
    skipped_errors = 0

    for entry in entries:
        if entry.get("error"):
            skipped_errors += 1
            continue
        term = entry.get("term", {})
        tid = term.get("id", "?")
        src = term.get("main_lang_term") or term.get("label") or tid
        selected = _select(entry, args.scope, accepted)
        if not selected:
            continue
        total_terms_with_cands += 1
        for c in selected:
            _, facet_name = _facet_id_name(c.get("aat_facet"))
            facet_disp = facet_name or (c.get("facet") or "(no facet)")
            shown, hid, ylabel = _below_facet(c, args.depth_below_facet)
            key = (shown, hid, ylabel)
            stats = by_facet[facet_disp][key]
            stats["terms"].add(tid)
            stats["cands"] += 1
            if tid not in stats["examples"] and len(stats["examples"]) < args.examples:
                stats["examples"][tid] = f"{src} -> {c.get('matched_label') or '?'}"
            facet_terms[facet_disp].add(tid)

    # ---- emit-yaml mode -----------------------------------------------------
    if args.emit_yaml:
        print("# Paste under `facets:` in the profile (after the FacetConfig field exists).")
        print("# Derived from", args.input, f"(scope={args.scope}, depth={args.depth_below_facet}).")
        print("# Real sub-hierarchy nodes only; synthetic buckets and anchorless rows omitted.")
        print("preferred_hierarchies:")
        any_row = False
        for facet_disp in sorted(by_facet, key=lambda f: -len(facet_terms[f])):
            rows = [
                (k, s) for k, s in by_facet[facet_disp].items()
                if k[1] and len(s["terms"]) >= args.min_count   # k[1] is hierarchy id
            ]
            rows.sort(key=lambda kv: -len(kv[1]["terms"]))
            if not rows:
                continue
            print(f"  # --- {facet_disp} ---")
            for (shown, hid, ylabel), s in rows:
                mark = "  # already anchored" if hid in anchored else ""
                label = (ylabel or "").replace('"', "'")
                print(f'  "{hid}": "{label}"   # {len(s["terms"])} terms{mark}')
                any_row = True
        if not any_row:
            print("  {}  # nothing met --min-count; lower it or widen --scope")
        return 0

    # ---- report mode --------------------------------------------------------
    print(f"input={args.input}  scope={args.scope}  depth_below_facet={args.depth_below_facet}")
    print(f"terms with in-scope candidates: {total_terms_with_cands}"
          + (f"   (skipped {skipped_errors} errored term(s))" if skipped_errors else ""))
    if args.profile:
        print(f"accepted facets from profile: {sorted(accepted) or 'ALL (accept_all)'}"
              + (f"   already-anchored hierarchies: {len(anchored)}" if anchored else ""))
    print("=" * 100)

    for facet_disp in sorted(by_facet, key=lambda f: -len(facet_terms[f])):
        denom = len(facet_terms[facet_disp]) or 1
        print(f"\n{facet_disp}   ({len(facet_terms[facet_disp])} distinct terms)")
        print(f"  {'terms':>6} {'%':>5} {'cands':>6}  sub-hierarchy (depth "
              f"{args.depth_below_facet} below facet)")
        print("  " + "-" * 96)
        rows = sorted(by_facet[facet_disp].items(), key=lambda kv: -len(kv[1]["terms"]))
        shown_rows = 0
        for (shown, hid, _ylabel), s in rows:
            nterms = len(s["terms"])
            if nterms < args.min_count:
                continue
            if args.top and shown_rows >= args.top:
                remaining = sum(1 for _, ss in rows[shown_rows:] if len(ss["terms"]) >= args.min_count)
                if remaining:
                    print(f"  … {remaining} more sub-hierarchies (raise --top to see)")
                break
            mark = "  *anchored*" if hid and hid in anchored else ""
            pct = 100.0 * nterms / denom
            print(f"  {nterms:>6} {pct:>4.0f}% {s['cands']:>6}  {shown}{mark}")
            for ex in s["examples"].values():
                print(f"           · {ex}")
            shown_rows += 1
    print("\n" + "=" * 100)
    print("Pick anchor ids from the high-coverage rows, confirm them with "
          "`python tools/verify_facets.py <id> ...`,")
    print("then re-run with --emit-yaml to get a paste-ready preferred_hierarchies block.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
