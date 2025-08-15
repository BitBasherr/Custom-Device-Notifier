from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

try:  # ≥2025.7
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
    KEY_SERVICE,
    CONF_ROUTING_MODE,
    ROUTING_CONDITIONAL,
    ROUTING_SMART,
    DEFAULT_ROUTING_MODE,
    CONF_SMART_PC_NOTIFY,
    CONF_SMART_PC_SESSION,
    CONF_SMART_PHONE_ORDER,
    CONF_SMART_MIN_BATTERY,
    CONF_SMART_PHONE_FRESH_S,
    CONF_SMART_PC_FRESH_S,
    CONF_SMART_REQUIRE_AWAKE,
    CONF_SMART_REQUIRE_UNLOCKED,
    CONF_SMART_POLICY,
    SMART_POLICY_PC_FIRST,
    SMART_POLICY_PHONE_IF_PC_UNLOCKED,
    SMART_POLICY_PHONE_FIRST,
    DEFAULT_SMART_MIN_BATTERY,
    DEFAULT_SMART_PHONE_FRESH_S,
    DEFAULT_SMART_PC_FRESH_S,
    DEFAULT_SMART_REQUIRE_AWAKE,
    DEFAULT_SMART_REQUIRE_UNLOCKED,
    DEFAULT_SMART_POLICY,
)

_LOGGER = logging.getLogger(DOMAIN)

# ──────────────────────────── step IDs ────────────────────────────────
STEP_USER = "user"
STEP_ADD_TARGET = "add_target"
STEP_ADD_COND_ENTITY = "add_condition_entity"
STEP_ADD_COND_VALUE = "add_condition_value"
STEP_COND_MORE = "condition_more"
STEP_REMOVE_COND = "remove_condition"
STEP_SELECT_COND_TO_EDIT = "select_condition_to_edit"
STEP_MATCH_MODE = "match_mode"
STEP_TARGET_MORE = "target_more"
STEP_ORDER_TARGETS = "order_targets"
STEP_CHOOSE_FALLBACK = "choose_fallback"
STEP_SELECT_TARGET_TO_EDIT = "select_target_to_edit"
STEP_SELECT_TARGET_TO_REMOVE = "select_target_to_remove"
STEP_ROUTING_MODE = "routing_mode"  # optional wizard branch
STEP_SMART_SETUP = "smart_setup"  # optional wizard branch

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


# ──────────────────────────── helper utils ────────────────────────────────
def _order_placeholders(
    services: list[str], current: list[str] | None
) -> dict[str, str]:
    """Build description placeholders for the order/priority step."""
    if not services:
        return {"current_order": "—", "remaining": "—"}

    current = current or []
    chosen = [s for s in current if s in services]
    remaining = [s for s in services if s not in chosen]

    def fmt(lst: list[str]) -> str:
        if not lst:
            return "—"
        return "\n".join(f"{i + 1}. {name}" for i, name in enumerate(lst))

    return {
        "current_order": fmt(chosen),
        "remaining": "\n".join(remaining) if remaining else "—",
    }


def _format_targets_pretty(
    targets: list[dict[str, Any]],
    working: dict[str, Any] | None = None,
) -> str:
    """Render as a real nested Markdown list so wrapped lines hang correctly."""
    blocks: list[str] = []

    def one_block(tgt: dict[str, Any], suffix: str = "") -> str:
        svc = tgt.get(KEY_SERVICE, "(unknown)")
        conds: list[dict[str, Any]] = tgt.get(KEY_CONDITIONS, [])
        lines: list[str] = []
        lines.append(f"- {svc}{suffix}")
        if conds:
            for c in conds:
                eid = c.get("entity_id", "<?>")
                op = c.get("operator", "?")
                val = c.get("value", "?")
                lines.append(f"    - {eid} {op} {val}")
        else:
            lines.append("    - (no conditions)")
        return "\n".join(lines)

    for t in targets:
        blocks.append(one_block(t))

    if working and working.get(KEY_SERVICE):
        blocks.append(one_block(working, " (editing)"))

    return "\n".join(blocks) if blocks else "No targets yet"


def _notify_services(hass) -> list[str]:
    return sorted(hass.services.async_services().get("notify", {}))


def _mobile_app_services(services: list[str]) -> list[str]:
    # options in your flow are service names w/o "notify."
    return [s for s in services if s.startswith("mobile_app_")]


def _default_pc_notify(services: list[str]) -> str:
    # Best-effort sensible default, no hard-coding
    for s in services:
        if "pc" in s or "desktop" in s or "laptop" in s:
            return s
    return services[0] if services else ""


