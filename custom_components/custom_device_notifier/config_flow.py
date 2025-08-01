from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

try:
    from homeassistant.helpers.text import slugify
except ImportError:
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
    VERSION = 2

    def __init__(self):
        self._data: dict = {}
        self._targets: list[dict] = []
        self._current: dict = {}

    async def async_step_user(self, user_input=None):
        try:
            _LOGGER.debug("async_step_user ENTRY user_input=%s", user_input)
            if user_input:
                raw = user_input["service_name_raw"]
                slug = slugify(raw)
                if not slug:
                    slug = "custom_notifier"
                self._data[CONF_SERVICE_NAME_RAW] = raw
                self._data[CONF_SERVICE_NAME] = slug
                await self.async_set_unique_id(slug)
                self._abort_if_unique_id_configured()
                _LOGGER.debug(" → slug=%s", slug)
                _LOGGER.debug(" → _data=%s", self._data)
                return await self.async_step_add_target()

            schema = vol.Schema({
                vol.Required(
                    "service_name_raw",
                    default=self._data.get(CONF_SERVICE_NAME_RAW, "Custom Notifier")
                ): str
            })
            return self.async_show_form(step_id=STEP_NAME, data_schema=schema)
        except Exception as e:
            _LOGGER.error("Error in user step: %s", e)
            return self.async_show_form(
                step_id=STEP_NAME,
                data_schema=vol.Schema({
                    vol.Required(
                        "service_name_raw",
                        default=self._data.get(CONF_SERVICE_NAME_RAW, "Custom Notifier")
                    ): str
                }),
                errors={"base": "unknown"}
            )

    async def async_step_add_target(self, user_input=None):
        try:
            _LOGGER.debug("async_step_add_target ENTRY user_input=%s", user_input)
            errors = {}
            if user_input:
                svc = user_input["target_service"]
                domain, _ = svc.split(".", 1)
                if domain != "notify":
                    errors["target_service"] = "must_be_notify"
                if not errors:
                    self._current = {KEY_SERVICE: svc, KEY_CONDITIONS: []}
                    _LOGGER.debug(" → _current=%s", self._current)
                    return await self.async_step_add_condition()

            schema = vol.Schema({
                vol.Required("target_service", default=""): selector({
                    "service": {}
                })
            })
            return self.async_show_form(step_id=STEP_ADD_TARGET, data_schema=schema, errors=errors)
        except Exception as e:
            _LOGGER.error("Error in add_target step: %s", e)
            return self.async_show_form(
                step_id=STEP_ADD_TARGET,
                data_schema=vol.Schema({
                    vol.Required("target_service", default=""): selector({
                        "service": {}
                    })
                }),
                errors={"base": "unknown"}
            )

    async def async_step_add_condition(self, user_input=None):
        try:
            _LOGGER.debug("async_step_add_condition ENTRY user_input=%s", user_input)
            if not user_input or "entity" not in user_input:
                schema = vol.Schema({
                    vol.Required("entity", default=""): selector({
                        "entity": {"domain": ENTITY_DOMAINS}
                    })
                })
                return self.async_show_form(step_id=STEP_ADD_COND, data_schema=schema)

            ent_id = user_input["entity"]
            self._current[KEY_CONDITIONS].append({"entity": ent_id})
            _LOGGER.debug(" → condition entity added: %s", ent_id)

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
                    vol.Required("value", default=0): selector(val_sel),
                })
            else:
                opts = [
                    st.state if st else "",
                    "unknown or unavailable",
                    "unknown",
                    "unavailable",
                ]
                seen = set()
                final = []
                for o in opts:
                    if o not in seen:
                        final.append(o)
                        seen.add(o)
                schema = vol.Schema({
                    vol.Required("operator", default="=="): selector({
                        "select": {"options": _OPS_STR}
                    }),
                    vol.Required("value", default=final[0]): selector({
                        "select": {"options": final}
                    }),
                })

            return self.async_show_form(step_id=STEP_MATCH_MODE, data_schema=schema)
        except Exception as e:
            _LOGGER.error("Error in add_condition step: %s", e)
            return self.async_show_form(
                step_id=STEP_ADD_COND,
                data_schema=vol.Schema({
                    vol.Required("entity", default=""): selector({
                        "entity": {"domain": ENTITY_DOMAINS}
                    })
                }),
                errors={"base": "unknown"}
            )

    async def async_step_match_mode(self, user_input=None):
        try:
            _LOGGER.debug("async_step_match_mode ENTRY user_input=%s", user_input)
            if user_input:
                self._current[KEY_MATCH] = user_input[CONF_MATCH_MODE]
                _LOGGER.debug(" → match mode set: %s", self._current[KEY_MATCH])
                return await self.async_step_condition_more()

            schema = vol.Schema({
                vol.Required(CONF_MATCH_MODE, default="all"): selector({
                    "select": {
                        "options": [
                            ("Match all conditions", "all"),
                            ("Match any condition", "any"),
                        ]
                    }
                })
            })
            return self.async_show_form(step_id=STEP_MATCH_MODE, data_schema=schema)
        except Exception as e:
            _LOGGER.error("Error in match_mode step: %s", e)
            return self.async_show_form(
                step_id=STEP_MATCH_MODE,
                data_schema=vol.Schema({
                    vol.Required(CONF_MATCH_MODE, default="all"): selector({
                        "select": {
                            "options": [
                                ("Match all conditions", "all"),
                                ("Match any condition", "any"),
                            ]
                        }
                    })
                }),
                errors={"base": "unknown"}
            )

    async def async_step_condition_more(self, user_input=None):
        try:
            _LOGGER.debug("async_step_condition_more ENTRY user_input=%s", user_input)
            if user_input:
                if user_input["choice"] == "add":
                    return await self.async_step_add_condition()
                self._targets.append(self._current)
                _LOGGER.debug(" → target added: %s", self._current)
                self._current = {}
                return await self.async_step_target_more()

            schema = vol.Schema({
                vol.Required("choice", default="add"): selector({
                    "select": {"options": {
                        "add": "➕ Add another condition",
                        "done": "✅ Done this target",
                    }}
                })
            })
            return self.async_show_form(step_id=STEP_COND_MORE, data_schema=schema)
        except Exception as e:
            _LOGGER.error("Error in condition_more step: %s", e)
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=vol.Schema({
                    vol.Required("choice", default="add"): selector({
                        "select": {"options": {
                            "add": "➕ Add another condition",
                            "done": "✅ Done this target",
                        }}
                    })
                }),
                errors={"base": "unknown"}
            )

    async def async_step_target_more(self, user_input=None):
        try:
            _LOGGER.debug("async_step_target_more ENTRY user_input=%s", user_input)
            if user_input:
                if user_input["next"] == "add":
                    return await self.async_step_add_target()
                return await self.async_step_order_targets()

            schema = vol.Schema({
                vol.Required("next", default="add"): selector({
                    "select": {"options": {
                        "add": "➕ Add another notify target",
                        "done": "✅ Done targets",
                    }}
                })
            })
            return self.async_show_form(step_id=STEP_TARGET_MORE, data_schema=schema)
        except Exception as e:
            _LOGGER.error("Error in target_more step: %s", e)
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=vol.Schema({
                    vol.Required("next", default="add"): selector({
                        "select": {"options": {
                            "add": "➕ Add another notify target",
                            "done": "✅ Done targets",
                        }}
                    })
                }),
                errors={"base": "unknown"}
            )

    async def async_step_order_targets(self, user_input=None):
        try:
            _LOGGER.debug("async_step_order_targets ENTRY user_input=%s", user_input)
            errors = {}
            if user_input:
                self._data[CONF_TARGETS] = self._targets
                self._data[CONF_PRIORITY] = user_input["priority"]
                _LOGGER.debug(" → priority set: %s", self._data[CONF_PRIORITY])
                _LOGGER.debug(" → targets: %s", self._data[CONF_TARGETS])
                return await self.async_step_choose_fallback()

            opts = [t[KEY_SERVICE] for t in self._targets]
            if not opts:
                errors["base"] = "no_targets"
            schema = vol.Schema({
                vol.Required("priority", default=opts or [""]): selector({
                    "select": {"options": opts, "mode": "list"}
                })
            })
            return self.async_show_form(step_id=STEP_ORDER, data_schema=schema, errors=errors)
        except Exception as e:
            _LOGGER.error("Error in order_targets step: %s", e)
            return self.async_show_form(
                step_id=STEP_ORDER,
                data_schema=vol.Schema({
                    vol.Required("priority", default=[""]): selector({
                        "select": {"options": [], "mode": "list"}
                    })
                }),
                errors={"base": "unknown"}
            )

    async def async_step_choose_fallback(self, user_input=None):
        try:
            _LOGGER.debug("async_step_choose_fallback ENTRY user_input=%s", user_input)
            errors = {}
            if user_input:
                fb = user_input["fallback"]
                domain, _ = fb.split(".", 1)
                if domain != "notify":
                    errors["fallback"] = "must_be_notify"
                if not errors:
                    self._data[CONF_FALLBACK] = fb
                    _LOGGER.debug(" → fallback set: %s", self._data[CONF_FALLBACK])
                    _LOGGER.debug("FINAL DATA: %s", self._data)
                    return self.async_create_entry(
                        title=self._data[CONF_SERVICE_NAME_RAW],
                        data=self._data,
                    )

            default_fb = self._targets[0][KEY_SERVICE] if self._targets else ""
            schema = vol.Schema({
                vol.Required("fallback", default=default_fb): selector({
                    "service": {}
                })
            })
            return self.async_show_form(step_id=STEP_FALLBACK, data_schema=schema, errors=errors)
        except Exception as e:
            _LOGGER.error("Error in choose_fallback step: %s", e)
            return self.async_show_form(
                step_id=STEP_FALLBACK,
                data_schema=vol.Schema({
                    vol.Required("fallback", default=""): selector({
                        "service": {}
                    })
                }),
                errors={"base": "unknown"}
            )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        flow = CustomDeviceNotifierConfigFlow()
        flow.hass = config_entry.hass
        flow._data = dict(config_entry.data)
        flow._targets = list(config_entry.data.get(CONF_TARGETS, []))
        return flow
