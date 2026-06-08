"""Profile loading and validation.

A *profile* is a declarative YAML file describing how to reconcile one source
vocabulary against one authority. Onboarding a new vocabulary should mean
writing a profile, never editing engine code.

All tunable behaviour lives here, including the auto-accept thresholds and the
two overrides added in review:

  facets.accept_all            (bool, default False)
      If True, any facet the authority returns is treated as accepted and the
      explicit ``facets.accepted`` list is ignored.

  thresholds.auto_accept.mode  ("full" | "exact_only" | "off", default "full")
      full        - trusted-language exact match OR score/gap thresholds (with
                    an accepted facet) auto-accept.
      exact_only  - ONLY a trusted-language exact match (with an accepted facet)
                    auto-accepts; score-based acceptance is disabled.
      off         - nothing auto-accepts; every term goes to review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency hint
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc


VALID_AUTO_ACCEPT_MODES = {"full", "exact_only", "off"}


@dataclass
class LanguageConfig:
    source: str = "nb"
    target: str = "en"
    # Query order keyed by "leaf" / "root"; falls back to [source, target].
    query_order_by_depth: dict[str, list[str]] = field(
        default_factory=lambda: {"leaf": ["nb", "en"], "root": ["en", "nb"]}
    )
    # Languages in which an exact label match is trusted enough to auto-accept.
    # Empty list (e.g. for Iconclass, which has no Norwegian) disables the
    # trusted-exact-match signal for that profile.
    trusted_exact_match_langs: list[str] = field(default_factory=list)
    # Variant language codes -> canonical code, so a source export tagged "NO"
    # is recognised as nb. Tolerates schema drift in level language codes.
    aliases: dict[str, str] = field(
        default_factory=lambda: {"no": "nb", "nob": "nb", "nor": "nb", "nno": "nn"}
    )

    def canonical(self, code: str) -> str:
        c = (code or "").lower()
        return self.aliases.get(c, c)


@dataclass
class FacetConfig:
    preferred: str | None = None       # influences tiering/ranking, never hard-filters
    accept_all: bool = False           # accept any returned facet (ignores `accepted`)
    accepted: list[str] = field(default_factory=list)
    # facet -> {"target": "production"|"object"|..., "prop": "classified_as"|"made_of"|...}
    linked_art_property: dict[str, dict[str, str]] = field(default_factory=dict)

    def is_accepted(self, facet: str | None) -> bool:
        if self.accept_all:
            return True
        if facet is None:
            return False
        return facet in self.accepted


@dataclass
class AutoAcceptConfig:
    mode: str = "full"
    min_score: float = 25.0
    min_score_gap: float = 5.0
    trusted_lang_exact_match: bool = True

    def __post_init__(self) -> None:
        if self.mode not in VALID_AUTO_ACCEPT_MODES:
            raise ValueError(
                f"auto_accept.mode must be one of {sorted(VALID_AUTO_ACCEPT_MODES)}, "
                f"got {self.mode!r}"
            )


@dataclass
class ReviewIfConfig:
    cross_facet_ambiguity: bool = True
    broader_only: bool = True


@dataclass
class ThresholdConfig:
    auto_accept: AutoAcceptConfig = field(default_factory=AutoAcceptConfig)
    review_if: ReviewIfConfig = field(default_factory=ReviewIfConfig)


@dataclass
class SourceSchemaConfig:
    id_field: str = "ID"
    status_field: str = "Status"
    include_status: list[str] = field(default_factory=lambda: ["Gyldig"])
    logical_name_field: str = "logicalName"
    label_field: str = "label"
    # {n} = level index, {lang} = language code; tolerant of extra levels / codes.
    level_pattern: str = "Level_{n}_{lang}"
    dedupe_by: str = "ID"


@dataclass
class LookupConfig:
    # Candidates requested per reconciliation query.
    result_limit: int = 5
    # Enrich at most this many candidates per term (highest score first). Each
    # enrichment fetches the concept and walks its hierarchy, so this is the
    # main lever on lookup speed and cache size.
    enrich_top_n: int = 5
    # Drop candidates scoring below this before enriching (0 = keep all).
    min_candidate_score: float = 0.0


@dataclass
class ReviewConfig:
    # Also include auto-accepted terms in the review CSV (for spot-checking).
    include_auto_accepted: bool = False


@dataclass
class TranslationConfig:
    """Optional step 1b: LLM-recommended English for terms missing one."""
    provider: str = "anthropic"        # seam for future providers
    model: str = "claude-sonnet-4-6"   # pinned in profile; change as needed
    context: str = ""                  # domain description grounding the prompt
    batch_size: int = 15               # terms per API call (cost control)
    max_tokens: int = 4000
    temperature: float = 0.0
    include_siblings: bool = True
    max_siblings: int = 6
    prompt_version: str = "v2"         # bump to invalidate the translation cache
    # Optional: top-level (root) parent term -> domain phrase for the prompt, so
    # a term under "Arkitektonisk" is read as architecture, "Billedkunst" as
    # visual arts, etc. Unmapped roots are passed through verbatim.
    domain_by_root: dict[str, str] = field(default_factory=dict)


@dataclass
class Profile:
    profile: str
    authority: str
    languages: LanguageConfig
    facets: FacetConfig
    thresholds: ThresholdConfig
    source_schema: SourceSchemaConfig
    lookup: LookupConfig
    review: ReviewConfig
    translation: TranslationConfig

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        return cls(
            profile=data["profile"],
            authority=data["authority"],
            languages=LanguageConfig(**data.get("languages", {})),
            facets=FacetConfig(**data.get("facets", {})),
            thresholds=_build_thresholds(data.get("thresholds", {})),
            source_schema=SourceSchemaConfig(**data.get("source_schema", {})),
            lookup=LookupConfig(**data.get("lookup", {})),
            review=ReviewConfig(**data.get("review", {})),
            translation=TranslationConfig(**data.get("translation", {})),
        )

    def validate(self) -> list[str]:
        """Return a list of warnings (empty == clean). Hard errors raise."""
        warnings: list[str] = []
        if not self.facets.accept_all and not self.facets.accepted:
            warnings.append(
                "facets.accept_all is False but facets.accepted is empty: "
                "no facet will ever be accepted, so every term will go to review."
            )
        if (
            self.thresholds.auto_accept.mode == "exact_only"
            and not self.languages.trusted_exact_match_langs
        ):
            warnings.append(
                "auto_accept.mode is 'exact_only' but trusted_exact_match_langs is "
                "empty: nothing can auto-accept, equivalent to mode 'off'."
            )
        return warnings


def _build_thresholds(data: dict[str, Any]) -> ThresholdConfig:
    return ThresholdConfig(
        auto_accept=AutoAcceptConfig(**data.get("auto_accept", {})),
        review_if=ReviewIfConfig(**data.get("review_if", {})),
    )
