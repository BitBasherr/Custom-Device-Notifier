from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.template import Template


async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    """
    Evaluate either a native Home-Assistant condition dict or a compact custom dict.

    Compact format example:
        {
            "entity_id": "sensor.phone_battery",
            "operator": ">",
            "value": 20
        }
    Special values: "unknown", "unavailable", "unknown or unavailable"
    """
    data = dict(cfg)

    # ── Native HA condition ───────────────────────────────────────────────────
    if "condition" in data:
        checker = await condition.async_from_config(hass, data)
        result = checker(hass, {})
        if isinstance(result, Awaitable):
            result = await result
        return bool(result)

    # ── Compact format ────────────────────────────────────────────────────────
    entity_id = data["entity_id"]
    operator: str = data["operator"]
    value: str | int | float = data["value"]

    ids = entity_id if isinstance(entity_id, list) else [entity_id]
    outcomes: list[bool] = []

    for eid in ids:
        state_obj = hass.states.get(eid)
        if state_obj is None:
            outcomes.append(False)
            continue

        # Build a proper HA condition dict
        ha_cfg: dict[str, Any]

        # ── Special strings ---------------------------------------------------
        if isinstance(value, str) and value in (
            "unknown",
            "unavailable",
            "unknown or unavailable",
        ):
            if operator not in ("==", "!="):
                raise ValueError("Special values only support == or !=")

            if value == "unknown or unavailable":
                expr = f"states('{eid}') in ['unknown', 'unavailable']"
            else:
                expr = f"is_state('{eid}', '{value}')"

            if operator == "!=":
                expr = f"not ({expr})"

            ha_cfg = {
                "condition": "template",
                "value_template": Template(f"{{{{ {expr} }}}}", hass),
            }

        # ── Determine numeric vs string path ---------------------------------
        else:
            try:
                float(state_obj.state)
                is_numeric_state = True
            except ValueError:
                is_numeric_state = False

            # ── String state comparisons -------------------------------------
            if not is_numeric_state:
                if operator in (">", "<", ">=", "<="):
                    outcomes.append(False)
                    continue

                if operator == "==":
                    ha_cfg = {
                        "condition": "state",
                        "entity_id": [eid],
                        "state": str(value),
                    }
                elif operator == "!=":
                    tmpl = Template(f"{{{{ not is_state('{eid}', '{value}') }}}}", hass)
                    ha_cfg = {"condition": "template", "value_template": tmpl}
                else:
                    raise ValueError("Invalid operator for string comparison")

            # ── Numeric state comparisons ------------------------------------
            else:
                if operator == "==":
                    ha_cfg = {
                        "condition": "state",
                        "entity_id": [eid],
                        "state": str(value),
                    }
                elif operator == "!=":
                    tmpl = Template(
                        f"{{{{ (states('{eid}') | float) != ({value} | float) }}}}",
                        hass,
                    )
                    ha_cfg = {"condition": "template", "value_template": tmpl}
                elif operator == ">":
                    ha_cfg = {
                        "condition": "numeric_state",
                        "entity_id": [eid],
                        "above": float(value),
                    }
                elif operator == "<":
                    ha_cfg = {
                        "condition": "numeric_state",
                        "entity_id": [eid],
                        "below": float(value),
                    }
                elif operator == ">=":
                    ha_cfg = {
                        "condition": "numeric_state",
                        "entity_id": [eid],
                        "above": float(value),
                    }
                elif operator == "<=":
                    ha_cfg = {
                        "condition": "numeric_state",
                        "entity_id": [eid],
                        "below": float(value),
                    }
                else:
                    raise ValueError("Invalid operator for numeric comparison")

        # ── Run the compiled HA condition ------------------------------------
        checker = await condition.async_from_config(hass, ha_cfg)
        result = checker(hass, {})
        if isinstance(result, Awaitable):
            result = await result
        outcomes.append(bool(result))

    return any(outcomes)
