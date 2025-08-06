from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.template import Template


async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    """Evaluate either a native HA condition dict or a compact custom one."""
    data = dict(cfg)

    # ── Native Home-Assistant condition ──────────────────────────────────────
    if "condition" in data:
        if data.get("condition") == "template" and isinstance(
            data.get("value_template"), str
        ):
            # HA 2025.7 expects a Template object
            data["value_template"] = Template(data["value_template"], hass)

        checker = await condition.async_from_config(hass, data)
        res = checker(hass, {})
        if isinstance(res, Awaitable):
            res = await res
        return bool(res)

    # ── Compact custom format ────────────────────────────────────────────────
    entity_id = data["entity_id"]
    operator: str = data["operator"]
    value = data["value"]

    entity_ids = entity_id if isinstance(entity_id, list) else [entity_id]
    results: list[bool] = []

    for eid in entity_ids:
        st = hass.states.get(eid)
        if st is None:
            results.append(False)
            continue

        ha_cfg: dict[str, Any]

        # special strings -----------------------------------------------------
        if isinstance(value, str) and value in (
            "unknown",
            "unavailable",
            "unknown or unavailable",
        ):
            if operator not in ("==", "!="):
                raise ValueError("Special values only support == / !=")

            expr = (
                f"states('{eid}') in ['unknown','unavailable']"
                if value == "unknown or unavailable"
                else f"is_state('{eid}', '{value}')"
            )
            if operator == "!=":
                expr = f"not ({expr})"

            ha_cfg = {
                "condition": "template",
                "value_template": Template(f"{{{{ {expr} }}}}", hass),
            }

        # numeric vs. string --------------------------------------------------
        else:
            try:
                float(st.state)
                is_numeric = True
            except ValueError:
                is_numeric = False

            if not is_numeric:
                if operator in (">", "<", ">=", "<="):
                    results.append(False)
                    continue

                if operator == "==":
                    ha_cfg = {"condition": "state", "entity_id": [eid], "state": str(value)}
                elif operator == "!=":
                    ha_cfg = {
                        "condition": "template",
                        "value_template": Template(
                            f"{{{{ not is_state('{eid}', '{value}') }}}}", hass
                        ),
                    }
                else:
                    raise ValueError("Invalid operator for string comparison")

            else:
                if operator == "==":
                    ha_cfg = {"condition": "state", "entity_id": [eid], "state": str(value)}
                elif operator == "!=":
                    ha_cfg = {
                        "condition": "template",
                        "value_template": Template(
                            f"{{{{ (states('{eid}') | float) != ({value} | float) }}}}", hass
                        ),
                    }
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

        checker = await condition.async_from_config(hass, ha_cfg)
        rv = checker(hass, {})
        if isinstance(rv, Awaitable):
            rv = await rv
        results.append(bool(rv))

    return any(results)
