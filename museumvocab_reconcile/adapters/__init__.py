"""Adapter registry: profile.authority -> adapter class."""
from __future__ import annotations

from .aat import AatAdapter
from .base import AuthorityAdapter
from .iconclass import IconclassAdapter

REGISTRY: dict[str, type[AuthorityAdapter]] = {
    AatAdapter.name: AatAdapter,
    IconclassAdapter.name: IconclassAdapter,
}


def get_adapter(name: str, **kwargs) -> AuthorityAdapter:
    try:
        return REGISTRY[name](**kwargs)
    except KeyError:
        raise ValueError(
            f"Unknown authority {name!r}. Registered: {sorted(REGISTRY)}"
        ) from None
