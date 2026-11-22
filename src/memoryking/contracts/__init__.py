"""Versioned wire contracts with strict validation and canonical serialization."""

from typing import TYPE_CHECKING, Any

from .canonical import ContractError, canonical_bytes, canonical_hash, load_strict

if TYPE_CHECKING:
    from .validation import validate_contract

__all__ = ["ContractError", "canonical_bytes", "canonical_hash", "load_strict", "validate_contract"]


def __getattr__(name: str) -> Any:
    # Validation loads jsonschema and every schema; a caller that only serializes (a hook,
    # the relay) should not pay for that at import.
    if name == "validate_contract":
        from .validation import validate_contract

        return validate_contract
    raise AttributeError(name)
