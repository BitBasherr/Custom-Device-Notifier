"""Custom Device Notifier integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_track_state_change_event, EventCancelCallback
from homeassistant.components.notify.const import ATTR_MESSAGE, ATTR_TITLE

from .const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_MATCH,          # legacy name some configs may still use
    KEY_SERVICE,
    CONF_MATCH_MODE,    # new name used by the flow
)
from .evaluate import evaluate_condition

_LOGGER: Final = logging.getLogger(DOMAIN)

# ---------- user-facing notify service schema ----------
SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): vol.Coerce(str),
        vol.Optional(ATTR_TITLE): vol.Coerce(str),
        vol.Optional("data"): dict,
    }
)


def _strip_notify(svc_full: str) -> str:
    """'notify.foo' -> 'foo'."""
    return svc_full.split(".", 1)[1] if svc_full.startswith("notify.") else svc_full


def _norm_match_mode(t: dict[str, Any]) -> str:
    """Support both KEY_MATCH and CONF_MATCH_MODE."""
    if CONF_MATCH_MODE in t and t[CONF_MATCH_MODE]:
        return cast(str, t[CONF_MATCH_MODE])
    if KEY_MATCH in t and t[KEY_MATCH]:
        return cast(str, t[KEY_MATCH])
    return "all"


class NotifierController:
    """Routes notifications dynamically and exposes a live 'active target' sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        cfg = {**(entry.data or {}), **(entry.options or {})}
        self.slug = cast(str, cfg.get(CONF_SERVICE_NAME, "custom_notifier"))
        self.friendly = cast(str, cfg.get(CONF_SERVICE_NAME_RAW, self.slug))
        self.targets: list[dict[str, Any]] = list(cast(list[dict[str, Any]], cfg.get(CONF_TARGETS, [])))
        self.priority: list[str] = list(cast(list[str], cfg.get(CONF_PRIORITY, [])))
        self.fallback_full: str = cast(str, cfg.get(CONF_FALLBACK, ""))

        # Subscriptions
        self._state_unsub: EventCancelCallback | None = None
        self._watched_entities: set[str] = set()

        # Cache last selected service to avoid spamming the sensor
        self._last_pick: str | None = None

        self._service_registered = False

    # ------------------------ lifecycle ------------------------

    async def async_start(self) -> None:
        # Register notify.<slug>
        if not self._service_registered:
            self.hass.services.async_register(
                "notify", self.slug, self._handle_notify, schema=SERVICE_SCHEMA
            )
            self._service_registered = True
            _LOGGER.debug("Registered notify.%s", self.slug)

        # Register debug evaluate helper (kept for parity with your previous version)
        self.hass.services.async_register(
            DOMAIN,
            "evaluate",
            self._handle_evaluate,
            vol.Schema({vol.Optional("entry_id"): str}),
        )

        # Watch entities referenced by conditions and set initial sensor
        await self._rebuild_watchers_and_refresh()

        # React to options/data updates without reloading the whole entry
        self.entry.async_on_unload(self.entry.add_update_listener(self._entry_updated))

    async def async_stop(self) -> None:
        if self._service_registered:
            try:
                self.hass.services.async_remove("notify", self.slug)
            except Exception:  # pragma: no cover (defensive)
                _LOGGER.exception("Failed removing notify.%s", self.slug)
            self._service_registered = False

        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None

    # ------------------------ service handlers ------------------------

    async def _handle_notify(self, call: ServiceCall) -> None:
        """Entry point for notify.<slug>."""
        data = dict(call.data)
        message = data.get(ATTR_MESSAGE)
        title = data.get(ATTR_TITLE)
        payload: dict[str, Any] = {}
        if message is not None:
            payload[ATTR_MESSAGE] = message
        if title is not None:
            payload[ATTR_TITLE] = title
        payload.update(data.get("data", {}))

        svc_full = await self._pick_now()
        if not svc_full:
            _LOGGER.warning("No target matched and no fallback configured; dropping message")
            return

        dom, name = svc_full.split(".", 1)
        await self.hass.services.async_call(dom, name, payload, blocking=True)

    async def _handle_evaluate(self, call: ServiceCall) -> None:
        """Log which target would be chosen now (no send)."""
        entry_id = call.data.get("entry_id")
        if entry_id and entry_id != self.entry.entry_id:
            return

        targets_by_full = {t[KEY_SERVICE]: t for t in self.targets if KEY_SERVICE in t}
        for svc_full in self.priority or []:
            tgt = targets_by_full.get(svc_full)
            if not tgt:
                continue

            conds = list(cast(list[dict[str, Any]], tgt.get(KEY_CONDITIONS, [])))
            mode = _norm_match_mode(tgt)

            results = await asyncio.gather(*(evaluate_condition(self.hass, c) for c in conds))
            matched = all(results) if mode == "all" else any(results)
            _LOGGER.debug("  target %s match=%s (conditions=%s)", svc_full, matched, results)
            if matched:
                _LOGGER.debug("  → would forward to %s", svc_full)
                return

        if self.fallback_full:
            _LOGGER.debug("  → would fallback to %s", self.fallback_full)
        else:
            _LOGGER.debug("  → would drop (no fallback)")

    # ------------------------ config updates ------------------------

    async def _entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Re-parse options/data and rebuild listeners when the user saves changes."""
        cfg = {**(entry.data or {}), **(entry.options or {})}
        self.targets = list(cast(list[dict[str, Any]], cfg.get(CONF_TARGETS, [])))
        self.priority = list(cast(list[str], cfg.get(CONF_PRIORITY, [])))
        self.fallback_full = cast(str, cfg.get(CONF_FALLBACK, self.fallback_full))

        await self._rebuild_watchers_and_refresh()

    # ------------------------ selection logic ------------------------

    async def _pick_now(self) -> str | None:
        """Pick the best target right now and update the sensor if it changed."""
        chosen = await self._compute_best_target()

        if chosen != self._last_pick:
            self._last_pick = chosen
            self._update_sensor(chosen)

        return chosen

    async def _compute_best_target(self) -> str | None:
        """Evaluate conditions live to pick a service."""
        if not self.targets or not self.priority:
            return self.fallback_full or None

        targets_by_full = {t[KEY_SERVICE]: t for t in self.targets if KEY_SERVICE in t}

        for svc_full in self.priority:
            tgt = targets_by_full.get(svc_full)
            if not tgt:
                continue

            conds = list(cast(list[dict[str, Any]], tgt.get(KEY_CONDITIONS, [])))
            mode = _norm_match_mode(tgt)

            if not conds:
                return svc_full  # no conditions => always match

            results = await asyncio.gather(*(evaluate_condition(self.hass, c) for c in conds))
            matched = all(results) if mode == "all" else any(results)
            if matched:
                return svc_full

        return self.fallback_full or None

    async def _rebuild_watchers_and_refresh(self) -> None:
        """Subscribe to all condition entities and refresh sensor immediately."""
        # Unsubscribe old
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None

        # Collect entities from all targets
        ents: set[str] = set()
        for t in self.targets:
            for c in cast(list[dict[str, Any]], t.get(KEY_CONDITIONS, [])):
                eid = c.get("entity_id")
                if isinstance(eid, str) and eid:
                    ents.add(eid)

        self._watched_entities = ents

        if ents:
            @callback
            def _on_change(_event) -> None:
                # Any relevant entity changed -> recompute and update sensor
                self.hass.async_create_task(self._pick_now())

            self._state_unsub = async_track_state_change_event(
                self.hass, list(ents), _on_change
            )

        # Refresh immediately
        await self._pick_now()

    # ------------------------ sensor ------------------------

    def _update_sensor(self, svc_full: str | None) -> None:
        """Expose the current routing decision as a sensor."""
        entity_id = f"sensor.{self.slug}_active_target"
        if not svc_full:
            state = "none"
            attrs: dict[str, Any] = {
                "friendly_name": f"{self.friendly} Active Target",
                "domain": DOMAIN,
                "matched": False,
                "via_fallback": False,
                "service_full": "none",
            }
            self.hass.states.async_set(entity_id, state, attrs)
            return

        # determine if this is fallback
        is_fallback = (svc_full == self.fallback_full)
        attrs = {
            "friendly_name": f"{self.friendly} Active Target",
            "domain": DOMAIN,
            "matched": not is_fallback,
            "via_fallback": is_fallback,
            "service_full": svc_full,
        }
        self.hass.states.async_set(entity_id, svc_full, attrs)


# ---------- HA entry points ----------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up integration from a config entry."""
    ctr = NotifierController(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ctr
    await ctr.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ctr: NotifierController | None = cast(
        NotifierController | None, hass.data.get(DOMAIN, {}).get(entry.entry_id)
    )
    if ctr:
        await ctr.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Normalize and migrate stored data."""
    _LOGGER.debug("Running migration/normalization for %s (v%s)", entry.title, entry.version)

    data = {**entry.data}  # mutable copy

    # Normalize fallback to lowercase "domain.name"
    fallback = data.get(CONF_FALLBACK)
    if isinstance(fallback, str) and "." in fallback:
        domain, name = fallback.split(".", 1)
        data[CONF_FALLBACK] = f"{domain.strip().lower()}.{name.strip()}"

    # Normalize priority list
    if CONF_PRIORITY in data and isinstance(data[CONF_PRIORITY], list):
        data[CONF_PRIORITY] = [str(s).strip().lower() for s in data[CONF_PRIORITY]]

    # Normalize targets
    normalized_targets: list[dict[str, Any]] = []
    for tgt in data.get(CONF_TARGETS, []) or []:
        new_tgt = dict(tgt)
        new_tgt.setdefault(KEY_CONDITIONS, [])
        new_tgt[KEY_SERVICE] = str(new_tgt[KEY_SERVICE]).strip().lower()
        # migrate KEY_MATCH -> CONF_MATCH_MODE if needed
        if KEY_MATCH in new_tgt and CONF_MATCH_MODE not in new_tgt:
            new_tgt[CONF_MATCH_MODE] = new_tgt[KEY_MATCH]
        new_tgt.setdefault(CONF_MATCH_MODE, "all")
        normalized_targets.append(new_tgt)
    data[CONF_TARGETS] = normalized_targets

    if data != entry.data:
        hass.config_entries.async_update_entry(entry, data=data)
        _LOGGER.info("Config entry for %s normalized", entry.title)

    return True
