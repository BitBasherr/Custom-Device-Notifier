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
    CONF_SMART_PHONE_UNLOCK_WINDOW_S,
    DEFAULT_SMART_PHONE_UNLOCK_WINDOW_S,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_SERVICE,
    # routing / smart-select
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
    # NEW
    CONF_SMART_REQUIRE_PHONE_UNLOCKED,
    DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
    # ── Audio / TTS (new) ──
    CONF_TTS_ENABLE,
    CONF_TTS_DEFAULT,
    CONF_TTS_SERVICE,
    CONF_TTS_LANGUAGE,
    CONF_MEDIA_PLAYER_ORDER,
    DEFAULT_TTS_ENABLE,
    DEFAULT_TTS_DEFAULT,
    DEFAULT_TTS_SERVICE,
    DEFAULT_TTS_LANGUAGE,
    # steps
    STEP_USER,
    STEP_ROUTING_MODE,
    STEP_ADD_TARGET,
    STEP_ADD_COND_ENTITY,
    STEP_ADD_COND_VALUE,
    STEP_COND_MORE,
    STEP_REMOVE_COND,
    STEP_SELECT_COND_TO_EDIT,
    STEP_MATCH_MODE,
    STEP_TARGET_MORE,
    STEP_ORDER_TARGETS,
    STEP_CHOOSE_FALLBACK,
    STEP_SELECT_TARGET_TO_EDIT,
    STEP_SELECT_TARGET_TO_REMOVE,
    STEP_SMART_SETUP,
    STEP_SMART_ORDER_PHONES,
    # audio steps
    STEP_AUDIO_SETUP,
    STEP_MEDIA_ORDER,
    CONF_BOOT_STICKY_TARGET_S,
    DEFAULT_BOOT_STICKY_TARGET_S,
    CONF_MSG_ENABLE,
    CONF_MSG_SOURCE_SENSOR,
    CONF_MSG_APPS,
    CONF_MSG_TARGETS,
    CONF_MSG_REPLY_TRANSPORT,
    CONF_MSG_KDECONNECT_DEVICE_ID,
    CONF_MSG_TASKER_EVENT,
    DEFAULT_MSG_ENABLE,
    DEFAULT_MSG_APPS,
    DEFAULT_MSG_REPLY_TRANSPORT,
    DEFAULT_MSG_TASKER_EVENT,
    # Config-flow step id
    STEP_MESSAGES_SETUP,
)

_LOGGER = logging.getLogger(DOMAIN)

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

# Sentinels for insertion controls
_INSERT_TOP = "__TOP__"
_INSERT_BOTTOM = "__BOTTOM__"

# Keys that belong exclusively to one routing mode (used to avoid cross-mode bleed)
CONDITIONAL_KEYS: list[str] = [CONF_TARGETS, CONF_PRIORITY]
SMART_KEYS: list[str] = [
    CONF_SMART_PC_NOTIFY,
    CONF_SMART_PC_SESSION,
    CONF_SMART_PHONE_ORDER,
    CONF_SMART_MIN_BATTERY,
    CONF_SMART_PHONE_FRESH_S,
    CONF_SMART_PC_FRESH_S,
    CONF_SMART_REQUIRE_AWAKE,
    CONF_SMART_REQUIRE_UNLOCKED,
    CONF_SMART_POLICY,
    CONF_SMART_REQUIRE_PHONE_UNLOCKED,
    CONF_SMART_PHONE_UNLOCK_WINDOW_S,
]
# NOTE: Audio/TTS keys persist regardless of routing mode, so they are not wiped.


# ──────────────────────────── helper utils ────────────────────────────────
def _order_placeholders(
    services: list[str], current: list[str] | None
) -> dict[str, str]:
    """Build description placeholders for a priority/order step."""
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
    """Render as a nested Markdown list so wrapped lines hang correctly."""
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
    """Return service names in the 'notify' domain (without 'notify.' prefix)."""
    return sorted(hass.services.async_services().get("notify", {}))


def _tts_services(hass) -> list[str]:
    """Return 'tts' domain services as fully qualified (tts.speak, tts.xxx)."""
    return [f"tts.{s}" for s in sorted(hass.services.async_services().get("tts", {}))]


def _media_players(hass) -> list[str]:
    """Return media_player.* entity_ids (sorted)."""
    return sorted(
        [e for e in hass.states.async_entity_ids() if e.startswith("media_player.")]
    )


def _default_pc_notify(services: list[str]) -> str:
    """Best-effort sensible default for PC notify service (raw name)."""
    tokens = (
        "pc",
        "computer",
        "desktop",
        "laptop",
        "workstation",
        "main_pc",
        "mainpc",
        "tower",
        "macbook",
        "imac",
    )
    for s in services:
        low = s.lower()
        if any(t in low for t in tokens):
            return s
    return services[0] if services else ""


def _insert_items_at(
    current: list[str], items: list[str], anchor: str | None
) -> list[str]:
    """
    Insert items into current before the anchor.

    - anchor == _INSERT_TOP  -> index 0
    - anchor == _INSERT_BOTTOM or None -> end
    - anchor in current -> before that item
    - duplicates are removed by first stripping items from current
    """
    items = [i for i in items if i]  # clean Nones
    if not items:
        return current

    base = [x for x in current if x not in items]
    if anchor == _INSERT_TOP:
        idx = 0
    elif anchor == _INSERT_BOTTOM or anchor is None:
        idx = len(base)
    else:
        try:
            idx = base.index(anchor)
        except ValueError:
            idx = len(base)

    return base[:idx] + items + base[idx:]


def _wipe_keys(d: dict[str, Any], keys: list[str]) -> None:
    for k in keys:
        d.pop(k, None)


def _messages_placeholders(self) -> dict[str, str]:
    return {
        "note": "Mirror notifications from a phone’s Last Notification sensor and allow inline replies."
    }


