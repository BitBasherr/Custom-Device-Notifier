"""Helpers to evaluate Home-Assistant condition configs."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition

ConditionConfig = Mapping[str, Any]


async def evaluate_condition(hass: HomeAssistant, cfg: ConditionConfig) -> bool:
    """Return True if *cfg* matches for the current HA state."""
    # Build a callable from the YAML/UI condition config
    check = condition.async_from_config(cfg, validate_config=False)

    # Invalid config ⇒ treat as not-matched instead of raising
    if check is None:
        return False

    result = check(hass)
    if asyncio.iscoroutine(result):
        result = await result

    return bool(result)