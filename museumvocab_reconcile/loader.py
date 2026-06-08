"""Prep stage: load and normalise a MuseumPlus-style vocabulary export.

Tolerant of schema drift per the profile's ``source_schema``:
  * level language codes other than NO/EN (e.g. nb/nn/en/de),
  * an arbitrary number of levels (not just 0-3),
  * different field names for ID / Status / label / logicalName.

Main-term rule (unchanged from the original pipeline): levels are ordered
broad -> specific by ascending index, and the *highest non-empty* level is the
main term. Lower non-empty levels are its parents (broader terms).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import Profile
from .model import SourceTerm


def _level_regex(pattern: str) -> re.Pattern[str]:
    # Turn "Level_{n}_{lang}" into a regex capturing index and language code.
    esc = re.escape(pattern)
    esc = esc.replace(re.escape("{n}"), r"(?P<n>\d+)")
    esc = esc.replace(re.escape("{lang}"), r"(?P<lang>[A-Za-z]{2,3})")
    return re.compile(f"^{esc}$", re.IGNORECASE)


def _normalise_label(value: str) -> str:
    """Strip CSV-quoting artifacts like 'Kollodium ""våtplate"", negativ'."""
    if not value:
        return ""
    v = value.strip()
    if v.startswith('"') and v.endswith('"') and len(v) >= 2:
        v = v[1:-1]
    v = v.replace('""', '"').strip()
    # MuseumPlus exports a literal "NULL" for empty cells; treat as empty so it
    # never becomes a term, parent, or LLM context.
    if v.casefold() == "null":
        return ""
    return v


def _value_at_level(
    row: dict[str, Any], levels: dict[int, dict[str, list[str]]], idx: int, lang: str
) -> str:
    """First non-empty normalised value at a specific level for one language."""
    for fieldname in levels.get(idx, {}).get(lang, []):
        val = _normalise_label(str(row.get(fieldname, "")))
        if val:
            return val
    return ""


def discover_levels(rows: list[dict[str, Any]], profile: Profile) -> dict[int, dict[str, list[str]]]:
    """Map level index -> {canonical_lang: [field_name, ...]} from the keys present.

    Language codes are canonicalised via the profile's alias map, so e.g.
    Level_0_NO and Level_0_NB both register under "nb".
    """
    rx = _level_regex(profile.source_schema.level_pattern)
    levels: dict[int, dict[str, list[str]]] = {}
    keys = set()
    for r in rows:
        keys.update(r.keys())
    for key in sorted(keys):
        m = rx.match(key)
        if m:
            idx = int(m.group("n"))
            lang = profile.languages.canonical(m.group("lang"))
            fields = levels.setdefault(idx, {}).setdefault(lang, [])
            if key not in fields:
                fields.append(key)
    return levels


def _main_and_parents(
    row: dict[str, Any], levels: dict[int, dict[str, list[str]]], lang: str
) -> tuple[str, int, list[str]]:
    """Return (main_term, main_level, parents) for one canonical language."""
    main_term, main_level = "", -1
    values: dict[int, str] = {}
    for idx in sorted(levels):
        val = ""
        for field in levels[idx].get(lang, []):
            val = _normalise_label(str(row.get(field, "")))
            if val:
                break
        values[idx] = val
        if val:
            main_term, main_level = val, idx
    parents = [values[i] for i in sorted(levels) if i < main_level and values.get(i)]
    return main_term, main_level, parents


def load_source(path: str | Path, profile: Profile) -> list[SourceTerm]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Expected the source vocabulary to be a JSON array of rows.")

    schema = profile.source_schema
    levels = discover_levels(raw, profile)
    if not levels:
        raise ValueError(
            f"No level fields matched pattern {schema.level_pattern!r}. "
            "Check source_schema.level_pattern in the profile."
        )

    # Per-row, case-insensitive field access, so a profile saying "logicalName"
    # finds a "logicalname" column even if casing varies between rows.
    def cig(row: dict[str, Any], name: str, default: Any = None) -> Any:
        if name in row:
            return row[name]
        low = name.lower()
        for k, v in row.items():
            if k.lower() == low:
                return v
        return default

    src, tgt = profile.languages.source, profile.languages.target
    seen: set[str] = set()
    out: list[SourceTerm] = []

    for row in raw:
        status = str(cig(row, schema.status_field, "")).strip()
        if schema.include_status and status not in schema.include_status:
            continue
        tid = str(cig(row, schema.id_field, "")).strip()
        if not tid:
            continue
        dedupe_key = str(cig(row, schema.dedupe_by, tid))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        main_src, lvl_src, parents_src = _main_and_parents(row, levels, src)
        # Target term at the SAME level as the source main term — so a term
        # whose specific level lacks English is correctly "missing English",
        # even when a higher level carries an English label (that becomes a
        # target-language parent, not the main term).
        if lvl_src >= 0:
            main_tgt = _value_at_level(row, levels, lvl_src, tgt)
            parents_tgt = [
                v for i in sorted(levels) if i < lvl_src
                for v in (_value_at_level(row, levels, i, tgt),) if v
            ]
        else:
            main_tgt, parents_tgt = "", []

        ln = cig(row, schema.logical_name_field)
        out.append(
            SourceTerm(
                id=tid,
                status=status,
                logical_name=ln if (ln is None or isinstance(ln, str)) else str(ln),
                label=_normalise_label(str(cig(row, schema.label_field, ""))) or None,
                main_lang_term=main_src,
                main_target_term=main_tgt,
                main_level=lvl_src if lvl_src >= 0 else 0,
                parents_source=parents_src,
                parents_target=parents_tgt,
                raw=row,
            )
        )
    return out
