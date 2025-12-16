"""Medication tracking sensors for Custom Device Notifier."""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import CONF_MEDICATIONS, CONF_MED_NAME, CONF_MED_SCHEDULE, DOMAIN

_LOGGER = logging.getLogger(__name__)


def get_medication_log_path(hass: HomeAssistant, med_name: str) -> Path:
    """Get the path to the medication log CSV file."""
    config_dir = Path(hass.config.path())
    med_dir = config_dir / "custom_components" / DOMAIN / "medication_logs"
    med_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize medication name for filename
    safe_name = "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in med_name)
    safe_name = safe_name.strip().replace(" ", "_")
    
    return med_dir / f"{safe_name}.csv"


def log_medication_taken(hass: HomeAssistant, med_name: str, timestamp: datetime) -> None:
    """Log medication taken to CSV file."""
    log_path = get_medication_log_path(hass, med_name)
    
    # Create CSV if it doesn't exist
    file_exists = log_path.exists()
    
    try:
        with open(log_path, "a", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            
            # Write header if new file
            if not file_exists:
                writer.writerow(["Timestamp", "Date", "Time", "Medication"])
            
            # Write log entry
            writer.writerow([
                timestamp.isoformat(),
                timestamp.strftime("%Y-%m-%d"),
                timestamp.strftime("%H:%M:%S"),
                med_name,
            ])
            
        _LOGGER.info("Logged medication '%s' taken at %s to %s", med_name, timestamp, log_path)
    except Exception as e:
        _LOGGER.error("Failed to log medication '%s': %s", med_name, e)


def get_today_doses(hass: HomeAssistant, med_name: str) -> List[datetime]:
    """Get list of doses taken today for a medication from CSV log."""
    log_path = get_medication_log_path(hass, med_name)
    
    if not log_path.exists():
        return []
    
    today = dt_util.now().date()
    doses_today = []
    
    try:
        with open(log_path, "r", newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                try:
                    timestamp_str = row.get("Timestamp", "")
                    if timestamp_str:
                        timestamp = dt_util.parse_datetime(timestamp_str)
                        if timestamp and timestamp.date() == today:
                            doses_today.append(timestamp)
                except Exception:
                    continue
    except Exception as e:
        _LOGGER.error("Failed to read medication log for '%s': %s", med_name, e)
    
    return sorted(doses_today)


class MedicationSensor(RestoreEntity, SensorEntity):
    """Sensor for tracking individual medication."""
    
    _attr_should_poll = False
    _attr_icon = "mdi:pill"
    
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        med_name: str,
        schedule: List[str],
    ) -> None:
        """Initialize medication sensor."""
        self.hass = hass
        self._entry = entry
        self._med_name = med_name
        self._schedule = schedule  # List of times like ["08:00", "20:00"]
        
        # Sanitize name for entity_id
        safe_name = med_name.lower().replace(" ", "_")
        slug = str(entry.data.get("service_name", "custom_notifier"))
        
        self._attr_name = f"Medication {med_name}"
        self._attr_unique_id = f"{slug}_medication_{safe_name}"
        
        self._attr_native_value = "Not Taken"
        self._last_taken: Optional[datetime] = None
        self._doses_today: List[datetime] = []
        
        self._unsub_timer = None
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return extra attributes."""
        attrs = {
            "medication_name": self._med_name,
            "schedule": self._schedule,
            "scheduled_doses_count": len(self._schedule),
        }
        
        # Last Taken at
        if self._last_taken:
            attrs["Last Taken at"] = self._last_taken.isoformat()
        else:
            attrs["Last Taken at"] = "Never"
        
        # Doses taken today
        doses_today_times = [d.strftime("%H:%M:%S") for d in self._doses_today]
        attrs["Doses taken today"] = doses_today_times if doses_today_times else []
        
        # Doses Taken/Doses Scheduled (as string, not number)
        taken_count = len(self._doses_today)
        scheduled_count = len(self._schedule)
        attrs["Doses Taken/Doses Scheduled"] = f"{taken_count}/{scheduled_count}"
        
        # CSV log location
        log_path = get_medication_log_path(self.hass, self._med_name)
        attrs["log_file"] = str(log_path)
        
        return attrs
    
    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        
        # Restore state
        last_state = await self.async_get_last_state()
        if last_state is not None:
            if last_state.attributes.get("Last Taken at") not in (None, "Never"):
                try:
                    self._last_taken = dt_util.parse_datetime(
                        last_state.attributes["Last Taken at"]
                    )
                except Exception:
                    pass
        
        # Load today's doses from CSV
        await self.hass.async_add_executor_job(self._load_today_doses)
        
        # Update midnight to reset daily counters
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_update_daily, timedelta(hours=1)
        )
        
        self.async_write_ha_state()
    
    async def async_will_remove_from_hass(self) -> None:
        """When entity is removed."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
    
    def _load_today_doses(self) -> None:
        """Load today's doses from CSV (runs in executor)."""
        self._doses_today = get_today_doses(self.hass, self._med_name)
    
    @callback
    async def _async_update_daily(self, _now) -> None:
        """Update daily dose counts."""
        await self.hass.async_add_executor_job(self._load_today_doses)
        self._update_state()
        self.async_write_ha_state()
    
    def _update_state(self) -> None:
        """Update the sensor state based on doses taken."""
        if not self._doses_today:
            self._attr_native_value = "Not Taken"
        elif len(self._doses_today) >= len(self._schedule):
            self._attr_native_value = "Complete"
        else:
            self._attr_native_value = f"Partial ({len(self._doses_today)}/{len(self._schedule)})"
    
    async def async_mark_taken(self, timestamp: Optional[datetime] = None) -> None:
        """Mark medication as taken."""
        if timestamp is None:
            timestamp = dt_util.now()
        
        # Update in-memory state
        self._last_taken = timestamp
        
        # Check if this is today
        if timestamp.date() == dt_util.now().date():
            # Add to today's doses if not a duplicate (within 1 minute)
            is_duplicate = any(
                abs((timestamp - d).total_seconds()) < 60 for d in self._doses_today
            )
            if not is_duplicate:
                self._doses_today.append(timestamp)
                self._doses_today.sort()
        
        # Log to CSV (in executor to avoid blocking)
        await self.hass.async_add_executor_job(
            log_medication_taken, self.hass, self._med_name, timestamp
        )
        
        # Update state
        self._update_state()
        self.async_write_ha_state()
        
        _LOGGER.info("Marked medication '%s' as taken at %s", self._med_name, timestamp)
