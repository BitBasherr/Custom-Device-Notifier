from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

try:
    # ≥2025.7
    from homeassistant.helpers.text import slugify
except ImportError:  # ≤2025.6
    from homeassistant.util import slugify

from .const import (
    CONF_FALLBACK,
    CONF_MATCH_MODE,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_MATCH,
    KEY_SERVICE,
)

_LOGGER = logging.getLogger(DOMAIN)

# ----- constants for step ids -------------------------------------------------
STEP_USER = "user"
STEP_ADD_TARGET = "add_target"
STEP_ADD_COND_ENTITY = "add_condition_entity"
STEP_ADD_COND_VALUE = "add_condition_value"
STEP_COND_MORE = "condition_more"
STEP_MATCH_MODE = "match_mode"
STEP_TARGET_MORE = "target_more"
STEP_ORDER_TARGETS = "order_targets"
STEP_CHOOSE_FALLBACK = "choose_fallback"
# ------------------------------------------------------------------------------

_OPS_NUM = [">", "<", ">=", "<=", "==", "!="]
_OPS_STR = ["==", "!="]

ENTITY_DOMAINS = [
    "sensor",
    "binary_sensor",
    "device_tracker",
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
]


class CustomDeviceNotifierConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Custom Device Notifier."""

    VERSION = 3  # bump because we changed the data layout

    # ---- life-cycle ----------------------------------------------------------
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._targets: list[dict[str, Any]] = []
        self._working_target: dict[str, Any] | None = None
        self._working_condition: dict[str, Any] | None = None

    # ---- step: user ----------------------------------------------------------
    async def async_step_user(self, user_input=None):
        _LOGGER.debug("STEP user | input=%s", user_input)
        if user_input is not None:
            raw = user_input["service_name_raw"]
            slug = slugify(raw) or "custom_notifier"
            await self.async_set_unique_id(slug)
            self._abort_if_unique_id_configured()

            self._data[CONF_SERVICE_NAME_RAW] = raw
            self._data[CONF_SERVICE_NAME] = slug
            return await self.async_step_add_target()

        schema = vol.Schema(
            {vol.Required("service_name_raw", default="Custom Notifier"): str}
        )
        return self.async_show_form(step_id=STEP_USER, data_schema=schema)

    # ---- step: add_target ----------------------------------------------------
    async def async_step_add_target(self, user_input=None):
        _LOGGER.debug("STEP add_target | input=%s", user_input)
        errors = {}
        notify_services = self.hass.services.async_services().get("notify", [])
        services = sorted(notify_services)

        if user_input is not None:
            svc = user_input["target_service"]
            if svc not in notify_services:
                errors["target_service"] = "must_be_notify"
            if not errors:
                self._working_target = {
                    KEY_SERVICE: f"notify.{svc}",
                    KEY_CONDITIONS: [],
                }
                return await self.async_step_add_condition_entity()

        schema = vol.Schema({vol.Required("target_service"): vol.In(services)})
        return self.async_show_form(
            step_id=STEP_ADD_TARGET, data_schema=schema, errors=errors
        )

    # ---- step: add_condition_entity -----------------------------------------
    async def async_step_add_condition_entity(self, user_input=None):
        _LOGGER.debug("STEP add_condition_entity | input=%s", user_input)
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required("entity"): selector(
                        {"entity": {"domain": ENTITY_DOMAINS}}
                    )
                }
            )
            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY, data_schema=schema
            )

        # store entity, move to value step
        ent_id = user_input["entity"]
        self._working_condition = {"entity_id": ent_id}
        return await self.async_step_add_condition_value()

    # ---- step: add_condition_value ------------------------------------------
    async def async_step_add_condition_value(self, user_input=None):
        _LOGGER.debug("STEP add_condition_value | input=%s", user_input)
        if user_input is not None:
            # persist operator & value, push into target’s list
            self._working_condition["operator"] = user_input["operator"]
            self._working_condition["value"] = user_input["value"]
            self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = None
            return await self.async_step_condition_more()

        # build form based on entity type
        ent_id = self._working_condition["entity_id"]
        st = self.hass.states.get(ent_id)
        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        if is_num:
            val_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in ent_id
                else {"number": {}}
            )
            schema = vol.Schema(
                {
                    vol.Required("operator", default="=="): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required(
                        "value", default=float(st.state) if st else 0
                    ): selector(val_sel),
                }
            )
        else:
            opts = [
                st.state if st else "",
                "unknown or unavailable",
                "unknown",
                "unavailable",
            ]
            # keep order, remove dups
            final = []
            seen = set()
            for o in opts:
                if o not in seen:
                    final.append(o)
                    seen.add(o)
            schema = vol.Schema(
                {
                    vol.Required("operator", default="=="): selector(
                        {"select": {"options": _OPS_STR}}
                    ),
                    vol.Required("value", default=final[0]): selector(
                        {"select": {"options": final}}
                    ),
                }
            )

        return self.async_show_form(step_id=STEP_ADD_COND_VALUE, data_schema=schema)

    # ---- step: condition_more ------------------------------------------------
    async def async_step_condition_more(self, user_input=None):
        _LOGGER.debug("STEP condition_more | input=%s", user_input)
        if user_input is not None:
            if user_input["choice"] == "add":
                return await self.async_step_add_condition_entity()
            # finished adding conditions → ask for match_mode
            return await self.async_step_match_mode()

        schema = vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add another condition"},
                                {"value": "done", "label": "✅ Done this target"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(step_id=STEP_COND_MORE, data_schema=schema)

    # ---- step: match_mode ----------------------------------------------------
    async def async_step_match_mode(self, user_input=None):
        _LOGGER.debug("STEP match_mode | input=%s", user_input)
        if user_input is not None:
            self._working_target[KEY_MATCH] = user_input[CONF_MATCH_MODE]
            self._targets.append(self._working_target)
            self._working_target = None
            return await self.async_step_target_more()

        schema = vol.Schema(
            {
                vol.Required(CONF_MATCH_MODE, default="all"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "all", "label": "Match all conditions"},
                                {"value": "any", "label": "Match any condition"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(step_id=STEP_MATCH_MODE, data_schema=schema)

    # ---- step: target_more ---------------------------------------------------
    async def async_step_target_more(self, user_input=None):
        _LOGGER.debug("STEP target_more | input=%s", user_input)
        if user_input is not None:
            if user_input["next"] == "add":
                return await self.async_step_add_target()
            return await self.async_step_order_targets()

        schema = vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": "add",
                                    "label": "➕ Add another notify target",
                                },
                                {"value": "done", "label": "✅ Done targets"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(step_id=STEP_TARGET_MORE, data_schema=schema)

    # ---- step: order_targets -------------------------------------------------
    async def async_step_order_targets(self, user_input=None):
        _LOGGER.debug("STEP order_targets | input=%s", user_input)
        errors = {}
        opts = [t[KEY_SERVICE] for t in self._targets]

        if user_input is not None:
            priority = user_input["priority"]
            if set(priority) != set(opts):
                errors["priority"] = "invalid_priority"
            if not errors:
                self._data[CONF_TARGETS] = self._targets
                self._data[CONF_PRIORITY] = priority
                return await self.async_step_choose_fallback()

        schema = vol.Schema(
            {
                vol.Required("priority"): selector(
                    {"select": {"options": opts, "mode": "list", "multiple": True}}
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS, data_schema=schema, errors=errors
        )

    # ---- step: choose_fallback ----------------------------------------------
    async def async_step_choose_fallback(self, user_input=None):
        _LOGGER.debug("STEP choose_fallback | input=%s", user_input)
        errors = {}
        notify_services = self.hass.services.async_services().get("notify", [])
        services = sorted(notify_services)

        if user_input is not None:
            fb = user_input["fallback"]
            if fb not in notify_services:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                return self.async_create_entry(
                    title=self._data[CONF_SERVICE_NAME_RAW], data=self._data
                )

        default_fb = (
            self._targets[0][KEY_SERVICE].replace("notify.", "")
            if self._targets
            else None
        )
        schema = vol.Schema(
            {vol.Required("fallback", default=default_fb): vol.In(services)}
        )
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK, data_schema=schema, errors=errors
        )

    # ---- options flow (reuse same UI) ---------------------------------------
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        flow = CustomDeviceNotifierConfigFlow()
        flow.hass = config_entry.hass  # type: ignore[assignment]
        flow._data = dict(config_entry.data)
        flow._targets = list(config_entry.data.get(CONF_TARGETS, []))
        return flow
