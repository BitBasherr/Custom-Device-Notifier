# custom_components/custom_device_notifier/evaluate.py
from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition


async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    """
    Evaluate a condition (custom or native HA format).

    Supports:
      - Native HA condition dicts (contain 'condition' key)
      - Compact custom dicts:
          {
            'entity_id': <str|list[str]>,
            'operator': '==','!=','>','<','>=','<=',
            'value': <str|num|special>
          }
      - Special values: 'unknown', 'unavailable', 'unknown or unavailable'
    """
    data = dict(cfg)

    # ── Native Home Assistant format: delegate directly ───────────────────────
    if "condition" in data:
        checker = await condition.async_from_config(hass, data)
        ret = checker(hass, {})
        if isinstance(ret, Awaitable):
            ret = await ret
        return bool(ret)

    # ── Compact custom format ─────────────────────────────────────────────────
    entity_id = data["entity_id"]
    operator = data["operator"]
    value = data["value"]

    ids = entity_id if isinstance(entity_id, list) else [entity_id]
    results: list[bool] = []

    for eid in ids:
        st = hass.states.get(eid)
        if st is None:
            results.append(False)
            continue

        ha_cfg: dict[str, Any]  # single annotation; assign in branches below

        # ---------- Special-state handling -----------------------------------
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

            ha_cfg = {"condition": "template", "value_template": f"{{{{ {expr} }}}}"}

        else:
            # ---------- Decide numeric vs string based on current state -------
            try:
                float(st.state)
                is_numeric = True
            except ValueError:
                is_numeric = False

            if not is_numeric:
                # Strings: only == / != are meaningful
                if operator in (">", "<", ">=", "<="):
                    results.append(False)
                    continue
                if operator == "==":
                    ha_cfg = {"condition": "state", "entity_id": [eid], "state": str(value)}
                elif operator == "!=":
                    ha_cfg = {
                        "condition": "template",
                        "value_template": f"{{{{ not is_state('{eid}', '{value}') }}}}",
                    }
                else:
                    raise ValueError("Invalid operator for string comparison")
            else:
                # Numbers
                if operator == "==":
                    ha_cfg = {"condition": "state", "entity_id": [eid], "state": str(value)}
                elif operator == "!=":
                    ha_cfg = {
                        "condition": "template",
                        "value_template": (
                            f"{{{{ (states('{eid}') | float) != ({value} | float) }}}}"
                        ),
                    }
                elif operator == ">":
                    ha_cfg = {"condition": "numeric_state", "entity_id": [eid], "above": str(value)}
                elif operator == "<":
                    ha_cfg = {"condition": "numeric_state", "entity_id": [eid], "below": str(value)}
                elif operator == ">=":
                    ha_cfg = {
                        "condition": "numeric_state",
                        "entity_id": [eid],
                        "above": str(float(value) - 1e-7),
                    }
                elif operator == "<=":
                    ha_cfg = {
                        "condition": "numeric_state",
                        "entity_id": [eid],
                        "below": str(float(value) + 1e-7),
                    }
                else:
                    raise ValueError("Invalid operator for numeric comparison")

        # ---------- Execute the compiled HA condition ------------------------
        checker = await condition.async_from_config(hass, ha_cfg)
        rv = checker(hass, {})
        if isinstance(rv, Awaitable):
            rv = await rv
        results.append(bool(rv))

    return any(results)
