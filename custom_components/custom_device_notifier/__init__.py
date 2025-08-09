"""Custom Device Notifier – core init + live resolver + clean unload."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Final

import voluptuous as vol
from homeassistant.components.notify import BaseNotificationService
from homeassistant.components.notify.const import ATTR_MESSAGE, ATTR_TITLE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

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

# -------------------- service schema --------------------

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional("data"): dict,
    }
)


def _signal(entry_id: str) -> str:
    """Dispatcher signal name for this entry."""
    return f"{DOMAIN}_{entry_id}_resolved_update"


# -------------------- live resolver --------------------


class _PriorityResolver:
    """Recompute the winning notify service whenever relevant entities change."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        targets: list[dict[str, Any]],
        priority: list[str],
        fallback: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.targets = targets
        self.priority = priority
        self.fallback = fallback
        self.current: str | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._recompute_task: asyncio.Task | None = None

    def start(self) -> None:
        """Begin tracking state changes for all condition entities and compute once."""
        # gather all entity_ids referenced in conditions
        ent_ids: set[str] = set()
        for tgt in self.targets:
            for cond in tgt.get(KEY_CONDITIONS, []):
                eid = cond.get("entity_id")
                if isinstance(eid, str):
                    ent_ids.add(eid)

        if ent_ids:
            _LOGGER.debug(
                "[%s] tracking %d entities for live resolve: %s",
                self.entry.title,
                len(ent_ids),
                ", ".join(sorted(ent_ids)),
            )
            unsub = async_track_state_change_event(
                self.hass, ent_ids, self._on_state_change
            )
            # track_state_change returns a callable to unsubscribe
            self._unsubs.append(unsub)

        # initial compute (don’t block setup)
        self._schedule_recompute()

    def stop(self) -> None:
        """Unsubscribe from all listeners and cancel any pending recompute."""
        for u in self._unsubs:
            try:
                u()
            except Exception:  # pragma: no cover - best effort
                _LOGGER.debug("Unsub failed (ignored)", exc_info=True)
        self._unsubs.clear()

        if self._recompute_task and not self._recompute_task.done():
            self._recompute_task.cancel()
        self._recompute_task = None

    # ---- callbacks ----

    @callback
    def _on_state_change(self, event) -> None:
        # throttle via single task – if many updates land together, we recompute once
        self._schedule_recompute()

    @callback
    def _schedule_recompute(self) -> None:
        if self._recompute_task and not self._recompute_task.done():
            return
        self._recompute_task = self.hass.async_create_task(self._recompute())

    async def _recompute(self) -> None:
        """Evaluate targets in priority order and publish the current winner."""
        winner = await self._pick_winner()
        if winner != self.current:
            self.current = winner
            async_dispatcher_send(self.hass, _signal(self.entry.entry_id), winner)
            _LOGGER.debug("[%s] resolved winner now: %s", self.entry.title, winner)

    async def _pick_winner(self) -> str:
        # Iterate through the configured priority list; choose the first that matches.
        for svc in self.priority:
            tgt = next((t for t in self.targets if t[KEY_SERVICE] == svc), None)
            if not tgt:
                continue
            mode = tgt.get(KEY_MATCH, "all")
            results = await asyncio.gather(
                *(evaluate_condition(self.hass, c) for c in tgt.get(KEY_CONDITIONS, []))
            )
            matched = all(results) if mode == "all" else any(results)
            _LOGGER.debug("  check %s -> %s (conds=%s)", svc, matched, results)
            if matched:
                return svc
        return self.fallback


