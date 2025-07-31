# custom_components/custom_device_notifier/config_flow.py
"""Config flow for Custom Device Notifier."""

import logging
from .const import DOMAIN

_LOGGER = logging.getLogger(DOMAIN)  # define the logger before you use it

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector
from homeassistant.util import slugify

from .const import (
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    CONF_PRIORITY,
    CONF_FALLBACK,
    CONF_MATCH_MODE,
    KEY_SERVICE,
    KEY_CONDITIONS,
    KEY_MATCH,
)

STEP_NAME         = "user"
STEP_ADD_TARGET   = "add_target"
STEP_ADD_COND     = "add_condition"
STEP_MATCH_MODE   = "match_mode"
STEP_COND_MORE    = "condition_more"
STEP_TARGET_MORE  = "target_more"
STEP_ORDER        = "order_targets"
STEP_FALLBACK     = "choose_fallback"

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
    """Wizard-style setup for notify.<your_name> service."""

    VERSION = 2

    def __init__(self):
        self._data: dict = {}
        self._targets: list[dict] = []
        self._current: dict = {}

    async def async_step_user(self, user_input=None):
        """Step 1: enter a human-friendly name (spaces & apostrophes OK)."""
        _LOGGER.debug("async_step_user called with: %s", user_input)
        errors: dict[str, str] = {}
        if user_input:
            raw = user_input["service_name_raw"]
            slug = slugify(raw, separator="_")
            if not slug:
                errors["service_name_raw"] = "invalid_name"
            else:
                self._data[CONF_SERVICE_NAME_RAW] = raw
                self._data[CONF_SERVICE_NAME]     = slug
                return await self.async_step_add_target()

        schema = vol.Schema({
            vol.Required(
                "service_name_raw",
                default=self._data.get(CONF_SERVICE_NAME_RAW, "Custom Notifier")
            ): str
        })
        return self.async_show_form(
            step_id=STEP_NAME,
            data_schema=schema,
            errors=errors
        )

    async def async_step_add_target(self, user_input=None):
        """Step 2: pick an existing notify.* service to wrap."""
        if user_input:
            svc = user_input["target_service"]
            self._current = {KEY_SERVICE: svc, KEY_CONDITIONS: []}
            return await self.async_step_add_condition()

        schema = vol.Schema({
            vol.Required("target_service"): selector({
                "service": {"domain": "notify"}
            })
        })
        return self.async_show_form(step_id=STEP_ADD_TARGET, data_schema=schema)

    async def async_step_add_condition(self, user_input=None):
        """
        Step 3: choose an entity, then operator & value.
        Numeric → raw symbols + slider if battery.
        Non-numeric → ==/!= + combined/individual unknown.
        """
        # 1) no input → pick entity
        if not user_input or "entity" not in user_input:
            schema = vol.Schema({
                vol.Required("entity"): selector({
                    "entity": {"domain": ENTITY_DOMAINS}
                })
            })
            return self.async_show_form(step_id=STEP_ADD_COND, data_schema=schema)

        # 2) entity chosen → stash and show operator/value
        ent_id = user_input["entity"]
        self._current[KEY_CONDITIONS].append({"entity": ent_id})
        st = self.hass.states.get(ent_id)
        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        if is_num:
            val_sel = {"number": {"min": 0, "max": 100, "step": 1}} if "battery" in ent_id else {"number": {}}
            schema = vol.Schema({
                vol.Required("operator", default="=="): selector({
                    "select": {"options": _OPS_NUM}
                }),
                vol.Required("value"): selector(val_sel)
            })
        else:
            options = [st.state if st else "", "unknown or unavailable", "unknown", "unavailable"]
            # dedupe
            seen = set(); final = []
            for o in options:
                if o not in seen:
                    final.append(o); seen.add(o)
            schema = vol.Schema({
                vol.Required("operator", default="=="): selector({
                    "select": {"options": _OPS_STR}
                }),
                vol.Required("value"): selector({
                    "select": {"options": final}
                })
            })

        return self.async_show_form(step_id=STEP_ADD_COND, data_schema=schema)

    async def async_step_match_mode(self, user_input=None):
        """Step 4: choose whether to match ALL or ANY conditions."""
        if user_input:
            self._current[KEY_MATCH] = user_input["match_mode"]
            return await self.async_step_condition_more()

        schema = vol.Schema({
            vol.Required("match_mode", default="all"): selector({
                "select": {
                    "options": [
                        ("Match all conditions", "all"),
                        ("Match any condition",  "any")
                    ]
                }
            })
        })
        return self.async_show_form(step_id=STEP_MATCH_MODE, data_schema=schema)

    async def async_step_condition_more(self, user_input=None):
        """Step 5: add more conditions or finish this target."""
        if user_input:
            if user_input["choice"] == "add":
                return await self.async_step_add_condition()
            # done with this target
            self._targets.append(self._current)
            self._current = {}
            return await self.async_step_target_more()

        schema = vol.Schema({
            vol.Required("choice", default="add"): selector({
                "select": {"options": {
                    "add":  "➕ Add another condition",
                    "done": "✅ Done this target"
                }}
            })
        })
        return self.async_show_form(step_id=STEP_COND_MORE, data_schema=schema)

    async def async_step_target_more(self, user_input=None):
        """Step 6: add another target or proceed to ordering."""
        if user_input:
            if user_input["next"] == "add":
                return await self.async_step_add_target()
            return await self.async_step_order_targets()

        schema = vol.Schema({
            vol.Required("next", default="add"): selector({
                "select": {"options": {
                    "add":  "➕ Add another notify target",
                    "done": "✅ Done targets"
                }}
            })
        })
        return self.async_show_form(step_id=STEP_TARGET_MORE, data_schema=schema)

    async def async_step_order_targets(self, user_input=None):
        """Step 7: order targets by priority."""
        if user_input:
            self._data[CONF_TARGETS]  = self._targets
            self._data[CONF_PRIORITY] = user_input["priority"]
            return await self.async_step_choose_fallback()

        options = [t[KEY_SERVICE] for t in self._targets]
        schema = vol.Schema({
            vol.Required("priority", default=options): selector({
                "select": {"options": options, "mode": "list"}
            })
        })
        return self.async_show_form(step_id=STEP_ORDER, data_schema=schema)

    async def async_step_choose_fallback(self, user_input=None):
        """Step 8: pick a fallback service."""
        if user_input:
            self._data[CONF_FALLBACK] = user_input["fallback"]
            return self.async_create_entry(
                title=self._data[CONF_SERVICE_NAME_RAW],
                data=self._data
            )

        default_fb = self._targets[0][KEY_SERVICE]
        schema = vol.Schema({
            vol.Required("fallback", default=default_fb): selector({
                "service": {"domain": "notify"}
            })
        })
        return self.async_show_form(step_id=STEP_FALLBACK, data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Enable Options-based reconfiguration."""
        flow = CustomDeviceNotifierConfigFlow()
        flow.hass     = config_entry.hass
        flow._data    = dict(config_entry.data)
        flow._targets = list(config_entry.data.get(CONF_TARGETS, []))
        return flow
