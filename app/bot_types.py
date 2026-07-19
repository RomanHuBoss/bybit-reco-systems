from __future__ import annotations

SUPPORTED_BOT_TYPES: tuple[str, ...] = (
    "futures_grid",
    "directional_trend",
)

LINEAR_BOT_TYPES: tuple[str, ...] = SUPPORTED_BOT_TYPES
GRID_BOT_TYPES: tuple[str, ...] = ("futures_grid",)
DIRECTIONAL_BOT_TYPES: tuple[str, ...] = ("directional_trend",)
SINGLE_POSITION_BOT_TYPES: tuple[str, ...] = ("directional_trend",)
SHADOW_ONLY_BOT_TYPES: tuple[str, ...] = ()


def is_supported_bot_type(bot_type: str | None) -> bool:
    return str(bot_type or "") in SUPPORTED_BOT_TYPES


def is_shadow_only_bot_type(bot_type: str | None) -> bool:
    return str(bot_type or "") in SHADOW_ONLY_BOT_TYPES


def sql_in_clause(column: str = "bot_type") -> tuple[str, list[str]]:
    placeholders = ",".join("?" for _ in SUPPORTED_BOT_TYPES)
    return f"{column} IN ({placeholders})", list(SUPPORTED_BOT_TYPES)
