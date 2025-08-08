# custom_components/custom_device_notifier/config_flow.py
from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Optional, cast

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

# ───────────────────────────── constants ──────────────────────────────
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

_BACK = "__cdnotifier_back__"

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

_StepCallable = Callable[
    [Optional[dict[str, Any]]], Coroutine[Any, Any, ConfigFlowResult]
]


# ───────────────────────────── nav mixin ──────────────────────────────
class _NavMixin:
    def _nav_init(self) -> None:
        self._nav_stack: list[tuple[str, dict[str, Any]]] = []

    def _snapshot_state(self) -> dict[str, Any]:
        return {
            "_data": getattr(self, "_data", {}).copy(),
            "_targets": [t.copy() for t in getattr(self, "_targets", [])],
            "_working_target": getattr(self, "_working_target", {}).copy(),
            "_working_condition": getattr(self, "_working_condition", {}).copy(),
            "_editing_target_index": getattr(self, "_editing_target_index", None),
            "_editing_condition_index": getattr(self, "_editing_condition_index", None),
            "_ordering_targets_remaining": (
                None
                if getattr(self, "_ordering_targets_remaining", None) is None
                else list(getattr(self, "_ordering_targets_remaining"))
            ),
            "_priority_list": (
                None
                if getattr(self, "_priority_list", None) is None
                else list(getattr(self, "_priority_list"))
            ),
        }

    def _restore_state(self, snap: dict[str, Any]) -> None:
        for k, v in snap.items():
            setattr(self, k, v)

    def _push(self, current_step_id: str) -> None:
        self._nav_stack.append((current_step_id, self._snapshot_state()))

    async def _go_back(self) -> ConfigFlowResult:
        if not self._nav_stack:
            return await self._call_step(STEP_USER)
        prev_step, state = self._nav_stack.pop()
        self._restore_state(state)
        return await self._call_step(prev_step)

    async def _call_step(self, step_id: str) -> ConfigFlowResult:
        step_fn = cast(_StepCallable, getattr(self, f"async_step_{step_id}"))
        return await step_fn(None)

    @staticmethod
    def _with_back_option(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"value": _BACK, "label": "⬅ Back"}] + options