def _get_messages_setup_schema(
    self, existing: dict[str, Any] | None = None
) -> vol.Schema:
    existing = existing or {}
    notify_options = [f"notify.{s}" for s in _notify_services(self.hass)]
    common_pkgs = [
        "com.google.android.apps.messaging",
        "org.thoughtcrime.securesms",  # Signal
        "com.whatsapp",
        "org.telegram.messenger",
        "com.facebook.orca",  # Messenger
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_MSG_ENABLE,
                default=existing.get(CONF_MSG_ENABLE, DEFAULT_MSG_ENABLE),
            ): selector({"boolean": {}}),
            vol.Required(
                CONF_MSG_SOURCE_SENSOR,
                default=existing.get(CONF_MSG_SOURCE_SENSOR, ""),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                CONF_MSG_APPS,
                default=existing.get(CONF_MSG_APPS, DEFAULT_MSG_APPS),
            ): selector(
                {
                    "select": {
                        "options": common_pkgs,
                        "multiple": True,
                        "custom_value": True,
                    }
                }
            ),
            vol.Optional(
                CONF_MSG_TARGETS,
                default=existing.get(CONF_MSG_TARGETS, []),
            ): selector(
                {
                    "select": {
                        "options": notify_options,
                        "multiple": True,
                        "custom_value": True,
                    }
                }
            ),
            vol.Required(
                CONF_MSG_REPLY_TRANSPORT,
                default=existing.get(
                    CONF_MSG_REPLY_TRANSPORT, DEFAULT_MSG_REPLY_TRANSPORT
                ),
            ): selector(
                {
                    "select": {
                        "options": [
                            {"value": "kdeconnect", "label": "KDE Connect (SMS)"},
                            {"value": "tasker", "label": "Tasker / AutoNotification"},
                        ]
                    }
                }
            ),
            vol.Optional(
                CONF_MSG_KDECONNECT_DEVICE_ID,
                default=existing.get(CONF_MSG_KDECONNECT_DEVICE_ID, ""),
            ): str,
            vol.Optional(
                CONF_MSG_TASKER_EVENT,
                default=existing.get(CONF_MSG_TASKER_EVENT, DEFAULT_MSG_TASKER_EVENT),
            ): str,
        }
    )


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
        self._priority_list: list[str] = []  # conditional ordering buffer
        self._phone_order_list: list[str] = []  # smart phone-order builder

        # audio/tts
        self._media_order_list: list[str] = []

    # ───────── schema helpers ─────────
    def _get_routing_mode_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(
                    CONF_ROUTING_MODE,
                    default=self._data.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE),
                ): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": ROUTING_CONDITIONAL,
                                    "label": "Regular Prioritization (Targets + Order)",
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

    def _smart_phone_candidates(self) -> list[str]:
        """
        Candidate notify services for the numbered 'phone order' builder.

        Include *all* notify services; phone-like (mobile_app_*) shown first.
        """
        services = _notify_services(self.hass)
        phone_like = [f"notify.{s}" for s in services if s.startswith("mobile_app_")]
        others = [f"notify.{s}" for s in services if not s.startswith("mobile_app_")]

        # Ensure previously chosen items remain selectable (defensive)
        for s in self._phone_order_list:
            if s not in phone_like and s not in others:
                others.append(s)

        return sorted(phone_like) + sorted(others)

    def _smart_pc_candidates(self) -> list[str]:
        """PC / other notify services (non-mobile)."""
        services = _notify_services(self.hass)
        pcs = [f"notify.{s}" for s in services if not s.startswith("mobile_app_")]
        return sorted(pcs)

    def _get_smart_setup_schema(
        self, existing: dict[str, Any] | None = None
    ) -> vol.Schema:
        existing = existing or {}
        services = _notify_services(self.hass)  # raw names without "notify."

        pc_default = existing.get(CONF_SMART_PC_NOTIFY)
        if not pc_default:
            guess = _default_pc_notify(services)
            pc_default = f"notify.{guess}" if guess else ""

        pc_session_default = existing.get(CONF_SMART_PC_SESSION) or (
            f"sensor.{(pc_default or '').removeprefix('notify.').lower()}_sessionstate"
            if pc_default
            else ""
        )

        return vol.Schema(
            {
                vol.Required(
                    CONF_SMART_PC_NOTIFY,
                    default=existing.get(CONF_SMART_PC_NOTIFY, pc_default),
                ): selector(
                    {
                        "select": {
                            "options": self._smart_pc_candidates(),
                            "custom_value": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_SMART_PC_SESSION,
                    default=existing.get(CONF_SMART_PC_SESSION, pc_session_default),
                ): selector({"entity": {"domain": "sensor"}}),
                # Button to jump to the numbered phone-order builder
                vol.Optional("nav"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "reorder_phones", "label": "Reorder phones…"},
                                {"value": "audio", "label": "Audio / TTS setup…"},
                                {"value": "messages", "label": "Messages bridge…"},
                                {"value": "routing", "label": "Choose routing mode…"},
                                {"value": "stay", "label": "Stay here"},
                            ],
                            "custom_value": False,
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
                    CONF_SMART_PHONE_UNLOCK_WINDOW_S,
                    default=existing.get(
                        CONF_SMART_PHONE_UNLOCK_WINDOW_S,
                        DEFAULT_SMART_PHONE_UNLOCK_WINDOW_S,
                    ),
                ): selector({"number": {"min": 30, "max": 7200, "step": 10}}),
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
                vol.Required(
                    CONF_SMART_REQUIRE_PHONE_UNLOCKED,
                    default=existing.get(
                        CONF_SMART_REQUIRE_PHONE_UNLOCKED,
                        DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
                    ),
                ): selector({"boolean": {}}),
                # NEW: Boot-sticky window (seconds). 0 disables.
                vol.Required(
                    CONF_BOOT_STICKY_TARGET_S,
                    default=existing.get(
                        CONF_BOOT_STICKY_TARGET_S, DEFAULT_BOOT_STICKY_TARGET_S
                    ),
                ): selector({"number": {"min": 0, "max": 900, "step": 5}}),
            }
        )

    # ── Audio / TTS helpers ────────────────────────────────────────────
    def _audio_placeholders(self) -> dict[str, str]:
        mp = _media_players(self.hass)
        return _order_placeholders(mp, self._media_order_list)

    def _get_audio_setup_schema(
        self, existing: dict[str, Any] | None = None
    ) -> vol.Schema:
        existing = existing or {}
        services = _tts_services(self.hass)
        if DEFAULT_TTS_SERVICE not in services:
            services = [DEFAULT_TTS_SERVICE] + services  # ensure default present

        return vol.Schema(
            {
                vol.Required(
                    CONF_TTS_ENABLE,
                    default=existing.get(CONF_TTS_ENABLE, DEFAULT_TTS_ENABLE),
                ): selector({"boolean": {}}),
                vol.Required(
                    CONF_TTS_DEFAULT,
                    default=existing.get(CONF_TTS_DEFAULT, DEFAULT_TTS_DEFAULT),
                ): selector({"boolean": {}}),
                vol.Required(
                    CONF_TTS_SERVICE,
                    default=existing.get(CONF_TTS_SERVICE, DEFAULT_TTS_SERVICE),
                ): selector({"select": {"options": services, "custom_value": True}}),
                vol.Optional(
                    CONF_TTS_LANGUAGE,
                    default=existing.get(CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE),
                ): str,
                vol.Optional("nav"): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": "reorder_players",
                                    "label": "Reorder media players…",
                                },
                                {"value": "routing", "label": "Back to routing…"},
                                {"value": "stay", "label": "Stay here"},
                            ],
                            "custom_value": False,
                        }
                    }
                ),
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

    def _insertion_choices(self, current: list[str]) -> list[dict[str, str]]:
        """Choices for 'insert before' control."""
        if not current:
            return [{"value": _INSERT_BOTTOM, "label": "Bottom (first position)"}]
        choices: list[dict[str, str]] = [
            {"value": _INSERT_TOP, "label": "Top (before #1)"},
        ]
        for i, s in enumerate(current, 1):
            choices.append({"value": s, "label": f"Before: {i}. {s}"})
        choices.append({"value": _INSERT_BOTTOM, "label": "Bottom (after last)"})
        return choices

    def _get_target_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add target"},
            {"value": "audio", "label": "🔊 Audio / TTS setup"},
            {"value": "messages", "label": "💬 Messages bridge"},
            {"value": "routing", "label": "🧠 Choose routing mode"},
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

    # Reusable schema for ordering (targets / phones / media players)
    def _get_order_targets_schema(
        self,
        *,
        services: list[str],
        current: list[str] | None,
        default_action: str = "confirm",
    ) -> vol.Schema:
        current = current or []
        # When building (default_action == "add"), start with nothing checked.
        default_priority = [] if default_action == "add" else current
        insertion_opts = self._insertion_choices(current)

        return vol.Schema(
            {
                vol.Optional("priority", default=default_priority): selector(
                    {"select": {"options": services, "multiple": True}}
                ),
                vol.Optional("next_priority", default=_INSERT_BOTTOM): selector(
                    {"select": {"options": insertion_opts}}
                ),
                vol.Optional("action", default=default_action): selector(
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

    # ─── steps (config) ───
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            raw = user_input["service_name_raw"].strip()
            slug = slugify(raw) or "custom_notifier"
            await self.async_set_unique_id(slug)
            self._abort_if_unique_id_configured()

            # persist basic names
            self._data.update({CONF_SERVICE_NAME_RAW: raw, CONF_SERVICE_NAME: slug})

            # default to conditional and start adding a target
            self._data.setdefault(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE)
            return await self.async_step_add_target()

        return self.async_show_form(
            step_id=STEP_USER,
            data_schema=vol.Schema({vol.Required("service_name_raw"): str}),
        )

    async def async_step_routing_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select Regular vs Smart and wipe irrelevant keys to prevent cross-mode bleed."""
        if user_input:
            mode = user_input[CONF_ROUTING_MODE]
            self._data[CONF_ROUTING_MODE] = mode

            if mode == ROUTING_SMART:
                _wipe_keys(self._data, CONDITIONAL_KEYS)
                self._targets.clear()
                self._priority_list.clear()
                return self.async_show_form(
                    step_id=STEP_SMART_SETUP,
                    data_schema=self._get_smart_setup_schema(self._data),
                )

            _wipe_keys(self._data, SMART_KEYS)
            self._phone_order_list.clear()
            return await self.async_step_add_target()

        return self.async_show_form(
            step_id=STEP_ROUTING_MODE,
            data_schema=self._get_routing_mode_schema(),
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
                final_value = (
                    str(int(final_value))
                    if float(final_value).is_integer()
                    else str(final_value)
                )
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
            if nxt == "audio":
                return self.async_show_form(
                    step_id=STEP_AUDIO_SETUP,
                    data_schema=self._get_audio_setup_schema(self._data),
                    description_placeholders=self._audio_placeholders(),
                )
            if nxt == "messages":
                return self.async_show_form(
                    step_id=STEP_MESSAGES_SETUP,
                    data_schema=_get_messages_setup_schema(self, self._data),
                    description_placeholders=_messages_placeholders(self),
                )
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

    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        services = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            action = user_input.get("action", "confirm")
            next_anchor = user_input.get("next_priority", _INSERT_BOTTOM)
            if action == "add":
                # items to add/move (filter to known services)
                to_add = [s for s in user_input.get("priority", []) if s in services]
                self._priority_list = _insert_items_at(
                    self._priority_list, to_add, next_anchor
                )
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

            # Confirm (or implicit confirm)
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
                                {"value": "audio", "label": "Audio / TTS setup…"},
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
            # quick navs
            if user_input.get("nav") == "audio":
                return self.async_show_form(
                    step_id=STEP_AUDIO_SETUP,
                    data_schema=self._get_audio_setup_schema(self._data),
                    description_placeholders=self._audio_placeholders(),
                )

            if (
                user_input.get("nav") == "back"
                and self._data.get(CONF_ROUTING_MODE) == ROUTING_CONDITIONAL
            ):
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
                # Finish the wizard
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                title = (
                    self._data.get(CONF_SERVICE_NAME_RAW)
                    or self._data.get("service_name_raw")
                    or ""
                )
                return self.async_create_entry(title=title, data=self._data)

        # (When in Smart mode, there is no meaningful order to show here.)
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

    # ─────────────── SMART SETUP / PHONE ORDER ───────────────
    async def async_step_smart_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input and user_input.get("nav") == "routing":
            return self.async_show_form(
                step_id=STEP_ROUTING_MODE,
                data_schema=self._get_routing_mode_schema(),
            )
        # Open reorder builder
        if user_input and user_input.get("nav") == "reorder_phones":
            services = self._smart_phone_candidates()
            placeholders = _order_placeholders(services, self._phone_order_list)
            return self.async_show_form(
                step_id=STEP_SMART_ORDER_PHONES,
                data_schema=self._get_order_targets_schema(
                    services=services,
                    current=self._phone_order_list,
                    default_action="add",
                ),
                description_placeholders=placeholders,
            )
        # Audio / TTS
        if user_input and user_input.get("nav") == "audio":
            return self.async_show_form(
                step_id=STEP_AUDIO_SETUP,
                data_schema=self._get_audio_setup_schema(self._data),
                description_placeholders=self._audio_placeholders(),
            )

        # open Messages bridge directly from Smart setup
        if user_input and user_input.get("nav") == "messages":
            return self.async_show_form(
                step_id=STEP_MESSAGES_SETUP,
                data_schema=_get_messages_setup_schema(self, self._data),
                description_placeholders=_messages_placeholders(self),
            )

        # Stay here: persist posted values but do not advance
        if user_input and user_input.get("nav") == "stay":
            self._data[CONF_SMART_PHONE_ORDER] = list(self._phone_order_list)
            self._data.update(
                {
                    CONF_SMART_PC_NOTIFY: user_input.get(
                        CONF_SMART_PC_NOTIFY, self._data.get(CONF_SMART_PC_NOTIFY)
                    ),
                    CONF_SMART_PC_SESSION: user_input.get(
                        CONF_SMART_PC_SESSION, self._data.get(CONF_SMART_PC_SESSION)
                    ),
                    CONF_SMART_POLICY: user_input.get(
                        CONF_SMART_POLICY, self._data.get(CONF_SMART_POLICY)
                    ),
                    CONF_SMART_MIN_BATTERY: user_input.get(
                        CONF_SMART_MIN_BATTERY, self._data.get(CONF_SMART_MIN_BATTERY)
                    ),
                    CONF_SMART_PHONE_FRESH_S: user_input.get(
                        CONF_SMART_PHONE_FRESH_S,
                        self._data.get(CONF_SMART_PHONE_FRESH_S),
                    ),
                    CONF_SMART_PHONE_UNLOCK_WINDOW_S: user_input.get(
                        CONF_SMART_PHONE_UNLOCK_WINDOW_S,
                        self._data.get(CONF_SMART_PHONE_UNLOCK_WINDOW_S),
                    ),
                    CONF_SMART_PC_FRESH_S: user_input.get(
                        CONF_SMART_PC_FRESH_S, self._data.get(CONF_SMART_PC_FRESH_S)
                    ),
                    CONF_SMART_REQUIRE_AWAKE: user_input.get(
                        CONF_SMART_REQUIRE_AWAKE,
                        self._data.get(CONF_SMART_REQUIRE_AWAKE),
                    ),
                    CONF_SMART_REQUIRE_UNLOCKED: user_input.get(
                        CONF_SMART_REQUIRE_UNLOCKED,
                        self._data.get(CONF_SMART_REQUIRE_UNLOCKED),
                    ),
                    CONF_SMART_REQUIRE_PHONE_UNLOCKED: user_input.get(
                        CONF_SMART_REQUIRE_PHONE_UNLOCKED,
                        self._data.get(CONF_SMART_REQUIRE_PHONE_UNLOCKED),
                    ),
                    # NEW: boot-sticky window
                    CONF_BOOT_STICKY_TARGET_S: user_input.get(
                        CONF_BOOT_STICKY_TARGET_S,
                        self._data.get(
                            CONF_BOOT_STICKY_TARGET_S, DEFAULT_BOOT_STICKY_TARGET_S
                        ),
                    ),
                }
            )
            return self.async_show_form(
                step_id=STEP_SMART_SETUP,
                data_schema=self._get_smart_setup_schema(self._data),
            )

        if user_input:
            # Persist phone ordering + smart config, then go to fallback (no conditional order step)
            self._data[CONF_SMART_PHONE_ORDER] = list(self._phone_order_list)
            self._data.update(
                {
                    CONF_SMART_PC_NOTIFY: user_input.get(CONF_SMART_PC_NOTIFY),
                    CONF_SMART_PC_SESSION: user_input.get(CONF_SMART_PC_SESSION),
                    CONF_SMART_POLICY: user_input.get(CONF_SMART_POLICY),
                    CONF_SMART_MIN_BATTERY: user_input.get(CONF_SMART_MIN_BATTERY),
                    CONF_SMART_PHONE_FRESH_S: user_input.get(CONF_SMART_PHONE_FRESH_S),
                    CONF_SMART_PHONE_UNLOCK_WINDOW_S: user_input.get(
                        CONF_SMART_PHONE_UNLOCK_WINDOW_S
                    ),
                    CONF_SMART_PC_FRESH_S: user_input.get(CONF_SMART_PC_FRESH_S),
                    CONF_SMART_REQUIRE_AWAKE: user_input.get(CONF_SMART_REQUIRE_AWAKE),
                    CONF_SMART_REQUIRE_UNLOCKED: user_input.get(
                        CONF_SMART_REQUIRE_UNLOCKED
                    ),
                    CONF_SMART_REQUIRE_PHONE_UNLOCKED: user_input.get(
                        CONF_SMART_REQUIRE_PHONE_UNLOCKED
                    ),
                    # NEW: boot-sticky window
                    CONF_BOOT_STICKY_TARGET_S: user_input.get(
                        CONF_BOOT_STICKY_TARGET_S
                    ),
                }
            )
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
                description_placeholders={
                    "available_services": ", ".join(
                        sorted(self.hass.services.async_services().get("notify", {}))
                    ),
                    "current_order": "—",
                    "remaining": "—",
                },
            )

        return self.async_show_form(
            step_id=STEP_SMART_SETUP,
            data_schema=self._get_smart_setup_schema(self._data),
        )

    async def async_step_smart_order_phones(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        services = self._smart_phone_candidates()

        if user_input:
            # Picking next_priority without touching "action" counts as Add
            action = user_input.get("action") or (
                "add" if user_input.get("next_priority") else "confirm"
            )
            anchor = user_input.get("next_priority", _INSERT_BOTTOM)

            if action == "add":
                to_add = [s for s in user_input.get("priority", []) if s in services]
                self._phone_order_list = _insert_items_at(
                    self._phone_order_list, to_add, anchor
                )
                placeholders = _order_placeholders(services, self._phone_order_list)
                return self.async_show_form(
                    step_id=STEP_SMART_ORDER_PHONES,
                    data_schema=self._get_order_targets_schema(
                        services=services,
                        current=self._phone_order_list,
                        default_action="add",
                    ),
                    description_placeholders=placeholders,
                )

            if action == "reset":
                self._phone_order_list = []
                placeholders = _order_placeholders(services, self._phone_order_list)
                return self.async_show_form(
                    step_id=STEP_SMART_ORDER_PHONES,
                    data_schema=self._get_order_targets_schema(
                        services=services,
                        current=self._phone_order_list,
                        default_action="add",
                    ),
                    description_placeholders=placeholders,
                )

            # confirm (or anything else) → persist and jump back to Smart setup
            selected = user_input.get("priority")
            if isinstance(selected, list) and selected:
                final_priority = [s for s in selected if s in services]
            elif self._phone_order_list:
                final_priority = [s for s in self._phone_order_list if s in services]
            else:
                final_priority = []

            self._data[CONF_SMART_PHONE_ORDER] = final_priority
            return self.async_show_form(
                step_id=STEP_SMART_SETUP,
                data_schema=self._get_smart_setup_schema(self._data),
            )

        # Default render when first opening the phone-order page
        placeholders = _order_placeholders(services, self._phone_order_list)
        return self.async_show_form(
            step_id=STEP_SMART_ORDER_PHONES,
            data_schema=self._get_order_targets_schema(
                services=services, current=self._phone_order_list, default_action="add"
            ),
            description_placeholders=placeholders,
        )

    # ─────────────── AUDIO / TTS ───────────────
    async def async_step_audio_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input and user_input.get("nav") == "routing":
            return self.async_show_form(
                step_id=STEP_ROUTING_MODE, data_schema=self._get_routing_mode_schema()
            )

        if user_input and user_input.get("nav") == "reorder_players":
            services = _media_players(self.hass)
            placeholders = self._audio_placeholders()
            return self.async_show_form(
                step_id=STEP_MEDIA_ORDER,
                data_schema=self._get_order_targets_schema(
                    services=services,
                    current=self._media_order_list,
                    default_action="add",
                ),
                description_placeholders=placeholders,
            )

        if user_input and user_input.get("nav") == "stay":
            self._data.update(
                {
                    CONF_TTS_ENABLE: user_input.get(
                        CONF_TTS_ENABLE,
                        self._data.get(CONF_TTS_ENABLE, DEFAULT_TTS_ENABLE),
                    ),
                    CONF_TTS_DEFAULT: user_input.get(
                        CONF_TTS_DEFAULT,
                        self._data.get(CONF_TTS_DEFAULT, DEFAULT_TTS_DEFAULT),
                    ),
                    CONF_TTS_SERVICE: user_input.get(
                        CONF_TTS_SERVICE,
                        self._data.get(CONF_TTS_SERVICE, DEFAULT_TTS_SERVICE),
                    ),
                    CONF_TTS_LANGUAGE: user_input.get(
                        CONF_TTS_LANGUAGE,
                        self._data.get(CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE),
                    ),
                }
            )
            return self.async_show_form(
                step_id=STEP_AUDIO_SETUP,
                data_schema=self._get_audio_setup_schema(self._data),
                description_placeholders=self._audio_placeholders(),
            )

        if user_input:
            self._data.update(
                {
                    CONF_TTS_ENABLE: user_input.get(
                        CONF_TTS_ENABLE, DEFAULT_TTS_ENABLE
                    ),
                    CONF_TTS_DEFAULT: user_input.get(
                        CONF_TTS_DEFAULT, DEFAULT_TTS_DEFAULT
                    ),
                    CONF_TTS_SERVICE: user_input.get(
                        CONF_TTS_SERVICE, DEFAULT_TTS_SERVICE
                    ),
                    CONF_TTS_LANGUAGE: user_input.get(
                        CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE
                    ),
                    CONF_MEDIA_PLAYER_ORDER: list(self._media_order_list),
                }
            )
            # take user back to fallback/continue path
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
                description_placeholders={
                    "available_services": ", ".join(
                        sorted(self.hass.services.async_services().get("notify", {}))
                    ),
                    "current_order": "—",
                    "remaining": "—",
                },
            )

        # default render
        return self.async_show_form(
            step_id=STEP_AUDIO_SETUP,
            data_schema=self._get_audio_setup_schema(self._data),
            description_placeholders=self._audio_placeholders(),
        )

    async def async_step_media_order(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        services = _media_players(self.hass)
        if user_input:
            action = user_input.get("action") or (
                "add" if user_input.get("next_priority") else "confirm"
            )
            anchor = user_input.get("next_priority", _INSERT_BOTTOM)

            if action == "add":
                to_add = [s for s in user_input.get("priority", []) if s in services]
                self._media_order_list = _insert_items_at(
                    self._media_order_list, to_add, anchor
                )
                placeholders = self._audio_placeholders()
                return self.async_show_form(
                    step_id=STEP_MEDIA_ORDER,
                    data_schema=self._get_order_targets_schema(
                        services=services,
                        current=self._media_order_list,
                        default_action="add",
                    ),
                    description_placeholders=placeholders,
                )

            if action == "reset":
                self._media_order_list = []
                placeholders = self._audio_placeholders()
                return self.async_show_form(
                    step_id=STEP_MEDIA_ORDER,
                    data_schema=self._get_order_targets_schema(
                        services=services,
                        current=self._media_order_list,
                        default_action="add",
                    ),
                    description_placeholders=placeholders,
                )

            selected = user_input.get("priority")
            if isinstance(selected, list) and selected:
                final_priority = [s for s in selected if s in services]
            elif self._media_order_list:
                final_priority = [s for s in self._media_order_list if s in services]
            else:
                final_priority = []

            self._data[CONF_MEDIA_PLAYER_ORDER] = final_priority
            return self.async_show_form(
                step_id=STEP_AUDIO_SETUP,
                data_schema=self._get_audio_setup_schema(self._data),
                description_placeholders=self._audio_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_MEDIA_ORDER,
            data_schema=self._get_order_targets_schema(
                services=services, current=self._media_order_list, default_action="add"
            ),
            description_placeholders=self._audio_placeholders(),
        )

    async def async_step_messages_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            self._data.update(
                {
                    CONF_MSG_ENABLE: user_input.get(
                        CONF_MSG_ENABLE, DEFAULT_MSG_ENABLE
                    ),
                    CONF_MSG_SOURCE_SENSOR: user_input.get(CONF_MSG_SOURCE_SENSOR, ""),
                    CONF_MSG_APPS: user_input.get(CONF_MSG_APPS, DEFAULT_MSG_APPS),
                    CONF_MSG_TARGETS: user_input.get(CONF_MSG_TARGETS, []),
                    CONF_MSG_REPLY_TRANSPORT: user_input.get(
                        CONF_MSG_REPLY_TRANSPORT, DEFAULT_MSG_REPLY_TRANSPORT
                    ),
                    CONF_MSG_KDECONNECT_DEVICE_ID: user_input.get(
                        CONF_MSG_KDECONNECT_DEVICE_ID, ""
                    ),
                    CONF_MSG_TASKER_EVENT: user_input.get(
                        CONF_MSG_TASKER_EVENT, DEFAULT_MSG_TASKER_EVENT
                    ),
                }
            )
            # After saving, send them back to the fallback page (same behavior as Audio)
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                description_placeholders={
                    "available_services": ", ".join(
                        sorted(self.hass.services.async_services().get("notify", {}))
                    ),
                    "current_order": "—",
                    "remaining": "—",
                },
                errors={},
            )

        return self.async_show_form(
            step_id=STEP_MESSAGES_SETUP,
            data_schema=_get_messages_setup_schema(self, self._data),
            description_placeholders=_messages_placeholders(self),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return CustomDeviceNotifierOptionsFlowHandler(config_entry)


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
        self._phone_order_list: list[str] = list(
            self._data.get(CONF_SMART_PHONE_ORDER, [])
        )
        self._media_order_list: list[str] = list(
            self._data.get(CONF_MEDIA_PLAYER_ORDER, [])
        )

    # ───────── schema helpers (mirror) ─────────
    def _get_routing_mode_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(
                    CONF_ROUTING_MODE,
                    default=self._data.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE),
                ): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": ROUTING_CONDITIONAL,
                                    "label": "Regular Prioritization (Targets + Order)",
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

    def _smart_phone_candidates(self) -> list[str]:
        """Mirror: include *all* notify services (phones first)."""
        services = _notify_services(self.hass)
        phone_like = [f"notify.{s}" for s in services if s.startswith("mobile_app_")]
        others = [f"notify.{s}" for s in services if not s.startswith("mobile_app_")]
        for s in self._phone_order_list:
            if s not in phone_like and s not in others:
                others.append(s)
        return sorted(phone_like) + sorted(others)

    def _smart_pc_candidates(self) -> list[str]:
        services = _notify_services(self.hass)
        pcs = [f"notify.{s}" for s in services if not s.startswith("mobile_app_")]
        return sorted(pcs)

    def _get_smart_setup_schema(
        self, existing: dict[str, Any] | None = None
    ) -> vol.Schema:
        existing = existing or {}
        services = _notify_services(self.hass)

        pc_default = existing.get(CONF_SMART_PC_NOTIFY)
        if not pc_default:
            guess = _default_pc_notify(services)
            pc_default = f"notify.{guess}" if guess else ""
        pc_session_default = existing.get(CONF_SMART_PC_SESSION) or (
            f"sensor.{(pc_default or '').removeprefix('notify.').lower()}_sessionstate"
            if pc_default
            else ""
        )

        return vol.Schema(
            {
                vol.Required(
                    CONF_SMART_PC_NOTIFY,
                    default=existing.get(CONF_SMART_PC_NOTIFY, pc_default),
                ): selector(
                    {
                        "select": {
                            "options": self._smart_pc_candidates(),
                            "custom_value": True,
                        }
                    }
                ),
                vol.Required(
                    CONF_SMART_PC_SESSION,
                    default=existing.get(CONF_SMART_PC_SESSION, pc_session_default),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Optional("nav"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "reorder_phones", "label": "Reorder phones…"},
                                {"value": "audio", "label": "Audio / TTS setup…"},
                                {"value": "messages", "label": "Messages bridge…"},
                                {"value": "routing", "label": "Choose routing mode…"},
                                {"value": "stay", "label": "Stay here"},
                            ],
                            "custom_value": False,
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
                    CONF_SMART_PHONE_UNLOCK_WINDOW_S,
                    default=existing.get(
                        CONF_SMART_PHONE_UNLOCK_WINDOW_S,
                        DEFAULT_SMART_PHONE_UNLOCK_WINDOW_S,
                    ),
                ): selector({"number": {"min": 30, "max": 7200, "step": 10}}),
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
                vol.Required(
                    CONF_SMART_REQUIRE_PHONE_UNLOCKED,
                    default=existing.get(
                        CONF_SMART_REQUIRE_PHONE_UNLOCKED,
                        DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
                    ),
                ): selector({"boolean": {}}),
                # NEW: Boot-sticky window (seconds). 0 disables.
                vol.Required(
                    CONF_BOOT_STICKY_TARGET_S,
                    default=existing.get(
                        CONF_BOOT_STICKY_TARGET_S, DEFAULT_BOOT_STICKY_TARGET_S
                    ),
                ): selector({"number": {"min": 0, "max": 900, "step": 5}}),
            }
        )

    # Audio/TTS mirrors
    def _audio_placeholders(self) -> dict[str, str]:
        mp = _media_players(self.hass)
        return _order_placeholders(mp, self._media_order_list)

    def _get_audio_setup_schema(
        self, existing: dict[str, Any] | None = None
    ) -> vol.Schema:
        existing = existing or {}
        services = _tts_services(self.hass)
        if DEFAULT_TTS_SERVICE not in services:
            services = [DEFAULT_TTS_SERVICE] + services
        return vol.Schema(
            {
                vol.Required(
                    CONF_TTS_ENABLE,
                    default=existing.get(CONF_TTS_ENABLE, DEFAULT_TTS_ENABLE),
                ): selector({"boolean": {}}),
                vol.Required(
                    CONF_TTS_DEFAULT,
                    default=existing.get(CONF_TTS_DEFAULT, DEFAULT_TTS_DEFAULT),
                ): selector({"boolean": {}}),
                vol.Required(
                    CONF_TTS_SERVICE,
                    default=existing.get(CONF_TTS_SERVICE, DEFAULT_TTS_SERVICE),
                ): selector({"select": {"options": services, "custom_value": True}}),
                vol.Optional(
                    CONF_TTS_LANGUAGE,
                    default=existing.get(CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE),
                ): str,
                vol.Optional("nav"): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": "reorder_players",
                                    "label": "Reorder media players…",
                                },
                                {"value": "routing", "label": "Back to routing…"},
                                {"value": "stay", "label": "Stay here"},
                            ],
                            "custom_value": False,
                        }
                    }
                ),
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

    def _insertion_choices(self, current: list[str]) -> list[dict[str, str]]:
        if not current:
            return [{"value": _INSERT_BOTTOM, "label": "Bottom (first position)"}]
        choices: list[dict[str, str]] = [
            {"value": _INSERT_TOP, "label": "Top (before #1)"}
        ]
        for i, s in enumerate(current, 1):
            choices.append({"value": s, "label": f"Before: {i}. {s}"})
        choices.append({"value": _INSERT_BOTTOM, "label": "Bottom (after last)"})
        return choices

    def _get_target_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add target"},
            {"value": "audio", "label": "🔊 Audio / TTS setup"},
            {"value": "messages", "label": "💬 Messages bridge"},
            {"value": "routing", "label": "🧠 Choose routing mode"},
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

    def _get_order_targets_schema(
        self,
        *,
        services: list[str],
        current: list[str] | None,
        default_action: str = "confirm",
    ) -> vol.Schema:
        current = current or []
        default_priority = [] if default_action == "add" else current
        insertion_opts = self._insertion_choices(current)
        return vol.Schema(
            {
                vol.Optional("priority", default=default_priority): selector(
                    {"select": {"options": services, "multiple": True}}
                ),
                vol.Optional("next_priority", default=_INSERT_BOTTOM): selector(
                    {"select": {"options": insertion_opts}}
                ),
                vol.Optional("action", default=default_action): selector(
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

    # ─── entry point (options) ───
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        has_conditional = bool(self._targets or self._data.get(CONF_PRIORITY))
        has_smart = any(self._data.get(k) for k in SMART_KEYS) or bool(
            self._phone_order_list
        )

        if has_conditional:
            self._data[CONF_ROUTING_MODE] = ROUTING_CONDITIONAL
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )

        if has_smart:
            self._data[CONF_ROUTING_MODE] = ROUTING_SMART
            return self.async_show_form(
                step_id=STEP_SMART_SETUP,
                data_schema=self._get_smart_setup_schema(self._data),
            )

        return self.async_show_form(
            step_id=STEP_ROUTING_MODE,
            data_schema=self._get_routing_mode_schema(),
        )

    # ─── mirrors of config steps (options) ───
    async def async_step_routing_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            mode = user_input[CONF_ROUTING_MODE]
            self._data[CONF_ROUTING_MODE] = mode

            if mode == ROUTING_SMART:
                _wipe_keys(self._data, CONDITIONAL_KEYS)
                self._targets.clear()
                self._priority_list.clear()
                return self.async_show_form(
                    step_id=STEP_SMART_SETUP,
                    data_schema=self._get_smart_setup_schema(self._data),
                )

            _wipe_keys(self._data, SMART_KEYS)
            self._phone_order_list.clear()
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_ROUTING_MODE, data_schema=self._get_routing_mode_schema()
        )

    async def async_step_smart_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input and user_input.get("nav") == "routing":
            return self.async_show_form(
                step_id=STEP_ROUTING_MODE, data_schema=self._get_routing_mode_schema()
            )
        if user_input and user_input.get("nav") == "reorder_phones":
            services = self._smart_phone_candidates()
            placeholders = _order_placeholders(services, self._phone_order_list)
            return self.async_show_form(
                step_id=STEP_SMART_ORDER_PHONES,
                data_schema=self._get_order_targets_schema(
                    services=services,
                    current=self._phone_order_list,
                    default_action="add",
                ),
                description_placeholders=placeholders,
            )
        if user_input and user_input.get("nav") == "audio":
            return self.async_show_form(
                step_id=STEP_AUDIO_SETUP,
                data_schema=self._get_audio_setup_schema(self._data),
                description_placeholders=self._audio_placeholders(),
            )

        # open Messages bridge directly from Smart setup
        if user_input and user_input.get("nav") == "messages":
            return self.async_show_form(
                step_id=STEP_MESSAGES_SETUP,
                data_schema=_get_messages_setup_schema(self, self._data),
                description_placeholders=_messages_placeholders(self),
            )
        if user_input and user_input.get("nav") == "stay":
            self._data.update(
                {
                    CONF_SMART_PC_NOTIFY: user_input.get(
                        CONF_SMART_PC_NOTIFY, self._data.get(CONF_SMART_PC_NOTIFY)
                    ),
                    CONF_SMART_PC_SESSION: user_input.get(
                        CONF_SMART_PC_SESSION, self._data.get(CONF_SMART_PC_SESSION)
                    ),
                    CONF_SMART_POLICY: user_input.get(
                        CONF_SMART_POLICY, self._data.get(CONF_SMART_POLICY)
                    ),
                    CONF_SMART_MIN_BATTERY: user_input.get(
                        CONF_SMART_MIN_BATTERY, self._data.get(CONF_SMART_MIN_BATTERY)
                    ),
                    CONF_SMART_PHONE_FRESH_S: user_input.get(
                        CONF_SMART_PHONE_FRESH_S,
                        self._data.get(CONF_SMART_PHONE_FRESH_S),
                    ),
                    CONF_SMART_PHONE_UNLOCK_WINDOW_S: user_input.get(
                        CONF_SMART_PHONE_UNLOCK_WINDOW_S,
                        self._data.get(CONF_SMART_PHONE_UNLOCK_WINDOW_S),
                    ),
                    CONF_SMART_PC_FRESH_S: user_input.get(
                        CONF_SMART_PC_FRESH_S, self._data.get(CONF_SMART_PC_FRESH_S)
                    ),
                    CONF_SMART_REQUIRE_AWAKE: user_input.get(
                        CONF_SMART_REQUIRE_AWAKE,
                        self._data.get(CONF_SMART_REQUIRE_AWAKE),
                    ),
                    CONF_SMART_REQUIRE_UNLOCKED: user_input.get(
                        CONF_SMART_REQUIRE_UNLOCKED,
                        self._data.get(CONF_SMART_REQUIRE_UNLOCKED),
                    ),
                    CONF_SMART_REQUIRE_PHONE_UNLOCKED: user_input.get(
                        CONF_SMART_REQUIRE_PHONE_UNLOCKED,
                        self._data.get(CONF_SMART_REQUIRE_PHONE_UNLOCKED),
                    ),
                    # NEW: boot-sticky window
                    CONF_BOOT_STICKY_TARGET_S: user_input.get(
                        CONF_BOOT_STICKY_TARGET_S,
                        self._data.get(
                            CONF_BOOT_STICKY_TARGET_S, DEFAULT_BOOT_STICKY_TARGET_S
                        ),
                    ),
                }
            )
            return self.async_show_form(
                step_id=STEP_SMART_SETUP,
                data_schema=self._get_smart_setup_schema(self._data),
            )

        if user_input:
            self._data.update(
                {
                    CONF_SMART_PC_NOTIFY: user_input.get(CONF_SMART_PC_NOTIFY),
                    CONF_SMART_PC_SESSION: user_input.get(CONF_SMART_PC_SESSION),
                    CONF_SMART_POLICY: user_input.get(CONF_SMART_POLICY),
                    CONF_SMART_MIN_BATTERY: user_input.get(CONF_SMART_MIN_BATTERY),
                    CONF_SMART_PHONE_FRESH_S: user_input.get(CONF_SMART_PHONE_FRESH_S),
                    CONF_SMART_PHONE_UNLOCK_WINDOW_S: user_input.get(
                        CONF_SMART_PHONE_UNLOCK_WINDOW_S
                    ),
                    CONF_SMART_PC_FRESH_S: user_input.get(CONF_SMART_PC_FRESH_S),
                    CONF_SMART_REQUIRE_AWAKE: user_input.get(CONF_SMART_REQUIRE_AWAKE),
                    CONF_SMART_REQUIRE_UNLOCKED: user_input.get(
                        CONF_SMART_REQUIRE_UNLOCKED
                    ),
                    CONF_SMART_REQUIRE_PHONE_UNLOCKED: user_input.get(
                        CONF_SMART_REQUIRE_PHONE_UNLOCKED
                    ),
                    # NEW: boot-sticky window
                    CONF_BOOT_STICKY_TARGET_S: user_input.get(
                        CONF_BOOT_STICKY_TARGET_S
                    ),
                }
            )
            self._data[CONF_SMART_PHONE_ORDER] = list(self._phone_order_list)
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
                description_placeholders={
                    "available_services": ", ".join(
                        sorted(self.hass.services.async_services().get("notify", {}))
                    ),
                    "current_order": "—",
                    "remaining": "—",
                },
            )

        return self.async_show_form(
            step_id=STEP_SMART_SETUP,
            data_schema=self._get_smart_setup_schema(self._data),
        )

    async def async_step_smart_order_phones(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        services = self._smart_phone_candidates()

        if user_input:
            action = user_input.get("action") or (
                "add" if user_input.get("next_priority") else "confirm"
            )
            anchor = user_input.get("next_priority", _INSERT_BOTTOM)

            if action == "add":
                to_add = [s for s in user_input.get("priority", []) if s in services]
                self._phone_order_list = _insert_items_at(
                    self._phone_order_list, to_add, anchor
                )
                placeholders = _order_placeholders(services, self._phone_order_list)
                return self.async_show_form(
                    step_id=STEP_SMART_ORDER_PHONES,
                    data_schema=self._get_order_targets_schema(
                        services=services,
                        current=self._phone_order_list,
                        default_action="add",
                    ),
                    description_placeholders=placeholders,
                )

            if action == "reset":
                self._phone_order_list = []
                placeholders = _order_placeholders(services, self._phone_order_list)
                return self.async_show_form(
                    step_id=STEP_SMART_ORDER_PHONES,
                    data_schema=self._get_order_targets_schema(
                        services=services,
                        current=self._phone_order_list,
                        default_action="add",
                    ),
                    description_placeholders=placeholders,
                )

            selected = user_input.get("priority")
            if isinstance(selected, list) and selected:
                final_priority = [s for s in selected if s in services]
            elif self._phone_order_list:
                final_priority = [s for s in self._phone_order_list if s in services]
            else:
                final_priority = []

            self._data[CONF_SMART_PHONE_ORDER] = final_priority
            return self.async_show_form(
                step_id=STEP_SMART_SETUP,
                data_schema=self._get_smart_setup_schema(self._data),
            )

        placeholders = _order_placeholders(services, self._phone_order_list)
        return self.async_show_form(
            step_id=STEP_SMART_ORDER_PHONES,
            data_schema=self._get_order_targets_schema(
                services=services, current=self._phone_order_list, default_action="add"
            ),
            description_placeholders=placeholders,
        )

    # ─────────────── AUDIO / TTS (Options flow) ───────────────
    async def async_step_audio_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Back to routing chooser
        if user_input and user_input.get("nav") == "routing":
            return self.async_show_form(
                step_id=STEP_ROUTING_MODE, data_schema=self._get_routing_mode_schema()
            )

        # Open media player ordering
        if user_input and user_input.get("nav") == "reorder_players":
            services = _media_players(self.hass)
            return self.async_show_form(
                step_id=STEP_MEDIA_ORDER,
                data_schema=self._get_order_targets_schema(
                    services=services,
                    current=self._media_order_list,
                    default_action="add",
                ),
                description_placeholders=self._audio_placeholders(),
            )

        # Stay here: persist posted values but remain on the same step
        if user_input and user_input.get("nav") == "stay":
            self._data.update(
                {
                    CONF_TTS_ENABLE: user_input.get(
                        CONF_TTS_ENABLE,
                        self._data.get(CONF_TTS_ENABLE, DEFAULT_TTS_ENABLE),
                    ),
                    CONF_TTS_DEFAULT: user_input.get(
                        CONF_TTS_DEFAULT,
                        self._data.get(CONF_TTS_DEFAULT, DEFAULT_TTS_DEFAULT),
                    ),
                    CONF_TTS_SERVICE: user_input.get(
                        CONF_TTS_SERVICE,
                        self._data.get(CONF_TTS_SERVICE, DEFAULT_TTS_SERVICE),
                    ),
                    CONF_TTS_LANGUAGE: user_input.get(
                        CONF_TTS_LANGUAGE,
                        self._data.get(CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE),
                    ),
                }
            )
            return self.async_show_form(
                step_id=STEP_AUDIO_SETUP,
                data_schema=self._get_audio_setup_schema(self._data),
                description_placeholders=self._audio_placeholders(),
            )

        # Submit → save and jump to fallback chooser
        if user_input:
            self._data.update(
                {
                    CONF_TTS_ENABLE: user_input.get(
                        CONF_TTS_ENABLE, DEFAULT_TTS_ENABLE
                    ),
                    CONF_TTS_DEFAULT: user_input.get(
                        CONF_TTS_DEFAULT, DEFAULT_TTS_DEFAULT
                    ),
                    CONF_TTS_SERVICE: user_input.get(
                        CONF_TTS_SERVICE, DEFAULT_TTS_SERVICE
                    ),
                    CONF_TTS_LANGUAGE: user_input.get(
                        CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE
                    ),
                    CONF_MEDIA_PLAYER_ORDER: list(self._media_order_list),
                }
            )
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
                description_placeholders={
                    "available_services": ", ".join(
                        sorted(self.hass.services.async_services().get("notify", {}))
                    ),
                    "current_order": "—",
                    "remaining": "—",
                },
            )

        # default render
        return self.async_show_form(
            step_id=STEP_AUDIO_SETUP,
            data_schema=self._get_audio_setup_schema(self._data),
            description_placeholders=self._audio_placeholders(),
        )

    async def async_step_media_order(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        services = _media_players(self.hass)

        if user_input:
            action = user_input.get("action") or (
                "add" if user_input.get("next_priority") else "confirm"
            )
            anchor = user_input.get("next_priority", _INSERT_BOTTOM)

            if action == "add":
                to_add = [s for s in user_input.get("priority", []) if s in services]
                self._media_order_list = _insert_items_at(
                    self._media_order_list, to_add, anchor
                )
                return self.async_show_form(
                    step_id=STEP_MEDIA_ORDER,
                    data_schema=self._get_order_targets_schema(
                        services=services,
                        current=self._media_order_list,
                        default_action="add",
                    ),
                    description_placeholders=self._audio_placeholders(),
                )

            if action == "reset":
                self._media_order_list = []
                return self.async_show_form(
                    step_id=STEP_MEDIA_ORDER,
                    data_schema=self._get_order_targets_schema(
                        services=services,
                        current=self._media_order_list,
                        default_action="add",
                    ),
                    description_placeholders=self._audio_placeholders(),
                )

            # confirm
            selected = user_input.get("priority")
            if isinstance(selected, list) and selected:
                final_priority = [s for s in selected if s in services]
            elif self._media_order_list:
                final_priority = [s for s in self._media_order_list if s in services]
            else:
                final_priority = []

            self._data[CONF_MEDIA_PLAYER_ORDER] = final_priority
            return self.async_show_form(
                step_id=STEP_AUDIO_SETUP,
                data_schema=self._get_audio_setup_schema(self._data),
                description_placeholders=self._audio_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_MEDIA_ORDER,
            data_schema=self._get_order_targets_schema(
                services=services, current=self._media_order_list, default_action="add"
            ),
            description_placeholders=self._audio_placeholders(),
        )

    async def async_step_messages_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input:
            self._data.update(
                {
                    CONF_MSG_ENABLE: user_input.get(
                        CONF_MSG_ENABLE, DEFAULT_MSG_ENABLE
                    ),
                    CONF_MSG_SOURCE_SENSOR: user_input.get(CONF_MSG_SOURCE_SENSOR, ""),
                    CONF_MSG_APPS: user_input.get(CONF_MSG_APPS, DEFAULT_MSG_APPS),
                    CONF_MSG_TARGETS: user_input.get(CONF_MSG_TARGETS, []),
                    CONF_MSG_REPLY_TRANSPORT: user_input.get(
                        CONF_MSG_REPLY_TRANSPORT, DEFAULT_MSG_REPLY_TRANSPORT
                    ),
                    CONF_MSG_KDECONNECT_DEVICE_ID: user_input.get(
                        CONF_MSG_KDECONNECT_DEVICE_ID, ""
                    ),
                    CONF_MSG_TASKER_EVENT: user_input.get(
                        CONF_MSG_TASKER_EVENT, DEFAULT_MSG_TASKER_EVENT
                    ),
                }
            )
            # Stay in options; send them back to fallback chooser for consistency
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                description_placeholders={
                    "available_services": ", ".join(
                        sorted(self.hass.services.async_services().get("notify", {}))
                    ),
                    "current_order": "—",
                    "remaining": "—",
                },
                errors={},
            )

        return self.async_show_form(
            step_id=STEP_MESSAGES_SETUP,
            data_schema=_get_messages_setup_schema(self, self._data),
            description_placeholders=_messages_placeholders(self),
        )

    # ─── conditional editors (mirror) ───
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
                final_value = (
                    str(int(final_value))
                    if float(final_value).is_integer()
                    else str(final_value)
                )
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
            if nxt == "audio":
                return self.async_show_form(
                    step_id=STEP_AUDIO_SETUP,
                    data_schema=self._get_audio_setup_schema(self._data),
                    description_placeholders=self._audio_placeholders(),
                )
            if nxt == "messages":
                return self.async_show_form(
                    step_id=STEP_MESSAGES_SETUP,
                    data_schema=_get_messages_setup_schema(self, self._data),
                    description_placeholders=_messages_placeholders(self),
                )
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

    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        services = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            action = user_input.get("action", "confirm")
            anchor = user_input.get("next_priority", _INSERT_BOTTOM)
            if action == "add":
                to_add = [s for s in user_input.get("priority", []) if s in services]
                self._priority_list = _insert_items_at(
                    self._priority_list, to_add, anchor
                )
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
                                {"value": "audio", "label": "Audio / TTS setup…"},
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
            if (
                user_input.get("nav") == "back"
                and self._data.get(CONF_ROUTING_MODE) == ROUTING_CONDITIONAL
            ):
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
            if user_input.get("nav") == "audio":
                return self.async_show_form(
                    step_id=STEP_AUDIO_SETUP,
                    data_schema=self._get_audio_setup_schema(self._data),
                    description_placeholders=self._audio_placeholders(),
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
