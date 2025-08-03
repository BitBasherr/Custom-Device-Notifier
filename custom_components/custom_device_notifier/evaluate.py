from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition


# The callable type returned by async_from_config
ConditionChecker = (
    condition.ConditionCheckerType  # available in HA ≥2023.9
    if hasattr(condition, "ConditionCheckerType")
    else Any                         # fallback for older versions
)


async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    """Validate *one* HA condition block and return True/False."""
    # 1️⃣ build the checker from the raw config
    checker: ConditionChecker = await condition.async_from_config(hass, cfg)

    # 2️⃣ run the checker (no template variables needed here)
    result = checker(hass, {})

    # async_from_config guarantees bool | None → make the type checker happy
    return bool(result)