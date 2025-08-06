"""Custom Device Notifier integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components.notify import BaseNotificationService
from homeassistant.components.notify.const import ATTR_MESSAGE, ATTR_TITLE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_MATCH,
    KEY_SERVICE,
)
from .evaluate import evaluate_condition
from .config_flow import CustomDeviceNotifierOptionsFlowHandler  # ✅ Add this import

_LOGGER: Final = logging.getLogger(DOMAIN)

# ---------- service schema -------------------------------------------------

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional("data"): dict,
    }
)

# ---------- setup ----------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the notify service from a ConfigEntry."""
    try:
        data = entry.data
        slug: str = data[CONF_SERVICE_NAME]
        targets: list[dict[str, Any]] = data[CONF_TARGETS]
        priority: list[str] = data[CONF_PRIORITY]
        fallback: str = data[CONF_FALLBACK]

        service = _NotifierService(hass, slug, targets, priority, fallback)

        hass.services.async_register(
            "notify", slug, service.async_send_message, schema=SERVICE_SCHEMA
        )

        async def handle_evaluate(call):
            entry_id = call.data.get("entry_id")
            _LOGGER.debug("Evaluating conditions for entry %s...", entry_id or "all")
            if entry_id and entry_id != entry.entry_id:
                return
            # Log evaluation without sending
            for svc in priority:
                tgt = next((t for t in targets if t[KEY_SERVICE] == svc), None)
                if not tgt:
                    continue
                mode = tgt.get(KEY_MATCH, "all")
                results = await asyncio.gather(
                    *(evaluate_condition(hass, c) for c in tgt[KEY_CONDITIONS])
                )
                matched = all(results) if mode == "all" else any(results)
                _LOGGER.debug(
                    "  target %s match=%s (conditions: %s)", svc, matched, results
                )
                if matched:
                    _LOGGER.debug("  → would forward to %s", svc)
                    return
            _LOGGER.debug("  → would fallback to %s", fallback)

        hass.services.async_register(
            DOMAIN,
            "evaluate",
            handle_evaluate,
            vol.Schema({vol.Optional("entry_id"): str}),
        )

        # ✅ Enable options flow support
        entry.options_flow_class = CustomDeviceNotifierOptionsFlowHandler

        # Forward any companion platforms (e.g. sensor/)
        await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
        return True

    except Exception:  # pragma: no cover – log & signal failure
        _LOGGER.exception("Error setting up %s", DOMAIN)
        return False


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Always normalize and migrate config entry data, regardless of version."""
    _LOGGER.debug(
        "Running unconditional migration for %s (v%s)", entry.title, entry.version
    )

    data = {**entry.data}  # mutable copy

    # Normalize fallback
    fallback = data.get(CONF_FALLBACK)
    if isinstance(fallback, str) and "." in fallback:
        domain, name = fallback.split(".", 1)
        data[CONF_FALLBACK] = f"{domain.strip().lower()}.{name.strip()}"

    # Normalize priority list
    if CONF_PRIORITY in data:
        data[CONF_PRIORITY] = [svc.strip().lower() for svc in data[CONF_PRIORITY]]

    # Normalize targets
    normalized_targets = []
    for tgt in data.get(CONF_TARGETS, []):
        new_tgt = dict(tgt)
        new_tgt.setdefault(KEY_MATCH, "all")
        new_tgt.setdefault(KEY_CONDITIONS, [])
        new_tgt[KEY_SERVICE] = new_tgt[KEY_SERVICE].strip().lower()
        normalized_targets.append(new_tgt)
    data[CONF_TARGETS] = normalized_targets

    # Save back even if version is unchanged
    hass.config_entries.async_update_entry(entry, data=data)
    _LOGGER.info("Config entry for %s migrated/normalized successfully", entry.title)
    return True


# ---------- service implementation ----------------------------------------


class _NotifierService(BaseNotificationService):
    """Forward a message to the first target whose conditions match."""

    def __init__(
        self,
        hass: HomeAssistant,
        slug: str,
        targets: list[dict[str, Any]],
        priority: list[str],
        fallback: str,
    ) -> None:
        self.hass = hass
        self._slug = slug
        self._targets = targets
        self._priority = priority
        self._fallback = fallback

    # ---------------------------------------------------------------------

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        """Dispatch *message* to the first matching target (or fallback)."""
        title = kwargs.get(ATTR_TITLE)
        data: dict[str, Any] = {ATTR_MESSAGE: message}
        if title:
            data[ATTR_TITLE] = title
        data.update(kwargs.get("data", {}))

        _LOGGER.debug("notify.%s called: %s / %s", self._slug, title, message)

        # — iterate through priority list —
        for svc in self._priority:
            tgt = next((t for t in self._targets if t[KEY_SERVICE] == svc), None)
            if not tgt:
                continue

            mode = tgt.get(KEY_MATCH, "all")

            # Evaluate all conditions concurrently
            results = await asyncio.gather(
                *(evaluate_condition(self.hass, c) for c in tgt[KEY_CONDITIONS])
            )
            matched = all(results) if mode == "all" else any(results)
            _LOGGER.debug("  target %s match=%s", svc, matched)

            if matched:
                dom, name = svc.split(".", 1)
                _LOGGER.debug("  → forwarding to %s.%s", dom, name)
                await self.hass.services.async_call(dom, name, data, blocking=True)
                return  # stop after first successful target

        # Nothing matched ⇒ fallback
        dom, name = self._fallback.split(".", 1)
        _LOGGER.debug("  → fallback to %s.%s", dom, name)
        await self.hass.services.async_call(dom, name, data, blocking=True)
