"""YAML 1.1 turns bare off/on/no/yes into booleans, so `hierarchy_mode: off`
and `auto_accept.mode: off` reach config as Python False and used to fail enum
validation with a confusing 'got False'. Both must accept the bare word; truly
invalid values must still raise a clear, listing error.

Loads YAML the same way Profile.load does, so it exercises the real coercion
path. Offline.
"""
from __future__ import annotations

import pytest
import yaml

from museumvocab_reconcile.config import AutoAcceptConfig, FacetConfig, Profile

_BASE = """
profile: t
authority: aat
languages: {source: nb, target: en, trusted_exact_match_langs: [nb, nn]}
facets:
  accepted: [materials]
  hierarchy_mode: __HMODE__
thresholds:
  auto_accept: {mode: __AMODE__}
source_schema: {id_field: ID}
"""


def _load(tmp_path, hmode="prefer", amode="full"):
    p = tmp_path / "p.yaml"
    text = _BASE.replace("__HMODE__", hmode).replace("__AMODE__", amode)
    p.write_text(text, encoding="utf-8")
    return Profile.load(p)


def test_bare_off_parses_as_boolean_false():
    # documents *why* this fix exists
    assert yaml.safe_load("x: off")["x"] is False


def test_hierarchy_mode_off_loads(tmp_path):
    prof = _load(tmp_path, hmode="off")
    assert prof.facets.hierarchy_mode == "off"


def test_auto_accept_mode_off_loads(tmp_path):
    prof = _load(tmp_path, amode="off")
    assert prof.thresholds.auto_accept.mode == "off"


def test_both_off_together_loads(tmp_path):
    prof = _load(tmp_path, hmode="off", amode="off")
    assert prof.facets.hierarchy_mode == "off"
    assert prof.thresholds.auto_accept.mode == "off"


def test_quoted_off_still_loads(tmp_path):
    prof = _load(tmp_path, hmode="'off'")
    assert prof.facets.hierarchy_mode == "off"


def test_normal_values_unaffected(tmp_path):
    prof = _load(tmp_path, hmode="prefer", amode="exact_only")
    assert prof.facets.hierarchy_mode == "prefer"
    assert prof.thresholds.auto_accept.mode == "exact_only"


def test_truthy_yaml_bool_still_errors_clearly():
    # `hierarchy_mode: on` -> True is not a real mode; error must list options
    with pytest.raises(ValueError, match="hierarchy_mode must be one of"):
        FacetConfig(accepted=["materials"], hierarchy_mode=True)
    with pytest.raises(ValueError, match="auto_accept.mode must be one of"):
        AutoAcceptConfig(mode=True)


def test_genuinely_invalid_mode_still_errors():
    with pytest.raises(ValueError, match="hierarchy_mode must be one of"):
        FacetConfig(accepted=["materials"], hierarchy_mode="sometimes")
