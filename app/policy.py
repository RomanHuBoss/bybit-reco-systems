from __future__ import annotations

import hashlib
import json
from typing import Any


CALIBRATION_LABEL_GRACE_SEC = 120


def _exact_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def policy_label_due_ts(
    recommendation_ts: Any,
    horizon_sec: Any,
    *,
    grace_sec: Any = CALIBRATION_LABEL_GRACE_SEC,
) -> int | None:
    """Return the conservative timestamp at which an outcome may enter training.

    The same helper is used by recommendation persistence, outcome labeling,
    lineage validation and startup repair.  This avoids a split contract where
    the worker can persist a label before the policy says that label is usable.
    """
    ts = _exact_positive_int(recommendation_ts)
    horizon = _exact_positive_int(horizon_sec)
    grace = _exact_positive_int(grace_sec)
    if ts is None or horizon is None or grace is None:
        return None
    return int(ts + horizon + grace)


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
