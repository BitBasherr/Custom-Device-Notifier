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
    # 1️⃣  cfg goes first, hass second – matches the stub
    checker: ConditionCheckerType = await condition.async_from_config(cfg, hass)

    # 2️⃣  The checker’s return type is bool | None → force to bool for MyPy.
    return bool(checker(hass, {}))  # no template variables needed