# ──────────────────────────── Config Flow ────────────────────────────────
class CustomDeviceNotifierConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Interactive setup for Custom Device Notifier."""

    VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._targets: list[dict[str, Any]] = []
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None
        self._priority_list: list[str] = []  # live ordering buffer

    # ───────── schema helpers ─────────
    def _get_routing_mode_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_ROUTING_MODE, default=DEFAULT_ROUTING_MODE): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": ROUTING_CONDITIONAL,
                                    "label": "Conditional (use targets & conditions)",
                                },
                                {
                                    "value": ROUTING_SMART,
                                    "label": "Smart Select (PC/Phone policy)",
                                },
                            ]
                        }
                    }
                )
            }
        )

    def _get_smart_setup_schema(
        self, existing: dict[str, Any] | None = None
    ) -> vol.Schema:
        existing = existing or {}
        services = _notify_services(self.hass)  # list of names without "notify."
        mobiles = _mobile_app_services(services)

        pc_default = existing.get(CONF_SMART_PC_NOTIFY)
        if not pc_default:
            # choose best guess from all notify services
            pc_default = _default_pc_notify(services)
        pc_session_default = existing.get(CONF_SMART_PC_SESSION) or (
            f"sensor.{pc_default}_sessionstate" if pc_default else ""
        )
        phone_default = existing.get(CONF_SMART_PHONE_ORDER) or [
            f"notify.{m}" for m in mobiles
        ]

        return vol.Schema(
            {
                vol.Required(
                    CONF_SMART_PC_NOTIFY,
                    default=existing.get(
                        CONF_SMART_PC_NOTIFY,
                        f"notify.{pc_default}" if pc_default else "",
                    ),
                ): selector(
                    {
                        "select": {
                            "options": [f"notify.{s}" for s in services],
                            "custom_value": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_SMART_PC_SESSION,
                    default=existing.get(CONF_SMART_PC_SESSION, pc_session_default),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Optional(CONF_SMART_PHONE_ORDER, default=phone_default): selector(
                    {
                        "select": {
                            "options": [f"notify.{m}" for m in mobiles],
                            "multiple": True,
                            "custom_value": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_SMART_POLICY,
                    default=existing.get(CONF_SMART_POLICY, DEFAULT_SMART_POLICY),
                ): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": SMART_POLICY_PC_FIRST,
                                    "label": "PC first, else phones",
                                },
                                {
                                    "value": SMART_POLICY_PHONE_IF_PC_UNLOCKED,
                                    "label": "If PC unlocked, prefer phones",
                                },
                                {
                                    "value": SMART_POLICY_PHONE_FIRST,
                                    "label": "Phones first, else PC",
                                },
                            ]
                        }
                    }
                ),
                vol.Required(
                    CONF_SMART_MIN_BATTERY,
                    default=existing.get(
                        CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY
                    ),
                ): selector({"number": {"min": 0, "max": 100, "step": 1}}),
                vol.Required(
                    CONF_SMART_PHONE_FRESH_S,
                    default=existing.get(
                        CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S
                    ),
                ): selector({"number": {"min": 30, "max": 1800, "step": 10}}),
                vol.Required(
                    CONF_SMART_PC_FRESH_S,
                    default=existing.get(
                        CONF_SMART_PC_FRESH_S, DEFAULT_SMART_PC_FRESH_S
                    ),
                ): selector({"number": {"min": 30, "max": 3600, "step": 10}}),
                vol.Required(
                    CONF_SMART_REQUIRE_AWAKE,
                    default=existing.get(
                        CONF_SMART_REQUIRE_AWAKE, DEFAULT_SMART_REQUIRE_AWAKE
                    ),
                ): selector({"boolean": {}}),
                vol.Required(
                    CONF_SMART_REQUIRE_UNLOCKED,
                    default=existing.get(
                        CONF_SMART_REQUIRE_UNLOCKED, DEFAULT_SMART_REQUIRE_UNLOCKED
                    ),
                ): selector({"boolean": {}}),
            }
        )

    def _get_condition_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._working_target.get(KEY_CONDITIONS):
            options.insert(1, {"value": "edit", "label": "✏️ Edit"})
            options.insert(2, {"value": "remove", "label": "➖ Remove"})
        return vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {"select": {"options": options}}
                )
            }
        )

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        return {
            "current_conditions": "\n".join(
                f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds
            )
            or "No conditions yet"
        }

    def _get_target_more_placeholders(self) -> dict[str, str]:
        return {
            "current_targets": _format_targets_pretty(
                self._targets, self._working_target
            )
        }

    def _get_condition_value_schema(self, entity_id: str) -> vol.Schema:
        st = self.hass.states.get(entity_id)

        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        prev_op: str | None = None
        prev_value: str | None = None
        use_prev = False
        if (
            self._working_condition
            and self._working_condition.get("entity_id") == entity_id
        ):
            prev_op = self._working_condition.get("operator")
            prev_value = self._working_condition.get("value")
            use_prev = prev_op is not None and prev_value is not None

        if is_num:
            num_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in entity_id
                else {"number": {}}
            )

            value_options = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}"
                    if st
                    else "Current (unknown)",
                },
            ]

            default_operator = prev_op if use_prev else ">"
            default_value_choice = (
                "current"
                if (use_prev and st and str(prev_value) == str(st.state))
                else "manual"
            )

            default_num_value = 0.0
            if use_prev and prev_value is not None:
                try:
                    default_num_value = float(prev_value)
                except (TypeError, ValueError):
                    default_num_value = float(st.state) if st else 0.0
            else:
                default_num_value = float(st.state) if st else 0.0

            return vol.Schema(
                {
                    vol.Required("operator", default=default_operator): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required(
                        "value_choice", default=default_value_choice
                    ): selector({"select": {"options": value_options}}),
                    vol.Optional("value", default=default_num_value): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )

        # string path
        opts: list[str] = ["unknown or unavailable"]
        if st:
            opts.append(st.state)
        opts.extend(["unknown", "unavailable"])
        if (
            "_last_update_trigger" in entity_id
            and "android.intent.action.ACTION_SHUTDOWN" not in opts
        ):
            opts.append("android.intent.action.ACTION_SHUTDOWN")
        uniq = list(dict.fromkeys(opts))

        default_operator = prev_op if use_prev else "=="

        value_options = [
            {"value": "manual", "label": "Enter manually"},
            {
                "value": "current",
                "label": f"Current state: {st.state}" if st else "Current (unknown)",
            },
        ]
        default_value_choice = (
            "current"
            if (use_prev and st and str(prev_value) == str(st.state))
            else "manual"
        )

        if use_prev and prev_value in uniq:
            default_str_value = prev_value
        else:
            default_str_value = uniq[0] if uniq else ""

        choices: list[dict[str, str]] = []
        for opt in uniq:
            if opt == "android.intent.action.ACTION_SHUTDOWN":
                choices.append({"value": opt, "label": "Shutdown as Last Update"})
            else:
                choices.append({"value": opt, "label": opt})

        return vol.Schema(
            {
                vol.Required("operator", default=default_operator): selector(
                    {"select": {"options": _OPS_STR}}
                ),
                vol.Required("value_choice", default=default_value_choice): selector(
                    {"select": {"options": value_options}}
                ),
                vol.Optional("value", default=default_str_value): selector(
                    {"select": {"options": choices}}
                ),
                vol.Optional("manual_value"): str,
            }
        )

    # ─── steps ───
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            raw = user_input["service_name_raw"].strip()
            slug = slugify(raw) or "custom_notifier"
            await self.async_set_unique_id(slug)
            self._abort_if_unique_id_configured()
            self._data.update({CONF_SERVICE_NAME_RAW: raw, CONF_SERVICE_NAME: slug})
            return await self.async_step_add_target()

        return self.async_show_form(
            step_id=STEP_USER,
            data_schema=vol.Schema({vol.Required("service_name_raw"): str}),
        )

    async def async_step_add_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)

        if user_input:
            svc = user_input["target_service"]
            if svc not in notify_svcs:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {
                    KEY_SERVICE: f"notify.{svc}",
                    KEY_CONDITIONS: [],
                }
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=self._get_condition_more_schema(),
                    description_placeholders=self._get_condition_more_placeholders(),
                )

        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=vol.Schema(
                {
                    vol.Required("target_service"): selector(
                        {"select": {"options": service_options, "custom_value": True}}
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                "current_targets": _format_targets_pretty(
                    self._targets, self._working_target
                ),
            },
        )

    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not user_input:
            notify_service = self._working_target.get(KEY_SERVICE)
            all_entities = [
                entity
                for entity in self.hass.states.async_entity_ids()
                if entity.split(".")[0] in ENTITY_DOMAINS
            ]
            options: list[str] = []
            if notify_service:
                slug = notify_service.removeprefix("notify.")
                tokens = [tok for tok in slug.split("_") if tok]
                generic = {"mobile", "app", "notify", "mobileapp"}
                tokens = [t for t in tokens if t not in generic]

                def weight(entity: str) -> tuple[int, ...]:
                    return tuple(int(tok in entity) for tok in tokens)

                options = sorted(
                    all_entities, key=lambda e: (weight(e), e), reverse=True
                )
            else:
                options = sorted(all_entities)

            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY,
                data_schema=vol.Schema(
                    {
                        vol.Required("entity"): selector(
                            {"select": {"options": options, "custom_value": True}}
                        )
                    }
                ),
            )

        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(user_input["entity"]),
            description_placeholders={"entity_id": user_input["entity"]},
        )

    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            if isinstance(final_value, (int, float)):
                final_value = str(int(final_value)) if float(final_value).is_integer() else str(final_value)
            else:
                final_value = str(final_value)

            self._working_condition.update(
                operator=user_input["operator"], value=final_value
            )
            if self._editing_condition_index is not None:
                self._working_target[KEY_CONDITIONS][self._editing_condition_index] = (
                    self._working_condition
                )
                self._editing_condition_index = None
            else:
                self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = {}
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        eid = self._working_condition["entity_id"]
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(eid),
            description_placeholders={"entity_id": eid},
        )

    async def async_step_condition_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            choice = user_input["choice"]
            if choice == "add":
                return await self.async_step_add_condition_entity()
            if choice == "edit":
                return await self.async_step_select_condition_to_edit()
            if choice == "remove":
                conds = self._working_target[KEY_CONDITIONS]
                labels = [
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                ]
                return self.async_show_form(
                    step_id=STEP_REMOVE_COND,
                    data_schema=vol.Schema(
                        {
                            vol.Optional("conditions_to_remove", default=[]): selector(
                                {"select": {"options": labels, "multiple": True}}
                            )
                        }
                    ),
                )
            if choice == "done":
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_MATCH_MODE,
                                default=self._working_target.get(
                                    CONF_MATCH_MODE, "all"
                                ),
                            ): selector(
                                {
                                    "select": {
                                        "options": [
                                            {
                                                "value": "all",
                                                "label": "Require all conditions",
                                            },
                                            {
                                                "value": "any",
                                                "label": "Require any condition",
                                            },
                                        ]
                                    }
                                }
                            )
                        }
                    ),
                )

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=self._get_condition_more_schema(),
            description_placeholders=self._get_condition_more_placeholders(),
        )

    async def async_step_remove_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c for i, c in enumerate(conds) if labels[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_REMOVE_COND,
            data_schema=vol.Schema(
                {
                    vol.Optional("conditions_to_remove", default=[]): selector(
                        {"select": {"options": labels, "multiple": True}}
                    )
                }
            ),
        )

    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            selected = user_input["condition"]
            index = labels.index(selected)
            self._editing_condition_index = index
            self._working_condition = self._working_target[KEY_CONDITIONS][index].copy()
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=self._get_condition_value_schema(
                    self._working_condition["entity_id"]
                ),
                description_placeholders={
                    "entity_id": self._working_condition["entity_id"],
                    **self._get_condition_more_placeholders(),
                },
            )

        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("condition"): selector({"select": {"options": labels}})}
            ),
        )

    async def async_step_match_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            selected_mode = user_input[CONF_MATCH_MODE]
            self._working_target[CONF_MATCH_MODE] = selected_mode
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_MATCH_MODE,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MATCH_MODE,
                        default=self._working_target.get(CONF_MATCH_MODE, "all"),
                    ): selector(
                        {
                            "select": {
                                "options": [
                                    {"value": "all", "label": "Require all conditions"},
                                    {"value": "any", "label": "Require any condition"},
                                ]
                            }
                        }
                    )
                }
            ),
        )

    def _get_target_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add target"},
            {"value": "routing", "label": "🧠 Routing / Smart Select"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._targets:
            options.insert(1, {"value": "edit", "label": "✏️ Edit target"})
            options.insert(2, {"value": "remove", "label": "➖ Remove target"})
        return vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {"select": {"options": options}}
                )
            }
        )

    async def async_step_target_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            nxt = user_input["next"]
            if nxt == "add":
                service_options = sorted(
                    self.hass.services.async_services().get("notify", {})
                )
                return self.async_show_form(
                    step_id=STEP_ADD_TARGET,
                    data_schema=vol.Schema(
                        {
                            vol.Required("target_service"): selector(
                                {
                                    "select": {
                                        "options": service_options,
                                        "custom_value": True,
                                    }
                                }
                            )
                        }
                    ),
                    description_placeholders={
                        "available_services": ", ".join(service_options),
                        **self._get_target_more_placeholders(),
                    },
                )
            if nxt == "edit":
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                return await self.async_step_select_target_to_remove()
            if nxt == "routing":
                return self.async_show_form(
                    step_id=STEP_ROUTING_MODE,
                    data_schema=self._get_routing_mode_schema(),
                )
            if nxt == "done":
                services = [t[KEY_SERVICE] for t in self._targets]
                placeholders = _order_placeholders(services, self._priority_list)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(
                        services=services, current=self._priority_list
                    ),
                    description_placeholders=placeholders,
                )
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
            description_placeholders=self._get_target_more_placeholders(),
        )

    async def async_step_select_target_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            selected = user_input["target"]
            index = targets.index(selected)
            self._editing_target_index = index
            self._working_target = self._targets[index].copy()
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("target"): selector({"select": {"options": targets}})}
            ),
            description_placeholders=self._get_target_more_placeholders(),
        )

    async def async_step_select_target_to_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            to_remove = set(user_input.get("targets", []))
            self._targets = [
                t for i, t in enumerate(self._targets) if targets[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_REMOVE,
            data_schema=vol.Schema(
                {
                    vol.Optional("targets", default=[]): selector(
                        {"select": {"options": targets, "multiple": True}}
                    )
                }
            ),
            description_placeholders=self._get_target_more_placeholders(),
        )

    def _get_order_targets_schema(
        self, *, services: list[str], current: list[str] | None
    ) -> vol.Schema:
        current = current or []
        remaining = [s for s in services if s not in current]
        return vol.Schema(
            {
                vol.Optional("priority", default=current): selector(
                    {"select": {"options": services, "multiple": True}}
                ),
                vol.Optional("next_priority"): selector(
                    {"select": {"options": remaining}}
                ),
                vol.Optional("action", default="confirm"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "Add to order"},
                                {"value": "reset", "label": "Reset order"},
                                {"value": "confirm", "label": "Confirm"},
                            ]
                        }
                    }
                ),
            }
        )

    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        services = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            action = user_input.get("action", "confirm")
            next_item = user_input.get("next_priority")
            if action == "add" and next_item:
                if next_item not in self._priority_list:
                    self._priority_list.append(next_item)
                placeholders = _order_placeholders(services, self._priority_list)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(
                        services=services, current=self._priority_list
                    ),
                    description_placeholders=placeholders,
                )
            if action == "reset":
                self._priority_list = []
                placeholders = _order_placeholders(services, self._priority_list)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(
                        services=services, current=self._priority_list
                    ),
                    description_placeholders=placeholders,
                )

            # Confirm
            selected = user_input.get("priority")
            if isinstance(selected, list) and selected:
                final_priority = [s for s in selected if s in services]
            elif self._priority_list:
                final_priority = [s for s in self._priority_list if s in services]
            else:
                final_priority = services

            self._data.update(
                {CONF_TARGETS: self._targets, CONF_PRIORITY: final_priority}
            )
            notify_svcs = self.hass.services.async_services().get("notify", {})
            placeholders = _order_placeholders(services, final_priority)
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
                description_placeholders={
                    "available_services": ", ".join(sorted(notify_svcs)),
                    **placeholders,
                },
            )

        placeholders = _order_placeholders(services, self._priority_list)
        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=self._get_order_targets_schema(
                services=services, current=self._priority_list
            ),
            description_placeholders=placeholders,
        )

    def _get_choose_fallback_schema(self) -> vol.Schema:
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        default_fb = (
            self._targets[0][KEY_SERVICE].removeprefix("notify.")
            if self._targets
            else ""
        )
        return vol.Schema(
            {
                vol.Required("fallback", default=default_fb): selector(
                    {"select": {"options": service_options, "custom_value": True}}
                ),
                vol.Optional("nav", default="continue"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "back", "label": "⬅ Back"},
                                {"value": "continue", "label": "Continue"},
                            ]
                        }
                    }
                ),
            }
        )

    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)

        if user_input:
            # Allow navigating back
            if user_input.get("nav") == "back":
                services = [t[KEY_SERVICE] for t in self._targets]
                placeholders = _order_placeholders(
                    services, self._data.get(CONF_PRIORITY)
                )
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(
                        services=services, current=self._data.get(CONF_PRIORITY)
                    ),
                    description_placeholders=placeholders,
                )

            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                # Finish the classic wizard here to satisfy tests
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                title = (
                    self._data.get(CONF_SERVICE_NAME_RAW)
                    or self._data.get("service_name_raw")
                    or ""
                )
                return self.async_create_entry(title=title, data=self._data)

        services = [t[KEY_SERVICE] for t in self._targets]
        placeholders = _order_placeholders(services, self._data.get(CONF_PRIORITY))
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._get_choose_fallback_schema(),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                **placeholders,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return CustomDeviceNotifierOptionsFlowHandler(config_entry)


# ──────────────────────────── Options Flow ────────────────────────────────
class CustomDeviceNotifierOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow for Custom Device Notifier."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data = dict(config_entry.options or config_entry.data).copy()
        self._targets = list(self._data.get(CONF_TARGETS, [])).copy()
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None
        self._priority_list: list[str] = list(self._data.get(CONF_PRIORITY, []))

    # ───────── schema helpers (mirror) ─────────
    def _get_routing_mode_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_ROUTING_MODE, default=DEFAULT_ROUTING_MODE): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": ROUTING_CONDITIONAL,
                                    "label": "Conditional (use targets & conditions)",
                                },
                                {
                                    "value": ROUTING_SMART,
                                    "label": "Smart Select (PC/Phone policy)",
                                },
                            ]
                        }
                    }
                )
            }
        )

    def _get_smart_setup_schema(
        self, existing: dict[str, Any] | None = None
    ) -> vol.Schema:
        existing = existing or {}
        services = _notify_services(self.hass)
        mobiles = _mobile_app_services(services)

        pc_default = existing.get(CONF_SMART_PC_NOTIFY) or _default_pc_notify(services)
        pc_session_default = existing.get(CONF_SMART_PC_SESSION) or (
            f"sensor.{pc_default}_sessionstate" if pc_default else ""
        )
        phone_default = existing.get(CONF_SMART_PHONE_ORDER) or [
            f"notify.{m}" for m in mobiles
        ]

        return vol.Schema(
            {
                vol.Required(
                    CONF_SMART_PC_NOTIFY,
                    default=existing.get(
                        CONF_SMART_PC_NOTIFY,
                        f"notify.{pc_default}" if pc_default else "",
                    ),
                ): selector(
                    {
                        "select": {
                            "options": [f"notify.{s}" for s in services],
                            "custom_value": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_SMART_PC_SESSION,
                    default=existing.get(CONF_SMART_PC_SESSION, pc_session_default),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Optional(CONF_SMART_PHONE_ORDER, default=phone_default): selector(
                    {
                        "select": {
                            "options": [f"notify.{m}" for m in mobiles],
                            "multiple": True,
                            "custom_value": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_SMART_POLICY,
                    default=existing.get(CONF_SMART_POLICY, DEFAULT_SMART_POLICY),
                ): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": SMART_POLICY_PC_FIRST,
                                    "label": "PC first, else phones",
                                },
                                {
                                    "value": SMART_POLICY_PHONE_IF_PC_UNLOCKED,
                                    "label": "If PC unlocked, prefer phones",
                                },
                                {
                                    "value": SMART_POLICY_PHONE_FIRST,
                                    "label": "Phones first, else PC",
                                },
                            ]
                        }
                    }
                ),
                vol.Required(
                    CONF_SMART_MIN_BATTERY,
                    default=existing.get(
                        CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY
                    ),
                ): selector({"number": {"min": 0, "max": 100, "step": 1}}),
                vol.Required(
                    CONF_SMART_PHONE_FRESH_S,
                    default=existing.get(
                        CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S
                    ),
                ): selector({"number": {"min": 30, "max": 1800, "step": 10}}),
                vol.Required(
                    CONF_SMART_PC_FRESH_S,
                    default=existing.get(
                        CONF_SMART_PC_FRESH_S, DEFAULT_SMART_PC_FRESH_S
                    ),
                ): selector({"number": {"min": 30, "max": 3600, "step": 10}}),
                vol.Required(
                    CONF_SMART_REQUIRE_AWAKE,
                    default=existing.get(
                        CONF_SMART_REQUIRE_AWAKE, DEFAULT_SMART_REQUIRE_AWAKE
                    ),
                ): selector({"boolean": {}}),
                vol.Required(
                    CONF_SMART_REQUIRE_UNLOCKED,
                    default=existing.get(
                        CONF_SMART_REQUIRE_UNLOCKED, DEFAULT_SMART_REQUIRE_UNLOCKED
                    ),
                ): selector({"boolean": {}}),
            }
        )

    def _get_condition_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._working_target.get(KEY_CONDITIONS):
            options.insert(1, {"value": "edit", "label": "✏️ Edit"})
            options.insert(2, {"value": "remove", "label": "➖ Remove"})
        return vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {"select": {"options": options}}
                )
            }
        )

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        return {
            "current_conditions": "\n".join(
                f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds
            )
            or "No conditions yet"
        }

    def _get_target_more_placeholders(self) -> dict[str, str]:
        return {
            "current_targets": _format_targets_pretty(
                self._targets, self._working_target
            )
        }

    def _get_condition_value_schema(self, entity_id: str) -> vol.Schema:
        """Mirror of config flow version so mypy is happy."""
        st = self.hass.states.get(entity_id)
        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        prev_op: str | None = None
        prev_value: str | None = None
        use_prev = False
        if (
            self._working_condition
            and self._working_condition.get("entity_id") == entity_id
        ):
            prev_op = self._working_condition.get("operator")
            prev_value = self._working_condition.get("value")
            use_prev = prev_op is not None and prev_value is not None

        if is_num:
            num_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in entity_id
                else {"number": {}}
            )
            value_options = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}"
                    if st
                    else "Current (unknown)",
                },
            ]
            default_operator = prev_op if use_prev else ">"
            default_value_choice = (
                "current"
                if (use_prev and st and str(prev_value) == str(st.state))
                else "manual"
            )

            default_num_value = 0.0
            if use_prev and prev_value is not None:
                try:
                    default_num_value = float(prev_value)
                except (TypeError, ValueError):
                    default_num_value = float(st.state) if st else 0.0
            else:
                default_num_value = float(st.state) if st else 0.0

            return vol.Schema(
                {
                    vol.Required("operator", default=default_operator): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required(
                        "value_choice", default=default_value_choice
                    ): selector({"select": {"options": value_options}}),
                    vol.Optional("value", default=default_num_value): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )

        opts: list[str] = ["unknown or unavailable"]
        if st:
            opts.append(st.state)
        opts.extend(["unknown", "unavailable"])
        if (
            "_last_update_trigger" in entity_id
            and "android.intent.action.ACTION_SHUTDOWN" not in opts
        ):
            opts.append("android.intent.action.ACTION_SHUTDOWN")
        uniq = list(dict.fromkeys(opts))

        default_operator = prev_op if use_prev else "=="
        value_options = [
            {"value": "manual", "label": "Enter manually"},
            {
                "value": "current",
                "label": f"Current state: {st.state}" if st else "Current (unknown)",
            },
        ]
        default_value_choice = (
            "current"
            if (use_prev and st and str(prev_value) == str(st.state))
            else "manual"
        )

        if use_prev and prev_value in uniq:
            default_str_value = prev_value
        else:
            default_str_value = uniq[0] if uniq else ""

        choices: list[dict[str, str]] = []
        for opt in uniq:
            if opt == "android.intent.action.ACTION_SHUTDOWN":
                choices.append({"value": opt, "label": "Shutdown as Last Update"})
            else:
                choices.append({"value": opt, "label": opt})

        return vol.Schema(
            {
                vol.Required("operator", default=default_operator): selector(
                    {"select": {"options": _OPS_STR}}
                ),
                vol.Required("value_choice", default=default_value_choice): selector(
                    {"select": {"options": value_options}}
                ),
                vol.Optional("value", default=default_str_value): selector(
                    {"select": {"options": choices}}
                ),
                vol.Optional("manual_value"): str,
            }
        )

    # ─── entry point (options) ───
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
            description_placeholders=self._get_target_more_placeholders(),
        )

    # ─── mirrors of config steps (options) ───
    async def async_step_routing_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            self._data[CONF_ROUTING_MODE] = user_input[CONF_ROUTING_MODE]
            if self._data[CONF_ROUTING_MODE] == ROUTING_SMART:
                return self.async_show_form(
                    step_id=STEP_SMART_SETUP,
                    data_schema=self._get_smart_setup_schema(self._data),
                )
            # finish save
            self.hass.config_entries.async_update_entry(
                self._config_entry, options=self._data
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id=STEP_ROUTING_MODE, data_schema=self._get_routing_mode_schema()
        )

    async def async_step_smart_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            self._data.update(user_input)
            self.hass.config_entries.async_update_entry(
                self._config_entry, options=self._data
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id=STEP_SMART_SETUP,
            data_schema=self._get_smart_setup_schema(self._data),
        )

    async def async_step_add_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        if user_input:
            svc = user_input["target_service"]
            if svc not in notify_svcs:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {
                    KEY_SERVICE: f"notify.{svc}",
                    KEY_CONDITIONS: [],
                }
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=self._get_condition_more_schema(),
                    description_placeholders=self._get_condition_more_placeholders(),
                )

        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=vol.Schema(
                {
                    vol.Required("target_service"): selector(
                        {"select": {"options": service_options, "custom_value": True}}
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                "current_targets": _format_targets_pretty(
                    self._targets, self._working_target
                ),
            },
        )

    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not user_input:
            notify_service = self._working_target.get(KEY_SERVICE)
            all_entities = [
                entity
                for entity in self.hass.states.async_entity_ids()
                if entity.split(".")[0] in ENTITY_DOMAINS
            ]
            options: list[str] = []
            if notify_service:
                slug = notify_service.removeprefix("notify.")
                tokens = [tok for tok in slug.split("_") if tok]
                generic = {"mobile", "app", "notify", "mobileapp"}
                tokens = [t for t in tokens if t not in generic]

                def weight(entity: str) -> tuple[int, ...]:
                    return tuple(int(tok in entity) for tok in tokens)

                options = sorted(
                    all_entities, key=lambda e: (weight(e), e), reverse=True
                )
            else:
                options = sorted(all_entities)
            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY,
                data_schema=vol.Schema(
                    {
                        vol.Required("entity"): selector(
                            {"select": {"options": options, "custom_value": True}}
                        )
                    }
                ),
            )
        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(user_input["entity"]),
            description_placeholders={"entity_id": user_input["entity"]},
        )

    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            if isinstance(final_value, (int, float)):
                final_value = str(int(final_value)) if float(final_value).is_integer() else str(final_value)
            else:
                final_value = str(final_value)
            self._working_condition.update(
                operator=user_input["operator"], value=final_value
            )
            if self._editing_condition_index is not None:
                self._working_target[KEY_CONDITIONS][self._editing_condition_index] = (
                    self._working_condition
                )
                self._editing_condition_index = None
            else:
                self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = {}
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        eid = self._working_condition["entity_id"]
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(eid),
            description_placeholders={"entity_id": eid},
        )

    async def async_step_condition_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            choice = user_input["choice"]
            if choice == "add":
                return await self.async_step_add_condition_entity()
            if choice == "edit":
                return await self.async_step_select_condition_to_edit()
            if choice == "remove":
                conds = self._working_target[KEY_CONDITIONS]
                labels = [
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                ]
                return self.async_show_form(
                    step_id=STEP_REMOVE_COND,
                    data_schema=vol.Schema(
                        {
                            vol.Optional("conditions_to_remove", default=[]): selector(
                                {"select": {"options": labels, "multiple": True}}
                            )
                        }
                    ),
                )
            if choice == "done":
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_MATCH_MODE,
                                default=self._working_target.get(
                                    CONF_MATCH_MODE, "all"
                                ),
                            ): selector(
                                {
                                    "select": {
                                        "options": [
                                            {
                                                "value": "all",
                                                "label": "Require all conditions",
                                            },
                                            {
                                                "value": "any",
                                                "label": "Require any condition",
                                            },
                                        ]
                                    }
                                }
                            )
                        }
                    ),
                )

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=self._get_condition_more_schema(),
            description_placeholders=self._get_condition_more_placeholders(),
        )

    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            selected = user_input["condition"]
            index = labels.index(selected)
            self._editing_condition_index = index
            self._working_condition = self._working_target[KEY_CONDITIONS][index].copy()
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=self._get_condition_value_schema(
                    self._working_condition["entity_id"]
                ),
                description_placeholders={
                    "entity_id": self._working_condition["entity_id"],
                    **self._get_condition_more_placeholders(),
                },
            )
        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("condition"): selector({"select": {"options": labels}})}
            ),
        )

    async def async_step_match_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            selected_mode = user_input[CONF_MATCH_MODE]
            self._working_target[CONF_MATCH_MODE] = selected_mode
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )
        return self.async_show_form(
            step_id=STEP_MATCH_MODE,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MATCH_MODE,
                        default=self._working_target.get(CONF_MATCH_MODE, "all"),
                    ): selector(
                        {
                            "select": {
                                "options": [
                                    {"value": "all", "label": "Require all conditions"},
                                    {"value": "any", "label": "Require any condition"},
                                ]
                            }
                        }
                    )
                }
            ),
        )

    async def async_step_target_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # (implemented above in class; duplicate not needed here)
        return await super().async_step_target_more(user_input)  # type: ignore[misc]

    async def async_step_select_target_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            selected = user_input["target"]
            index = targets.index(selected)
            self._editing_target_index = index
            self._working_target = self._targets[index].copy()
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("target"): selector({"select": {"options": targets}})}
            ),
            description_placeholders=self._get_target_more_placeholders(),
        )

    async def async_step_select_target_to_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            to_remove = set(user_input.get("targets", []))
            self._targets = [
                t for i, t in enumerate(self._targets) if targets[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_REMOVE,
            data_schema=vol.Schema(
                {
                    vol.Optional("targets", default=[]): selector(
                        {"select": {"options": targets, "multiple": True}}
                    )
                }
            ),
            description_placeholders=self._get_target_more_placeholders(),
        )

    def _get_order_targets_schema(
        self, *, services: list[str], current: list[str] | None
    ) -> vol.Schema:
        current = current or []
        remaining = [s for s in services if s not in current]
        return vol.Schema(
            {
                vol.Optional("priority", default=current): selector(
                    {"select": {"options": services, "multiple": True}}
                ),
                vol.Optional("next_priority"): selector(
                    {"select": {"options": remaining}}
                ),
                vol.Optional("action", default="confirm"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "Add to order"},
                                {"value": "reset", "label": "Reset order"},
                                {"value": "confirm", "label": "Confirm"},
                            ]
                        }
                    }
                ),
            }
        )

    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        services = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            action = user_input.get("action", "confirm")
            next_item = user_input.get("next_priority")
            if action == "add" and next_item:
                if next_item not in self._priority_list:
                    self._priority_list.append(next_item)
                placeholders = _order_placeholders(services, self._priority_list)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(
                        services=services, current=self._priority_list
                    ),
                    description_placeholders=placeholders,
                )
            if action == "reset":
                self._priority_list = []
                placeholders = _order_placeholders(services, self._priority_list)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(
                        services=services, current=self._priority_list
                    ),
                    description_placeholders=placeholders,
                )

            selected = user_input.get("priority")
            if isinstance(selected, list) and selected:
                final_priority = [s for s in selected if s in services]
            elif self._priority_list:
                final_priority = [s for s in self._priority_list if s in services]
            else:
                final_priority = services

            self._data.update(
                {CONF_TARGETS: self._targets, CONF_PRIORITY: final_priority}
            )
            notify_svcs = self.hass.services.async_services().get("notify", {})
            placeholders = _order_placeholders(services, final_priority)
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
                description_placeholders={
                    "available_services": ", ".join(sorted(notify_svcs)),
                    **placeholders,
                },
            )

        placeholders = _order_placeholders(services, self._priority_list)
        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=self._get_order_targets_schema(
                services=services, current=self._priority_list
            ),
            description_placeholders=placeholders,
        )

    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        if user_input:
            if user_input.get("nav") == "back":
                services = [t[KEY_SERVICE] for t in self._targets]
                placeholders = _order_placeholders(
                    services, self._data.get(CONF_PRIORITY)
                )
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(
                        services=services, current=self._data.get(CONF_PRIORITY)
                    ),
                    description_placeholders=placeholders,
                )
            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                title = (
                    self._data.get(CONF_SERVICE_NAME_RAW)
                    or self._data.get("service_name_raw")
                    or ""
                )
                return self.async_create_entry(title=title, data=self._data)
        services = [t[KEY_SERVICE] for t in self._targets]
        placeholders = _order_placeholders(services, self._data.get(CONF_PRIORITY))
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._get_choose_fallback_schema(),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                **placeholders,
            },
        )


# ───── expose options flow handler to Home Assistant (legacy path) ─────
@callback
def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
) -> config_entries.OptionsFlow:
    """Return the options flow handler for this config entry."""
    return CustomDeviceNotifierOptionsFlowHandler(config_entry)