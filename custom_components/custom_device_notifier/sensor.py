from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Set

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    DOMAIN,
    # names
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    # routing
    CONF_ROUTING_MODE,
    ROUTING_SMART,
    ROUTING_CONDITIONAL,
    # conditional path
    CONF_TARGETS,
    KEY_CONDITIONS,
    CONF_FALLBACK,
)

# Reuse helpers from __init__.py so logic stays single-sourced
from . import __init__ as core  # noqa: PLC0415

_LOGGER = logging.getLogger(DOMAIN)

SCAN_INTERVAL = timedelta(
    seconds=30
)  # light safety refresh; events do most of the work


def _signal_name(entry_id: str) -> str:
    return core._signal_name(entry_id)  # noqa: SLF001


@dataclass
class _LastDecision:
    ts: str | None = None
    via: str | None = None
    mode: str | None = None
    service_full: str | None = None
    dump: dict[str, Any] | None = None


class PreferredNotifierSensor(SensorEntity):
    """Live preview of the notifier that would be chosen right now (Smart or Conditional)."""

    _attr_icon = "mdi:account-star"
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._cfg = self._config_view()

        base = self._cfg.get(CONF_SERVICE_NAME_RAW) or self._cfg.get(CONF_SERVICE_NAME)
        self._attr_name = f"{base}'s Preferred Notifier (Live)"
        self._attr_unique_id = f"{entry.entry_id}_preferred_notifier_current_target"

        # State: short service name (e.g., mobile_app_s23_ultra) or "none"
        self._state_short: str | None = None
        # Chosen (full) service like notify.mobile_app_s23_ultra
        self._chosen_full: str | None = None

        # Last actual router decision (from dispatcher)
        self._last: _LastDecision | None = None

        # Entity watchers
        self._watched: Set[str] = set()
        self._unsub_watch: list[Callable[[], None]] = []
        self._unsub_tick: Callable[[], None] | None = None

    # ───────── Home Assistant entity API ─────────

    @property
    def native_value(self) -> str | None:
        return self._state_short

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self._last
        return {
            # last real routing decision (from a sent notification)
            "last_decision_ts": last.ts if last else None,
            "last_decision_via": last.via if last else None,
            "last_decision_mode": last.mode if last else None,
            "last_decision_service": last.service_full if last else None,
            "last_decision_dump": last.dump if last else None,
            # live preview details
            "live_mode": self._cfg.get(CONF_ROUTING_MODE),
            "live_chosen_full": self._chosen_full,
        }

    async def async_added_to_hass(self) -> None:
        # Listen for actual decisions (attributes - not state)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, _signal_name(self.entry.entry_id), self._on_router_decision
            )
        )
        # Periodic safety refresh
        self._unsub_tick = async_track_time_interval(
            self.hass, self._handle_tick, SCAN_INTERVAL
        )
        self.async_on_remove(self._remove_tick)

        # Install watchers and compute first value
        self._reinstall_watchers()
        await self._recompute_live()

    async def async_will_remove_from_hass(self) -> None:
        for u in self._unsub_watch:
            try:
                u()
            except Exception:  # noqa: BLE001
                pass
        self._unsub_watch.clear()
        self._remove_tick()

    # ───────── Dispatcher / timers ─────────

    @callback
    def _remove_tick(self) -> None:
        if self._unsub_tick:
            try:
                self._unsub_tick()
            except Exception:  # noqa: BLE001
                pass
        self._unsub_tick = None

    @callback
    def _on_router_decision(self, decision: dict[str, Any]) -> None:
        """Store last real decision (attributes only)."""
        self._last = _LastDecision(
            ts=decision.get("timestamp"),
            via=decision.get("via"),
            mode=decision.get("mode"),
            service_full=decision.get("service_full"),
            dump=decision,
        )
        self.async_write_ha_state()

    async def _handle_tick(self, _now) -> None:
        # If options changed (routing mode/phone list), watchers may need to be rebuilt
        self._cfg = self._config_view()
        self._reinstall_watchers()
        await self._recompute_live()

    # ───────── Watchers ─────────

    def _config_view(self) -> dict[str, Any]:
        cfg = dict(self.entry.data)
        cfg.update(self.entry.options or {})
        return cfg

    def _collect_watch_ids_for_smart(self, cfg: dict[str, Any]) -> Set[str]:
        """PC/phone entities the smart selector cares about."""
        watch: Set[str] = set()

        # PC session
        pc_session = (
            cfg.get("smart_pc_session")
            or cfg.get("smart_pc_sensor")
            or cfg.get("smart_pc_session_entity")
        )
        pc_session = (
            cfg.get("smart_pc_session")
            or cfg.get("smart_pc_session_entity")
            or cfg.get("smart_pc_sensor")
        )
        pc_session = cfg.get("smart_pc_session")  # canonical per const.py
        if isinstance(pc_session, str) and pc_session:
            watch.add(pc_session)

        # Phones (by ordered list)
        for full in cfg.get("smart_phone_order", []):
            domain, short = core._split_service(full)  # noqa: SLF001
            if domain != "notify":
                continue
            slug = short[11:] if short.startswith("mobile_app_") else short

            # Locks
            watch.update(
                {
                    f"binary_sensor.{slug}_device_locked",
                    f"binary_sensor.{slug}_lock",
                    f"sensor.{slug}_keyguard",
                    f"sensor.{slug}_lock_state",
                }
            )
            # Interactive/awake hints
            watch.update(
                {
                    f"binary_sensor.{slug}_interactive",
                    f"sensor.{slug}_interactive",
                    f"binary_sensor.{slug}_is_interactive",
                    f"binary_sensor.{slug}_screen_on",
                    f"sensor.{slug}_screen_state",
                    f"sensor.{slug}_display_state",
                    f"binary_sensor.{slug}_awake",
                    f"sensor.{slug}_awake",
                }
            )
            # Freshness + shutdown + presence
            watch.update(
                {
                    f"sensor.{slug}_last_update_trigger",
                    f"sensor.{slug}_last_update",
                    f"device_tracker.{slug}",
                }
            )
            # Battery
            watch.update(
                {
                    f"sensor.{slug}_battery_level",
                    f"sensor.{slug}_battery",
                }
            )

        existing = {s.entity_id for s in self.hass.states.async_all()}
        return {w for w in watch if w in existing}

    def _collect_watch_ids_for_conditional(self, cfg: dict[str, Any]) -> Set[str]:
        watch: Set[str] = set()
        for tgt in cfg.get(CONF_TARGETS, []):
            for c in tgt.get(KEY_CONDITIONS, []):
                eid = c.get("entity_id")
                if isinstance(eid, str) and self.hass.states.get(eid) is not None:
                    watch.add(eid)
        return watch

    def _reinstall_watchers(self) -> None:
        """(Re)install state-change watchers based on the current routing mode."""
        for u in self._unsub_watch:
            try:
                u()
            except Exception:  # noqa: BLE001
                pass
        self._unsub_watch.clear()

        mode = self._cfg.get(CONF_ROUTING_MODE)
        if mode == ROUTING_SMART:
            to_watch = self._collect_watch_ids_for_smart(self._cfg)
        elif mode == ROUTING_CONDITIONAL:
            to_watch = self._collect_watch_ids_for_conditional(self._cfg)
        else:
            to_watch = set()

        if not to_watch:
            self._watched = set()
            return

        @callback
        async def _cb(evt):
            await self._recompute_live()

        self._unsub_watch.append(
            async_track_state_change_event(self.hass, list(to_watch), _cb)
        )
        self._watched = to_watch
        _LOGGER.debug(
            "%s: watching %d entities (%s mode)",
            self.name,
            len(self._watched),
            mode,
        )

    # ───────── Live recompute ─────────

    async def _recompute_live(self) -> None:
        """Re-evaluate the current choice using the active routing mode."""
        cfg = self._cfg
        fallback = cfg.get(CONF_FALLBACK)
        mode = cfg.get(CONF_ROUTING_MODE)

        chosen_full: str | None = None
        via = "mode"

        if mode == ROUTING_SMART:
            chosen, info = core._choose_service_smart(self.hass, cfg)  # noqa: SLF001
            if chosen:
                chosen_full = chosen if "." in chosen else f"notify.{chosen}"
                via = "smart"
            elif isinstance(fallback, str) and fallback:
                chosen_full = fallback if "." in fallback else f"notify.{fallback}"
                via = "fallback"
            else:
                via = "none"

            # attach smart info
            attrs = dict(self.extra_state_attributes or {})
            attrs.update(
                {
                    "live_mode": ROUTING_SMART,
                    "live_smart_info": info,
                    "live_via": via,
                }
            )
            self._attr_extra_state_attributes = attrs

        elif mode == ROUTING_CONDITIONAL:
            svc, info = core._choose_service_conditional_with_info(  # noqa: SLF001
                self.hass, cfg
            )
            if svc:
                chosen_full = svc if "." in svc else f"notify.{svc}"
                via = "conditional"
            elif isinstance(fallback, str) and fallback:
                chosen_full = fallback if "." in fallback else f"notify.{fallback}"
                via = "fallback"
            else:
                via = "none"

            attrs = dict(self.extra_state_attributes or {})
            attrs.update(
                {
                    "live_mode": ROUTING_CONDITIONAL,
                    "live_conditional_info": info,
                    "live_via": via,
                }
            )
            self._attr_extra_state_attributes = attrs

        else:
            # Unknown mode → only fallback (if any)
            if isinstance(fallback, str) and fallback:
                chosen_full = fallback if "." in fallback else f"notify.{fallback}"
                via = "fallback"
            else:
                via = "none"
            attrs = dict(self.extra_state_attributes or {})
            attrs.update({"live_mode": mode, "live_via": via})
            self._attr_extra_state_attributes = attrs

        # Derive short state
        if chosen_full:
            try:
                _, short = chosen_full.split(".", 1)
            except ValueError:
                short = chosen_full
        else:
            short = "none"

        self._chosen_full = chosen_full
        self._state_short = short

        _LOGGER.debug(
            "%s live preview → %s (%s, mode=%s)",
            self.entity_id,
            self._state_short,
            via,
            mode,
        )
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    async_add_entities([PreferredNotifierSensor(hass, entry)], update_before_add=True)
