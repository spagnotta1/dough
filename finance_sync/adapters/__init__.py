"""Adapter registry.

Adding a new institution requires exactly one thing: subclass
:class:`~finance_sync.adapters.base.FinancialInstitutionAdapter` and decorate
it with :func:`register_adapter`. The sync engine, routes, and UI discover it
automatically — no synchronization code changes.
"""

from __future__ import annotations

from typing import Dict, List, Type, TYPE_CHECKING

from ..exceptions import UnsupportedInstitutionError

if TYPE_CHECKING:  # pragma: no cover
    from .base import FinancialInstitutionAdapter

ADAPTER_REGISTRY: Dict[str, Type["FinancialInstitutionAdapter"]] = {}


def register_adapter(cls: Type["FinancialInstitutionAdapter"]) -> Type["FinancialInstitutionAdapter"]:
    """Class decorator: register an adapter under its institution slug."""
    if not getattr(cls, "institution", None):
        raise ValueError(f"{cls.__name__} must define an 'institution' slug")
    ADAPTER_REGISTRY[cls.institution] = cls
    return cls


def get_adapter_class(institution: str) -> Type["FinancialInstitutionAdapter"]:
    """Look up the adapter class for an institution slug."""
    try:
        return ADAPTER_REGISTRY[institution]
    except KeyError:
        raise UnsupportedInstitutionError(
            f"No adapter registered for institution {institution!r}"
        ) from None


def available_institutions() -> List[Type["FinancialInstitutionAdapter"]]:
    """Adapter classes offered in the connect catalog, sorted by display name.

    Adapters with ``offered = False`` are withheld from the picker but stay in
    the registry, so :func:`get_adapter_class` still resolves them for existing
    connections. Use ``ADAPTER_REGISTRY`` directly to see everything.
    """
    return sorted((c for c in ADAPTER_REGISTRY.values() if c.offered),
                  key=lambda c: c.display_name)


# Import concrete adapters so they self-register on package import.
from . import coinbase, plaid_adapter  # noqa: E402,F401
