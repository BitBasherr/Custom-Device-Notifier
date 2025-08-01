from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_TARGETS,
    KEY_CONDITIONS,
    KEY_MATCH,
    KEY_SERVICE,
)

_LOGGER = logging.getLogger(__name__)


async def async_register_services(hass: HomeAssistant, entry) -> None:
    name = entry.data["service_name"]

    async def _handle_notify(call: ServiceCall) -> None:
        targets: list[dict] = entry.data[CONF_TARGETS]
        ordered: list[str] = entry.data[CONF_PRIORITY]
        fallback: str = entry.data[CONF_FALLBACK]

        _LOGGER.debug("Received notify call: data=%s", call.data)
        for svc in ordered:
            match = next((t for t in targets if t[KEY_SERVICE] == svc), None)
            if not match:
                continue

            conditions = match.get(KEY_CONDITIONS, [])
            mode = match.get(KEY_MATCH, "all")

            results = []
            for cond in conditions:
                ent_id = cond["entity"]
                op = cond.get("operator", "==")
                val = cond.get("value")
                st = hass.states.get(ent_id)
                if st is None or st.state in ("unknown", "unavailable"):
                    results.append(False)
                    continue

                try:
                    if isinstance(val, int | float):
                        st_val = float(st.state)
                        result = eval(f"{st_val} {op} {val}")
                    else:
                        result = eval(f'"{st.state}" {op} "{val}"')
                    results.append(result)
                except Exception as e:
                    _LOGGER.warning("Condition eval error for %s: %s", ent_id,
                                    e)
                    results.append(False)

            all_pass = all(results) if mode == "all" else any(results)
            if all_pass:
                await hass.services.async_call(
                    "notify",
                    svc,
                    call.data,
                    blocking=True
                )
                return

        if fallback:
            _LOGGER.debug("No match. Using fallback: %s", fallback)
            await hass.services.async_call(
                "notify",
                fallback,
                call.data,
                blocking=True
            )

    hass.services.async_register("notify", name, _handle_notify)