# ───────────────────────── config flow (create) ───────────────────────
class CustomDeviceNotifierConfigFlow(
    _NavMixin, config_entries.ConfigFlow, domain=DOMAIN
):
    VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._targets: list[dict[str, Any]] = []
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None
        self._ordering_targets_remaining: list[str] | None = None
        self._priority_list: list[str] | None = None
        self._nav_init()

    # ───────── display helpers ─────────
    def _get_targets_overview(self) -> str:
        lines: list[str] = []
        for tgt in self._targets:
            svc = tgt.get(KEY_SERVICE, "(unknown)")
            conds = tgt.get(KEY_CONDITIONS, [])
            if conds:
                cond_desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc}: {cond_desc}")
            else:
                lines.append(f"{svc}: (no conditions)")
        if self._working_target.get(KEY_SERVICE):
            svc = self._working_target[KEY_SERVICE]
            conds = self._working_target.get(KEY_CONDITIONS, [])
            if conds:
                cond_desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc} (editing): {cond_desc}")
            else:
                lines.append(f"{svc} (editing): (no conditions)")
        return "\n".join(lines) if lines else "No targets yet"

    def _get_target_names_overview(self) -> str:
        names: list[str] = [t.get(KEY_SERVICE, "(unknown)") for t in self._targets]
        if self._working_target.get(KEY_SERVICE):
            names.append(self._working_target[KEY_SERVICE] + " (editing)")
        return "\n".join(names) if names else "No targets yet"

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        formatted = "\n".join(
            f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds
        )
        return {"current_conditions": formatted or "No conditions yet"}

    def _get_target_more_placeholders(self) -> dict[str, str]:
        return {"current_targets": self._get_target_names_overview()}

    # ───────── schema helpers ─────────
    def _schema_add_target(self) -> vol.Schema:
        notify_svcs = self.hass.services.async_services().get("notify", {})
        opts = self._with_back_option(
            [{"value": s, "label": s} for s in sorted(notify_svcs)]
        )
        return vol.Schema(
            {
                vol.Required("target_service"): selector(
                    {"select": {"options": opts, "custom_value": True}}
                )
            }
        )

    def _schema_condition_more(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._working_target.get(KEY_CONDITIONS):
            options.insert(1, {"value": "edit", "label": "✏️ Edit"})
            options.insert(2, {"value": "remove", "label": "➖ Remove"})
        options = self._with_back_option(options)
        return vol.Schema(
            {vol.Required("choice"): selector({"select": {"options": options}})}
        )

    def _schema_select_condition_to_edit(self) -> vol.Schema:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        opts = self._with_back_option(
            [{"value": label, "label": label} for label in labels]
        )
        return vol.Schema(
            {vol.Required("condition"): selector({"select": {"options": opts}})}
        )

    def _schema_remove_conditions(self) -> vol.Schema:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        # No separate nav field — Back is handled on the previous page
        return vol.Schema(
            {
                vol.Optional("conditions_to_remove", default=[]): selector(
                    {"select": {"options": labels, "multiple": True}}
                )
            }
        )

    def _schema_match_mode(self) -> vol.Schema:
        opts = self._with_back_option(
            [
                {"value": "all", "label": "Require all conditions"},
                {"value": "any", "label": "Require any condition"},
            ]
        )
        default_val = self._working_target.get(CONF_MATCH_MODE, "all")
        return vol.Schema(
            {
                vol.Required(CONF_MATCH_MODE, default=default_val): selector(
                    {"select": {"options": opts}}
                )
            }
        )

    def _schema_target_more(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add target"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._targets:
            options.insert(1, {"value": "edit", "label": "✏️ Edit target"})
            options.insert(2, {"value": "remove", "label": "➖ Remove target"})
        options = self._with_back_option(options)
        return vol.Schema(
            {vol.Required("next"): selector({"select": {"options": options}})}
        )

    def _schema_select_target_to_edit(self) -> vol.Schema:
        targets = [t[KEY_SERVICE] for t in self._targets]
        opts = self._with_back_option([{"value": t, "label": t} for t in targets])
        return vol.Schema(
            {vol.Required("target"): selector({"select": {"options": opts}})}
        )

    def _schema_select_target_to_remove(self) -> vol.Schema:
        targets = [t[KEY_SERVICE] for t in self._targets]
        return vol.Schema(
            {
                vol.Optional("targets", default=[]): selector(
                    {"select": {"options": targets, "multiple": True}}
                )
            }
        )

    def _schema_order_targets(self) -> vol.Schema:
        """Support BOTH classic multi-select (`priority`) and progressive (`target`)."""
        remaining = self._ordering_targets_remaining or [
            t[KEY_SERVICE] for t in self._targets
        ]
        progressive_opts = self._with_back_option(
            [{"value": s, "label": s} for s in remaining]
        )
        classic_opts = [t[KEY_SERVICE] for t in self._targets]
        return vol.Schema(
            {
                vol.Optional("target"): selector(
                    {"select": {"options": progressive_opts}}
                ),
                vol.Optional("priority", default=classic_opts): selector(
                    {"select": {"options": classic_opts, "multiple": True}}
                ),
            }
        )

    def _placeholder_ordering(self) -> dict[str, str]:
        current = ", ".join(self._priority_list or []) or "(none yet)"
        return {"current_order": current}

    def _schema_choose_fallback(self) -> vol.Schema:
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        default_fb = (
            self._targets[0][KEY_SERVICE].removeprefix("notify.")
            if self._targets
            else ""
        )
        opts = self._with_back_option(
            [{"value": s, "label": s} for s in service_options]
        )
        default_val = default_fb or _BACK
        return vol.Schema(
            {
                vol.Required("fallback", default=default_val): selector(
                    {"select": {"options": opts}}
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
            num_value_options = [
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
                    ): selector({"select": {"options": num_value_options}}),
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
        str_value_options = [
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
        default_str_value = (
            prev_value
            if (use_prev and prev_value in uniq)
            else (uniq[0] if uniq else "")
        )

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
                    {"select": {"options": str_value_options}}
                ),
                vol.Optional("value", default=default_str_value): selector(
                    {"select": {"options": choices}}
                ),
                vol.Optional("manual_value"): str,
            }
        )

    # ───────── steps ─────────
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP user | input=%s", user_input)
        if user_input:
            raw = user_input["service_name_raw"].strip()
            slug = slugify(raw) or "custom_notifier"
            await self.async_set_unique_id(slug)
            self._abort_if_unique_id_configured()
            self._data.update({CONF_SERVICE_NAME_RAW: raw, CONF_SERVICE_NAME: slug})
            self._push(STEP_USER)
            return await self.async_step_add_target()

        return self.async_show_form(
            step_id=STEP_USER,
            data_schema=vol.Schema({vol.Required("service_name_raw"): str}),
        )

    async def async_step_add_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_target | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        if user_input:
            if user_input.get("target_service") == _BACK:
                return await self._go_back()
            svc = user_input["target_service"]
            if svc not in notify_svcs:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {
                    KEY_SERVICE: f"notify.{svc}",
                    KEY_CONDITIONS: [],
                }
                self._push(STEP_ADD_TARGET)
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=self._schema_condition_more(),
                    description_placeholders=self._get_condition_more_placeholders(),
                )

        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=self._schema_add_target(),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(sorted(notify_svcs)),
                "current_targets": self._get_targets_overview(),
            },
        )

    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_entity | input=%s", user_input)
        if user_input:
            if user_input.get("entity") == _BACK:
                return await self._go_back()
            self._working_condition = {"entity_id": user_input["entity"]}
            self._push(STEP_ADD_COND_ENTITY)
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=self._schema_condition_value(user_input["entity"]),
            )

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

            options = sorted(all_entities, key=lambda e: (weight(e), e), reverse=True)
        else:
            options = sorted(all_entities)

        opts = [{"value": _BACK, "label": "⬅ Back"}] + [
            {"value": e, "label": e} for e in options
        ]
        return self.async_show_form(
            step_id=STEP_ADD_COND_ENTITY,
            data_schema=vol.Schema(
                {
                    vol.Required("entity"): selector(
                        {"select": {"options": opts, "custom_value": True}}
                    )
                }
            ),
        )

    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_value | input=%s", user_input)
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            if (
                isinstance(final_value, (int, float))
                and getattr(final_value, "is_integer", lambda: False)()
            ):
                final_value = str(int(final_value))
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
            self._push(STEP_ADD_COND_VALUE)
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._schema_condition_more(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._schema_condition_value(
                self._working_condition["entity_id"]
            ),
        )

    async def async_step_condition_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP condition_more | input=%s", user_input)
        if user_input:
            choice = user_input["choice"]
            if choice == _BACK:
                return await self._go_back()
            if choice == "add":
                self._push(STEP_COND_MORE)
                return await self.async_step_add_condition_entity()
            if choice == "edit":
                self._push(STEP_COND_MORE)
                return await self.async_step_select_condition_to_edit()
            if choice == "remove":
                self._push(STEP_COND_MORE)
                conds = self._working_target.get(KEY_CONDITIONS, [])
                if not conds:
                    return self.async_show_form(
                        step_id=STEP_COND_MORE,
                        data_schema=self._schema_condition_more(),
                        description_placeholders=self._get_condition_more_placeholders(),
                    )
                return self.async_show_form(
                    step_id=STEP_REMOVE_COND,
                    data_schema=self._schema_remove_conditions(),
                )
            if choice == "done":
                self._push(STEP_COND_MORE)
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE, data_schema=self._schema_match_mode()
                )

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=self._schema_condition_more(),
            description_placeholders=self._get_condition_more_placeholders(),
        )

    async def async_step_remove_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP remove_condition | input=%s", user_input)
        if user_input:
            conds = self._working_target.get(KEY_CONDITIONS, [])
            labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c for i, c in enumerate(conds) if labels[i] not in to_remove
            ]
            self._push(STEP_REMOVE_COND)
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._schema_condition_more(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_REMOVE_COND, data_schema=self._schema_remove_conditions()
        )

    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_condition_to_edit | input=%s", user_input)
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            sel = user_input["condition"]
            if sel == _BACK:
                return await self._go_back()
            if sel in labels:
                index = labels.index(sel)
                self._editing_condition_index = index
                self._working_condition = self._working_target[KEY_CONDITIONS][
                    index
                ].copy()
                self._push(STEP_SELECT_COND_TO_EDIT)
                return self.async_show_form(
                    step_id=STEP_ADD_COND_VALUE,
                    data_schema=self._schema_condition_value(
                        self._working_condition["entity_id"]
                    ),
                    description_placeholders=self._get_condition_more_placeholders(),
                )
        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=self._schema_select_condition_to_edit(),
        )

    async def async_step_match_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP match_mode | input=%s", user_input)
        if user_input:
            mode = user_input[CONF_MATCH_MODE]
            if mode == _BACK:
                return await self._go_back()
            self._working_target[CONF_MATCH_MODE] = mode
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            self._push(STEP_MATCH_MODE)
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._schema_target_more(),
                description_placeholders=self._get_target_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_MATCH_MODE, data_schema=self._schema_match_mode()
        )

    async def async_step_target_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP target_more | input=%s", user_input)
        if user_input:
            nxt = user_input["next"]
            if nxt == _BACK:
                return await self._go_back()
            if nxt == "add":
                self._push(STEP_TARGET_MORE)
                return self.async_show_form(
                    step_id=STEP_ADD_TARGET,
                    data_schema=self._schema_add_target(),
                    description_placeholders=self._get_target_more_placeholders(),
                )
            if nxt == "edit":
                self._push(STEP_TARGET_MORE)
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                self._push(STEP_TARGET_MORE)
                return await self.async_step_select_target_to_remove()
            if nxt == "done":
                self._ordering_targets_remaining = [
                    t[KEY_SERVICE] for t in self._targets
                ]
                self._priority_list = []
                self._push(STEP_TARGET_MORE)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._schema_order_targets(),
                    description_placeholders=self._placeholder_ordering(),
                )

        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._schema_target_more(),
            description_placeholders=self._get_target_more_placeholders(),
        )

    async def async_step_select_target_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_target_to_edit | input=%s", user_input)
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            sel = user_input["target"]
            if sel == _BACK:
                return await self._go_back()
            if sel in targets:
                idx = targets.index(sel)
                self._editing_target_index = idx
                self._working_target = self._targets[idx].copy()
                self._push(STEP_SELECT_TARGET_TO_EDIT)
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=self._schema_condition_more(),
                    description_placeholders=self._get_condition_more_placeholders(),
                )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=self._schema_select_target_to_edit(),
        )

    async def async_step_select_target_to_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_target_to_remove | input=%s", user_input)
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            to_remove = set(user_input.get("targets", []))
            self._targets = [
                t for i, t in enumerate(self._targets) if targets[i] not in to_remove
            ]
            self._push(STEP_SELECT_TARGET_TO_REMOVE)
            return self.async_show_form(
                step_id=STEP_TARGET_MORE, data_schema=self._schema_target_more()
            )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_REMOVE,
            data_schema=self._schema_select_target_to_remove(),
        )

    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept either classic `priority` or progressive `target`."""
        _LOGGER.debug("STEP order_targets | input=%s", user_input)
        if self._ordering_targets_remaining is None or self._priority_list is None:
            self._ordering_targets_remaining = [t[KEY_SERVICE] for t in self._targets]
            self._priority_list = []

        if user_input:
            # Classic path (tests): full list at once
            if "priority" in user_input and user_input["priority"]:
                self._priority_list = list(user_input["priority"])
                self._ordering_targets_remaining = []
            else:
                # Progressive path
                choice = user_input.get("target")
                if choice == _BACK:
                    return await self._go_back()
                if choice in self._ordering_targets_remaining:
                    self._priority_list.append(choice)
                    self._ordering_targets_remaining.remove(choice)

        if self._ordering_targets_remaining:
            return self.async_show_form(
                step_id=STEP_ORDER_TARGETS,
                data_schema=self._schema_order_targets(),
                description_placeholders=self._placeholder_ordering(),
            )

        # Apply ordering
        ordered: list[dict[str, Any]] = []
        for svc in self._priority_list:
            for tgt in list(self._targets):
                if tgt[KEY_SERVICE] == svc:
                    ordered.append(tgt)
                    self._targets.remove(tgt)
                    break
        ordered.extend(self._targets)
        self._targets = ordered
        self._data.update(
            {CONF_TARGETS: self._targets, CONF_PRIORITY: self._priority_list}
        )

        # Reset helpers
        self._ordering_targets_remaining = None
        self._priority_list = None

        self._push(STEP_ORDER_TARGETS)
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK, data_schema=self._schema_choose_fallback()
        )

    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP choose_fallback | input=%s", user_input)
        notify_svcs = self.hass.services.async_services().get("notify", {})
        errors: dict[str, str] = {}
        if user_input:
            fb = user_input["fallback"]
            if fb == _BACK:
                return await self._go_back()
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


# ───────────────────────── options flow (edit) ────────────────────────
class CustomDeviceNotifierOptionsFlowHandler(_NavMixin, config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data = dict(config_entry.options or config_entry.data).copy()
        self._targets = list(self._data.get(CONF_TARGETS, [])).copy()
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None
        self._ordering_targets_remaining: list[str] | None = None
        self._priority_list: list[str] | None = None
        self._nav_init()

    # ───────── display helpers ─────────
    def _get_targets_overview(self) -> str:
        lines: list[str] = []
        for tgt in self._targets:
            svc = tgt.get(KEY_SERVICE, "(unknown)")
            conds = tgt.get(KEY_CONDITIONS, [])
            if conds:
                cond_desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc}: {cond_desc}")
            else:
                lines.append(f"{svc}: (no conditions)")
        if self._working_target.get(KEY_SERVICE):
            svc = self._working_target[KEY_SERVICE]
            conds = self._working_target.get(KEY_CONDITIONS, [])
            if conds:
                cond_desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc} (editing): {cond_desc}")
            else:
                lines.append(f"{svc} (editing): (no conditions)")
        return "\n".join(lines) if lines else "No targets yet"

    def _get_target_names_overview(self) -> str:
        names: list[str] = [t.get(KEY_SERVICE, "(unknown)") for t in self._targets]
        if self._working_target.get(KEY_SERVICE):
            names.append(self._working_target[KEY_SERVICE] + " (editing)")
        return "\n".join(names) if names else "No targets yet"

    def _get_target_more_placeholders(self) -> dict[str, str]:
        return {"current_targets": self._get_targets_overview()}

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        formatted = "\n".join(
            f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds
        )
        return {"current_conditions": formatted or "No conditions yet"}

    # ───────── schema helpers ─────────
    def _schema_add_target(self) -> vol.Schema:
        notify_svcs = self.hass.services.async_services().get("notify", {})
        opts = self._with_back_option(
            [{"value": s, "label": s} for s in sorted(notify_svcs)]
        )
        return vol.Schema(
            {
                vol.Required("target_service"): selector(
                    {"select": {"options": opts, "custom_value": True}}
                )
            }
        )

    def _schema_condition_more(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._working_target.get(KEY_CONDITIONS):
            options.insert(1, {"value": "edit", "label": "✏️ Edit"})
            options.insert(2, {"value": "remove", "label": "➖ Remove"})
        options = self._with_back_option(options)
        return vol.Schema(
            {vol.Required("choice"): selector({"select": {"options": options}})}
        )

    def _schema_select_condition_to_edit(self) -> vol.Schema:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        opts = self._with_back_option(
            [{"value": label, "label": label} for label in labels]
        )
        return vol.Schema(
            {vol.Required("condition"): selector({"select": {"options": opts}})}
        )

    def _schema_remove_conditions(self) -> vol.Schema:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        return vol.Schema(
            {
                vol.Optional("conditions_to_remove", default=[]): selector(
                    {"select": {"options": labels, "multiple": True}}
                )
            }
        )

    def _schema_match_mode(self) -> vol.Schema:
        opts = self._with_back_option(
            [
                {"value": "all", "label": "Require all conditions"},
                {"value": "any", "label": "Require any condition"},
            ]
        )
        default_val = self._working_target.get(CONF_MATCH_MODE, "all")
        return vol.Schema(
            {
                vol.Required(CONF_MATCH_MODE, default=default_val): selector(
                    {"select": {"options": opts}}
                )
            }
        )

    def _schema_target_more(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add target"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._targets:
            options.insert(1, {"value": "edit", "label": "✏️ Edit target"})
            options.insert(2, {"value": "remove", "label": "➖ Remove target"})
        options = self._with_back_option(options)
        return vol.Schema(
            {vol.Required("next"): selector({"select": {"options": options}})}
        )

    def _schema_select_target_to_edit(self) -> vol.Schema:
        targets = [t[KEY_SERVICE] for t in self._targets]
        opts = self._with_back_option([{"value": t, "label": t} for t in targets])
        return vol.Schema(
            {vol.Required("target"): selector({"select": {"options": opts}})}
        )

    def _schema_select_target_to_remove(self) -> vol.Schema:
        targets = [t[KEY_SERVICE] for t in self._targets]
        return vol.Schema(
            {
                vol.Optional("targets", default=[]): selector(
                    {"select": {"options": targets, "multiple": True}}
                )
            }
        )

    def _schema_order_targets(self) -> vol.Schema:
        remaining = self._ordering_targets_remaining or [
            t[KEY_SERVICE] for t in self._targets
        ]
        progressive_opts = self._with_back_option(
            [{"value": s, "label": s} for s in remaining]
        )
        classic_opts = [t[KEY_SERVICE] for t in self._targets]
        return vol.Schema(
            {
                vol.Optional("target"): selector(
                    {"select": {"options": progressive_opts}}
                ),
                vol.Optional("priority", default=classic_opts): selector(
                    {"select": {"options": classic_opts, "multiple": True}}
                ),
            }
        )

    def _placeholder_ordering(self) -> dict[str, str]:
        current = ", ".join(self._priority_list or []) or "(none yet)"
        return {"current_order": current}

    def _schema_choose_fallback(self) -> vol.Schema:
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        default_fb = (
            self._targets[0][KEY_SERVICE].removeprefix("notify.")
            if self._targets
            else ""
        )
        opts = self._with_back_option(
            [{"value": s, "label": s} for s in service_options]
        )
        default_val = default_fb or _BACK
        return vol.Schema(
            {
                vol.Required("fallback", default=default_val): selector(
                    {"select": {"options": opts}}
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
            num_value_options = [
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
                    ): selector({"select": {"options": num_value_options}}),
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
        str_value_options = [
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
        default_str_value = (
            prev_value
            if (use_prev and prev_value in uniq)
            else (uniq[0] if uniq else "")
        )

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
                    {"select": {"options": str_value_options}}
                ),
                vol.Optional("value", default=default_str_value): selector(
                    {"select": {"options": choices}}
                ),
                vol.Optional("manual_value"): str,
            }
        )

    # ───────── steps ─────────
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP init | input=%s", user_input)
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._schema_target_more(),
            description_placeholders=self._get_target_more_placeholders(),
        )

    async def async_step_add_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_target | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        if user_input:
            if user_input.get("target_service") == _BACK:
                return await self._go_back()
            svc = user_input["target_service"]
            if svc not in notify_svcs:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {
                    KEY_SERVICE: f"notify.{svc}",
                    KEY_CONDITIONS: [],
                }
                self._push(STEP_ADD_TARGET)
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=self._schema_condition_more(),
                    description_placeholders=self._get_condition_more_placeholders(),
                )

        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=self._schema_add_target(),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(sorted(notify_svcs)),
                **self._get_target_more_placeholders(),
            },
        )

    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_entity | input=%s", user_input)
        if user_input:
            if user_input.get("entity") == _BACK:
                return await self._go_back()
            self._working_condition = {"entity_id": user_input["entity"]}
            self._push(STEP_ADD_COND_ENTITY)
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=self._schema_condition_value(user_input["entity"]),
            )

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

            options = sorted(all_entities, key=lambda e: (weight(e), e), reverse=True)
        else:
            options = sorted(all_entities)
        opts = [{"value": _BACK, "label": "⬅ Back"}] + [
            {"value": e, "label": e} for e in options
        ]
        return self.async_show_form(
            step_id=STEP_ADD_COND_ENTITY,
            data_schema=vol.Schema(
                {
                    vol.Required("entity"): selector(
                        {"select": {"options": opts, "custom_value": True}}
                    )
                }
            ),
        )

    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_value | input=%s", user_input)
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            if (
                isinstance(final_value, (int, float))
                and getattr(final_value, "is_integer", lambda: False)()
            ):
                final_value = str(int(final_value))
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
            self._push(STEP_ADD_COND_VALUE)
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._schema_condition_more(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._schema_condition_value(
                self._working_condition["entity_id"]
            ),
        )

    async def async_step_condition_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP condition_more | input=%s", user_input)
        if user_input:
            choice = user_input["choice"]
            if choice == _BACK:
                return await self._go_back()
            if choice == "add":
                self._push(STEP_COND_MORE)
                return await self.async_step_add_condition_entity()
            if choice == "edit":
                self._push(STEP_COND_MORE)
                return await self.async_step_select_condition_to_edit()
            if choice == "remove":
                self._push(STEP_COND_MORE)
                return self.async_show_form(
                    step_id=STEP_REMOVE_COND,
                    data_schema=self._schema_remove_conditions(),
                )
            if choice == "done":
                self._push(STEP_COND_MORE)
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE, data_schema=self._schema_match_mode()
                )

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=self._schema_condition_more(),
            description_placeholders=self._get_condition_more_placeholders(),
        )

    async def async_step_remove_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP remove_condition | input=%s", user_input)
        if user_input:
            conds = self._working_target.get(KEY_CONDITIONS, [])
            labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c for i, c in enumerate(conds) if labels[i] not in to_remove
            ]
            self._push(STEP_REMOVE_COND)
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._schema_condition_more(),
                description_placeholders=self._get_condition_more_placeholders(),
            )
        return self.async_show_form(
            step_id=STEP_REMOVE_COND, data_schema=self._schema_remove_conditions()
        )

    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_condition_to_edit | input=%s", user_input)
        conds = self._working_target.get(KEY_CONDITIONS, [])
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            sel = user_input["condition"]
            if sel == _BACK:
                return await self._go_back()
            if sel in labels:
                index = labels.index(sel)
                self._editing_condition_index = index
                self._working_condition = self._working_target[KEY_CONDITIONS][
                    index
                ].copy()
                self._push(STEP_SELECT_COND_TO_EDIT)
                return self.async_show_form(
                    step_id=STEP_ADD_COND_VALUE,
                    data_schema=self._schema_condition_value(
                        self._working_condition["entity_id"]
                    ),
                    description_placeholders=self._get_condition_more_placeholders(),
                )
        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=self._schema_select_condition_to_edit(),
        )

    async def async_step_match_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP match_mode | input=%s", user_input)
        if user_input:
            mode = user_input[CONF_MATCH_MODE]
            if mode == _BACK:
                return await self._go_back()
            self._working_target[CONF_MATCH_MODE] = mode
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            self._push(STEP_MATCH_MODE)
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._schema_target_more(),
                description_placeholders={
                    "current_targets": self._get_target_names_overview()
                },
            )
        return self.async_show_form(
            step_id=STEP_MATCH_MODE, data_schema=self._schema_match_mode()
        )

    async def async_step_target_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP target_more | input=%s", user_input)
        if user_input:
            nxt = user_input["next"]
            if nxt == _BACK:
                return await self._go_back()
            if nxt == "add":
                self._push(STEP_TARGET_MORE)
                return self.async_show_form(
                    step_id=STEP_ADD_TARGET,
                    data_schema=self._schema_add_target(),
                    description_placeholders={
                        "current_targets": self._get_target_names_overview()
                    },
                )
            if nxt == "edit":
                self._push(STEP_TARGET_MORE)
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                self._push(STEP_TARGET_MORE)
                return await self.async_step_select_target_to_remove()
            if nxt == "done":
                self._ordering_targets_remaining = [
                    t[KEY_SERVICE] for t in self._targets
                ]
                self._priority_list = []
                self._push(STEP_TARGET_MORE)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._schema_order_targets(),
                    description_placeholders=self._placeholder_ordering(),
                )
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._schema_target_more(),
            description_placeholders={
                "current_targets": self._get_target_names_overview()
            },
        )

    async def async_step_select_target_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_target_to_edit | input=%s", user_input)
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            sel = user_input["target"]
            if sel == _BACK:
                return await self._go_back()
            if sel in targets:
                idx = targets.index(sel)
                self._editing_target_index = idx
                self._working_target = self._targets[idx].copy()
                self._push(STEP_SELECT_TARGET_TO_EDIT)
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=self._schema_condition_more(),
                    description_placeholders=self._get_condition_more_placeholders(),
                )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=self._schema_select_target_to_edit(),
        )

    async def async_step_select_target_to_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_target_to_remove | input=%s", user_input)
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            to_remove = set(user_input.get("targets", []))
            self._targets = [
                t for i, t in enumerate(self._targets) if targets[i] not in to_remove
            ]
            self._push(STEP_SELECT_TARGET_TO_REMOVE)
            return self.async_show_form(
                step_id=STEP_TARGET_MORE, data_schema=self._schema_target_more()
            )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_REMOVE,
            data_schema=self._schema_select_target_to_remove(),
        )

    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP order_targets (options) | input=%s", user_input)
        if self._ordering_targets_remaining is None or self._priority_list is None:
            self._ordering_targets_remaining = [t[KEY_SERVICE] for t in self._targets]
            self._priority_list = []

        if user_input:
            if "priority" in user_input and user_input["priority"]:
                self._priority_list = list(user_input["priority"])
                self._ordering_targets_remaining = []
            else:
                choice = user_input.get("target")
                if choice == _BACK:
                    return await self._go_back()
                if choice in self._ordering_targets_remaining:
                    self._priority_list.append(choice)
                    self._ordering_targets_remaining.remove(choice)

        if self._ordering_targets_remaining:
            return self.async_show_form(
                step_id=STEP_ORDER_TARGETS,
                data_schema=self._schema_order_targets(),
                description_placeholders=self._placeholder_ordering(),
            )

        ordered: list[dict[str, Any]] = []
        for svc in self._priority_list:
            for tgt in list(self._targets):
                if tgt[KEY_SERVICE] == svc:
                    ordered.append(tgt)
                    self._targets.remove(tgt)
                    break
        ordered.extend(self._targets)
        self._targets = ordered
        self._data.update(
            {CONF_TARGETS: self._targets, CONF_PRIORITY: self._priority_list}
        )

        self._ordering_targets_remaining = None
        self._priority_list = None

        self._push(STEP_ORDER_TARGETS)
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK, data_schema=self._schema_choose_fallback()
        )

    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP choose_fallback | input=%s", user_input)
        notify_svcs = self.hass.services.async_services().get("notify", {})
        errors: dict[str, str] = {}
        if user_input:
            fb = user_input["fallback"]
            if fb == _BACK:
                return await self._go_back()
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


@callback
def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
) -> config_entries.OptionsFlow:
    return CustomDeviceNotifierOptionsFlowHandler(config_entry)
