from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_policy_fingerprint(contract: Any) -> str:
    """Hash a finite mapping using one canonical JSON representation."""
    if not isinstance(contract, dict):
        raise ValueError("policy contract must be an object")
    try:
        canonical = json.dumps(
            contract,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("policy contract must be finite canonical JSON") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_sha256_fingerprint(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(
        len(normalized) == 64
        and all(character in "0123456789abcdef" for character in normalized)
    )
