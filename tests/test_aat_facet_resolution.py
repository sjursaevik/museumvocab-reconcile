"""Facet-resolution tests for the preferred-parent climb.

The climb walks one lineage to the AAT facet (the node directly under the AAT
root). The FACET_ROOTS hit closest to the top wins, so a correct facet root
overrides a lower, possibly-mislabelled guide term. Nodes are injected, so no
network is touched.
"""
from __future__ import annotations

from museumvocab_reconcile.adapters.aat import AAT_TOP_ID, AatAdapter


class _GraphAdapter(AatAdapter):
    """Resolve hierarchy against an in-memory {id: {label, broader}} graph."""

    def __init__(self, nodes):
        super().__init__(cache=None)
        self._nodes = nodes

    def _node(self, concept_id):
        return self._nodes[concept_id]


def _n(label, broader):
    return {"label": label, "broader": list(broader)}


def test_climb_reaches_objects_facet_root():
    nodes = {
        "300999002": _n("intermediate", ["300264092"]),
        "300264092": _n("Objects Facet", [AAT_TOP_ID]),
    }
    ancestors, facet, aat_facet = _GraphAdapter(nodes)._resolve_hierarchy(
        "300999001", ["300999002"]
    )
    assert facet == "work_types"
    assert aat_facet == "Objects Facet (300264092)"
    assert [a["id"] for a in ancestors] == ["300999002", "300264092"]


def test_topmost_facet_root_wins_over_lower_one():
    # Lower chain node maps to `techniques`; the higher facet root maps to
    # `work_types`. The node closest to the top must win.
    nodes = {
        "300054216": _n("painting techniques", ["300264092"]),  # FACET_ROOTS -> techniques
        "300264092": _n("Objects Facet", [AAT_TOP_ID]),         # FACET_ROOTS -> work_types
    }
    _ancestors, facet, _aat = _GraphAdapter(nodes)._resolve_hierarchy(
        "300999001", ["300054216"]
    )
    assert facet == "work_types"


def test_aat_facet_is_node_directly_under_root_even_without_mapping():
    # An unmapped facet id still yields aat_facet (node sitting under the root),
    # so a missing FACET_ROOTS entry is visible to the reviewer, not silent.
    nodes = {
        "300888888": _n("Some Facet", [AAT_TOP_ID]),
    }
    _ancestors, facet, aat_facet = _GraphAdapter(nodes)._resolve_hierarchy(
        "300999001", ["300888888"]
    )
    assert facet is None
    assert aat_facet == "Some Facet (300888888)"


def test_cycle_is_handled_without_looping():
    # A node pointing back at its child must not loop forever.
    nodes = {
        "300999001": _n("leaf", ["300999002"]),
        "300999002": _n("loop", ["300999001"]),
    }
    ancestors, facet, _aat = _GraphAdapter(nodes)._resolve_hierarchy(
        "300999001", ["300999002"]
    )
    # 300999001 is the start (already visited); only 300999002 is climbed.
    assert [a["id"] for a in ancestors] == ["300999002"]
    assert facet is None
