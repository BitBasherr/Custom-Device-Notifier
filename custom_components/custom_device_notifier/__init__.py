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

_LOGGER: Final = logging.getLogger(DOMAIN)

PLATFORMS = ["sensor"]

# ---------- service schema -------------------------------------------------

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional("data"): dict,
        vol.Optional("target"): vol.Any(str, [str]),  # pass-through if caller provides
    }
)

EVALUATE_SCHEMA = vol.Schema({vol.Optional("entry_id"): str})


def _get_entry_data(entry: ConfigEntry) -> dict[str, Any]:
    """Prefer options over data; options flow writes there."""
    # mypy: options is MappingProxyType[str, Any] | None; fall back to data
    return entry.options or entry.data  # type: ignore[return-value]


# ---------- setup / unload -------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the notify service and sensor platform from a ConfigEntry."""
    try:
        data = _get_entry_data(entry)
        slug: str = data[CONF_SERVICE_NAME]
        targets: list[dict[str, Any]] = data[CONF_TARGETS]
        priority: list[str] = data[CONF_PRIORITY]
        fallback: str = data[CONF_FALLBACK]

        # Register the per-entry notify service
        service = _NotifierService(hass, slug, targets, priority, fallback)
        hass.services.async_register(
            "notify", slug, service.async_send_message, schema=SERVICE_SCHEMA
        )

        # Register the debug evaluate service once per HA instance
        domain_state = hass.data.setdefault(DOMAIN, {})
        if not domain_state.get("evaluate_registered"):

            async def handle_evaluate(call):
                target_entry_id = call.data.get("entry_id")
                entries = [
                    e
                    for e in hass.config_entries.async_entries(DOMAIN)
                    if not target_entry_id or e.entry_id == target_entry_id
                ]
                if not entries:
                    _LOGGER.info(
                        "No entries to evaluate (entry_id=%s).", target_entry_id
                    )
                    return

                for e in entries:
                    ed = _get_entry_data(e)
                    prio: list[str] = ed.get(CONF_PRIORITY, [])
                    tgts: list[dict[str, Any]] = ed.get(CONF_TARGETS, [])
                    fb: str = ed.get(CONF_FALLBACK, "")
                    _LOGGER.debug("Evaluating entry %s (%s)", e.entry_id, e.title)

                    matched_svc: str | None = None
                    for svc in prio:
                        tgt = next((t for t in tgts if t[KEY_SERVICE] == svc), None)
                        if not tgt:
                            continue
                        mode = tgt.get(KEY_MATCH, "all")
                        results = await asyncio.gather(
                            *(evaluate_condition(hass, c) for c in tgt[KEY_CONDITIONS])
                        )
                        matched = all(results) if mode == "all" else any(results)
                        _LOGGER.debug(
                            "  target %s match=%s (conditions: %s)",
                            svc,
                            matched,
                            results,
                        )
                        if matched and matched_svc is None:
                            matched_svc = svc

                    if matched_svc:
                        _LOGGER.debug("  → would forward to %s", matched_svc)
                    else:
                        _LOGGER.debug("  → would fallback to %s", fb)

            hass.services.async_register(
                DOMAIN, "evaluate", handle_evaluate, schema=EVALUATE_SCHEMA
            )
            domain_state["evaluate_registered"] = True

        # Forward platforms (sensor reflects current dynamic target)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Reload on config/option updates
        async def _reload_on_update(
            hass_: HomeAssistant, updated_entry: ConfigEntry
        ) -> None:
            _LOGGER.debug("Config entry updated, reloading %s", updated_entry.entry_id)
            await hass_.config_entries.async_reload(updated_entry.entry_id)

        entry.async_on_unload(entry.add_update_listener(_reload_on_update))

        return True

    except Exception:  # pragma: no cover – log & signal failure
        _LOGGER.exception("Error setting up %s", DOMAIN)
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entry cleanly so reload works (no stale config)."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Remove the per-entry notify service
    try:
        data = _get_entry_data(entry)
        slug: str = data[CONF_SERVICE_NAME]
        if hass.services.has_service("notify", slug):
            hass.services.async_remove("notify", slug)
    except Exception:  # pragma: no cover
        _LOGGER.exception("Error while removing notify service for %s", entry.entry_id)
        ok = False

    return ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Normalize config entry data regardless of stored version."""
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
    """Forward a message to the highest-priority target whose conditions match."""

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

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        """Dispatch *message* to the highest-priority matching target (else fallback)."""
        # Build a clean payload: preserve nested "data" and optional "target"
        payload: dict[str, Any] = {ATTR_MESSAGE: message}

        title = kwargs.get(ATTR_TITLE)
        if title:
            payload[ATTR_TITLE] = title

        if "target" in kwargs and kwargs["target"] is not None:
            payload["target"] = kwargs["target"]

        extra = kwargs.get("data")
        if isinstance(extra, dict):
            # keep rich options under the "data" key so mobile_app/agent can parse them
            payload["data"] = extra

        _LOGGER.debug("notify.%s called with payload: %s", self._slug, payload)

        # Always evaluate fresh — ensures dynamic reassessment per send.
        for svc in self._priority:
            tgt = next((t for t in self._targets if t[KEY_SERVICE] == svc), None)
            if not tgt:
                continue

            mode = tgt.get(KEY_MATCH, "all")
            results = await asyncio.gather(
                *(evaluate_condition(self.hass, c) for c in tgt[KEY_CONDITIONS])
            )
            matched = all(results) if mode == "all" else any(results)
            _LOGGER.debug("  target %s match=%s", svc, matched)

            if matched:
                dom, name = svc.split(".", 1)
                _LOGGER.debug("  → forwarding to %s.%s", dom, name)
                await self.hass.services.async_call(dom, name, payload, blocking=True)
                return  # stop after first successful target

        # Nothing matched ⇒ fallback
        dom, name = self._fallback.split(".", 1)
        _LOGGER.debug("  → fallback to %s.%s", dom, name)
        await self.hass.services.async_call(dom, name, payload, blocking=True)
