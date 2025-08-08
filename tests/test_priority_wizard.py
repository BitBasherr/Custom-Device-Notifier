import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.const import CONF_TARGETS, DOMAIN


@pytest.mark.asyncio
async def test_priority_reorder_wizard(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.services.async_register("notify", "phone_notify", lambda msg: None)
    hass.services.async_register("notify", "tablet_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    # Create entry with two targets (classic path)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Wizard Test"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "phone_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "add"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "tablet_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.phone_notify", "notify.tablet_notify"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )
    assert result["type"] == "create_entry"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert len(result["data"][CONF_TARGETS]) == 2

    # Open options and go to wizard reordering
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "reorder_wizard"}
    )
    # First pick
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"pick": "notify.tablet_notify"}
    )
    # Second pick completes
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"pick": "notify.phone_notify"}
    )
    assert result["type"] == "create_entry"

    # Confirm data updated
    updated = hass.config_entries.async_entries(DOMAIN)[0]
    assert updated.data["priority"] == ["notify.tablet_notify", "notify.phone_notify"]
