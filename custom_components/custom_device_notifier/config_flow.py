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


def _tokens_for_notify_service(full_service: str) -> list[str]:
    """Extract useful tokens from a notify service like 'notify.mobile_app_fold_7'."""
    slug = full_service.removeprefix("notify.")
    toks = [t for t in slug.split("_") if t]
    generic = {"notify", "mobile", "app", "mobileapp"}
    return [t for t in toks if t not in generic]


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

    # ───────── display helpers ─────────

    def _overview_targets(self) -> str:
        lines: list[str] = []
        for tgt in self._targets:
            svc = tgt.get(KEY_SERVICE, "(unknown)")
            conds = tgt.get(KEY_CONDITIONS, [])
            if conds:
                desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc}: {desc}")
            else:
                lines.append(f"{svc}: (no conditions)")
        if self._working_target.get(KEY_SERVICE):
            svc = self._working_target[KEY_SERVICE]
            conds = self._working_target.get(KEY_CONDITIONS, [])
            if conds:
                desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc} (editing): {desc}")
            else:
                lines.append(f"{svc} (editing): (no conditions)")
        return "\n".join(lines) if lines else "No targets yet"

    def _overview_conditions(self) -> str:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        return (
            "\n".join(f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds)
            or "No conditions yet"
        )

    # ───────── schema builders ─────────

    def _schema_condition_more(self) -> vol.Schema:
        opts = [
            {"value": "add", "label": "➕ Add"},
            {"value": "done", "label": "✅ Done"},
            {"value": "back", "label": "⬅️ Back"},
        ]
        if self._working_target.get(KEY_CONDITIONS):
            opts.insert(1, {"value": "edit", "label": "✏️ Edit"})
            opts.insert(2, {"value": "remove", "label": "➖ Remove"})
        return vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {"select": {"options": opts, "mode": "list"}}
                )
            }
        )

    def _schema_target_more(self) -> vol.Schema:
        """Build the 'Add more targets?' menu with section-ish headers (no unsupported keys)."""
        options: list[dict[str, str]] = []
        if self._targets:
            options.append(
                {
                    "value": "__header_current__",
                    "label": "Current targets (click to edit):",
                }
            )
            for idx, tgt in enumerate(self._targets):
                svc = tgt.get(KEY_SERVICE, "(unknown)")
                options.append({"value": f"edit__{idx}", "label": f"Edit: {svc}"})
            options.append({"value": "__header_other__", "label": "Other options:"})
        options.append({"value": "add", "label": "➕ Add target"})
        if self._targets:
            options.append({"value": "edit", "label": "✏️ Edit target"})
            options.append({"value": "remove", "label": "➖ Remove target"})
        options.append({"value": "done", "label": "✅ Done"})
        options.append({"value": "back", "label": "⬅️ Back"})
        return vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {"select": {"options": options, "mode": "list"}}
                )
            }
        )

    def _schema_order_targets(self) -> vol.Schema:
        opts = [t[KEY_SERVICE] for t in self._targets]
        # Keep classic multi-select so your tests can send `{"priority": [...]}` in one go.
        return vol.Schema(
            {
                vol.Required("priority", default=opts): selector(
                    {"select": {"options": opts, "multiple": True}}
                )
            }
        )

    def _schema_choose_fallback(self) -> vol.Schema:
        notify_svcs = self.hass.services.async_services().get("notify", {})
        return vol.Schema(
            {
                vol.Required(
                    "fallback",
                    default=(
                        self._targets[0][KEY_SERVICE].removeprefix("notify.")
                        if self._targets
                        else ""
                    ),
                ): selector(
                    {"select": {"options": sorted(notify_svcs), "custom_value": True}}
                )
            }
        )

    def _schema_condition_value(self, entity_id: str) -> vol.Schema:
        st = self.hass.states.get(entity_id)
        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        prev_op: str | None = None
        prev_val: str | None = None
        use_prev = False
        if (
            self._working_condition
            and self._working_condition.get("entity_id") == entity_id
        ):
            prev_op = self._working_condition.get("operator")
            prev_val = self._working_condition.get("value")
            use_prev = prev_op is not None and prev_val is not None

        if is_num:
            num_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in entity_id
                else {"number": {}}
            )
            default_operator = prev_op or ">"
            # value_choice defaults: prefer manual; show current in label
            value_choice_opts = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}"
                    if st
                    else "Current (unknown)",
                },
            ]
            default_value_choice = (
                "current"
                if (use_prev and st and str(prev_val) == str(st.state))
                else "manual"
            )

            # numeric default as float
            if use_prev and prev_val is not None:
                try:
                    default_val = float(prev_val)
                except (TypeError, ValueError):
                    default_val = float(st.state) if st else 0.0
            else:
                default_val = float(st.state) if st else 0.0

            return vol.Schema(
                {
                    vol.Required("operator", default=default_operator): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required(
                        "value_choice", default=default_value_choice
                    ): selector({"select": {"options": value_choice_opts}}),
                    vol.Optional("value", default=default_val): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )

        # string-ish: build options
        opt_vals: list[str] = ["unknown or unavailable"]
        if st:
            opt_vals.append(st.state)
        opt_vals.extend(["unknown", "unavailable"])
        if (
            "_last_update_trigger" in entity_id
            and "android.intent.action.ACTION_SHUTDOWN" not in opt_vals
        ):
            opt_vals.append("android.intent.action.ACTION_SHUTDOWN")
        # dedupe preserve order
        seen = set()
        uniq = [x for x in opt_vals if not (x in seen or seen.add(x))]

        default_operator = prev_op or "=="
        value_choice_opts = [
            {"value": "manual", "label": "Enter manually"},
            {
                "value": "current",
                "label": f"Current state: {st.state}" if st else "Current (unknown)",
            },
        ]
        default_value_choice = (
            "current"
            if (use_prev and st and str(prev_val) == str(st.state))
            else "manual"
        )

        if use_prev and prev_val in uniq:
            default_str_val = prev_val
        else:
            default_str_val = uniq[0] if uniq else ""

        # custom labels
        select_choices = [
            {
                "value": v,
                "label": (
                    "Shutdown as Last Update"
                    if v == "android.intent.action.ACTION_SHUTDOWN"
                    else v
                ),
            }
            for v in uniq
        ]

        return vol.Schema(
            {
                vol.Required("operator", default=default_operator): selector(
                    {"select": {"options": _OPS_STR}}
                ),
                vol.Required("value_choice", default=default_value_choice): selector(
                    {"select": {"options": value_choice_opts}}
                ),
                vol.Optional("value", default=default_str_val): selector(
                    {"select": {"options": select_choices}}
                ),
                vol.Optional("manual_value"): str,
            }
        )

    # ───────── STEP: user ─────────
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

    # ───────── STEP: add_target ─────────
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
                    data_schema=self._schema_condition_more(),
                    description_placeholders={
                        "current_conditions": self._overview_conditions()
                    },
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
                "current_targets": self._overview_targets(),
            },
        )

    # ─── STEP: add_condition_entity ───
    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not user_input:
            all_entities = [
                ent
                for ent in self.hass.states.async_entity_ids()
                if ent.split(".")[0] in ENTITY_DOMAINS
            ]
            options: list[str]
            if svc := self._working_target.get(KEY_SERVICE):
                tokens = _tokens_for_notify_service(svc)

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
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
            )

        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._schema_condition_value(user_input["entity"]),
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    # ─── STEP: add_condition_value ───
    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            chosen = user_input.get("manual_value") or user_input.get("value")
            # Normalize to string; keep ints clean
            if isinstance(chosen, float) and chosen.is_integer():
                final_value = str(int(chosen))
            else:
                final_value = str(chosen)

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
                data_schema=self._schema_condition_more(),
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
            )

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._schema_condition_value(
                self._working_condition["entity_id"]
            ),
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    # ─── STEP: condition_more ───
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
                conds = self._working_target.get(KEY_CONDITIONS, [])
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
                    description_placeholders={
                        "current_conditions": self._overview_conditions()
                    },
                )
            if choice == "back":
                # Back to add_target or target_more depending on context
                if self._editing_target_index is not None:
                    return self.async_show_form(
                        step_id=STEP_TARGET_MORE,
                        data_schema=self._schema_target_more(),
                        description_placeholders={
                            "current_targets": self._overview_targets()
                        },
                    )
                return await self.async_step_add_target()
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
                    description_placeholders={
                        "current_conditions": self._overview_conditions()
                    },
                )

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=self._schema_condition_more(),
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    # ─── STEP: remove_condition ───
    async def async_step_remove_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c for i, c in enumerate(conds) if labels[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._schema_condition_more(),
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
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
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    # ─── STEP: select_condition_to_edit ───
    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            selected = user_input["condition"]
            idx = labels.index(selected)
            self._editing_condition_index = idx
            self._working_condition = self._working_target[KEY_CONDITIONS][idx].copy()
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=self._schema_condition_value(
                    self._working_condition["entity_id"]
                ),
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
            )

        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("condition"): selector({"select": {"options": labels}})}
            ),
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    # ─── STEP: match_mode ───
    async def async_step_match_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            selected = user_input[CONF_MATCH_MODE]
            self._working_target[CONF_MATCH_MODE] = selected
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._schema_target_more(),
                description_placeholders={"current_targets": self._overview_targets()},
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
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    # ─── STEP: target_more ───
    async def async_step_target_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            nxt = user_input["next"]
            if nxt.startswith("__header_"):
                return self.async_show_form(
                    step_id=STEP_TARGET_MORE,
                    data_schema=self._schema_target_more(),
                    description_placeholders={
                        "current_targets": self._overview_targets()
                    },
                )
            if nxt.startswith("edit__"):
                try:
                    idx = int(nxt.split("__", 1)[1])
                except ValueError:
                    idx = None
                if idx is not None and 0 <= idx < len(self._targets):
                    self._editing_target_index = idx
                    self._working_target = self._targets[idx].copy()
                    return self.async_show_form(
                        step_id=STEP_COND_MORE,
                        data_schema=self._schema_condition_more(),
                        description_placeholders={
                            "current_conditions": self._overview_conditions()
                        },
                    )
                return self.async_step_target_more(None)
            if nxt == "add":
                return await self.async_step_add_target()
            if nxt == "edit":
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                return await self.async_step_select_target_to_remove()
            if nxt == "back":
                # If we just finished conditions, "back" returns there; otherwise go to add_target
                if self._working_target:
                    return self.async_show_form(
                        step_id=STEP_COND_MORE,
                        data_schema=self._schema_condition_more(),
                        description_placeholders={
                            "current_conditions": self._overview_conditions()
                        },
                    )
                return await self.async_step_add_target()
            if nxt == "done":
                # classic single-step priority page to satisfy tests
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._schema_order_targets(),
                    description_placeholders={
                        "current_targets": self._overview_targets()
                    },
                )

        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._schema_target_more(),
            description_placeholders={"current_targets": self._overview_targets()},
        )

    # ─── STEP: select_target_to_edit ───
    async def async_step_select_target_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            sel = user_input["target"]
            idx = targets.index(sel)
            self._editing_target_index = idx
            self._working_target = self._targets[idx].copy()
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._schema_condition_more(),
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
            )

        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("target"): selector({"select": {"options": targets}})}
            ),
            description_placeholders={"current_targets": self._overview_targets()},
        )

    # ─── STEP: select_target_to_remove ───
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
                data_schema=self._schema_target_more(),
                description_placeholders={"current_targets": self._overview_targets()},
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
            description_placeholders={"current_targets": self._overview_targets()},
        )

    # ─── STEP: order_targets (classic: accepts 'priority') ───
    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._targets:
            return self.async_abort(reason="no_targets")

        if user_input:
            order = user_input["priority"]
            # Reorder internal list by the provided order, then append any stragglers
            ordered: list[dict[str, Any]] = []
            remaining = list(self._targets)
            for svc in order:
                for tgt in list(remaining):
                    if tgt[KEY_SERVICE] == svc:
                        ordered.append(tgt)
                        remaining.remove(tgt)
                        break
            ordered.extend(remaining)
            self._targets = ordered
            self._data.update({CONF_TARGETS: self._targets, CONF_PRIORITY: order})
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._schema_choose_fallback(),
                errors={},
            )

        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=self._schema_order_targets(),
            description_placeholders={"current_targets": self._overview_targets()},
        )

    # ─── STEP: choose_fallback ───
    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        if user_input:
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

        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._schema_choose_fallback(),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(sorted(notify_svcs))
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return CustomDeviceNotifierOptionsFlowHandler(config_entry)


class CustomDeviceNotifierOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow mirrors the config flow behavior so tests and UX are consistent."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._data = dict(config_entry.options or config_entry.data).copy()
        self._targets = list(self._data.get(CONF_TARGETS, [])).copy()
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None

    # reuse helpers from the config flow via small wrappers
    def _overview_targets(self) -> str:
        lines: list[str] = []
        for tgt in self._targets:
            svc = tgt.get(KEY_SERVICE, "(unknown)")
            conds = tgt.get(KEY_CONDITIONS, [])
            if conds:
                lines.append(
                    f"{svc}: "
                    + "; ".join(
                        f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                    )
                )
            else:
                lines.append(f"{svc}: (no conditions)")
        if self._working_target.get(KEY_SERVICE):
            svc = self._working_target[KEY_SERVICE]
            conds = self._working_target.get(KEY_CONDITIONS, [])
            if conds:
                lines.append(
                    f"{svc} (editing): "
                    + "; ".join(
                        f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                    )
                )
            else:
                lines.append(f"{svc} (editing): (no conditions)")
        return "\n".join(lines) if lines else "No targets yet"

    def _overview_conditions(self) -> str:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        return (
            "\n".join(f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds)
            or "No conditions yet"
        )

    # ── Options entry points ──

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=CustomDeviceNotifierConfigFlow._schema_target_more(self),  # type: ignore
            description_placeholders={"current_targets": self._overview_targets()},
        )

    async def async_step_add_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        errors: dict[str, str] = {}

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
                    data_schema=CustomDeviceNotifierConfigFlow._schema_condition_more(
                        self
                    ),  # type: ignore
                    description_placeholders={
                        "current_conditions": self._overview_conditions()
                    },
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
                "current_targets": self._overview_targets(),
            },
        )

    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not user_input:
            all_entities = [
                ent
                for ent in self.hass.states.async_entity_ids()
                if ent.split(".")[0] in ENTITY_DOMAINS
            ]
            if svc := self._working_target.get(KEY_SERVICE):
                tokens = _tokens_for_notify_service(svc)

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
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
            )

        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=CustomDeviceNotifierConfigFlow._schema_condition_value(
                self, user_input["entity"]
            ),  # type: ignore
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            chosen = user_input.get("manual_value") or user_input.get("value")
            if isinstance(chosen, float) and chosen.is_integer():
                final_value = str(int(chosen))
            else:
                final_value = str(chosen)

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
                data_schema=CustomDeviceNotifierConfigFlow._schema_condition_more(self),  # type: ignore
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
            )

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=CustomDeviceNotifierConfigFlow._schema_condition_value(
                self, self._working_condition["entity_id"]
            ),  # type: ignore
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
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
                conds = self._working_target.get(KEY_CONDITIONS, [])
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
                    description_placeholders={
                        "current_conditions": self._overview_conditions()
                    },
                )
            if choice == "back":
                return self.async_show_form(
                    step_id=STEP_TARGET_MORE,
                    data_schema=CustomDeviceNotifierConfigFlow._schema_target_more(
                        self
                    ),  # type: ignore
                    description_placeholders={
                        "current_targets": self._overview_targets()
                    },
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
                    description_placeholders={
                        "current_conditions": self._overview_conditions()
                    },
                )

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=CustomDeviceNotifierConfigFlow._schema_condition_more(self),  # type: ignore
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    async def async_step_remove_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c for i, c in enumerate(conds) if labels[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=CustomDeviceNotifierConfigFlow._schema_condition_more(self),  # type: ignore
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
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
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            selected = user_input["condition"]
            idx = labels.index(selected)
            self._editing_condition_index = idx
            self._working_condition = self._working_target[KEY_CONDITIONS][idx].copy()
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=CustomDeviceNotifierConfigFlow._schema_condition_value(
                    self, self._working_condition["entity_id"]
                ),  # type: ignore
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
            )

        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("condition"): selector({"select": {"options": labels}})}
            ),
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    async def async_step_match_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            selected = user_input[CONF_MATCH_MODE]
            self._working_target[CONF_MATCH_MODE] = selected
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=CustomDeviceNotifierConfigFlow._schema_target_more(self),  # type: ignore
                description_placeholders={"current_targets": self._overview_targets()},
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
            description_placeholders={
                "current_conditions": self._overview_conditions()
            },
        )

    async def async_step_target_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            nxt = user_input["next"]
            if nxt.startswith("__header_"):
                return self.async_show_form(
                    step_id=STEP_TARGET_MORE,
                    data_schema=CustomDeviceNotifierConfigFlow._schema_target_more(
                        self
                    ),  # type: ignore
                    description_placeholders={
                        "current_targets": self._overview_targets()
                    },
                )
            if nxt.startswith("edit__"):
                try:
                    idx = int(nxt.split("__", 1)[1])
                except ValueError:
                    idx = None
                if idx is not None and 0 <= idx < len(self._targets):
                    self._editing_target_index = idx
                    self._working_target = self._targets[idx].copy()
                    return self.async_show_form(
                        step_id=STEP_COND_MORE,
                        data_schema=CustomDeviceNotifierConfigFlow._schema_condition_more(
                            self
                        ),  # type: ignore
                        description_placeholders={
                            "current_conditions": self._overview_conditions()
                        },
                    )
                return await self.async_step_target_more(None)
            if nxt == "add":
                return await self.async_step_add_target()
            if nxt == "edit":
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                return await self.async_step_select_target_to_remove()
            if nxt == "back":
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=CustomDeviceNotifierConfigFlow._schema_condition_more(
                        self
                    ),  # type: ignore
                    description_placeholders={
                        "current_conditions": self._overview_conditions()
                    },
                )
            if nxt == "done":
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=CustomDeviceNotifierConfigFlow._schema_order_targets(
                        self
                    ),  # type: ignore
                    description_placeholders={
                        "current_targets": self._overview_targets()
                    },
                )

        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=CustomDeviceNotifierConfigFlow._schema_target_more(self),  # type: ignore
            description_placeholders={"current_targets": self._overview_targets()},
        )

    async def async_step_select_target_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            sel = user_input["target"]
            idx = targets.index(sel)
            self._editing_target_index = idx
            self._working_target = self._targets[idx].copy()
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=CustomDeviceNotifierConfigFlow._schema_condition_more(self),  # type: ignore
                description_placeholders={
                    "current_conditions": self._overview_conditions()
                },
            )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("target"): selector({"select": {"options": targets}})}
            ),
            description_placeholders={"current_targets": self._overview_targets()},
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
                data_schema=CustomDeviceNotifierConfigFlow._schema_target_more(self),  # type: ignore
                description_placeholders={"current_targets": self._overview_targets()},
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
            description_placeholders={"current_targets": self._overview_targets()},
        )

    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._targets:
            return self.async_abort(reason="no_targets")

        if user_input:
            order = user_input["priority"]
            ordered: list[dict[str, Any]] = []
            remaining = list(self._targets)
            for svc in order:
                for tgt in list(remaining):
                    if tgt[KEY_SERVICE] == svc:
                        ordered.append(tgt)
                        remaining.remove(tgt)
                        break
            ordered.extend(remaining)
            self._targets = ordered
            self._data.update({CONF_TARGETS: self._targets, CONF_PRIORITY: order})
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=CustomDeviceNotifierConfigFlow._schema_choose_fallback(
                    self
                ),  # type: ignore
                errors={},
            )

        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=CustomDeviceNotifierConfigFlow._schema_order_targets(self),  # type: ignore
            description_placeholders={"current_targets": self._overview_targets()},
        )

    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        if user_input:
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

        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=CustomDeviceNotifierConfigFlow._schema_choose_fallback(self),  # type: ignore
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(sorted(notify_svcs))
            },
        )


@callback
def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
) -> config_entries.OptionsFlow:
    return CustomDeviceNotifierOptionsFlowHandler(config_entry)
