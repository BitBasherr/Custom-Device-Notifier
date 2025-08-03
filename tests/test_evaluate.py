# custom_components/custom_device_notifier/evaluate.py
from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.condition import ConditionCheckerType
from homeassistant.helpers.template import Template


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    """Evaluate a Home Assistant condition *immediately*.

    Parameters
    ----------
    hass:
        The running Home Assistant instance.
    cfg:
        A mapping that represents a condition block exactly as it would appear
        in YAML / config‐flow (e.g. {"condition": "state", "entity_id": "…", …}).

    Returns
    -------
    bool
        ``True`` when the condition matches, otherwise ``False``.
    """
    # Home Assistant’s `async_from_config` expects a real dict, _not_ a Mapping.
    cfg = dict(cfg)
    if "value_template" in cfg:
        cfg["value_template"] = Template(cfg["value_template"], hass)
    checker: ConditionCheckerType = await condition.async_from_config(hass, cfg)

    # The checker may be an async function or a plain function returning
    # `bool | None`.  We have to handle both cases to keep the type-checker happy.
    raw_result: bool | None | Awaitable[bool | None] = checker(hass, {})

    if isinstance(raw_result, Awaitable):
        raw_result = await raw_result

    # Coerce `None` → `False`
    return bool(raw_result)