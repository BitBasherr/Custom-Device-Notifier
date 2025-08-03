from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition

# The callable type returned by async_from_config().
ConditionChecker = (
    condition.ConditionCheckerType
    if hasattr(condition, "ConditionCheckerType")
    else Any  # Fallback for very old HA versions
)


async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    """Validate one Home-Assistant *condition* block and return True/False."""
    # 1. Build the checker from the raw config.
    checker: ConditionChecker = await condition.async_from_config(hass, cfg)

    # 2. Run the checker (no template vars needed for plain conditions).
    #    async_from_config() returns `bool | None`, coerce to strict bool.
    return bool(checker(hass, {}))