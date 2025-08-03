"""Helpers to evaluate Home Assistant condition dictionaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.condition import ConditionCheckerType

ConditionDict = Mapping[str, Any]


async def evaluate_condition(hass: HomeAssistant, cfg: ConditionDict) -> bool:
    """Return True if the single YAML condition *cfg* matches."""
    # Build a checker coroutine once …
    checker: ConditionCheckerType = await condition.async_from_config(cfg)
    # … then run it (no template variables needed here).
    return checker(hass, {})