# -------------------- setup / unload --------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the notify service + start live resolver + forward sensor platform."""
    try:
        data = entry.data
        slug: str = data[CONF_SERVICE_NAME]
        targets: list[dict[str, Any]] = data[CONF_TARGETS]
        priority: list[str] = data[CONF_PRIORITY]
        fallback: str = data[CONF_FALLBACK]

        # data bucket per-entry
        hass.data.setdefault(DOMAIN, {})
        store: dict[str, Any] = {
            "slug": slug,
            "signal": _signal(entry.entry_id),
        }
        hass.data[DOMAIN][entry.entry_id] = store

        # live resolver
        resolver = _PriorityResolver(hass, entry, targets, priority, fallback)
        store["resolver"] = resolver
        resolver.start()

        # notify service wrapper (always asks resolver which to use)
        service = _NotifierService(hass, slug, resolver)
        store["service"] = service

        hass.services.async_register(
            "notify", slug, service.async_send_message, schema=SERVICE_SCHEMA
        )

        # forward sensor platform (exposes the "winner" as a sensor)
        await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

        # when options or data change -> reload cleanly
        async def _reload_on_update(hass: HomeAssistant, updated_entry: ConfigEntry):
            _LOGGER.debug("Config entry updated; reloading %s", updated_entry.entry_id)
            await hass.config_entries.async_reload(updated_entry.entry_id)

        # keep a ref so we can remove it on unload
        store["unsub_update_listener"] = entry.add_update_listener(_reload_on_update)

        return True

    except Exception:  # pragma: no cover
        _LOGGER.exception("Error setting up %s", DOMAIN)
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Cleanly remove service, listeners, and sensor platform."""
    store = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not store:
        # nothing registered – treat as unloaded
        return True

    # 1) remove notify service
    slug: str | None = store.get("slug")
    if slug:
        try:
            hass.services.async_remove("notify", slug)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Service removal failed (ignored)", exc_info=True)

    # 2) stop resolver
    resolver: _PriorityResolver | None = store.get("resolver")
    if resolver:
        resolver.stop()

    # 3) remove update-listener
    unsub = store.get("unsub_update_listener")
    if callable(unsub):
        try:
            unsub()
        except Exception:  # pragma: no cover
            _LOGGER.debug("Update-listener removal failed (ignored)", exc_info=True)

    # 4) unload sensor platform
    ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    return ok


# -------------------- migration (normalize) --------------------


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Normalize data consistently across versions."""
    _LOGGER.debug(
        "Running migration/normalization for %s (v%s)", entry.title, entry.version
    )
    data = dict(entry.data)

    # normalize fallback
    fb = data.get(CONF_FALLBACK)
    if isinstance(fb, str) and "." in fb:
        d, n = fb.split(".", 1)
        data[CONF_FALLBACK] = f"{d.strip().lower()}.{n.strip()}"

    # normalize priority
    if isinstance(data.get(CONF_PRIORITY), list):
        data[CONF_PRIORITY] = [svc.strip().lower() for svc in data[CONF_PRIORITY]]

    # normalize targets
    norm_targets: list[dict[str, Any]] = []
    for tgt in data.get(CONF_TARGETS, []):
        new_tgt = dict(tgt)
        new_tgt.setdefault(KEY_MATCH, "all")
        new_tgt.setdefault(KEY_CONDITIONS, [])
        if isinstance(new_tgt.get(KEY_SERVICE), str):
            new_tgt[KEY_SERVICE] = new_tgt[KEY_SERVICE].strip().lower()
        norm_targets.append(new_tgt)
    data[CONF_TARGETS] = norm_targets

    if data != entry.data:
        hass.config_entries.async_update_entry(entry, data=data)

    _LOGGER.info("Config entry normalized for %s", entry.title)
    return True


# -------------------- notify service --------------------


class _NotifierService(BaseNotificationService):
    """Forward a message to the resolver’s current target (or fallback)."""

    def __init__(self, hass: HomeAssistant, slug: str, resolver: _PriorityResolver):
        self.hass = hass
        self._slug = slug
        self._resolver = resolver

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        title = kwargs.get(ATTR_TITLE)
        data: dict[str, Any] = {ATTR_MESSAGE: message}
        if title:
            data[ATTR_TITLE] = title
        # include any extra data passed by caller
        extra = kwargs.get("data")
        if isinstance(extra, dict):
            data.update(extra)

        # Always recompute before sending to ensure up-to-the-instant choice
        svc = await self._resolver._pick_winner()  # small, single pass; safe to call
        _LOGGER.debug("notify.%s dispatch -> %s", self._slug, svc)

        dom, name = svc.split(".", 1)
        await self.hass.services.async_call(dom, name, data, blocking=True)
